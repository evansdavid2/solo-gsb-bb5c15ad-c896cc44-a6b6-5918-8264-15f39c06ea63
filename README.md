# 依赖图发布回滚 API（FastAPI + SQLite）

本地可运行的服务节点依赖图发布系统：客户端提交服务节点及依赖关系（节点可带正整数预估工期 `estimated_seconds`），
创建带版本号的发布批次；
系统按**拓扑层级**生成执行计划，并对依赖 DAG 做**关键路径（CPM）动态规划**给出最短工期、各节点最早/最晚开始时间与零余量关键节点；
接收每个节点的成功/失败回报；节点失败时若仍有剩余尝试次数则**按可配置延迟自动重试**（等待期间不可回报、不出现在 next_nodes 中），
达到尝试上限后**仅回滚本批次已成功的节点**
并返回**可重试的断点**；同时拒绝**循环依赖**和**跨版本 / 乱序 / 重试未到期回报**。

## 一条命令启动

```bash
docker compose up -d --build
```

启动后：

- API 地址：`http://localhost:8000`
- 健康检查：`GET http://localhost:8000/health`
- 交互式文档（Swagger UI）：`http://localhost:8000/docs`
- OpenAPI Schema：`http://localhost:8000/openapi.json`
- SQLite 数据文件：容器内 `/data/releases.db`，通过命名卷 `release-data` 持久化

镜像构建阶段完成 Python 依赖安装；容器启动时由应用自动完成 SQLite 初始化（创建数据目录、建表、开启 WAL），无需手动初始化。

停止 / 清理：

```bash
docker compose down          # 停止并删除容器（保留数据卷）
docker compose down -v       # 同时删除数据卷（清空所有发布记录）
```

## 配置

