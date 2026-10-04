# 素材发行授权核验 HTTP API

为内容团队构建的素材发行授权核验服务：登记素材及其发行授权（**地区、渠道、带时区的左闭右开时段、可发行次数**），对携带多个素材的发行申请做**整体核验、原子占用**，并支持已通过申请的查询与**一次性撤销（释放占用）**；授权需要退役时，可将原授权承载的全部未撤销申请**一次性改挂到替代授权**。

- 技术栈：Python 3.11 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Pydantic v2
- 并发安全：数据库行锁 + 通道咨询锁 + 条件更新三重保障，**高并发下绝不超额**
- 一键启动：`docker compose up`，自动等待数据库就绪并初始化表结构

---

## 1. 快速开始（Docker Compose）

```bash
# 可选：复制环境变量配置并按需修改
cp .env.example .env

# 构建并一键启动（自动建表初始化）
docker compose up --build
```

启动后：

| 服务 | 地址 |
| --- | --- |
| API 服务 | <http://localhost:8000> |
| 交互式文档 Swagger UI | <http://localhost:8000/docs> |
| ReDoc 文档 | <http://localhost:8000/redoc> |
| 健康检查 | `GET http://localhost:8000/health` |
| PostgreSQL | `localhost:5432`（宿主机映射，容器内为 `db:5432`） |

停止：

```bash
docker compose down          # 停止，保留数据卷
docker compose down -v       # 停止并删除数据库数据卷（清空全部数据）
```

---

## 2. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POSTGRES_USER` | `licensing` | 数据库用户名 |
| `POSTGRES_PASSWORD` | `licensing` | 数据库密码 |
| `POSTGRES_DB` | `licensing` | 数据库名 |
| `DB_HOST_PORT` | `5432` | PostgreSQL 映射到宿主机的端口 |
| `API_HOST_PORT` | `8000` | API 映射到宿主机的端口 |
| `DATABASE_URL` | `postgresql+psycopg://licensing:licensing@db:5432/licensing` | 应用侧数据库连接串（compose 中由上面三个变量拼装） |
| `DB_WAIT_TIMEOUT` | `30` | 启动时等待数据库就绪的最长秒数 |
| `DB_POOL_SIZE` | `10` | 连接池常驻连接数 |
| `DB_MAX_OVERFLOW` | `20` | 连接池溢出连接数 |

数据库表在应用启动事件中通过 `Base.metadata.create_all` 幂等创建，**无需手工执行迁移**。

### 不用 Docker 的本地运行方式

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg://用户:密码@主机:5432/licensing"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 3. 接口一览

