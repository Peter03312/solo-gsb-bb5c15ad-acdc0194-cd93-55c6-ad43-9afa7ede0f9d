"""核心业务逻辑：登记 / 核验占用 / 查询 / 撤销。

并发安全
========
发行核验在单事务内完成「候选授权行锁 → 通道咨询锁 → 判定 →
条件更新扣次 → 落库」：

1. 先对候选授权按 id 升序执行 ``SELECT ... FOR UPDATE``（与撤销事务
   使用相同的第一加锁点和全局加锁顺序，杜绝跨事务死锁），涉及相同
   授权行的并发申请由数据库行锁天然串行化。
2. 再获取按 ``region+channel`` 分桶的事务级咨询锁，使同一申请通道内、
   涉及不同授权集合的并发申请也串行判定，消除「各自读到有余量、
   先后扣减导致超额」的竞态。
3. 扣次采用条件更新 ``used_count < max_count`` 并断言影响行数，
   作为超额的最后一道防线。
4. 核验不通过则回滚，不产生任何占用。
"""
from collections import Counter
from datetime import datetime, timezone
from zlib import crc32

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import models
from app.errors import (
    AlreadyRevoked,
    DistributionRejected,
    DuplicateMaterialItems,
    MaterialCodeTaken,
    ResourceNotFound,
    UnknownMaterial,
)
from app.schemas import AuthorizationCreate, DistributionCreate, MaterialCreate


# ---------------------------------------------------------------- 登记素材
def create_material(db: Session, data: MaterialCreate) -> models.Material:
    material = models.Material(code=data.code, name=data.name)
    db.add(material)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise MaterialCodeTaken(data.code)
    db.commit()
    db.refresh(material)
    return material


def get_material(db: Session, material_id: int) -> models.Material:
    material = db.get(models.Material, material_id)
    if material is None:
        raise ResourceNotFound("素材", material_id)
    return material


# ------------------------------------------------------------- 登记授权
def create_authorization(
    db: Session, data: AuthorizationCreate
) -> models.Authorization:
    if db.get(models.Material, data.material_id) is None:
        raise UnknownMaterial([data.material_id])

    auth = models.Authorization(
        material_id=data.material_id,
        region=data.region,
        channel=data.channel,
        starts_at=data.starts_at,
        ends_at=data.ends_at,
        max_count=data.max_count,
    )
    db.add(auth)
    db.commit()
    db.refresh(auth)
    return auth


def get_authorization(db: Session, authorization_id: int) -> models.Authorization:
    auth = db.get(models.Authorization, authorization_id)
    if auth is None:
        raise ResourceNotFound("授权", authorization_id)
    return auth


def set_authorization_status(
    db: Session, authorization_id: int, status: str
) -> models.Authorization:
    auth = db.get(models.Authorization, authorization_id)
    if auth is None:
        raise ResourceNotFound("授权", authorization_id)
    auth.status = status
    db.commit()
    db.refresh(auth)
    return auth


# ------------------------------------------------------- 发行核验与占用
def _bucket_lock_id(region: str, channel: str) -> int:
    """region+channel → 单个有符号 32 位整数（pg_advisory_xact_lock 参数）。"""
    raw = crc32(f"{region}\x1f{channel}".encode("utf-8"))
    return raw - 0x100000000 if raw >= 0x80000000 else raw


