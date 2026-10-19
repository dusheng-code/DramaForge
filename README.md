# DramaForge — AI 短剧受控生产系统

> 输入**题材**与**预算**，端到端产出：分集剧本、镜头表、图/视频/TTS 提示词包、定妆图、首帧、视频资产、机读约束报告与成本报告。
>
> 全链路**可中断、可恢复、人在回路、强确定性校验**——不是"一口气生成"的玩具，而是一条可被工程化管理的 AI 短剧生产线。

---

## ✨ 核心特性

- **18 段位受控流水线**：`reference → constraint → outline → episodes → characters → blocks → storyboard → prompt_pack → gate ×5 → quote → anchor_image → keyframe → video → assemble`，由状态机统一调度，支持段位级返工（revision 版本演进）与中断恢复（启动自愈）。
- **确定性校验 + 结构闸门**：18 个确定性校验器（数值 / 结构 / 文本 / 风控）全覆盖正反例测试；GateAgent 基于真实打标（payoff_tags / ref_tag_dist）与约束比对放行 5 个段位闸门。
- **7 角色 LLM 特化**：reference、constraint、script、storyboard、prompt_pack、review、tagger 各自独立配置模型、温度与额度，全局 `LLM_SEED` 保障可复现。
- **双路参照卡检索**：Milvus `hybrid_search` + RRF 融合，SQL 关键词全文检索兜底；未安装 BGE-M3 重依赖时自动惰性降级为确定性 Hash 向量。
- **真实资产生产与预算硬卡控**：AssetPump 异步消费泵并发产出定妆图 / 首帧 / 视频，fast/hero 计费阶梯、在途费用预留、报价单人工批准后方可花费，超预算自动熔断。
- **人在回路（Human-in-the-loop）**：8 种受控决议动作闭集——`approve_quote`、`reject_quote`、`extend_budget`、`pass_slot`、`retry_shot`、`skip_shot`、`veto_keyframe`、`approve_keyframe`。
- **Dry-Run 零花费模式**：Fake 供应商 + 模拟字节替代远端下载，离线环境秒级跑通全流程，所有计费记为 ¥0.000。
- **Web 控制台**：Vue 3 + Vite 构建，SSE 实时推送 18 个阶段状态流转，覆盖任务提交、进度大盘、故事板、人工审核与资产报告五大页面。

---

## 🏗️ 技术栈

| 层面 | 选型 |
|---|---|
| 语言 / 运行时 | Python 3.12+ |
| Web 框架 | FastAPI + Uvicorn（**必须单 worker**，保证状态机单写） |
| 数据模型 | Pydantic v2 + pydantic-settings |
| 存储 | MySQL 8（唯一事实源）via SQLAlchemy 2 (asyncio) + aiomysql |
| 向量检索 | Milvus Standalone via pymilvus，BGE-M3 embedding（可降级） |
| LLM | OpenAI 兼容接口（默认 DeepSeek），7 角色独立配置 |
| 图像 / 视频 / TTS | MiniMax / Hailuo 供应商薄封装 + Fake  dry-run 实现 |
| 日志 / 观测 | loguru + 分阶段日志管道 + SSE 事件总线 |
| 前端 | Vue 3 + Vite（构建产物由 FastAPI 在 `/app` 托管） |
| 包管理 / 测试 | uv + pytest / pytest-asyncio（140 个单元 / 集成 / E2E 测试） |

---

## 📁 项目结构

```text
src/dramaforge/
├── contracts/          # 契约层：枚举、ID、业务段位、产物模型、约束、资产
├── core/               # 核心层：配置、状态机迁移、事件总线、状态/资产仓储、调度器、返工、恢复
├── io/                 # IO 收口层：原子写、路径规范、jsonl、md
├── validators/         # 18 个确定性校验器（registry / numeric / structural / text / risk）
├── retrieval/          # 双路召回：Milvus hybrid_search + RRF、SQL 关键词兜底
├── providers/          # 供应商薄封装：计费阶梯、video、image、tts 及 fake 实现
├── llm/                # LLM 客户端与 7 角色特化
├── agents/             # 阶段智能体：reference / script / storyboard / prompt_pack / review(GateAgent)
├── assets/             # 资产流水线：预算总账、报价单、AssetPump 异步消费泵、审核环
├── assemble/           # 交付包装配器与 Markdown 模板
├── schemas/            # HTTP API 数据传输模型
├── api/                # FastAPI 路由层：tasks / constraints / assets / review / package / events(SSE)
├── obs/                # 观测层：分阶段日志管道
├── scripts/            # CLI：init_db / build_cards / embed_offline / verify_provider / demo
└── main.py             # 应用装配入口与生命周期托管

web/                    # Vue 3 前端（Submit / Progress / Board / Review / Assets）
tests/                  # 单元 / 集成 / 端到端测试
```

---

## 🚀 快速开始

### 环境要求

