# 园区分表电量推算 API

面向园区能源结算场景：采集系统导出的读数在结算分界时刻可能缺测，需要判断
**指定分表在指定起止时刻之间的耗电量能否被现有读数唯一确定**，以决定能否
直接结算、是否需要补抄。

本服务接收 1 个总表与 2~6 个分表的累计电量读数（UTC）及各分表功率上限，
用**线性规划**求该分表在该时段内可行耗电量的**精确最小值与最大值**
（不是线性插值）；读数自相矛盾时明确返回“读数不相容”。

v2 起新增**按结算期的整批发布与结算查询**：补传的整批电表读数只有在物理
相容且版本匹配时才会原子替换正式读数，结算查询永远读到完整的某一版本，
不会混入半批数据。

## 模型与假设

- 任意时段：总表耗电量 = 全部分表耗电量之和；
- 各分表耗电非负，且不超过 `功率上限 × 时段长度`；
- 不考虑损耗、换表与回零（累计读数单调不减）；
- 每只表均有观测期首、尾读数，中间允许缺测；读数时间统一为 UTC。

**求解方法**：取所有表读数时刻与查询起止时刻的并集切分时间轴，设
`x[j,k]` 为分表 j 在基本区间 k 的耗电量，约束为“每对相邻读数之差 =
区间内耗电之和”“总表 = 分表之和”“`0 ≤ x[j,k] ≤ P_j·Δt_k`”，对查询
窗口内 `x` 之和分别求 min / max（HiGHS 求解器）。

该边界是**精确**的：任何真实功率曲线必满足这些区间约束；反过来任一
LP 可行解都可用不超过功率上限的分段恒定功率物理实现。因此 min/max 就是
全部读数约束下的可行电量边界——线性插值只能给出来历不明的点估计，
无法回答“是否足以确定”。

## 结算期发布与版本控制

### 发布闸门（只发布物理相容的完整批次）

`POST /api/v1/periods/{period}/publish` 接收与 `/infer` 完全相同的
`meters` 结构，外加：

- `expected_version`：客户端预期的**当前**正式版本号，首次发布传 `0`；
- `request_key`：客户端请求键（幂等键），同一结算期内唯一标识本次提交。

处理顺序：

1. **结构校验**：沿用与 `/infer` 相同的模型校验（1 个总表、2~6 个分表、
   观测期首尾对齐、时间带时区等），不合法返回 422；
2. **物理相容性检查**：沿用同一个求解器，对整批读数跑一次 LP 可行性检查，
   无可行解则拒绝发布（409 `incompatible_batch`，附不相容诊断），
   **正式读数保持旧版本不变**；
3. **版本匹配 + 原子替换**：单条 `BEGIN IMMEDIATE` 写事务内检查
   `expected_version` 是否等于当前版本；匹配才写入新版本、把当前版本
   **原子递增**（0→1→2…），不匹配返回 409 `version_conflict` 并保留旧版。

**同版本并发提交最多一个成功**：写事务先抢锁，后到者在锁内看到版本已
递增而失败。任何失败整体回滚，因此不会出现半批数据，也不会出现重复版本。
LP 求解在写事务之外执行（WAL 读不阻塞写），**求解期间旧版读数仍可查询**。

### 幂等与安全重传

- 同一结算期 **同键同内容**重传：返回**原始发布结果**（`status=replayed`，
  版本号、发布时间均为首次发布的值）。即使首次响应丢失、之后已发布更新
  版本，重放的仍是当初那次结果——幂等记录与当前版本解耦；
- 同键**异内容**：拒绝（409 `key_content_conflict`），原键对应的原结果
  仍可继续重放；
- 业务失败（不相容、版本冲突）同样按请求键持久化，中断的提交可用同一键
  安全重传并得到确定的同一结果。

### 按结算期推算（整次推算同一版读数）

`POST /api/v1/periods/{period}/infer` 的 `query` 与 `/infer` 相同，
读数来自该结算期的**当前正式版本**：在一个只读事务中取出版本快照后再求解，
整次推算固定使用同一版读数，响应额外携带 `version` 字段。该期尚无正式
读数时返回 404。

### 持久化

数据保存在本地 SQLite（WAL 模式，`synchronous=FULL`），数据库文件位于
`METER_DB_PATH`（容器内 `/srv/data/meter_store.db`，Docker 命名卷挂载）。
读数版本与请求结果均落盘，**进程/容器重启后完整保留**；历史版本逐行留存，
版本号严格递增不重复。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

启动后访问交互式 API 文档：**http://localhost:8000/docs**

