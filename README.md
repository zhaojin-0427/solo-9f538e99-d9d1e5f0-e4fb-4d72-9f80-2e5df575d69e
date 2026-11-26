# 样书补印 / 急停续印 API

印厂用手头的单面印张和少量空白纸配齐一册样书。本服务在原有「开批 → 领纸 → 补印指令」之上，增加印机急停后的续印核对：

- **开批 / 领纸**：登记书帖正反版号与纸张已印版号（空白纸留空），二分图匹配保证全册配得齐；领纸发原补印指令。
- **记急停（每批一次）**：勾选同批已领纸中本轮上机的纸号，填印机记录的正、反面补印成功张数。
- **报验 / 更正**：按纸号登记或整体更正「停机时已补成功的面」。
- **核对**：结合两项张数、报验与原补印指令（空白纸先正后反）做约束求解。
  - 一张纸在所有可行进度组合中仍有多种进度 → **待查**；
  - 已补面唯一确定、且还有缺面 → **可领单**；
  - 已补面唯一确定且无缺面 → **已补齐**。
- **续印单**：按纸号领取，列明待印面与版号；同纸重领返回原单（`replayed: true`）。
- **冻结**：发单时把该纸确定的停机进度冻结进单；后续核对、更正都保留它。
- **并发**：发单与报验都在 `BEGIN IMMEDIATE` 事务内「读最新已提交状态 → 核对 → 落库」，后提交者按先提交者的结果核对。
- **原子性**：核对无解或保存失败一律回滚，原急停/报验记录与已发续印单照旧。
- **持久化**：全部状态存 SQLite 数据卷，重启后可查单、继续核对。

## 启动

```bash
docker compose up --build
```

