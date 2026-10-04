"""并发安全测试：高并发下绝不能超额占用。

场景：
1. 单一授权 max_count=5，20 个并发申请（同 region/channel），
   恰好 5 个成功、15 个 quota_exhausted，used_count == 5。
2. 多素材批量申请（每项引用不同授权）混合并发，验证行锁 + 咨询锁
   不会让任何一个授权超过额度。
3. 并发撤销与新申请交错，最终 used_count 必须恒等于「未撤销的通过申请数」。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_concurrency
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=CST)


def scenario_single_auth_quota() -> None:
    print("== 场景1：单授权 5 次额度，20 并发申请 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c1", "name": "并发素材"}
    ).json()["id"]
    aid = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "CN",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 5,
        },
    ).json()["id"]

    def one_request(i: int):
        c = TestClient(app)
        r = c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "CN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        return r.status_code, r.json()

    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(one_request, i) for i in range(20)]
        for f in as_completed(futures):
            results.append(f.result())

    ok = [r for code, r in results if code == 201]
    rej = [
        r
        for code, r in results
        if code == 422 and r["error"]["code"] == "distribution_rejected"
    ]
    assert len(ok) == 5, f"成功数应为 5，实际 {len(ok)}"
    assert len(rej) == 15, f"拒绝数应为 15，实际 {len(rej)}"
    assert all(
        d["error"]["details"][0]["reason"] == "quota_exhausted" for d in rej
    ), "拒绝原因应为 quota_exhausted"

    used = client.get(f"/api/v1/authorizations/{aid}").json()["used_count"]
    assert used == 5, f"used_count 应为 5，实际 {used}"
    print(f"  PASS  成功={len(ok)} 拒绝={len(rej)} used_count={used}（无超额）")


def scenario_multi_material() -> None:
    print("== 场景2：3 素材批量申请，各 4 次额度，30 并发 ==")
    client = TestClient(app)
    ids = []
    for code in ("c2a", "c2b", "c2c"):
        mid = client.post(
            "/api/v1/materials", json={"code": code, "name": code}
        ).json()["id"]
        aid = client.post(
            "/api/v1/authorizations",
            json={
                "material_id": mid,
                "region": "CN",
                "channel": "app",
                "starts_at": (NOW - timedelta(days=1)).isoformat(),
                "ends_at": (NOW + timedelta(days=1)).isoformat(),
                "max_count": 4,
            },
        ).json()["id"]
        ids.append((mid, aid))

    def one_request(i: int):
        c = TestClient(app)
        r = c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid for mid, _ in ids],
                "region": "CN",
                "channel": "app",
                "occur_at": NOW.isoformat(),
            },
        )
        return r.status_code

    with ThreadPoolExecutor(max_workers=15) as pool:
        codes = list(pool.map(one_request, range(30)))

    assert codes.count(201) == 4, f"整体成功应为 4，实际 {codes.count(201)}"
    assert codes.count(422) == 26, f"整体拒绝应为 26，实际 {codes.count(422)}"
    for mid, aid in ids:
        used = client.get(f"/api/v1/authorizations/{aid}").json()["used_count"]
        assert used == 4, f"授权 {aid} used_count 应为 4，实际 {used}"
    print(
        f"  PASS  整体成功={codes.count(201)} 各授权 used_count=4（无超额）"
    )


def scenario_revoke_race() -> None:
    print("== 场景3：申请占满额度后并发撤销 + 新申请交错 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c3", "name": "撤销竞态"}
    ).json()["id"]
    aid = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "JP",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 10,
        },
    ).json()["id"]

    # 先占满 10 次
    approved_ids = []
    for _ in range(10):
        r = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "JP",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        assert r.status_code == 201, r.text
        approved_ids.append(r.json()["id"])

    # 10 个撤销与 10 个新申请同时发起
    def revoke(did: int):
        c = TestClient(app)
        return c.post(f"/api/v1/distributions/{did}/revoke").status_code

    def apply(i: int):
        c = TestClient(app)
        r = c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "JP",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = [pool.submit(revoke, did) for did in approved_ids]
        futs += [pool.submit(apply, i) for i in range(10)]
        outcomes = [f.result() for f in as_completed(futs)]

    revoke_codes = [o for o in outcomes if isinstance(o, int)]
    apply_outs = [o for o in outcomes if isinstance(o, tuple)]
    assert revoke_codes.count(200) == 10, "10 个撤销均应成功"

    # 撤销恰好释放 10 个名额，新申请成功数必须为 0 或 ≤10；
    # 关键：used_count 必须恰好等于「仍处于 approved 状态的申请数」。
    new_ok = sum(1 for code, _ in apply_outs if code == 201)
    assert 0 <= new_ok <= 10

    # 以数据库直接统计 approved 明细数为准，校验账实相符。
    db = SessionLocal()
    try:
        used = db.get(models.Authorization, aid).used_count
        approved_items = db.scalar(
            select(func.count())
            .select_from(models.DistributionItem)
            .join(
                models.Distribution,
                models.DistributionItem.distribution_id
                == models.Distribution.id,
            )
            .where(
                models.DistributionItem.authorization_id == aid,
                models.Distribution.status == "approved",
            )
        )
    finally:
        db.close()

    assert used == approved_items, (
        f"used_count={used} 与有效占用明细数 {approved_items} 不一致"
    )
    assert used <= 10, f"超额！used_count={used}"
    print(
        f"  PASS  撤销成功=10，新申请成功={new_ok}，"
        f"used_count={used} == 有效明细={approved_items}（账实相符、无超额）"
    )


def scenario_repeat_revoke_race() -> None:
    print("== 场景4：同一申请并发撤销两次，必须只有一次成功 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c4", "name": "重复撤销竞态"}
    ).json()["id"]
    client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "EU",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 5,
        },
    )
    did = client.post(
        "/api/v1/distributions",
        json={
            "material_ids": [mid],
            "region": "EU",
            "channel": "web",
            "occur_at": NOW.isoformat(),
        },
    ).json()["id"]

    def revoke():
        c = TestClient(app)
        return c.post(f"/api/v1/distributions/{did}/revoke").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda _: revoke(), range(2)))
    assert sorted(codes) == [200, 409], f"应恰为一次200一次409，实际 {codes}"
    dist = client.get(f"/api/v1/distributions/{did}").json()
    assert dist["status"] == "revoked"
    print(f"  PASS  并发双撤销返回 {sorted(codes)}，状态=revoked，次数只释放一次")


def scenario_migrate_vs_apply() -> None:
    print("== 场景5：迁移与并发新申请抢占替代授权额度，不得超额 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c5", "name": "迁移并发素材"}
    ).json()["id"]
    # 先建源授权并提交 5 条申请（此时唯一候选，全部落在 src）。
    src = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "BR",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=2)).isoformat(),
            "max_count": 10,
        },
    ).json()["id"]
    for _ in range(5):
        r = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "BR",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["items"][0]["authorization"]["id"] == src
    # 再建替代授权：到期更早，之后的新申请优先命中它（溢出才落到 src）。
    tgt = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "BR",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=2)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 8,
        },
    ).json()["id"]

    def migrate():
        c = TestClient(app)
        r = c.post(
            f"/api/v1/authorizations/{src}/migrate",
            json={"target_authorization_id": tgt},
        )
        return r.status_code, r.json()

    def apply(i: int):
        c = TestClient(app)
        r = c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "BR",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        return r.status_code

    with ThreadPoolExecutor(max_workers=11) as pool:
        mig_future = pool.submit(migrate)
        app_futures = [pool.submit(apply, i) for i in range(10)]
        mig_code, mig_body = mig_future.result()
        app_codes = [f.result() for f in app_futures]

    new_ok = app_codes.count(201)
    src_now = client.get(f"/api/v1/authorizations/{src}").json()
    tgt_now = client.get(f"/api/v1/authorizations/{tgt}").json()

    if mig_code == 200:
        # 迁移成功：承接 5 条，tgt 仅剩 3 个名额，源授权停用。
        assert mig_body["migrated_count"] == 5, mig_body
        assert new_ok == 3, f"迁移成功后新申请应成功 3 个，实际 {new_ok}"
        assert src_now["status"] == "inactive" and src_now["used_count"] == 0
        assert tgt_now["used_count"] == 8, tgt_now
    else:
        # 迁移被拒绝：只能是余量不足；新申请占满 tgt 后溢出 src。
        assert mig_code == 422, mig_body
        assert mig_body["error"]["code"] == "migration_rejected", mig_body
        assert (
            mig_body["error"]["details"][0]["reason"] == "insufficient_quota"
        ), mig_body
        assert new_ok == 10, f"迁移失败后新申请应全部成功，实际 {new_ok}"
        assert tgt_now["used_count"] == 8, tgt_now
        assert src_now["used_count"] == 7, src_now

    # 账实相符：两端 used_count 恒等于各自 approved 明细数，且不超额。
    db = SessionLocal()
    try:
        for aid, used in ((src, src_now["used_count"]),
                          (tgt, tgt_now["used_count"])):
            approved_items = db.scalar(
                select(func.count())
                .select_from(models.DistributionItem)
                .join(
                    models.Distribution,
                    models.DistributionItem.distribution_id
                    == models.Distribution.id,
                )
                .where(
                    models.DistributionItem.authorization_id == aid,
                    models.Distribution.status == "approved",
                )
            )
            assert used == approved_items, (
                f"授权 {aid} used_count={used} 与有效明细 {approved_items} 不符"
            )
    finally:
        db.close()
    assert tgt_now["used_count"] <= 8, "替代授权超额！"
    print(
        f"  PASS  迁移={'成功' if mig_code == 200 else '拒绝(余量不足)'}，"
        f"新申请成功={new_ok}，tgt used={tgt_now['used_count']}/8（无超额、账实相符）"
    )


def scenario_migrate_vs_revoke() -> None:
    print("== 场景6：迁移与并发撤销交错，不得重复释放 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c6", "name": "迁移撤销竞态"}
    ).json()["id"]
    src = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "US",
            "channel": "app",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 10,
        },
    ).json()["id"]
    tgt = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "US",
            "channel": "app",
            "starts_at": (NOW - timedelta(days=2)).isoformat(),
            "ends_at": (NOW + timedelta(days=2)).isoformat(),
            "max_count": 20,
        },
    ).json()["id"]

    dist_ids = []
    for _ in range(10):
        r = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "US",
                "channel": "app",
                "occur_at": NOW.isoformat(),
            },
        )
        assert r.status_code == 201, r.text
        dist_ids.append(r.json()["id"])

    def migrate():
        c = TestClient(app)
        r = c.post(
            f"/api/v1/authorizations/{src}/migrate",
            json={"target_authorization_id": tgt},
        )
        return r.status_code, r.json()

    def revoke(did: int):
        c = TestClient(app)
        return c.post(f"/api/v1/distributions/{did}/revoke").status_code

    with ThreadPoolExecutor(max_workers=11) as pool:
        mig_future = pool.submit(migrate)
        revoke_futures = [pool.submit(revoke, did) for did in dist_ids]
        mig_code, mig_body = mig_future.result()
        revoke_codes = [f.result() for f in revoke_futures]

    # 替代授权余量充足，迁移必成功；每条申请撤销恰一次。
    assert mig_code == 200, mig_body
    migrated = mig_body["migrated_count"]
    assert 0 <= migrated <= 10, mig_body
    assert revoke_codes.count(200) == 10, revoke_codes

    # 全部申请最终均为 revoked；两端 used_count 都归零（无重复释放、
    # 无遗漏释放），且等于各自 approved 明细数（0）。
    src_now = client.get(f"/api/v1/authorizations/{src}").json()
    tgt_now = client.get(f"/api/v1/authorizations/{tgt}").json()
    assert src_now["used_count"] == 0, src_now
    assert tgt_now["used_count"] == 0, tgt_now
    assert src_now["status"] == "inactive", src_now
    for did in dist_ids:
        dist = client.get(f"/api/v1/distributions/{did}").json()
        assert dist["status"] == "revoked", dist

    db = SessionLocal()
    try:
        for aid in (src, tgt):
            approved_items = db.scalar(
                select(func.count())
                .select_from(models.DistributionItem)
                .join(
                    models.Distribution,
                    models.DistributionItem.distribution_id
                    == models.Distribution.id,
                )
                .where(
                    models.DistributionItem.authorization_id == aid,
                    models.Distribution.status == "approved",
                )
            )
            assert approved_items == 0, f"授权 {aid} 仍有 {approved_items} 条占用"
    finally:
        db.close()
    print(
        f"  PASS  迁移={migrated} 条，撤销成功=10，"
        f"两端 used_count=0（无重复释放、账实相符）"
    )


def scenario_double_migrate() -> None:
    print("== 场景7：同一源授权并发迁移两次，全部申请恰被改挂一次 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c7", "name": "双重迁移"}
    ).json()["id"]
    src = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "KR",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 5,
        },
    ).json()["id"]
    tgt = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "KR",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=2)).isoformat(),
            "ends_at": (NOW + timedelta(days=2)).isoformat(),
            "max_count": 10,
        },
    ).json()["id"]
    for _ in range(3):
        r = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "KR",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        assert r.status_code == 201, r.text

    def migrate():
        c = TestClient(app)
        r = c.post(
            f"/api/v1/authorizations/{src}/migrate",
            json={"target_authorization_id": tgt},
        )
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: migrate(), range(2)))

    codes = sorted(code for code, _ in results)
    assert codes == [200, 200], f"两次迁移均应成功，实际 {codes}"
    migrated_total = sum(body["migrated_count"] for _, body in results)
    assert migrated_total == 3, f"迁移总数应为 3，实际 {migrated_total}"
    src_now = client.get(f"/api/v1/authorizations/{src}").json()
    tgt_now = client.get(f"/api/v1/authorizations/{tgt}").json()
    assert src_now["status"] == "inactive" and src_now["used_count"] == 0
    assert tgt_now["used_count"] == 3, tgt_now
    print(
        f"  PASS  两次迁移均 200，迁移数合计={migrated_total}，"
        f"tgt used=3（无重复改挂）"
    )


def main() -> int:
    Base.metadata.drop_all(bind=engine)
    init_db()
    with TestClient(app):
        scenario_single_auth_quota()
        scenario_multi_material()
        scenario_revoke_race()
        scenario_repeat_revoke_race()
        scenario_migrate_vs_apply()
        scenario_migrate_vs_revoke()
        scenario_double_migrate()
    print("\n全部并发场景通过：未发生超额、账实相符。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
