# 排污口清单同步 API

环保数据员接收监管系统的**排污口全量清单**（分片上传）与**连续变更流**，巡查端按区查询**现役排口**。
FastAPI + SQLite（WAL 模式）实现，发布切换采用单事务原子换版：任何缺片、断号或写入失败都不会影响已供出的旧版清单。

## 一、快速启动

### Docker Compose（推荐）

```bash
docker compose up -d --build
# 服务：http://localhost:8000
# 交互文档：http://localhost:8000/docs  （可直接在页面上试接口）
# ReDoc：http://localhost:8000/redoc
```

SQLite 数据文件位于命名卷 `discharge-data` 内（`/data/discharge.db`）。

### 本地直接运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库路径可用环境变量覆盖：DB_PATH=/somewhere/discharge.db
```

### 运行测试

```bash
pip install -r requirements.txt
pytest                      # 38 个单元/并发用例（含 16 个维表回溯用例）
python3 tests/smoke_http.py # 需先启动服务，40 项 HTTP 端到端检查
```

## 二、核心业务规则

### 1. 全量清单（批次 + 分片）

- `POST /batches` **开批**：声明分片总数 `total_shards` 与来源截止序号 `source_cutoff_seq`（快照基线）。
  同一时刻只允许一个 `open` 批次，重复开批返回 `409 OPEN_BATCH_EXISTS`。
- `POST /batches/{id}/shards` **分片提交**：每片含排口号、区号、启停状态。
  - 同 `(批次, 片号)` 重试且**内容相同** → 幂等，只生效一次（响应 `idempotent=true`）。
  - 同片号**内容不同** → `409 SHARD_CONTENT_CONFLICT`。
  - 排口号在批次内重复（片内或跨片）→ `409 DUPLICATE_OUTLET(_IN_SHARD)`。
- 暂存区与已发布清单物理隔离：发布前巡查端查不到任何新批数据。

### 2. 变更流（连续序号）

`POST /changes` 提交 `add`（新增，需 district+active）/ `move`（迁区，需 district）/ `deactivate`（停用）。

- 序号 `seq` 从 1 开始必须**连续**：越过缺号提交返回 `422 SEQ_GAP`，并给出待补序号。
- 同序号内容相同重试 → 幂等；内容不同 → `409 CHANGE_CONTENT_CONFLICT`。
- 序号 ≤ 已供出水位 `last_served_seq` 的补写 → `409 SEQ_REGRESSION`（已发布历史不可改）。
- **全量上传期间变更照常到达**：合法变更立即套用到现役（旧版）清单，巡查端实时可见。
- 变更引用的排口不存在时，该序号置 `blocked`，**其后的变更不得越过断点**；
  待新快照补齐排口并发布时，窗口重放自动解除阻断。

### 3. 发布切换（原子换版）

`POST /batches/{id}/publish`，指定追平序号 `catch_up_seq`。全部条件满足才发布：

| 门禁 | 失败码 |
| --- | --- |
| 追平序号不得低于已供出水位（不可倒退） | `409 CATCHUP_REGRESSION` |
| 追平序号不得小于批次来源截止序号 | `422 CATCHUP_BELOW_CUTOFF` |
| 分片必须齐全 | `422 MISSING_SHARDS`（返回待补片号） |
| 截止序号之后至追平序号之间变更无缺号 | `422 CHANGE_SEQ_GAP`（返回待补序号） |
| 窗口变更在新快照上可套用 | `422 PUBLISH_CHANGE_FAILED` |
| 已生效的窗口外高序号变更，快照必须已含其效果 | `422 SNAPSHOT_MISSING_LIVE_CHANGE` |

发布在**单个 IMMEDIATE 事务**内完成：写入新版本快照 → 按序重放窗口 `(cutoff, catch_up_seq]` 内变更
→ 续跑窗口外未套用变更 → 删除旧版 → 推进版本号与水位 → 关闭批次。
任一步失败整体回滚，**旧版照常供数，批次保持 open**（可用 `POST /batches/{id}/abort` 废弃后等下一周期重开）。

### 4. 查询（始终取同一已提交状态）

- `GET /outlets?district=&active_only=&limit=&offset=`：按区查询现役排口。
- `GET /stats`：按区计数（总数/启用/停用），与 `/outlets` 读同一版本。
- `GET /batches/current`：当前批次的已收片号、**待补片号**、待补变更序号、断点原因、水位。
- `GET /changes`：变更流、全量缺号列表、阻断信息。
- `GET /health`：当前版本号与已供出序号水位。

所有读请求走 SQLite 只读连接（WAL 模式），不阻塞写。

### 5. 可回溯排口维表（水样按采样时刻归档）

水质资料员按**采样当时**的排口辖区归档水样，排口后来迁区、停用不得改变旧样本归属。
维表采用慢变维（SCD）思路，以**状态号 + 完整清单快照**实现，与现役 `outlet` 表物理隔离、只增不改：

- `POST /dimension/enable` **启用维表**：对当前现役清单拍初始快照，得到初始状态号（首次为 1）。
  尚无发布版本时对应空清单；重复启用幂等，不重拍快照。
- 此后两类事件在**同一 IMMEDIATE 事务**内留下新状态号，每个状态号都对应一份当时的**完整清单**
  （全部排口的辖区与启停）：
  - 已生效的增量变更（`source=change`，记录对应变更序号 `ref_seq`）——每条生效变更一个状态；
  - 整批换版发布（`source=publish`）——换版一个状态。
- **暂挂、阻断的变更不生成新状态**（现役状态号保持不变），幂等重试同样不产生状态。
- 现役查询 `GET /outlets`、`GET /stats` 返回当前 `state_no`，采样时记录该值。
- `POST /samples/attribute` **批量水样归属**：输入水样号、排口号、采样时取得的状态号，
  只读取 `dim_outlet` 快照表返回当时辖区与启停；**绝不回退到现役行推断旧状态**。
  无法归属给出明确原因：

| 原因码 | 含义 |
| --- | --- |
| `STATE_NOT_FOUND` | 状态号不存在（维表未启用或该状态从未生成） |
| `OUTLET_NOT_IN_STATE` | 该状态的完整清单中没有这个排口（如排口在采样之后才新增） |

- 增量与换版交错时，历史快照不可变，旧样本归属始终稳定；新状态一定对应完整清单。
- 发布/快照任一步失败随外层事务整体回滚，**不会留下可见的半份历史**，旧版与旧状态照常供数。
- `GET /dimension/states` 查看状态号流水（来源、关联变更序号、版本、清单规模），
  `GET /dimension/states/{state_no}` 查看某状态的完整快照。

## 三、接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/batches` | 开批（片数 + 来源截止序号） |
| GET | `/batches/current` | 当前批次进度与待补片号/序号 |
| POST | `/batches/{batch_id}/shards` | 提交分片（幂等/冲突） |
| POST | `/batches/{batch_id}/publish` | 发布切换（指定追平序号） |
| POST | `/batches/{batch_id}/abort` | 废弃进行中的失败批次 |
| POST | `/changes` | 提交连续序号变更 |
| GET | `/changes` | 变更流与缺号/断点查询 |
| GET | `/outlets` | 按区查询现役排口（返回当前状态号） |
| GET | `/stats` | 按区计数（返回当前状态号） |
| POST | `/dimension/enable` | 启用维表，记录现役清单初始状态号（幂等） |
| GET | `/dimension/states` | 状态号流水（初始/增量/换版来源） |
| GET | `/dimension/states/{state_no}` | 某状态号对应的完整清单快照 |
| POST | `/samples/attribute` | 批量水样归属回溯（返回当时归属及无法归属原因） |
| GET | `/health` | 健康检查、版本水位与维表状态 |

