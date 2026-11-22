# 打乱卷型单选测验 · 答项统计 API

同一场单选测验被排成多种卷型（题序、选项顺序、印刷字母都可能不同）。扫描答卷
有时丢失了卷型，只剩「卷面题号 → 印刷选项字母」。复核员按**标准题**登记实际
选项后，服务用卷型对照表反推扫描件对应的标准选项。

- 候选卷型：把该卷**全部**复核记录都能对上（且扫描件不缺该题号）的卷型。
- 只有所有候选卷型都还原出**同一标准选项**的题，才进入答项分布。
- 没有候选卷型时整卷待核，该卷不贡献任何一道题的计数。
- 登记 / 撤回复核只重算该卷；复核记录、逐卷结果和题目计数在同一事务里提交。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- Swagger UI / 演示入口：<http://localhost:8000/docs>
- OpenAPI：<http://localhost:8000/openapi.json>
- 健康检查：`GET /health`

首次启动会写入一组演示数据（1 场测验、5 道标准题、4 种卷型、5 份答卷）。
数据保存在命名卷 `quiz-data`（容器内 `/data/answers.db`），重启不会丢复核。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/questions` | 标准题与标准选项 |
| GET | `/forms` | 各卷型：卷面题号 → 标准题，及各标准选项的印刷字母 |
| GET | `/sheets` | 逐卷摘要，可按 `status` 过滤（`complete`/`partial`/`pending`/`not_computed`） |
| GET | `/sheets/{id}` | 逐卷答项：扫描作答、候选卷型、每题还原结果、复核记录 |
| GET | `/sheets/{id}/pending` | 待核原因（整卷级 + 题目级） |
| PUT | `/sheets/{id}/reviews/{question_id}` | 登记 / 覆盖一道标准题的实际选项 |
| DELETE | `/sheets/{id}/reviews/{question_id}` | 撤回该题的复核 |
| GET | `/stats`、`/stats/{question_id}` | 各标准题的答项计数 |
| POST | `/admin/import` | 用完整数据集替换全部内容并全量重算 |
| POST | `/admin/recompute` | 依据现有数据全量重算（修复用） |

### 演示步骤

```bash
# 1. 逐卷答项与候选卷型
curl -s localhost:8000/sheets/S001 | python3 -m json.tool

# 2. 某卷待核原因
curl -s localhost:8000/sheets/S004/pending | python3 -m json.tool

# 3. 登记复核（S003 原本 4 个候选卷型，登记 Q1=A 后收敛）
curl -s -X PUT localhost:8000/sheets/S003/reviews/Q1 \
     -H 'content-type: application/json' \
     -d '{"option_id":"Q1-A","reviewer":"alice"}' | python3 -m json.tool

# 4. 看计数变化，再撤回，计数还原
curl -s localhost:8000/stats/Q1 | python3 -m json.tool
curl -s -X DELETE localhost:8000/sheets/S003/reviews/Q1 | python3 -m json.tool
curl -s localhost:8000/stats/Q1 | python3 -m json.tool
```

### 卷状态

- `complete`：全部标准题都得到唯一答项。
- `partial`：至少一题已计数，仍有题目待核。
- `pending`：无候选卷型，整卷待核。
- `not_computed`：仅出现在列表过滤中（理论状态，正常写入后不会出现）。

## 数据模型

- `standard_questions` / `standard_options`：标准题与标准选项（选项 id 全局唯一）。
- `forms` / `form_questions` / `form_options`：卷型、卷面题号到标准题的对照、
  标准选项到印刷字母的对照。
- `sheets` / `sheet_answers`：答卷与「卷面题号 → 印刷字母」扫描结果。
- `reviews`：按 (答卷, 标准题) 的唯一有效复核；撤回即删除。
- `sheet_results` / `sheet_question_results`：缓存的逐卷与逐题结果。
- `question_option_counts`：题目 × 选项的累计计数。

计数采用增量维护：每次复核变更在事务里读取该卷旧的计入项，重算后只按差量
调整 `question_option_counts`，因此单卷复核不会触碰其它卷。

## 一致性设计

- 每个请求独立 SQLite 连接，写操作使用 `BEGIN IMMEDIATE`，由 SQLite 写锁串行化；
  `busy_timeout` 默认 15s，并对 `SQLITE_BUSY` 做指数退避重试。
- 一次复核变更 = 写/删复核 + 重算该卷结果 + 调整计数，同一事务提交。任一步失败
  整体回滚，接口返回 503，原复核、原结果、原计数保持不变。
- 并发复核按「已提交的全部复核记录」出数：后来者读到的复核证据已经包含先提交者，
  计数用差量更新而非「读-改-写」绝对值，不会丢更新。
- 读取在 `BEGIN` 事务内完成，保证多表结果来自同一快照。

## 本地开发（无 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
pytest -q
DATABASE_PATH=./data/answers.db uvicorn app.main:app --reload
```

## 已知限制

- 未集成鉴权与审计日志；`reviewer` 只是自由文本。
- 数据通过 `POST /admin/import` 整体替换，尚未提供单条增量导入。
- 计数为增量维护，若外部直接改库，可用 `POST /admin/recompute` 全量重建。