通过环境变量配置（见 `docker-compose.yml`，可用 `-e` 或 `.env` 覆盖）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_HOST` | `0.0.0.0` | 服务监听地址 |
| `APP_PORT` | `8000` | 服务监听端口（改端口时同步修改 compose 的端口映射） |
| `DB_PATH` | `/data/releases.db` | SQLite 数据库文件路径，其目录会自动创建 |

## 本地裸机运行（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./data/releases.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 核心语义

**执行计划（拓扑分层 + 工期分析）**

- 依赖边 `from -> to` 表示 `to` 依赖 `from`，`from` 必须先执行。
- 系统用 Kahn 算法分层：level 0 无依赖可先执行，level N 的所有前置节点都位于更低层级。
- 同一 level 内节点互不依赖，可并行回报；跨 level 必须按顺序。
- 存在环时创建批次返回 `422 cycle_detected`，并给出环上节点。
- 节点可携带 `estimated_seconds`（**正整数，秒**）作为预估工期；不传按 `0` 处理。
- 节点可配置有限自动重试参数（创建时持久化，之后不可修改）：
  - `max_attempts`：最大尝试次数，**含首次执行**，取值 `1～10`，默认 `1`（即失败即回滚，旧批次兼容此语义）；
  - `retry_delay_seconds`：失败后下一次重试的退避延迟，取值 `0～3600` 秒，默认 `0`（到期立即可重试）。
- `GET /api/releases/{id}/plan` 在拓扑分层之外，基于依赖 DAG 做关键路径（CPM）动态规划：
  - 前向 DP 得各节点最早开始时间 `earliest_start` 与**最短总工期** `shortest_duration_seconds`
    （同层节点可并行，总工期 = DAG 上的最长路径长度）；
  - 后向 DP 得各节点最晚开始时间 `latest_start` 与总时差 `total_slack = latest_start - earliest_start`；
  - `total_slack = 0` 的节点全部列入 `critical_nodes`，`is_critical` 在逐节点结果中同步标出；
  - `critical_path` 给出一条最长源-汇路径；并列最长路径存在时，返回**节点名序列字典序最小**的一条。
- 计划接口支持可选查询参数 `deadline_seconds`（非负整数秒）：
  - `deadline_seconds >= 最短总工期`：`feasible=true`、`gap_seconds=0`；
  - `deadline_seconds < 最短总工期`：仍返回**完整**分析（各时间、余量、关键节点/路径照常给出），
    同时 `feasible=false`、`gap_seconds = 最短总工期 - deadline_seconds`（缺口秒数）；
  - 该接口为**只读**分析，任何参数都不会改变批次或节点状态。
- 旧批次（创建时未提供工期 / 升级前的数据）缺省工期一律按 `0` 处理，CPM 仍可输出完整结果；
  服务启动时会自动为旧版 SQLite 文件补列（`ALTER TABLE` 迁移，幂等）。

**回报与并发控制**

- 回报必须携带与批次一致的 `version`，否则返回 `409 version_mismatch`（拒绝跨版本回报）。
- 节点的所有前置节点均为 `success` 后才允许回报，否则返回 `409 out_of_order`（拒绝乱序回报）。
- 已回报过的节点不可重复回报（`409 already_reported`）。
- 批次失败回滚后不可继续回报，需先调用 `/retry` 续发（`409 release_aborted`）。

**节点有限自动重试（退避等待）**

- 节点回报 `failed` 时，系统在同一事务内**原子递增** `attempt_count`（已失败次数，初始 0）：
  - `attempt_count < max_attempts`：**不触发批次回滚**。节点恢复为 `pending` 并标记 `retry_scheduled`，
    设置 `next_retry_at = 失败时间 + retry_delay_seconds`（延迟 0 即立即到期）；
    批次状态保持不变，已成功的其他节点不受影响。响应 `result=retry_scheduled`，
    含 `attempt_count` / `max_attempts` / `attempts_remaining` / `next_retry_at`。
  - `attempt_count` 达到 `max_attempts`：保持既有语义——失败节点置 `failed`、逆拓扑回滚本批次已成功节点、
    批次进入 `failed` / `partially_rolled_back` 并返回 `/retry` 断点。
- 等待期间（当前时间早于 `next_retry_at`）：
  - `GET /api/releases/{id}` 的 `next_nodes` **排除该节点**；到期后且前置全部成功时节点自动重新出现
    （GET 为只读接口，到期本身不写库、不清标记）；
  - 提前对该节点回报（成功或失败）返回 `409 retry_not_due`，节点与批次**状态完全不变**。
- 到期后重试成功：节点置 `success`，清除 `retry_scheduled` / `next_retry_at`；
  `attempt_count` 保留为历史失败次数。
- 节点详情返回 `attempt_count`、`max_attempts`、`retry_delay_seconds`、`retry_scheduled`、`next_retry_at`；
  旧批次（升级前创建）按 `max_attempts=1`、`attempt_count=0` 兼容，失败即回滚。

**失败回滚与断点重试**

- 任一节点回报 `failed`：批次标记为 `partially_rolled_back`（此前无成功节点时为 `failed`）。
- 回滚范围**仅限本批次已成功的节点**（历史批次完全不受影响），按**逆拓扑顺序**回滚：
  层级高的先回滚，同层按成功时间倒序；失败节点本身保持 `failed`。
- 回报响应中返回可重试断点 `checkpoint`：含 `/retry` 地址、`restart_level` 与 `restart_nodes`
  （调用重试后最先可执行的节点，即最低待执行层级）。
- `POST /retry` 把所有 `failed` / `rolled_back` 节点重置为 `pending`，从最低待执行层级继续，
  已经成功过的节点（如发生在“部分成功后才失败”的重试场景）保留成功状态，不重复执行。

节点状态：`pending` → `success` / `failed`；失败后可在退避等待期保持 `pending`（`retry_scheduled=true`，到期重新可执行）；
成功节点在达到尝试上限的失败回滚后变为 `rolled_back`，重试重置回 `pending`（`/retry` 会同时清零 `attempt_count` 与重试标记）。
批次状态：`pending` / `running` / `succeeded` / `failed` / `partially_rolled_back`。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/releases` | 创建发布批次（提交节点 + 依赖，节点可填正整数 `estimated_seconds`、`max_attempts`(1～10)、`retry_delay_seconds`(0～3600)，自动拓扑分层，拒绝环） |
| `GET` | `/api/releases?limit=50` | 发布批次列表 |
| `GET` | `/api/releases/{id}` | 查询发布状态（节点状态、进度、next_nodes；节点含 attempt_count/max_attempts/next_retry_at 等重试信息） |
| `GET` | `/api/releases/{id}/plan?deadline_seconds=` | 拓扑分层 + CPM 工期分析（最短工期、最早/最晚开始、total_slack、零余量关键节点、字典序最小关键路径；可选截止工期可行性，只读） |
| `POST` | `/api/releases/{id}/report` | 节点成功 / 失败回报（跨版本、乱序、重试未到期返回 409；失败未达上限返回 retry_scheduled，达上限触发回滚） |
| `POST` | `/api/releases/{id}/retry` | 从断点续发 |
| `GET` | `/health` | 健康检查 |