错误响应统一为：`{"code": "错误码", "message": "中文说明", "details": {...}}`。

## 四、目录结构

```
app/
  database.py   # SQLite 连接、WAL、建表、IMMEDIATE 事务（含 dim_state/dim_outlet 维表）
  models.py     # Pydantic 请求/响应模型
  service.py    # 全部业务规则与事务边界（维表快照、水样归属回溯）
  main.py       # FastAPI 路由与异常处理
tests/
  test_sync_api.py     # 18 个功能用例（含切换交错、失败回滚、阻断续链）
  test_dimension.py    # 16 个维表用例（初始状态、增量/换版快照、回溯、阻断不产状态、失败无半份历史）
  test_concurrency.py  # 4 个多线程并发用例（串行化、幂等、序号约束）
  smoke_http.py        # 针对运行中服务的 40 项 HTTP 冒烟检查
docker-compose.yml / Dockerfile
```

## 五、关键设计说明

- **幂等判定**：请求体规范化 JSON（排序键、无空格）后取 SHA-256 存库，重试只比对哈希。
- **并发控制**：所有写请求 `BEGIN IMMEDIATE` 串行化，配合 `busy_timeout=30s`；读请求只读连接，读写不互斥。
- **版本隔离**：`outlet` 表带 `version` 字段，发布成功前旧版行不动，切换时同事务删旧插新。
- **断点不越号**：变更链维护 `blocked/block_reason`，断点之后的变更保持未应用，缺号无法靠后续高序号“跳过”。
- **维表回溯**：`dim_state`（状态号元数据）+ `dim_outlet`（每状态完整清单快照）只增不改；
  状态号与现役版本在同一事务推进，水样归属只查快照表，保证迁区/停用后旧样本归档不变、失败无半份历史。