基准路径：`/api/v1`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/materials` | 登记素材（业务编号唯一） |
| `GET` | `/materials/{id}` | 查询素材 |
| `POST` | `/authorizations` | 为素材登记一条授权 |
| `GET` | `/authorizations/{id}` | 查询授权（含已用/剩余次数） |
| `PATCH` | `/authorizations/{id}` | 启用/停用授权（仅影响新申请） |
| `POST` | `/authorizations/{id}/migrate` | **退役迁移：将原授权承载的全部未撤销申请改挂到替代授权** |
| `POST` | `/distributions` | **提交发行申请：整体核验并原子占用** |
| `GET` | `/distributions/{id}` | 查询发行申请（含各项命中授权） |
| `POST` | `/distributions/{id}/revoke` | **撤销申请（仅可一次），释放占用次数** |

### 核验与占用规则

1. 匹配维度：素材、`region`（地区）、`channel`（渠道）、发行时刻。
2. 时段语义：**左闭右开** `[starts_at, ends_at)`，命中条件
   `starts_at <= occur_at < ends_at`；所有时间必须携带时区（如 `+08:00`）。
3. 次数：授权含 `max_count` 可发行次数；每次整体通过的申请对命中授权占用 1 次。
4. **重叠授权选择**：当一个素材存在多条匹配授权时，按
   **到期时间 `ends_at` 升序、到期相同再按授权编号 `id` 升序**，
   跳过已停用/无余量者，选择第一条可用授权。
5. **整体判定**：申请中的每个素材都必须有「匹配、启用、有余量」的授权，
   申请才整体通过并落库占用；**任一项不满足则整体拒绝、列明每项原因、不扣减任何次数**。
6. **停用语义**：停用的授权不参与新申请匹配，但**不影响已通过的申请**；
   已通过申请仍可查询、可撤销。
7. **撤销**：已通过申请仅可成功撤销一次；撤销后逐项释放占用次数，
   申请记录保留且状态变为 `revoked`，仍可查询。重复撤销返回 `409`。

### 授权退役迁移规则

`POST /api/v1/authorizations/{id}/migrate`，请求体
`{"target_authorization_id": <替代授权编号>}`，将原授权承载的
**全部未撤销发行申请**一次性改挂到替代授权：

1. 前置条件（**任一不满足则整体拒绝**，申请归属与两端次数均不变）：
   - 原授权与替代授权编号必须不同；
   - 两者的**素材、地区、渠道**必须一致；
   - 替代授权必须处于**启用**状态；
   - 替代授权时段必须**覆盖每条待迁移申请的发行时刻**（左闭右开）；
   - 替代授权剩余次数必须**足以承接全部待迁移占用**。
2. 成功后：全部未撤销申请的占用明细改挂替代授权（替代授权 `used_count`
   增加、原授权等额减少），**原授权停用（退役）**；返回迁移数与两端剩余次数。
3. **已撤销申请不迁移**：其明细仍指向原授权（历史快照），不占任何次数。
4. 改挂后的申请查询显示替代授权；仍可按原接口撤销，**撤销只释放
   替代授权一次**。
5. 原授权上没有未撤销申请时迁移依然成功（迁移数为 0），可用于
   幂等地完成退役；同一源授权的并发迁移串行执行，全部申请恰被改挂一次。
6. 与迁移并发新通过的申请可能仍落在已停用的原授权上（与既有
   「停用与并发申请」语义一致）：不迁移、计数正确、可正常查询与撤销，
   必要时可再次调用迁移将其改挂。

### 错误响应格式

所有业务错误统一为：

```json
{
  "error": {
    "code": "distribution_rejected",
    "message": "授权核验未通过，未占用任何发行次数",
    "details": [
      {
        "item_index": 1,
        "material_id": 8,
        "reason": "quota_exhausted",
        "message": "匹配授权（id=4）可发行次数已用尽：3/3"
      }
    ]
  }
}
```

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 422 | 请求体校验错误（FastAPI 默认格式） | 非法时段（`starts_at >= ends_at`）、时间无时区、次数非正整数等 |
| 409 | `duplicate_material` | 素材业务编号已存在 |
| 422 | `unknown_material` | 登记授权/发行申请引用了未知素材编号 |
| 422 | `duplicate_material_items` | 同一发行申请中携带重复素材 |
| 422 | `distribution_rejected` | 核验未通过；`details` 按项列明原因（见下） |
| 422 | `migration_rejected` | 退役迁移前置条件不满足；`details` 列明全部原因（见下） |
| 409 | `already_revoked` | 对已撤销申请重复撤销 |
| 404 | `not_found` | 路径中的素材/授权/申请编号不存在 |

`distribution_rejected` 的逐项原因 `reason`：

- `no_matching_authorization`：没有同时匹配地区、渠道与发行时刻的授权
- `authorization_inactive`：匹配的授权均已停用
- `quota_exhausted`：匹配授权的可发行次数已用尽

`migration_rejected` 的逐项原因 `reason`：

- `same_authorization`：原授权与替代授权编号相同
- `dimension_mismatch`：素材、地区、渠道不一致
- `target_inactive`：替代授权未启用
- `period_not_covered`：替代授权时段未覆盖某条申请的发行时刻
  （`details` 中给出 `distribution_id`）
- `insufficient_quota`：替代授权剩余次数不足以承接全部待迁移占用

---

## 4. 调用示例（curl）

```bash
# 1) 登记两个素材
curl -s -X POST http://localhost:8000/api/v1/materials \
  -H 'Content-Type: application/json' \
  -d '{"code": "VIDEO-0001", "name": "秋季宣传片"}'