文档内所有端点均内置可直接 *Try it out* 的示例。推荐演示顺序（结算期
`2026-01`）：

1. `POST /api/v1/periods/{period}/publish` → 选“首次发布
   （expected_version=0）”，得到 `version=1`；
2. 再次 *Execute* 同一请求 → `status=replayed`，返回同一原始结果；
3. 选“预期版本号不匹配”示例 → 409 `version_conflict`，旧版保留；
4. 选“不相容批次被拒绝”示例 → 409 `incompatible_batch`，附诊断；
5. `POST /api/v1/periods/{period}/infer` → 对版本 1 的读数推算，
   响应附 `"version": 1`；
6. 用“发布下一版（expected_version=1）”示例发布版本 2，再推算一次，
   响应变为 `"version": 2`；
7. `GET /api/v1/periods`、`GET /api/v1/periods/{period}`、
   `GET /api/v1/periods/{period}/versions` 查看期状态与版本历史。

原有的 `POST /api/v1/infer`（无状态即时推算）行为完全不变。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/infer` | 即时推算（入参自带读数，不落库），原有接口 |
| POST | `/api/v1/periods/{period}/publish` | 按结算期原子发布整批读数（版本+幂等） |
| POST | `/api/v1/periods/{period}/infer` | 基于该期当前正式版本推算，响应附版本号 |
| GET | `/api/v1/periods` | 列出所有结算期及当前版本 |
| GET | `/api/v1/periods/{period}` | 单个结算期的当前版本状态 |
| GET | `/api/v1/periods/{period}/versions` | 结算期历史版本列表 |
| GET | `/health` | 健康检查 |

`{period}` 允许字母、数字、点、下划线、短横线（1~64 字符）。

### 发布请求与响应

请求：

```json
{
  "meters": [ /* 与 /api/v1/infer 的 meters 完全相同的整批读数 */ ],
  "expected_version": 0,
  "request_key": "bulk-2026-01-upload-0001"
}
```

成功响应（HTTP 200）：

```json
{
  "status": "published",
  "period": "2026-01",
  "version": 1,
  "request_key": "bulk-2026-01-01-upload-0001",
  "content_hash": "6a5868f9…c65f83",
  "meters": [ /* 已发布的整批读数 */ ],
  "published_at": "2026-11-11T16:45:08.699190Z",
  "message": "结算期 2026-01 整批读数已原子发布，正式版本由 0 递增至 1"
}
```

同键同内容重传时 HTTP 仍为 200，`status` 为 `replayed`，其余字段与首次
发布完全一致。失败响应（HTTP 409）统一为错误信封：

```json
{
  "error": {
    "code": "version_conflict",
    "message": "预期版本号 0 与当前正式版本号 1 不匹配，拒绝发布，正式读数保持版本 1 不变",
    "period": "2026-01",
    "expected_version": 0,
    "current_version": 1
  }
}
```

错误码：`version_conflict`（预期版本不符）、`incompatible_batch`
（整批物理不相容，附 `diagnostics`）、`key_content_conflict`
（同键异内容）。入参结构不合法返回 HTTP 422。

### 按结算期推算响应

```json
{
  "status": "ok",
  "period": "2026-01",
  "version": 2,
  "meter_id": "sub_a",
  "start": "2026-01-01T01:00:00Z",
  "end": "2026-01-01T02:00:00Z",
  "min_kwh": 20.0,
  "max_kwh": 20.0,
  "is_determined": true,
  "message": "该时段电量可被现有读数唯一确定，可直接结算，无需补抄",
  "diagnostics": []
}
```

## 本地开发与测试

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q          # 39 个用例：求解、不相容诊断、入参校验、
                                    # 发布闸门、版本并发、幂等重放、重启/崩溃持久化
uvicorn app.main:app --reload       # 本地启动，文档见 http://127.0.0.1:8000/docs
```

本地运行时默认数据库为 `./data/meter_store.db`（可用环境变量
`METER_DB_PATH` 覆盖），该目录已在 `.gitignore` 与 `.dockerignore` 中忽略。

## 目录结构

```
app/
  main.py     # FastAPI 路由、异常映射与交互式文档示例
  models.py   # 请求/响应模型与业务校验（即时推算与发布共用）
  solver.py   # 线性规划建模、HiGHS 求解、整批相容性检查、不相容诊断
  service.py  # 发布闸门、乐观版本控制、幂等重放、按期间推算
  storage.py  # SQLite（WAL）持久化：事务、版本表、幂等表
tests/
  test_api.py     # 求解器与即时推算端到端测试（18）
  test_publish.py # 发布/版本/幂等/并发/持久化端到端测试（21）
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