def create_distribution(
    db: Session, data: DistributionCreate
) -> models.Distribution:
    # 1) 重复素材：同一申请中不得携带重复素材。
    counts = Counter(data.material_ids)
    duplicated = sorted(mid for mid, n in counts.items() if n > 1)
    if duplicated:
        raise DuplicateMaterialItems(duplicated)

    material_ids = list(data.material_ids)

    # 2) 未知编号（一次性查全，避免逐行往返）。
    found_ids = set(
        db.scalars(
            select(models.Material.id).where(
                models.Material.id.in_(material_ids)
            )
        )
    )
    unknown = [mid for mid in material_ids if mid not in found_ids]
    if unknown:
        raise UnknownMaterial(sorted(unknown))

    try:
        occur = data.occur_at
        # 3) 先按授权 id 升序一次性锁定全部候选行（与撤销事务相同的
        #    第一加锁点与全局顺序，杜绝跨事务死锁）。重叠授权的选择
        #    规则（到期早优先、其次编号小）在 Python 中排序完成。
        #    FOR UPDATE 同时锁定停用/无余量候选，保证并发判定稳定。
        locked_rows = list(
            db.scalars(
                select(models.Authorization)
                .where(
                    models.Authorization.region == data.region,
                    models.Authorization.channel == data.channel,
                    # 左闭右开：[starts_at, ends_at)
                    models.Authorization.starts_at <= occur,
                    models.Authorization.ends_at > occur,
                    models.Authorization.material_id.in_(material_ids),
                )
                .order_by(models.Authorization.id.asc())
                .with_for_update()
            )
        )

        # 4) 再取通道咨询锁：同 region+channel 的申请由此串行判定，
        #    消除「涉及不同授权集合的申请各自读到有余量、先后扣减
        #    导致超额」的竞态。咨询锁在提交/回滚时自动释放。
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lk)"),
            {"lk": _bucket_lock_id(data.region, data.channel)},
        )

        grouped: dict[int, list[models.Authorization]] = {
            mid: [] for mid in material_ids
        }
        for auth in locked_rows:
            grouped[auth.material_id].append(auth)

        # 5) 逐项判定，任一不通过则收集原因并整体回滚。
        reasons: list[dict] = []
        chosen: dict[int, models.Authorization] = {}
        for index, mid in enumerate(material_ids):
            candidates = sorted(
                grouped[mid],
                key=lambda a: (a.ends_at, a.id),
            )
            if not candidates:
                reasons.append(
                    {
                        "item_index": index,
                        "material_id": mid,
                        "reason": "no_matching_authorization",
                        "message": "不存在同时匹配地区、渠道与发行时刻的授权",
                    }
                )
                continue

            active = [a for a in candidates if a.status == "active"]
            if not active:
                reasons.append(
                    {
                        "item_index": index,
                        "material_id": mid,
                        "reason": "authorization_inactive",
                        "message": "匹配的授权均已停用（停用仅影响新申请）",
                    }
                )
                continue

            # 候选已按 (ends_at, id) 升序，取第一个有余量的。
            with_quota = next(
                (a for a in active if a.used_count < a.max_count), None
            )
            if with_quota is None:
                detail = active[0]
                reasons.append(
                    {
                        "item_index": index,
                        "material_id": mid,
                        "reason": "quota_exhausted",
                        "message": (
                            f"匹配授权（id={detail.id}）可发行次数已用尽："
                            f"{detail.used_count}/{detail.max_count}"
                        ),
                    }
                )
                continue
            chosen[mid] = with_quota

        if reasons:
            # 显式回滚，释放锁；不扣减任何次数。
            db.rollback()
            raise DistributionRejected(reasons)

        # 6) 全部通过：条件更新占用次数（防止超额的最后防线）。
        #    素材已去重，同一授权在一个申请中至多被选中一次。
        for auth in chosen.values():
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth.id,
                    models.Authorization.used_count
                    < models.Authorization.max_count,
                )
                .values(used_count=models.Authorization.used_count + 1)
            )
            if result.rowcount != 1:
                db.rollback()
                raise DistributionRejected(
                    [
                        {
                            "material_id": auth.material_id,
                            "reason": "quota_exhausted",
                            "message": (
                                f"授权 id={auth.id} 余量不足，"
                                "并发占用冲突，本次申请整体拒绝"
                            ),
                        }
                    ]
                )

        # 7) 落库申请及逐项占用记录。
        distribution = models.Distribution(
            region=data.region,
            channel=data.channel,
            occur_at=data.occur_at,
            status="approved",
        )
        db.add(distribution)
        db.flush()
        for index, mid in enumerate(material_ids):
            db.add(
                models.DistributionItem(
                    distribution_id=distribution.id,
                    material_id=mid,
                    authorization_id=chosen[mid].id,
                    item_index=index,
                )
            )
        db.commit()
    except DistributionRejected:
        raise
    except Exception:
        db.rollback()
        raise

    return get_distribution(db, distribution_id=distribution.id)


# ------------------------------------------------------------- 查询/撤销
def get_distribution(db: Session, distribution_id: int) -> models.Distribution:
    distribution = db.get(
        models.Distribution,
        distribution_id,
        options=[
            selectinload(models.Distribution.items).selectinload(
                models.DistributionItem.material
            ),
            selectinload(models.Distribution.items).selectinload(
                models.DistributionItem.authorization
            ),
        ],
    )
    if distribution is None:
        raise ResourceNotFound("发行申请", distribution_id)
    return distribution


def revoke_distribution(
    db: Session, distribution_id: int
) -> models.Distribution:
    try:
        # 先锁定申请行，串行化对同一申请的并发撤销，
        # 并保证后续状态判定基于最新已提交数据。
        distribution = db.scalar(
            select(models.Distribution)
            .where(models.Distribution.id == distribution_id)
            .with_for_update()
        )
        if distribution is None:
            raise ResourceNotFound("发行申请", distribution_id)
        if distribution.status == "revoked":
            raise AlreadyRevoked(distribution_id)

        # 再加载占用明细（授权 id 不可变）。
        items = list(
            db.scalars(
                select(models.DistributionItem).where(
                    models.DistributionItem.distribution_id
                    == distribution_id
                )
            )
        )
        auth_ids = [item.authorization_id for item in items]

        # 按授权 id 升序加锁，与核验事务保持同一全局加锁顺序，避免死锁；
        # 与进行中的核验事务互斥，避免「释放」与「占用」相互覆盖。
        if auth_ids:
            db.execute(
                select(models.Authorization.id)
                .where(models.Authorization.id.in_(auth_ids))
                .order_by(models.Authorization.id.asc())
                .with_for_update()
            )

        # 同一授权在本申请中至多出现一次（素材已去重），逐项释放。
        for auth_id in auth_ids:
            db.execute(
                update(models.Authorization)
                .where(models.Authorization.id == auth_id)
                .values(
                    used_count=models.Authorization.used_count - 1
                )
            )

        distribution.status = "revoked"
        distribution.revoked_at = datetime.now(timezone.utc)
        db.commit()
    except (ResourceNotFound, AlreadyRevoked):
        raise
    except Exception:
        db.rollback()
        raise

    return get_distribution(db, distribution_id=distribution_id)
