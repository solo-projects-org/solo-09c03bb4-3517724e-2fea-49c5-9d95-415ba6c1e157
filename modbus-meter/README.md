# Modbus TCP 电表试采接口

新电表接入能耗库前的单台 Modbus TCP 电表试采服务：厂务工程师提交测点的
保持寄存器起始地址（从 0 算）与字数（1 或 2），并给出设备的可读区间
（含两端）与单次读取上限；接口自动规划读段、按方案读表，返回读段与
无符号计数（双字按高字在前拼接）。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- API：<http://localhost:8000>，Swagger UI：<http://localhost:8000/docs>
- 模拟电表：`localhost:1502`（容器内 `meter:502`），保持寄存器 0..99

在 Swagger 中打开 `POST /read` → Try it out → 直接 Execute 即可对模拟表试采
（请求体默认指向容器内的模拟表）。也可在宿主机用 curl：

```bash
curl -X POST http://localhost:8000/read \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"address": 0, "words": 2},
      {"address": 2, "words": 2},
      {"address": 10, "words": 1},
      {"address": 12, "words": 1}
    ],
    "range_start": 0,
    "range_end": 99,
    "max_read": 8
  }'
```

## 接口说明

### `POST /read`

请求体：

| 字段 | 说明 |
| --- | --- |
| `points` | 测点列表，`address` 为保持寄存器起始地址（从 0 算），`words` 为字数（1 或 2） |
| `range_start` / `range_end` | 设备可读区间，含两端 |
| `max_read` | 单次读取寄存器字数上限 |
| `host` / `port` / `slave_id` / `timeout` | 设备连接参数，默认指向 Compose 中的模拟表 |

读段规划规则：

1. 每次读取一段连续寄存器，读段整体落在可读区间内，字数不超过 `max_read`；
2. 每个测点必须被某一次读取完整覆盖（不允许跨段拼接）；
3. 先使请求次数最少；次数相同时使读取寄存器总数最少
   （按地址排序后动态规划求解，读段紧贴测点，不多读无用寄存器）。

响应（200）：

- `request_count` / `total_registers`：实际请求次数与读取寄存器总数；
- `segments`：各读段的起止地址（含）、字数、覆盖的测点下标、原始寄存器值；
- `points`：按请求下标返回的测点值，`value` 为无符号计数，
  双字按高字在前（big-endian）拼接。

错误：

- `422`：参数校验失败或无可行方案（测点超出可读区间、字数超过单次上限等），
  `detail` 为中文原因；
- `502`：连接失败或 Modbus 读取失败（含设备异常响应），**不返回部分读数**。

## 模拟电表数据

`simulator/meter_sim.py`：保持寄存器 0..99，可读区间 `[0, 99]`。

| 地址 | 值 | 说明 |
| --- | --- | --- |
| 0..1 | 0x0001, 0x86A0 | 32 位无符号 100000（高字在前） |
| 2..3 | 0x0000, 0x0064 | 32 位无符号 100 |
| 10..13 | 1234, 5678, 65535, 42 | 单字演示值 |
| 其余 | `(addr * 37 + 11) & 0xFFFF` | 确定性填充，便于人工核对 |

## 本地开发（无 Docker）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
METER_PORT=1502 .venv/bin/python -m simulator.meter_sim &
METER_HOST=127.0.0.1 METER_PORT=1502 .venv/bin/uvicorn app.main:app --port 8000
.venv/bin/pytest            # 13 个用例：规划算法 + API 集成
```

环境变量：`METER_HOST` / `METER_PORT` / `METER_SLAVE_ID` / `METER_TIMEOUT`
为 API 请求体连接参数的默认值（Compose 中已注入）。

## 目录结构

```
app/main.py        FastAPI 接口与读表执行
app/planner.py     读段规划（动态规划：次数最少 -> 寄存器总数最少）
simulator/         pymodbus 内存模拟电表
tests/             规划单元测试 + API 集成测试（内存模拟表，无需 Docker）
docker-compose.yml api + meter 两个服务
```