错误响应统一格式：`{"error": {"code": "...", "message": "...", ...}}`。

## 端到端示例

```bash
# 1) 创建批次：a、b 是底层，c 依赖 a/b，d 依赖 c
curl -s -X POST http://localhost:8000/api/releases \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "2026.09.20-001",
    "nodes": [
      {"name": "svc-a"}, {"name": "svc-b"},
      {"name": "svc-c"}, {"name": "svc-d"}
    ],
    "dependencies": [
      {"from_node": "svc-a", "to_node": "svc-c"},
      {"from_node": "svc-b", "to_node": "svc-c"},
      {"from_node": "svc-c", "to_node": "svc-d"}
    ]
  }'
# -> 计划分层: level0=[svc-a,svc-b]  level1=[svc-c]  level2=[svc-d]

# 2) 查看执行计划（含 CPM 关键路径分析）
curl -s http://localhost:8000/api/releases/<ID>/plan
#    （上例未填 estimated_seconds，所有工期按 0：最短工期 0、全部节点 total_slack=0）

# 2b) 带工期的批次 + deadline 可行性分析（示例：a=2s, b=5s, c=3s, d=1s）
curl -s -X POST http://localhost:8000/api/releases \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "2026.09.21-002",
    "nodes": [
      {"name": "svc-a", "estimated_seconds": 2},
      {"name": "svc-b", "estimated_seconds": 5},
      {"name": "svc-c", "estimated_seconds": 3},
      {"name": "svc-d", "estimated_seconds": 1}
    ],
    "dependencies": [
      {"from_node": "svc-a", "to_node": "svc-c"},
      {"from_node": "svc-b", "to_node": "svc-c"},
      {"from_node": "svc-c", "to_node": "svc-d"}
    ]
  }'
# 关键路径 svc-b -> svc-c -> svc-d（5+3+1=9 秒，svc-a 有 3 秒余量）
curl -s 'http://localhost:8000/api/releases/<ID>/plan?deadline_seconds=8'
# -> schedule:
# {
#   "shortest_duration_seconds": 9,
#   "deadline_seconds": 8,
#   "feasible": false,
#   "gap_seconds": 1,
#   "critical_nodes": ["svc-b", "svc-c", "svc-d"],
#   "critical_path": ["svc-b", "svc-c", "svc-d"]
# }
# 每个节点附带：estimated_seconds / earliest_start / latest_start / total_slack / is_critical

# 3) 回报 level0 成功（同层可并行/乱序回报）
curl -s -X POST http://localhost:8000/api/releases/<ID>/report \
  -H 'Content-Type: application/json' \
  -d '{"node":"svc-a","status":"success","version":"2026.09.20-001"}'

# 4) 乱序回报会被拒绝（svc-b 尚未成功，svc-c 前置不齐）
#    -> 409 {"error":{"code":"out_of_order", ...}}

# 5) 跨版本回报会被拒绝
#    {"node":"svc-b","status":"success","version":"9999-old"}
#    -> 409 version_mismatch

# 6) svc-c 失败：svc-a/svc-b 被逆序回滚，响应含 checkpoint
curl -s -X POST http://localhost:8000/api/releases/<ID>/report \
  -H 'Content-Type: application/json' \
  -d '{"node":"svc-c","status":"failed","version":"2026.09.20-001",
       "error_code":"DEPLOY_TIMEOUT","error_message":"健康检查超时"}'
# -> status=partially_rolled_back
#    rolled_back_nodes=[{svc-b},{svc-a}]（同层按成功时间倒序）

# 7) 从断点重试：节点重置为 pending，next_nodes 重新变为 [svc-a, svc-b]
curl -s -X POST http://localhost:8000/api/releases/<ID>/retry

# 8) 查询状态
curl -s http://localhost:8000/api/releases/<ID>
```

