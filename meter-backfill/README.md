# 电表读数补传与用电区间修正服务

采集网关断网时会在本地缓存电表读数，网络恢复后批量补传。本服务接收补传
采样，用 SQLite 同时保存**原始采样**与**相邻采样之间的用电区间**，供厂务
数据工程师修正能耗库读取的用电区间，避免重复计量。

## 功能与规则

- **按批补传**：每批只提交一个计量点（`meter_id`）的采样，批内顺序不限；
  每条记录含 UTC 采样秒、电表序列号、与试采同口径的 32 位无符号累计计数
  （uint32，范围 `0..4294967295`）。
- **区间增量规则**（只看相邻两次采样）：
  - 两端电表序列号相同且计数未下降：`status=ok`，`increment = 后值 - 前值`；
  - 序列号不同（换表期间）：`status=pending_review`，原因
    `meter_serial_changed`，**不给增量**；
  - 序列号相同但计数下降（含 32 位回卷）：`status=pending_review`，原因
    `count_decreased`，**不给增量**。不做隐式回卷补偿，避免重复计量。
- **只改受影响区间**：补进中间读数时，被拆开的旧区间会删除并替换为两个
  实际相邻的新区间，窗口之外的区间原样保留。
- **重复跳过 / 冲突整批拒绝**：同一计量点同一秒记录完全相同则跳过；
  内容冲突（序列号或计数不同）整批拒绝并返回 `409`，库内无任何改动
  （批内冲突同样整批拒绝）。
- **原子提交 + 并发安全**：采样与区间在同一个 SQLite `IMMEDIATE` 事务内
  提交（WAL 模式），并发补传在写锁上串行化，任何时刻查询到的区间都只
  连接实际相邻的采样，不会出现悬挂或跨点区间。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- API：<http://localhost:8000>
- Swagger UI：<http://localhost:8000/docs>
- SQLite 数据文件保存在命名卷 `backfill-data`（容器内 `/data/backfill.db`），
  容器重建不丢数据。

## 在 Swagger 中演示换表期间的补传与查询

打开 <http://localhost:8000/docs>，在 `POST /backfill` 中点 **Try it out**，
请求体下拉框内置三个本地样例，按顺序执行：

1. **meter_change_demo（换表期间补传，乱序）**：直接 Execute。
   计量点 `MP-LINE-01` 在 `1730000300 → 1730000450` 之间换表
   （`METER-A-0001 → METER-B-0002`），该区间返回
   `pending_review / meter_serial_changed` 且 `increment=null`；同表且计数
   未下降的区间正常给出增量。
2. **insert_middle_reading_demo（补进中间读数）**：Execute 后可见旧区间
   `1730000000 → 1730000300` 被拆成两个新区间，`rebuilt_intervals=2`，
   其余区间不变。
3. **duplicate_and_conflict_demo（重复跳过）**：Execute 后同秒同内容的记录
   计入 `skipped_duplicates`，新记录正常写入。把样例中 `1730000000` 的
   `count` 改成别的值再 Execute，会得到 `409`，响应体给出已存 / 本次的
   冲突内容，且整批回滚。

然后在 `GET /intervals/{meter_id}` 中填入 `meter_id=MP-LINE-01` 查询全部
区间；加参数 `status=pending_review` 只看待核对区间；
`GET /samples/{meter_id}` 可核对原始采样。

也可以用宿主机 curl 配合 `examples/` 下的本地样例：

```bash
curl -X POST http://localhost:8000/backfill \
  -H 'Content-Type: application/json' \
  -d @examples/01_backfill_meter_change.json
curl -X POST http://localhost:8000/backfill \
  -H 'Content-Type: application/json' \
  -d @examples/02_backfill_insert_middle.json
curl 'http://localhost:8000/intervals/MP-LINE-01?status=pending_review'
```

## 接口说明

### `POST /backfill`

请求体：

| 字段 | 说明 |
| --- | --- |
| `meter_id` | 计量点标识（用电回路 / 能耗库测点编号），每批一个 |
| `samples[].ts` | UTC 采样秒（Unix 时间戳，非负整数） |
| `samples[].meter_serial` | 电表序列号，换表后变化 |
| `samples[].count` | 32 位无符号累计计数，`0..4294967295`，与试采同口径 |

响应（200）：`inserted`（新写入条数）、`skipped_duplicates`（跳过的重复
条数）、`rebuilt_intervals`（本次重建的区间条数）以及提交后该计量点的
全部实际相邻区间。

错误：

- `409`：同计量点同秒已存在不同内容，整批拒绝、无任何写入，响应体含
  `existing` / `incoming` 冲突详情；
- `422`：请求体校验失败（空批次、计数超 uint32、时间戳为负等）。

### `GET /intervals/{meter_id}`

按计量点查询区间，按起点采样秒升序。查询参数：

| 参数 | 说明 |
| --- | --- |
| `status` | 可选，`ok` 或 `pending_review` |
| `start_from` / `start_to` | 可选，按区间起点 UTC 秒过滤（含两端） |
| `limit` / `offset` | 分页，默认 `500 / 0` |

每条区间返回两端采样秒、两端电表序列号、两端计数、`status`、
`increment`（待核对时为 `null`）和 `pending_reason`。

### `GET /samples/{meter_id}`

按计量点查询已保存的原始补传采样（辅助人工核对），按采样秒升序。

## 数据模型（SQLite）

```text
samples    (meter_id, ts, meter_serial, count)            主键 (meter_id, ts)
intervals  (meter_id, start_ts, end_ts, start_meter_serial,
            end_meter_serial, start_count, end_count,
            status, increment, pending_reason)            主键 (meter_id, start_ts)
```

两张表均使用 SQLite STRICT 模式；写事务为 `BEGIN IMMEDIATE`，进入即获取
写锁，配合进程内写互斥锁与 WAL，保证并发补传串行化、读写互不阻塞。

## 本地开发（无 Docker）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
BACKFILL_DB=./data/backfill.db .venv/bin/uvicorn app.main:app --port 8000
.venv/bin/pytest            # 17 个用例：规则、局部重建、冲突回滚、并发一致性、API 集成
```

环境变量：`BACKFILL_DB` 指定 SQLite 文件路径（默认 `/data/backfill.db`，
测试使用临时文件，互不影响）。

## 目录结构

```
app/main.py       FastAPI 接口与 Swagger 本地样例
app/schemas.py    请求 / 响应 Pydantic 模型（uint32 校验）
app/store.py      SQLite 存储：事务、区间分类、受影响窗口局部重建
examples/         Swagger / curl 演示用本地样例（换表、中间读数、重复、冲突）
tests/            存储层规则 + 并发一致性 + API 集成测试
docker-compose.yml  单服务 + 命名卷持久化 SQLite
Dockerfile        python:3.11-slim 镜像
```