- Python ≥ 3.12，[uv](https://docs.astral.sh/uv/)
- MySQL 8.0（必需，唯一事实源）
- Milvus Standalone（可选，`localhost:19530`；缺省时自动降级为 SQL 关键词检索）
- Node.js ≥ 18（仅构建前端时需要）

### 1. 安装依赖

```bash
uv sync                 # 基础安装
uv sync --extra embed   # 可选：安装 BGE-M3 向量能力（含 torch，重依赖）
```

### 2. 配置

```bash
cp .env.example .env    # 填写 MySQL 连接、各角色 LLM API Key、MiniMax Key 等
```

未配置任何真实供应商 Key 时，可使用 `--dry-run` 模式零花费体验全流程。

### 3. 初始化数据与参照卡库

```bash
python -m dramaforge.scripts.init_db              # 建表
python -m dramaforge.scripts.build_cards --limit 36   # 写入 ≥30 张参照卡（MySQL + Milvus 双路）
```

### 4. 干跑验证（零花费，秒级跑通 18 个阶段）

```bash
python -m dramaforge.scripts.demo --dry-run --auto --episodes 1 --budget 60
```

产出物落在 `var/<task_id>/` 下：交付包 `final/drama_package.md`、镜头表 `shots.json`、资产报告、约束报告与报价单。

### 5. 启动服务与 Web 控制台

```bash
# 构建前端（首次）
cd web && npm install && npm run build && cd ..

# 启动后端 —— 注意：必须 --workers 1，保证状态机单写原则
uvicorn dramaforge.main:app --host 127.0.0.1 --port 8000 --workers 1
```

- 前端控制台：<http://127.0.0.1:8000/app/>
- API 文档（Swagger）：<http://127.0.0.1:8000/docs>

| 页面 | 功能 |
|---|---|
| Submit | 填写题材、集数、预算与偏好，一键提交任务 |
| Progress | SSE 实时观测 18 个阶段状态流转与阶段日志 |
| Board | 查看镜头、台词、分镜首帧与预览图 |
| Review | 报价批准、首帧否决、段位放行、预算追加 |
| Assets | 资产行明细、尝试次数与费用支出 |

---

## 🔌 API 概览

所有 `/api/drama/*` JSON 响应由统一外壳包装：`{"code": 0, "message": "ok", "data": ...}`。
错误映射：`ValueError → 400`、`LookupError → 404`、`RuntimeError → 409`、未知异常 → 500（不外泄堆栈）。SSE 流与文件下载原样透传。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/drama/tasks` | 提交任务（题材 + 预算） |
| GET | `/api/drama/tasks` | 任务列表 |
| GET | `/api/drama/tasks/{id}` | 任务详情 |
| GET | `/api/drama/tasks/{id}/stages` | 18 阶段状态表 |
| GET | `/api/drama/tasks/{id}/shots` | 镜头表 |
| GET | `/api/drama/tasks/{id}/stream` | SSE 事件流 |
| GET | `/api/drama/tasks/{id}/constraints` | 机读约束报告 |
| GET | `/api/drama/tasks/{id}/segments/{slot}` | 段位约束明细 |
| GET | `/api/drama/tasks/{id}/assets` | 资产行明细 |
| GET | `/api/drama/tasks/{id}/budget` | 预算总账（spent / reserved / cap） |
| GET | `/api/drama/tasks/{id}/pending-human` | 待人工决议列表 |
| POST | `/api/drama/tasks/{id}/resolve` | 提交决议（8 种闭集动作） |
| GET | `/api/drama/tasks/{id}/package` | 下载交付包 |
| GET | `/healthz` | 探活（进程 + 数据库） |

---

## 🧪 测试

```bash
pytest -q   # 140 个单元 / 集成 / 端到端测试
```

测试模式下全局禁用外网访问与付费 API，仅需本地 MySQL 即可运行。覆盖：计费阶梯、启动恢复、RRF 融合、状态机迁移、约束过滤、18 校验器正反例（36 用例）、段位返工、资产泵与预算卡控、端到端干跑。

---

## 📌 设计说明

- **单 worker 强约束**：状态机遵循单写原则，Uvicorn 必须配置 `--workers 1`。
- **可中断可恢复**：所有状态持久化于 MySQL，进程重启时自动执行 recovery 自愈，在途任务从断点继续。
- **预算三态口径**：`spent_cny` 统计历史全部实际支出（含 REJECTED / ORPHANED）；`reserved_cny` 仅统计当前 revision 的在途估算费用。
- **合规不放过但人来定**：违禁词校验器即使无命中仍产出 `needs_human=True`，机器只保证不漏词，最终由人工核准签署。
- **重依赖可选**：BGE-M3（数 GB torch 依赖）通过独立 extra 提供，缺省环境自动降级为 Hash 向量 / SQL 检索，不阻塞主流程。

---

## 🧰 真实供应商验证（可选）

```bash
# 配置 MINIMAX_API_KEY 后，发起生图 / 视频 / TTS 真实扣费探针
python -m dramaforge.scripts.verify_provider --mode all --timeout 60

# 安装 embed extra 后，批量计算 BGE-M3 向量回填 Milvus
python -m dramaforge.scripts.embed_offline --batch-size 32
```
