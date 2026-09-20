# 多层配额原子试算 API

基于 **Python + FastAPI + Pydantic v2** 的令牌桶配额试算服务。给定若干令牌桶与一条带时间戳的请求序列，服务按时间顺序模拟连续补充，并对每次请求执行 **桶链原子扣减**：

- 每个桶以 `refill_rate`（令牌/秒）按时间差**连续补充**，余额上限为 `capacity`；
- 请求携带一条桶链 `chain`，链上**每个桶**都需满足 `余额 >= cost`；
- 全部充足 → 链上每个桶原子扣减 `cost`；任一不足 → **全部桶保持不变**，并返回链上第一个不足的桶（`rejected_by`）；
- 逐项返回放行结果、限制桶以及链上各桶扣减前后余额。

## 一条命令启动

需要已安装 Docker（含 Compose 插件）。在本目录执行：

```bash
docker compose up --build
```

启动后访问地址与端口：

| 用途 | 地址 |
| --- | --- |
| 健康检查 | http://localhost:8000/health |
| 试算接口 | http://localhost:8000/simulate （POST） |
| Swagger 交互文档 | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |

依赖安装与启动初始化均已封装进镜像（见 `Dockerfile` 与 `entrypoint.sh`），宿主机无需 Python 环境。
后台运行可用 `docker compose up --build -d`，停止用 `docker compose down`。

## 请求格式 `POST /simulate`

```json
{
  "buckets": [
    {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
    {"id": "api_qps", "capacity": 5, "refill_rate": 5}
  ],
  "requests": [
    {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-2", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-3", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"]}
  ]
}
```

### 字段说明

**桶 `buckets[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 桶唯一标识，非空字符串 |
| `capacity` | 是 | 桶容量（令牌上限），必须 > 0 |
| `refill_rate` | 是 | 连续补充速率（令牌/秒），必须 ≥ 0 |
| `initial` | 否 | 初始余额，默认等于 `capacity`；要求 0 ≤ initial ≤ capacity |

**请求 `requests[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 请求唯一标识 |
| `timestamp` | 是 | 请求时间（秒，任意一致的时间原点，如相对秒或 Unix 时间） |
| `cost` | 是 | 消耗量，必须 > 0；从链上**每个**桶各扣此数量 |
| `chain` | 是 | 桶 ID 列表（至少 1 个），同一链内不可重复 |

### 模拟语义

1. **时间顺序**：所有请求按 `timestamp` 升序处理；同一时间戳按输入数组顺序处理。`results` 仍按原始数组顺序返回（含 `index`）。
2. **连续补充**：桶在模拟起始时刻（最早请求时间戳）持有 `initial` 余额，之后每次被请求触及时按 `(当前时间 - 上次更新时间) × refill_rate` 惰性补充，封顶 `capacity`。因此时间戳可以是任意大小（Unix 秒等），只取决于时间差。
3. **原子扣减**：判定时先对链上所有桶补充到当前时刻，再按链顺序找第一个余额不足的桶。不足则全部不扣，`balances` 中各桶 `after == before`；充足则每个桶都扣 `cost`。
4. **终态**：`final_balances` 给出所有桶推进到最后一个时间戳后的余额。

## 响应示例

```json
{
  "allowed_count": 2,
  "rejected_count": 1,
  "results": [
    {
      "request_id": "req-1",
      "index": 0,
      "timestamp": 0.0,
      "allowed": true,
      "rejected_by": null,
      "balances": {
        "tenant_quota": {"before": 100.0, "after": 96.0},
        "api_qps": {"before": 5.0, "after": 1.0}
      }
    },
    {
      "request_id": "req-2",
      "index": 1,
      "timestamp": 0.0,
      "allowed": false,
      "rejected_by": "api_qps",
      "balances": {
        "tenant_quota": {"before": 96.0, "after": 96.0},
        "api_qps": {"before": 1.0, "after": 1.0}
      }
    }
  ],
  "final_balances": {"tenant_quota": 96.0, "api_qps": 1.0}
}
```

- `before`：该请求时刻补充之后、扣减之前的余额；
- `after`：扣减之后的余额；被拒绝时与 `before` 相同（原子性）。

## 校验规则（HTTP 422）

以下非法输入都会被拒绝，错误体统一为 `{"detail": [ {"type", "loc", "msg", ...}, ... ]}`：

- **重复桶**：`buckets[].id` 重复（`duplicate_bucket`）；
- **非法速率/容量**：`capacity <= 0`、`refill_rate < 0`、非有限数值、`initial` 越界；
- **未知引用**：`chain` 引用了未在 `buckets` 中定义的 ID（`unknown_bucket_ref`）；
- 此外还校验：链为空、链内桶重复、`cost <= 0`、请求 ID 重复（`duplicate_request`）、多余字段（`extra="forbid"`）。

## 本地调用示例

```bash
curl -X POST http://localhost:8000/simulate \
  -H 'Content-Type: application/json' \
  -d @examples/request.json
```

## 不使用 Docker 的本地开发（可选）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由与异常处理
│   ├── models.py        # Pydantic 模型与输入校验
│   ├── simulation.py    # 连续补充 + 原子扣减模拟引擎
│   └── errors.py        # 跨字段校验错误类型
├── tests/               # pytest 测试（引擎语义 + HTTP）
├── examples/request.json
├── requirements.txt
├── Dockerfile           # 依赖安装在镜像构建阶段完成
├── docker-compose.yml   # 单命令启动，映射 8000 端口
├── entrypoint.sh        # 容器启动初始化
└── README.md
```