curl -s -X POST http://localhost:8000/api/v1/materials \
  -H 'Content-Type: application/json' \
  -d '{"code": "IMG-0007", "name": "封面图"}'

# 2) 登记授权：中国大陆 / web 渠道 / 10 月（左闭右开，带时区）/ 3 次
curl -s -X POST http://localhost:8000/api/v1/authorizations \
  -H 'Content-Type: application/json' \
  -d '{
    "material_id": 1,
    "region": "CN",
    "channel": "web",
    "starts_at": "2026-10-01T00:00:00+08:00",
    "ends_at":   "2026-11-01T00:00:00+08:00",
    "max_count": 3
  }'

curl -s -X POST http://localhost:8000/api/v1/authorizations \
  -H 'Content-Type: application/json' \
  -d '{
    "material_id": 2,
    "region": "CN",
    "channel": "web",
    "starts_at": "2026-10-01T00:00:00+08:00",
    "ends_at":   "2026-11-01T00:00:00+08:00",
    "max_count": 3
  }'

# 3) 提交发行申请（两个素材同时通过才占用）
curl -s -X POST http://localhost:8000/api/v1/distributions \
  -H 'Content-Type: application/json' \
  -d '{
    "material_ids": [1, 2],
    "region": "CN",
    "channel": "web",
    "occur_at": "2026-10-04T15:30:00+08:00"
  }'
# 201：返回申请 id 及每个素材命中的授权（含剩余次数）

# 4) 查询已通过的申请
curl -s http://localhost:8000/api/v1/distributions/1

# 5) 撤销（仅可一次），释放占用次数
curl -s -X POST http://localhost:8000/api/v1/distributions/1/revoke
# 再撤销一次 -> 409 already_revoked

# 6) 停用授权（仅影响之后的新申请）
curl -s -X PATCH http://localhost:8000/api/v1/authorizations/1 \
  -H 'Content-Type: application/json' -d '{"status": "inactive"}'
curl -s -X PATCH http://localhost:8000/api/v1/authorizations/1 \
  -H 'Content-Type: application/json' -d '{"status": "active"}'
```

### 授权退役迁移（本轮新增）

```bash
# 7) 登记替代授权：同素材/地区/渠道，时段覆盖待迁移申请、余量充足
curl -s -X POST http://localhost:8000/api/v1/authorizations \
  -H 'Content-Type: application/json' \
  -d '{
    "material_id": 1,
    "region": "CN",
    "channel": "web",
    "starts_at": "2026-09-01T00:00:00+08:00",
    "ends_at":   "2026-12-01T00:00:00+08:00",
    "max_count": 10
  }'
# 假设返回 id=3

# 8) 退役迁移：将授权 1 承载的全部未撤销申请改挂到授权 3
curl -s -X POST http://localhost:8000/api/v1/authorizations/1/migrate \
  -H 'Content-Type: application/json' \
  -d '{"target_authorization_id": 3}'
```

成功响应示例（迁移数 + 两端剩余次数）：

```json
{
  "source_authorization_id": 1,
  "target_authorization_id": 3,
  "migrated_count": 2,
  "source_status": "inactive",
  "source_remaining": 3,
  "target_remaining": 8
}
```

```bash
# 9) 改挂后的申请查询显示替代授权，仍可按原接口撤销（只释放替代授权）
curl -s http://localhost:8000/api/v1/distributions/2
curl -s -X POST http://localhost:8000/api/v1/distributions/2/revoke

# 10) 前置条件不满足 -> 422 migration_rejected，申请归属与两端次数均不变
curl -s -X POST http://localhost:8000/api/v1/authorizations/1/migrate \
  -H 'Content-Type: application/json' \
  -d '{"target_authorization_id": 1}'