节点有限自动重试示例（`max_attempts=3`、`retry_delay_seconds=30`）：

```bash
# 创建带重试参数的批次
curl -s -X POST http://localhost:8000/api/releases \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "2026.09.21-003",
    "nodes": [
      {"name": "svc-a", "max_attempts": 3, "retry_delay_seconds": 30}
    ]
  }'

# 第 1 次失败：attempt_count 递增为 1，不回滚，节点恢复 pending 等待 30 秒
curl -s -X POST http://localhost:8000/api/releases/<ID>/report \
  -H 'Content-Type: application/json' \
  -d '{"node":"svc-a","status":"failed","version":"2026.09.21-003"}'
# -> {"result":"retry_scheduled","attempt_count":1,"max_attempts":3,
#     "attempts_remaining":2,"next_retry_at":"...","rollback_triggered":false, ...}

# 等待期内 GET：next_nodes 不含 svc-a；提前回报 -> 409 retry_not_due（状态不变）
# 到期后 GET：next_nodes 重新出现 ["svc-a"]，再次回报成功即清除重试标记
# 若连续失败至 attempt_count=3：触发原失败回滚 / checkpoint 语义（与 max_attempts=1 一致）
```

循环依赖示例（返回 `422`）：

```bash
curl -i -X POST http://localhost:8000/api/releases \
  -H 'Content-Type: application/json' \
  -d '{"version":"bad","nodes":[{"name":"x"},{"name":"y"}],
       "dependencies":[{"from_node":"x","to_node":"y"},{"from_node":"y","to_node":"x"}]}'
```

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由、启动初始化、统一错误处理
│   ├── config.py      # 环境变量配置
│   ├── database.py    # SQLite 连接、建表、事务
│   ├── models.py      # 请求模型
│   ├── errors.py      # 业务异常
│   └── service.py     # 拓扑分层 / 批次 / 回报 / 回滚 / 重试
├── requirements.txt
├── smoke_test.py      # 端到端冒烟测试（pip install httpx 后 python3 smoke_test.py）
├── Dockerfile         # 构建期安装依赖
├── docker-compose.yml # 一条命令启动 + 数据卷持久化
└── README.md
```

## 测试

```bash
pip install -r requirements.txt httpx   # httpx 仅 TestClient 需要
python3 smoke_test.py
```

覆盖：循环依赖拒绝、拓扑分层、跨版本回报拒绝、乱序回报拒绝、重复回报拒绝、
失败逆序回滚本批次已成功节点、断点重试后走完全程、部分回滚、历史批次隔离、重复初始化幂等、
CPM 前向/后向分析与零余量关键节点、并列最长路径字典序最小、deadline 可行性与缺口秒数、
查询不改变批次状态、estimated_seconds 正整数校验、旧版 SQLite 文件自动迁移且旧工期按 0、
节点有限自动重试（退避等待、409 retry_not_due、next_nodes 到期过滤、达到上限触发回滚、参数 1～10 / 0～3600 校验）。
