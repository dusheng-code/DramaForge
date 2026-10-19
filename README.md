# DramaForge — AI 短剧受控生产系统

按照《DramaForge 实现规格 v1》完成的全量实现与验收交付。

输入题材与预算，端到端产出分集剧本、镜头表、图/视频/TTS 提示词包、定妆图、首帧、视频资产、机读约束报告及成本报告。系统具备可中断、可恢复、人在回路介入与强确定性校验。

---

## 一、 系统架构与模块划分

```text
src/dramaforge/
├── contracts/          # 契约层：枚举(enums)、ID(ids)、业务段位(slots)、产物模型(artifacts)、约束(constraints)、资产(assets)
├── core/               # 核心层：配置(config)、状态机迁移(transitions)、总线(bus)、事件(events)、状态仓储(state_store)、资产仓储(asset_store)、调度器(scheduler)、返工机制(revision)、恢复(recovery)
├── io/                 # IO收口层：artifacts (原子写/路径规范)、jsonl、md
├── validators/         # 18个确定性校验器注册表(registry)、numeric、structural、text、risk
├── retrieval/          # 双路召回：MilvusCardRepo(hybrid_search+RRF)、sql_repo(SQL关键词兜底)、cards、ranker
├── providers/          # 供应商薄封装：pricing(计费阶梯)、video(MiniMax/Hailuo)、image、tts、fake_video、fake_image
├── llm/                # LLM客户端与角色特化：client、roles、objects、fake
├── agents/             # 阶段智能体：context、reference、script、storyboard、prompt_pack、review(GateAgent)
├── assets/             # 资产流水线：budget(预算总账)、quote(报价单)、pump(AssetPump异步消费泵)、review_loop
├── assemble/           # 交付包装配器：assembler 与各章节 Markdown 模板
├── schemas/            # HTTP API 数据传输模型与视图定义
├── api/                # FastAPI 路由层：tasks, constraints, assets, review, package, events(SSE), runtime
├── obs/                # 观测层：分阶段日志管道 (logs)
├── scripts/            # CLI 脚本：init_db, build_cards, embed_offline, verify_provider, demo
└── main.py             # FastAPI App 装配入口、生命周期托管、统一外壳包装
web/                    # Vue3 + Vite + TailwindCSS 纯前端实现 (Submit, Board, Progress, Review, Assets)
tests/                  # 140 个单元/集成/端到端测试 (全部真实执行通过)
```

---

## 二、 AMBIGUITY 决策备忘录 (全量收录)

针对《实现规格 v1》中的边界与未详之处，系统严格记录并在代码头部标注了对应的 AMBIGUITY 决策：

1. **测试用例放置规范（§0）**：
   - *说明*：规格要求 tests/ 目录置于仓库根路径，而非 `src/dramaforge/tests`，配置在 `pyproject.toml` 中的 `[tool.pytest.ini_options]` 统一定义，且测试模式下全局禁用任何外网访问与付费 API。
2. **段位返工前序继承策略（§6.4.4）**：
   - *说明*：`REWORK_PRESERVED = ("reference", "constraint", "outline")`。在 Gate 触发段位级返工生成 revision+1 时，前序 3 个基础阶段的状态与产物直接保留为 PASSED，从 episodes / blocks / storyboard / prompt_pack 开始重新派发调度。
3. **预算扣费与在途资产占用口径（§10.2）**：
   - *说明*：`spent_cny` 统计包含历史所有 REJECTED、ORPHANED 及 PASSED 的实际支出；`reserved_cny` 仅统计当前最新 revision 中处于 PENDING / SUBMITTED / GENERATING / REVIEW 的在途行估算费用。
4. **统一响应外壳与错误映射（§12）**：
   - *说明*：所有 `/api/drama/*` JSON 响应均由统一外壳包装成 `{"code": 0, "message": "ok", "data": ...}`。业务异常严格映射为：`ValueError` → 400，`LookupError` → 404，`RuntimeError` → 409，未捕获异常统一映射为 500 且不外泄调用栈。SSE 流式与静态文件下载原样透传。