# {"error":{"code":"migration_rejected","message":"...","details":[{"reason":"same_authorization",...}]}}
```

成功的发行申请响应示例：

```json
{
  "id": 1,
  "region": "CN",
  "channel": "web",
  "occur_at": "2026-10-04T15:30:00+08:00",
  "status": "approved",
  "created_at": "2026-10-04T07:30:00Z",
  "revoked_at": null,
  "items": [
    {
      "item_index": 0,
      "material_id": 1,
      "authorization": {
        "id": 1, "material_id": 1, "region": "CN", "channel": "web",
        "starts_at": "2026-10-01T00:00:00+08:00",
        "ends_at": "2026-11-01T00:00:00+08:00",
        "max_count": 3, "used_count": 1, "remaining": 2,
        "status": "active",
        "created_at": "2026-10-04T07:00:00Z"
      }
    }
  ]
}
```

---

## 5. 并发控制设计

发行核验在单个数据库事务内完成，通过三层机制保证不超额：

1. **候选授权行锁 `SELECT ... FOR UPDATE`**：先按授权 `id` 全局升序加锁
   （与撤销事务相同的第一加锁点与加锁顺序，杜绝跨事务死锁），涉及相同授权的并发申请被数据库串行化。
2. **通道咨询锁 `pg_advisory_xact_lock`**：再加按 `region+channel` 分桶的咨询锁，
   使同一通道内、触及不同授权集合的申请也串行判定，消除
   「各自读到有余量、先后扣减导致超额」的竞态；事务提交/回滚时自动释放。
3. **条件更新扣次**：`UPDATE ... SET used_count = used_count + n
   WHERE used_count <= max_count - n` 并断言影响行数，作为超额的最后防线。

核验不通过即 `ROLLBACK`，不产生任何次数占用；撤销时先锁定申请行复查状态
（并发双重撤销恰为一次 `200`、一次 `409`），再锁授权行释放，
保证 `used_count` 恒等于处于 `approved` 状态的占用明细数。

退役迁移在同一事务内按全局一致的加锁层级执行：
**占用明细行锁 → 申请行锁（id 升序）→ 两端授权行锁（id 升序）→
通道咨询锁**，与撤销/核验事务的加锁点两两兼容、无死锁；定位待迁移集合
只使用普通列条件（行锁等待后由数据库重新评估，并发迁移的改挂、
并发撤销的状态变更在锁定后立即可见），全部前置校验在持锁后基于最新
数据判定，两端次数用条件更新兜底。因此迁移与提交、撤销并发时
**不超额、不重复释放**，同一源授权的并发迁移恰改挂一次。

---

## 6. 测试

测试套件基于真实 PostgreSQL（非模拟），通过 FastAPI TestClient 发起 HTTP 请求：

- `tests/test_api.py`：73 项端到端用例，覆盖左闭右开边界、地区/渠道匹配、
  次数耗尽、撤销释放、停用语义、整体拒绝原子性、重复素材、未知编号、
  重复撤销、重叠授权选择顺序，以及退役迁移的前置校验、改挂、
  原授权停用、已撤销申请不迁移、迁移后撤销只释放替代授权等。
- `tests/test_concurrency.py`：并发安全用例（20/30 并发抢占、多素材批量、
  撤销与新申请交错、并发双重撤销、迁移与新申请抢占替代授权、
  迁移与撤销交错、同一源授权并发双重迁移），断言绝不超额且账实相符。

```bash
pip install -r requirements.txt httpx
# 指向任意可用 PostgreSQL 16
export DATABASE_URL="postgresql+psycopg://licensing@localhost:5432/licensing"
python3 -m tests.test_api
python3 -m tests.test_concurrency
```

> 测试会先 `DROP` 再重建全部表，请勿指向含生产数据的数据库。

## 7. 项目结构

```
.
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── requirements.txt
├── README.md
├── app/
│   ├── main.py        # FastAPI 路由与异常处理
│   ├── config.py      # 环境变量配置
│   ├── database.py    # 引擎/会话/启动建表
│   ├── models.py      # Material / Authorization / Distribution(+Item)
│   ├── schemas.py     # 请求响应模型与时段/时区校验
│   ├── errors.py      # 业务错误码
│   └── services.py    # 核验、原子占用、撤销、退役迁移（核心事务逻辑）
└── tests/
    ├── test_api.py
    └── test_concurrency.py
```