服务在 `http://localhost:8000`，交互文档在 **`http://localhost:8000/docs`**（Swagger UI，可直接在页面里发请求）。
SQLite 落在命名卷 `reprint-data`（容器内 `/data/reprint.db`，可用环境变量 `REPRINT_DB` 改路径）。
Compose 带 `/health` 健康检查。

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest -q
```

## 端到端业务流程（在 /docs 操作）

1. `POST /batches` 开批（≤24 帖、≤48 纸，全册可配才 201）。
2. `POST /batches/{batch_id}/claims` 为书帖领纸，得到原补印指令（待印面 + 版号）。
3. `POST /batches/{batch_id}/stops` 记急停：
   ```json
   {
     "sheet_nos": ["P1", "P2", "P3"],
     "front_success": 2,
     "back_success": 1
   }
   ```
   `sheet_nos` 省略时默认本批全部已领纸；每批只能记一次。
4. `POST /batches/{batch_id}/inspections` 按纸号报验/更正：
   ```json
   {"sheet_no": "P1", "sides": ["正面"]}
   ```
   也可走 `PUT /batches/{id}/inspections/{sheet_no}`、`.../reports/...`。
5. `GET /batches/{batch_id}/reconciliation` 看每张纸的
   `status`（`待查 / 可领单 / 已补齐`）、`possible_progress`、
   `completed_sides`、`pending_prints`（待印面 + 版号）。
   支持 `?status=orderable|pending|completed` 与 `?sheet_no=P1` 过滤。
6. `POST /batches/{batch_id}/orders`，body `{"sheet_no":"P1"}`，或
   `POST /batches/{batch_id}/orders/P1`（可不带 body）领续印单；重复领取返回原单。
7. 发单后再更正报验：与冻结进度矛盾会得到 `409 核对无解`，原报验与原单不变；
   一致更正或对其它纸的更正在计入冻结进度后重新全局核对。
8. `GET /batches/{batch_id}/orders[?sheet_no=...]` 或
   `GET /batches/{batch_id}/orders/{sheet_no}` 查单；重启服务后依旧可查可核对。

## 接口一览

| 方法 | 路径（主路径） | 说明 |
| --- | --- | --- |
| POST | `/batches` | 开批 |
| GET | `/batches/{id}` | 批次详情（含领纸关系、急停/续印单计数） |
| POST | `/batches/{id}/claims` | 领纸，发原补印指令 |
| GET | `/batches/{id}/instructions` | 查询原补印指令 |
| POST | `/batches/{id}/stops` | 记一次急停（上机纸号 + 正/反面成功张数） |
| GET | `/batches/{id}/stops`、`/stop` | 急停记录 + 核对结果 |
| GET | `/batches/{id}/reconciliation` | 核对结果（待查/可领单/已补齐），可过滤 |
| POST/PUT/PATCH | `/batches/{id}/inspections`、`/inspections/{sheet_no}` | 报验 / 更正 |
| GET | `/batches/{id}/inspections/{sheet_no}` | 查询报验 |
| POST/PUT | `/batches/{id}/orders`、`/orders/{sheet_no}` | 领续印单 / 重领原单 |
| GET | `/batches/{id}/orders`、`/orders/{sheet_no}` | 查续印单（重启后可查） |
| GET | `/health` | 健康检查 |

为兼容不同调用习惯，另注册了多组别名：`emergency-stop(s)`、`press-stop(s)`、
`reports`、`inspection-reports`、`continuation-orders`、`reprint-orders`、
`progress`、`check` 等（连字符/下划线两种风格都可）。请求字段也兼容
`front_ok/front_count/...`、`completed_sides/printed_sides/...`、
`paper_no`、布尔标量 `front_completed`、英文面名 `front/back` 等常见近义写法。

## 核对模型（为什么会「待查」）

每张上机纸的停机进度是一个待求状态：

- 空白纸：原指令为正、反两面，且**先补正面再补反面**，所以进度只能是
  `未补 / 只补正面 / 正面+反面`，不可能「只补反面」；
- 只补一面的纸：进度只能是 `未补 / 已补该面`。

用印机两项总张数约束所有纸的逐面计数之和，再叠加报验（精确指定该纸已补面）、
已发单冻结进度（精确锁定），求全局可行组合。实现上用正/反面两维计数的
前后向动态规划（纸 ≤48、张数 ≤48），算出每张纸**真正参与某个可行解**的状态集合：

- 多于一个 → 待查；
- 恰一个且仍有缺面 → 可领单，缺面 + 版号即续印单内容；
- 恰一个且无缺面 → 已补齐；
- 全局无可行组合 → `409 核对无解`，本次写入整体回滚。

## 并发与一致性

- SQLite 连接 `isolation_level=None`，写事务显式 `BEGIN IMMEDIATE`，
  `timeout=10`，写锁把同批并发写串行化。
- 发单：同纸已有单直接重放原单；否则在锁内做全局核对、分类、插入。
  `continuation_orders` 上 `UNIQUE(batch_id, sheet_no)` 兜底一纸一单。
- 报验更正：在锁内先用「更正后的报验」覆盖试核对，无解直接 409、不写库。
- 因此「发单 vs 更正报验」并发时，后拿到锁的一方一定看得到先提交方的结果，
  并按其核对：发单先成则冻结进度，矛盾更正被拒；更正先成且使该纸已补齐，
  后发单被拒。测试用真实 uvicorn + 双线程 Barrier 反复验证。

## 测试与实测

```bash
pytest -q
# 42 passed
```

- 原有 18 个基线测试全部保持通过；
- 新增 24 个测试覆盖：急停一次性/纸号合法性/无解张数、空白纸先正后反、
  待查/可领单/已补齐分类、单面纸、续印单内容与幂等重领、
  **发单后矛盾更正被拒且原报验原单照旧**、一致更正与它纸更正、
  保存失败触发器回滚、重启（同库新建 app）查单与继续核对、
  接口/字段别名、以及真实 uvicorn 并发（发单 vs 更正、重复发单）。
- 核对 DP 另用 400+ 随机用例与全组合穷举交叉验证，参与状态集合完全一致。
- 已用真实 `uvicorn` 进程走 HTTP 实测上述「发单后更正报验」与「换进程重启后查单」，
  全部断言通过（沙箱内没有 Docker 守护进程，故未能在此处实际执行
  `docker compose up`；Compose YAML 已校验，运行时依赖无新增，按上方命令即可启动）。

## 目录

```
app/
  main.py      # FastAPI 路由、BEGIN IMMEDIATE 事务、核对/发单/报验编排与响应
  core.py      # 合用判定 + 二分图匹配 + 停机进度约束求解（DP）与分类
  db.py        # SQLite 连接与建表（含 press_stops/inspections/continuation_orders）
  schemas.py   # 请求模型与近义字段兼容解析
tests/
  test_api.py        # 基线 18 测
  test_stop_flow.py  # 急停续印新增 24 测（含真实并发）
```

## 待办 / 可继续加强

- 报验目前是「该纸停机已补面」的**整组覆盖（upsert）**；若现场需要
  `PATCH` 逐面追加/撤销并保留报验历史，可再加事件表与部分更新语义。
- 续印单尚未做「续印完成回销」状态流转；当前按需求保持原单不变、可反复重领。
- 急停记录按「每批一次」设计为不可改计数，只能被拒绝后重试；如允许机长更正
  两项张数，可加带审计的 `PUT /stops/current`。
- 未做鉴权/多租户与操作人字段；内网工具默认开放 CORS，上生产前应补认证与审计。
- 仓库内未提供 Docker 守护进程实测镜像构建；交付环境请跑一次
  `docker compose up --build` 做最终确认。