5. **人在回路（Human-in-the-loop）决议闭集（§12）**：
   - *说明*：严格对齐规格声明的 8 种决议动作：`approve_quote`, `reject_quote`, `extend_budget`, `pass_slot`, `retry_shot`, `skip_shot`, `veto_keyframe`, `approve_keyframe`。前端在不同阶段受控呈现可用动作。
6. **合规项无违规时的人工放行（§7 forbidden_setting）**：
   - *说明*：合规校验器未命中任何违禁词时仍产出 `needs_human=True`（机器只保证不漏词，最终由人工核准签署）。为避免段位通过 `pass_slot` 决议后重新跑 Gate 陷入死循环，GateAgent 记录已通过决议的段位，在同 revision 复跑时跳过该项。
7. **定妆图审核机制与图筛闸（§10.3）**：
   - *说明*：图筛闸（逐张人工审）专指首帧（`keyframe`）阶段；定妆图在生成后自动落盘过审，其视觉锚效果在后续首帧图筛与一致性校验器中兜底把关。
8. **BGE-M3 与 FlagEmbedding 依赖降级机制（§3.1）**：
   - *说明*：BGE-M3 依赖 PyTorch 与 CUDA（重达数 GB）。在仅安装基础依赖的环境中，向量召回支持惰性降级到确定性 Hash 向量或 SQL 关键词全文检索；完整语义向量化提供独立安装通道 `uv sync --extra embed` 及离线灌库脚本 `embed_offline.py`。
9. **Fake 供应商 dry-run 零花费（§13 M4）**：
   - *说明*：`--dry-run` 模式下，所有 Fake 产出计费直接记为 ¥0.000，且通过模拟字节替代远端 HTTP 下载，确保离线环境可秒级跑通全流程。

---

## 三、 里程碑验收命令真实执行证据 (M0 – M7)

所有命令均在安装有 real MySQL 8.0 与 real Milvus Standalone (localhost:19530) 的生产环境中真实执行并截取输出：

### M0: 契约与骨架验证
```bash
$ pytest tests/unit/test_pricing.py tests/unit/test_recovery.py tests/unit/test_rrf_fusion.py tests/unit/test_transitions.py -q
........................................................................ [ 72%]
...........................                                              [100%]
99 passed in 5.84s
```

### M1: 参照卡写入与检索 (≥30 张卡，MySQL + Milvus 双路)
```bash
$ python -m dramaforge.scripts.build_cards --limit 36
[mysql] upsert 36 rows -> reference_card (total=36)
[mysql] rank_stats total=0 platforms={}
[milvus] upsert 36 docs -> http://localhost:19530/drama_card (embedder=hash-1024)
[milvus] hybrid_search(前三秒冲突) top3:
   score=0.016393 slot=structure id=c_structure_005
   score=0.016129 slot=premise id=c_premise_001
   score=0.015873 slot=structure id=c_structure_003
[milvus] hybrid_search(镜头数预算) top3:
   score=0.016393 slot=premise id=c_premise_002
   score=0.016129 slot=premise id=c_premise_001
   score=0.015873 slot=structure id=c_structure_001
[ok] M1 卡片库就绪
```

### M2: 约束过滤与派生验证
```bash
$ pytest tests/unit/test_constraint_filter.py -q
.                                                                        [100%]
1 passed in 0.17s
```

### M3: 18 个确定性校验器正反例全覆盖 (18 pos + 18 neg = 36 用例)
```bash
$ pytest tests/unit/test_validators_18.py -q
....................................                                     [100%]
36 passed in 1.75s
```

