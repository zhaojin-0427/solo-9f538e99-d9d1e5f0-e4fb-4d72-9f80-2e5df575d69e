# 样书补印 API

印厂用手头的单面印张和少量空白纸配齐一册样书。本服务提供开批、领纸、补印指令管理，指令存 SQLite，可按批查询。

## 启动

```bash
docker compose up --build
```

服务起在 `http://localhost:8000`，在 **`http://localhost:8000/docs`**（Swagger UI）里即可开批、领纸。SQLite 数据落在名为 `reprint-data` 的卷里（容器内 `/data/reprint.db`，可用环境变量 `REPRINT_DB` 改路径）。

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest                 # 跑测试
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/batches` | 开批：至多 24 帖（正、反面版号）+ 至多 48 张纸（纸号、已印版号；空白纸留空）。全册配得齐才开批，否则 409 且不落库 |
| GET | `/batches/{batch_id}` | 批次详情：每帖领了哪张纸、每张纸发给了哪帖、是否已配齐 |
| POST | `/batches/{batch_id}/claims` | 领纸：报帖号、纸号，发补印指令（纸号、待印面、版号） |
| GET | `/batches/{batch_id}/instructions` | 按批查询全部补印指令 |

### 开批示例

```json
POST /batches
{
  "signatures": [
    {"signature_no": "S01", "front_plate": "A-01", "back_plate": "A-02"},
    {"signature_no": "S02", "front_plate": "B-01", "back_plate": "B-02"}
  ],
  "sheets": [
    {"sheet_no": "P001", "printed_plate": "A-01"},
    {"sheet_no": "P002", "printed_plate": ""}
  ]
}
```

### 领纸规则

- 已印版号对上哪一面就补印另一面（上例 `P001` 已印 `A-01`，发给 `S01` 即补印反面 `A-02`）；空白纸两面都印。
- 纸张合用，**且**领走后余纸仍能给未领的书帖各配一张，才发指令，否则 409。
- 同批并发领纸：**一帖只发一张，一张纸只发给一帖**；同帖同纸重试返回原指令（`replayed: true`）；领纸失败不占纸。

## 实现要点

- **可配性检查**：书帖—纸张构成二分图（合用即有边），用增广路法求最大匹配；开批时要求匹配覆盖全部书帖，领纸时要求扣除本帖本纸后剩余书帖仍可全覆盖。
- **并发控制**：每请求一个 SQLite 连接，领纸在 `BEGIN IMMEDIATE` 事务内完成"查重 → 判定 → 落库"，SQLite 写锁把同批并发请求串行化，后到者看到已落库状态后按规则拒发；`instructions` 表上 `UNIQUE(batch_id, signature_no)`、`UNIQUE(batch_id, sheet_no)` 两条唯一约束兜底。失败一律回滚，不占纸。
- **幂等**：同帖同纸的重复请求直接返回库里的原指令。

## 目录

```
app/
  main.py      # FastAPI 路由与领纸事务
  core.py      # 合用判定 + 二分图匹配
  db.py        # SQLite 连接与建表
  schemas.py   # 请求/响应模型（24 帖 / 48 纸上限在此校验）
tests/test_api.py
Dockerfile / docker-compose.yml
```
