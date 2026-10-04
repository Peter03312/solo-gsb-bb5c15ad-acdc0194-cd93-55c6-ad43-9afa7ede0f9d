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
    MigrationRejected,
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


# ------------------------------------------------------- 授权退役迁移
def migrate_authorization(
    db: Session, source_id: int, target_id: int
) -> dict:
    """将原授权承载的全部未撤销发行申请一次性改挂到替代授权。

    前置条件（任一不满足则整体拒绝，申请归属与两端次数均不变）：
    两者不同；素材/地区/渠道一致；替代授权启用；替代授权时段覆盖
    每条待迁移申请的发行时刻；替代授权余量足以承接全部占用。
    成功后原授权停用（退役），返回迁移数与两端剩余次数。

    并发安全：加锁顺序与核验/撤销事务构成同一全局层级——
    「明细行锁 → 申请行锁（id 升序）→ 授权行锁（id 升序）→
    通道咨询锁」，与撤销事务相同的申请加锁点、与核验事务相同的
    授权锁顺序与咨询锁，杜绝跨事务死锁。定位待迁移集合只使用
    普通列条件：READ COMMITTED 下行锁等待后会重新评估普通列
    条件（IN 子查询不会被重新评估），并发迁移/撤销提交后，已改挂
    明细与已撤销申请自动从集合中剔除。全部判定在持锁后基于最新
    数据执行，两端次数用条件更新兜底，绝不超额、绝不重复释放。
    """
    # 1) 两者必须不同（无需加锁即可判定）。
    if source_id == target_id:
        raise MigrationRejected(
            [
                {
                    "reason": "same_authorization",
                    "message": f"原授权与替代授权相同：{source_id}",
                }
            ]
        )

    # 2) 存在性检查（无删除操作，普通标量读即可；不加载 ORM 对象，
    #    避免后续行锁等待期间读到会话内的过期属性）。
    existing = set(
        db.scalars(
            select(models.Authorization.id).where(
                models.Authorization.id.in_([source_id, target_id])
            )
        )
    )
    if source_id not in existing:
        raise ResourceNotFound("授权", source_id)
    if target_id not in existing:
        raise ResourceNotFound("授权", target_id)

    try:
        # 3) 第一加锁点：源授权当前承载的全部占用明细行，按 id 升序
        #    FOR UPDATE。普通列条件在锁等待后重新评估：并发迁移已
        #    改挂的明细自动剔除；并发迁移由此串行（同一源授权的
        #    两次迁移逐个进行）。
        items = list(
            db.scalars(
                select(models.DistributionItem)
                .where(models.DistributionItem.authorization_id == source_id)
                .order_by(models.DistributionItem.id.asc())
                .with_for_update()
            )
        )

        # 4) 第二加锁点：上述明细所属的、仍未撤销的申请行，按 id
        #    升序 FOR UPDATE（与撤销事务相同的申请加锁点与全局
        #    顺序）。status 为普通列条件，锁等待后重新评估：并发
        #    撤销的申请自动剔除，已撤销申请不迁移。
        dist_ids = sorted({item.distribution_id for item in items})
        distributions: list[models.Distribution] = []
        if dist_ids:
            distributions = list(
                db.scalars(
                    select(models.Distribution)
                    .where(
                        models.Distribution.id.in_(dist_ids),
                        models.Distribution.status == "approved",
                    )
                    .order_by(models.Distribution.id.asc())
                    .with_for_update()
                )
            )

        # 5) 第三加锁点：两端授权行按 id 升序 FOR UPDATE（与核验/
        #    撤销事务同一全局加锁顺序，杜绝死锁）。行锁等待期间
        #    其他事务的扣次/释放均已提交，此处读到的是最新数据。
        locked = {
            row.id: row
            for row in db.scalars(
                select(models.Authorization)
                .where(models.Authorization.id.in_([source_id, target_id]))
                .order_by(models.Authorization.id.asc())
                .with_for_update()
            )
        }
        source, target = locked[source_id], locked[target_id]

        # 6) 第四加锁点：通道咨询锁（与核验事务相同分桶），使迁移
        #    与同 region+channel 的新申请串行判定；回滚/提交自动释放。
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lk)"),
            {"lk": _bucket_lock_id(source.region, source.channel)},
        )

        # 7) 持锁后逐项校验，收集全部失败原因，任一不满足整体回滚。
        reasons: list[dict] = []
        if (
            source.material_id != target.material_id
            or source.region != target.region
            or source.channel != target.channel
        ):
            reasons.append(
                {
                    "reason": "dimension_mismatch",
                    "message": (
                        "素材、地区、渠道须一致："
                        f"原授权（material_id={source.material_id}, "
                        f"region={source.region}, channel={source.channel}），"
                        f"替代授权（material_id={target.material_id}, "
                        f"region={target.region}, channel={target.channel}）"
                    ),
                }
            )
        if target.status != "active":
            reasons.append(
                {
                    "reason": "target_inactive",
                    "message": f"替代授权未启用：id={target.id}",
                }
            )
        for dist in distributions:
            if not (target.starts_at <= dist.occur_at < target.ends_at):
                reasons.append(
                    {
                        "distribution_id": dist.id,
                        "reason": "period_not_covered",
                        "message": (
                            f"替代授权时段未覆盖申请 {dist.id} 的发行时刻 "
                            f"{dist.occur_at.isoformat()}"
                        ),
                    }
                )
        migrated_count = len(distributions)
        if target.remaining < migrated_count:
            reasons.append(
                {
                    "reason": "insufficient_quota",
                    "message": (
                        f"替代授权余量不足：需承接 {migrated_count} 次占用，"
                        f"剩余 {target.remaining} 次"
                    ),
                }
            )
        if reasons:
            # 显式回滚，释放锁；申请归属与两端次数均不变。
            db.rollback()
            raise MigrationRejected(reasons)

        # 8) 条件更新两端次数（防止超额/重复释放的最后防线），
        #    维持不变量：used_count == 该授权上 approved 明细数。
        if migrated_count:
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == target_id,
                    models.Authorization.used_count
                    <= models.Authorization.max_count - migrated_count,
                )
                .values(
                    used_count=models.Authorization.used_count
                    + migrated_count
                )
            )
            if result.rowcount != 1:
                db.rollback()
                raise MigrationRejected(
                    [
                        {
                            "reason": "insufficient_quota",
                            "message": (
                                f"替代授权 id={target_id} 余量不足，"
                                "并发占用冲突，本次迁移整体拒绝"
                            ),
                        }
                    ]
                )
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == source_id,
                    models.Authorization.used_count >= migrated_count,
                )
                .values(
                    used_count=models.Authorization.used_count
                    - migrated_count
                )
            )
            if result.rowcount != 1:
                db.rollback()
                raise MigrationRejected(
                    [
                        {
                            "reason": "quota_conflict",
                            "message": (
                                f"原授权 id={source_id} 占用数并发变化，"
                                "本次迁移整体拒绝"
                            ),
                        }
                    ]
                )

            # 9) 改挂：仅未撤销申请中指向原授权的明细改挂替代授权；
            #    已撤销申请的明细保持原样（历史快照，不占次数）。
            db.execute(
                update(models.DistributionItem)
                .where(
                    models.DistributionItem.authorization_id == source_id,
                    models.DistributionItem.distribution_id.in_(
                        [dist.id for dist in distributions]
                    ),
                )
                .values(authorization_id=target_id)
            )

        # 10) 原授权停用（退役），此后不再参与新申请匹配。
        source.status = "inactive"
        db.commit()
    except MigrationRejected:
        raise
    except Exception:
        db.rollback()
        raise

    # 11) 提交后重新读取两端最新状态，返回迁移数与两端剩余次数。
    db.refresh(source)
    db.refresh(target)
    return {
        "source_authorization_id": source_id,
        "target_authorization_id": target_id,
        "migrated_count": migrated_count,
        "source_status": source.status,
        "source_remaining": source.remaining,
        "target_remaining": target.remaining,
    }


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