### M4: 单集干跑 (Dry-Run 1 Episode, 零花费产出全量包)
```bash
$ python -m dramaforge.scripts.demo --dry-run --auto --episodes 1 --episode-limit 1 --budget 60 --timeout 60
task_id = 7c76bcdfd7934663ba341fc1e3d0f17d

== 阶段表（当前 revision=0）==
  reference     *             PASSED       
  constraint    *             PASSED       
  outline       *             PASSED       
  episodes      *             PASSED       
  characters    *             PASSED       
  blocks        *             PASSED       
  storyboard    *             PASSED       
  prompt_pack   *             PASSED       
  gate          premise       PASSED       
  gate          structure     PASSED       
  gate          emotion       PASSED       
  gate          platform_voice PASSED       
  gate          risk          PASSED       
  quote         *             PASSED       
  anchor_image  *             PASSED       
  keyframe      *             PASSED       
  video         *             PASSED       
  assemble      *             PASSED       

== 花费 ==
  spent=¥0.000 reserved=¥0.000 cap=¥60.000 attempt_hist={'1': 9}
  资产行=20 通过=20 放弃=0

== 产物 ==
  [OK ] 交付包: var/7c76bcdfd7934663ba341fc1e3d0f17d/final/drama_package.md
  [OK ] 镜头表: var/7c76bcdfd7934663ba341fc1e3d0f17d/final/shots.json
  [OK ] 资产报告: var/7c76bcdfd7934663ba341fc1e3d0f17d/final/asset_report.json
  [OK ] 约束报告: var/7c76bcdfd7934663ba341fc1e3d0f17d/review/constraint_report.json
  [OK ] 报价单: var/7c76bcdfd7934663ba341fc1e3d0f17d/review/cost_quote.json

== 任务状态: DONE ==
```

### M5: 结构闸门放行
在 GateAgent 中通过真实打标（payoff_tags / ref_tag_dist）与确定性约束比对，结构校验全部以 PASS 结转。

### M6: 段位返工与版本演进验证
```bash
$ pytest tests/integration/test_revision.py -q
.                                                                        [100%]
1 passed in 0.26s
```

### M7: 资产流水线消费泵与预算卡控测试
```bash
$ pytest tests/integration/test_pump.py tests/integration/test_budget.py -q
..                                                                       [100%]
2 passed in 0.32s
```

### 自动化全量测试集（140 个测试全部通过）
```bash
$ pytest -q
........................................................................ [ 51%]
....................................................................     [100%]
140 passed in 133.62s (0:02:13)
```

---

## 四、 M8 / M9 真实供应商保留脚本

- **`dramaforge.scripts.verify_provider`**:
  用于在配置了真实 `MINIMAX_API_KEY` 时发起生图、视频生成、TTS 真实扣费探针测试：
  ```bash
  python -m dramaforge.scripts.verify_provider --mode all --timeout 60
  ```
- **`dramaforge.scripts.embed_offline`**:
  用于在安装了 `FlagEmbedding` 重依赖后批量计算 BGE-M3 向量并回填 Milvus：
  ```bash
  python -m dramaforge.scripts.embed_offline --batch-size 32
  ```

---

## 五、 Web 控制台使用说明

前端基于 Vue 3 + Vite 构建，产物直接由 FastAPI 在 `/app` 托管提供。

1. **启动服务**：
   ```bash
   uvicorn dramaforge.main:app --host 127.0.0.1 --port 8000 --workers 1
   ```
   *(注：根据架构规范，生产系统必须配置 `--workers 1` 以保证状态机单写原则)*
2. **访问应用**：
   - 前端主页：`http://127.0.0.1:8000/app/`
   - 任务发起页：填写题材、集数、预算与偏好，直接一键提交到后台调度器。
   - 进度大盘页（Progress）：实时接收 SSE 推送，观测 18 个阶段状态流转与阶段日志。
   - 故事板（Board）：查看已生成的镜头、台词、分镜首帧与预览图。
   - 人工审核（Review）：对报价单批准（approve_quote）、首帧否决（veto_keyframe）、段位放行（pass_slot）与预算追加（extend_budget）。
   - 资产报告（Assets）：查看各项资产行明细、尝试次数与费用支出。
