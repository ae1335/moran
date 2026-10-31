# 验证报告

> 生成时间：2026-10-31 · 环境：Ubuntu 24.04 (WSL) / Python 3.12.3 / Node 22.23.4

## 一句话结论

**单测 453 条通过 / 1 条跳过（pytest 收集 454 项，含 6 条参数化展开）；前端构建通过；15 个页面在真实 Chromium 里零控制台错误；PWA 离线队列 35 项 + 定时任务实跑 15 项端到端全通；8 章流水线跑通并导出。**

## 复现命令

```bash
bash scripts/make-demo.sh ws        # 重建离线 demo（8 章 + 导出）
bash scripts/verify-all.sh 8793     # 全量验证，末尾打印 RESULT=PASS|FAIL
```

## 1. 单元测试

```
$ PYTHONPATH=vendor/pylib:. python3 -m pytest tests/ -q
453 passed, 1 skipped in 354.58s
```

| 文件 | 覆盖 |
|---|---|
| `tests/test_agents.py` (60) | Planner 章号修正、Writer 版本留存、Reviewer 证据校验、Curator 白名单+结构补齐、拆书统计口径、伏笔状态流转 |
| `tests/test_frontend.py` (15) | 前端契约：服务端回传的告警必须被消费（#105/#122/#123）、反 AI 味润色的 Web 入口、导出成功分支的未处理问题告警、PWA 首屏输入绑定必须早于第一个 await（#124）、打标失败必须有前端出口（#126）、向量缺失必须有出口（#127） |
| `tests/test_core.py` (60) | 路径逃逸、原子写、提案深合并、伏笔状态机、时间线校验、中文计字、git（含打标失败不说谎 #126）、导出标题去重、存稿口径、古历日期 |
| `tests/test_retrieval.py` (46) | 分块稳定性、FTS5 中文检索、RRF 融合、embedding 降级链（含失败不说谎 #127）、GraphRAG、向量缺失进对账、只读读路径不制造 WAL 旁文件（#128） |
| `tests/test_model.py` (41) | 路由解析、回环强制、熔断、脱敏重试、JSON 抽取、token 估算、mock 文笔质量、章名唯一性 |
| `tests/test_api.py` (71) | REST 全链路、SSE 流式、路径逃逸拦截、未知 API 返回 404 JSON、导出预检（#123 真扫套语）、/polish、起名、静态资源 |
| `tests/test_platform.py` (28) | 导出预检/红线/钩子、反 AI 味润色（#123 预检真扫套语）、日更面板、统计曲线 |
| `tests/test_context.py` (24) | 6 层装配、L0/L1 空则拒生成、预算裁剪顺序、`@路径` 解析、检索降级不阻断、few-shot 片段注入 |
| `tests/test_pwa_inspiration.py` (5) | PWA 挂载、定时任务默认关闭、灵感入库 |
| `tests/test_fix_suggest.py` (17) | 套语替换/POV 删句/复读删句/diff 可读性/不落盘 |
| `tests/test_naming.py` (16) | 起名四类、seed 可复现、长度约束、去重、词池落盘与读取 |
| `tests/test_commercial_flow.py` (16) | published 状态迁移/存稿排除、DOCX ZIP/XML、反馈提案 ID/幂等/Planner 避雷注入、REST 确认、跳章发布的连载断层检测 |
| `tests/test_derivative.py` (23) | 派生内容（简纲/细纲/人物卡/关系图/地图/时间轴/大纲/灵感）生成与落盘 |
| `tests/test_scheduler.py` (21) | 定时任务三 job、同秒备份不撞名、失败留日志、真实 cron 触发、服务能带 scheduler 启动 |
| `tests/test_architecture.py` (5) | AST 全量扫分层依赖方向（README:79 铁律），含「扫描真的看见文件」自检 |

> 口径：括号内是**文件里 `test_*` 函数条数**（静态可数，由 `tests/test_architecture.py` 用 AST 反向盯着）；
> `pytest` 的收集数会多 3 项，那是 `parametrize` 展开出来的重复用例。

## 2. 前端构建

```
$ cd web && npx vite build
✓ 55 modules transformed.
dist/assets/index-*.css   34.06 kB │ gzip:  6.47 kB
dist/assets/index-*.js   223.63 kB │ gzip: 76.90 kB
✓ built in 933ms
```

## 3. 端到端流水线

```
$ python3 -m server.cli --root ws pipeline --chapters 1-8
[1/6] Planner 生成 8 章章纲…
[2/6] 确认章纲…
[3/6] Writer 生成正文…   ch-0001 2112 字 … ch-0008 2118 字
[4/6] Reviewer 审稿…     每章 1–2 个问题（全部带正文证据）
[5/6] Curator 产出提案…  每章 2–4 条
[6/6] 确认落盘 + git 提交…
→ 16 次 git 提交，8 个 tag（ch/0001…ch/0008）
```

产物核对：

| 检查项 | 结果 |
|---|---|
| 章文件 | 8 个 `chapters/ch-000N.md` ✅ |
| 章纲 | 8 个 `outline/chapters/ch-000N.json` ✅ |
| 伏笔池 | 4 条，状态 `resolved/hinted` 各 2 ✅ |
| 时间线 | 8 条，`validate_timeline` 0 问题 ✅ |
| 章摘要 | 8 个 `memory/ch-000N.summary.md` ✅ |
| 实体流 | `memory/entity-deltas.jsonl` 8 行 ✅ |
| 导出 | `exports/2026-10-28/凡人问道_tomato_2026-10-28.txt` + 报告 ✅ |
| 导出预检 | 8 章全 `ok`，`high=0` ✅ |
| 检索索引 | 5 章 → 20 chunk → `mode=bm25` ✅ |

## 4. REST API（真实 HTTP）

| 端点 | 状态 |
|---|---|
| `GET /api/health` | 200 |
| `GET /api/shelf` | 200 |
| `GET /api/books/{b}/chapters` | 200 |
| `GET /api/books/{b}/outline/1` | 200 |
| `GET /api/books/{b}/foreshadow` | 200（4 条） |
| `GET /api/books/{b}/timeline` | 200（8 条 / 0 issue） |
| `POST /api/books/{b}/assemble` | 200（L0 392 + L1 246 + L5 118 = 756 tok） |
| `GET /api/books/{b}/export/precheck` | 200（ready=true） |
| `POST /api/books/{b}/export` | 200（2 文件 / 3695 字） |
| `POST /api/books/{b}/feedback/stats` | 200 |
| `POST /api/books/{b}/feedback/reviews/4` | 200（5 条 / sentiment −0.2） |
| `POST /api/books/{b}/feedback/recheck` | 200（找到 ch4/ch3 + 归因） |
| `GET /api/providers/health?probe=true` | 200（mock ok） |
| `GET /api/usage?days=1` | 200（25 次调用 / 118665 tokens） |
| `GET /api/definitely-not-a-route` | **404 JSON（不是 HTML）** |

## 5. 浏览器 UI

真实 Chromium（Playwright）逐页打开，收集 `console.error` 与 `pageerror`：

```
---- 汇总 ----
页面数: 14   有错误的页面: 0
```

| 页面 | 结果 | 截图 |
|---|---|---|
| 书架 | 无错误 | `docs/screenshots/01-shelf.png` |
| 写正文 | 无错误 | `02-write.png` |
| 审稿 | 无错误 | `03-review.png` |
| 沉淀 | 无错误 | `04-curate.png` |
| 章纲 | 无错误 | `05-outline.png` |
| 伏笔 | 无错误 | `06-foreshadow.png` |
| 日更 | 无错误 | `07-daily.png` |
| 真相源 | 无错误 | `08-truth.png` |
| 拆书 | 无错误 | `09-benchmark.png` |
| 反馈 | 无错误 | `10-feedback.png` |
| 用量 | 无错误 | `11-usage.png` |
| 模型路由 | 无错误 | `12-providers.png` |
| 404 | 无错误 | `13-notfound.png` |
| 起名 | 无错误 | `14-names.png` |
| 伏笔·时间线 | 无错误 | `06-foreshadow-timeline.png` |

附带交互验证：点「导出预检」按钮 → 面板渲染 8 行、8 个「达标」、0 控制台错误（`scripts/click-precheck.mjs`）。

## 6. 开发过程中修复的真实缺陷

这些不是「为了好看」的改动，每一条都是实测复现后修的，并补了回归测试。

| # | 缺陷 | 根因 | 修复 | 测试 |
|---|---|---|---|---|
| 1 | SPA catch-all 吞掉 `/api/*` 与 `/pwa/*` | 路由注册顺序：fallback 在 API 之前 | 重排顺序 + fallback 显式拒绝 `api/` 前缀 | `test_api.py::test_unknown_api_path_returns_404_json_not_html` |
| 2 | 审稿把「禁用词清单」当正文命中 | 模型看整个 prompt，文风指纹里的「忌：眼中闪过一丝」被当成正文 | `LITERAL_EVIDENCE_CATEGORIES` + `_verify_evidence`：evidence 必须是正文子串 | `test_agents.py::test_verify_evidence_filters_hallucinations` |
| 3 | 正文整句重复（「指节泛白」一章 20 次） | mock 模板池太小 + 简单取模 | 洗牌袋 + 双池组合 + 扩池 | `test_model.py::test_mock_prose_has_no_massive_sentence_repetition` |
| 4 | `pipeline --chapters 1-4` 只出 3 章 | prompt 是 `生成 **4 章**`，mock 正则 `生成\s*(\d+)\s*章` 被 `**` 挡住 | 正则容忍 Markdown 强调符 | `test_model.py::test_mock_planner_*` |
| 5 | 伏笔池永远为空 | mock planner 不产 `foreshadow_actions`；curator 规则兜底只在模型全失败时触发 | planner 按节奏产动作 + `_ensure_structural` 从章纲补齐 | `test_agents.py::test_curator_backfills_foreshadow_when_model_omits_it` |
| 6 | 时间线抽出「着青云历」「得玄历」 | 纪年名正则宽取 6 字，把前面的动词吞了 | `_trim_era` 取末尾 2 字 + 停用字左削 | `test_agents.py::test_extract_date_does_not_swallow_verbs` |
| 7 | 「廿三」这类日期解析失败 | `cn_num` 不支持 廿/卅；月份无 正/冬/腊 别名 | 补齐古历写法 | `test_core.py::test_cn_num_supports_archaic_day_forms` |
| 8 | 定稿 8 章仍报「存稿 0 天 · 断更预警」 | `stock` 只算 draft+reviewed，没算 final | final 计入存稿 + 加 `stock_breakdown` | `test_core.py::test_daily_panel_counts_final_as_stock` |
| 9 | 「774 处 8-gram 重复」误导人 | 统计的是去重 8-gram 集合大小，短碰撞与整段照搬混为一谈 | 改为连续共同片段（≥30 字）检测 | `test_agents.py::test_shared_runs_ignores_scattered_short_collisions` |
| 10 | mock 正文只有 1845 字，预检每章都失败 | 生成循环提前 200 字收手 | 写满 target | `test_model.py::test_mock_prose_meets_declared_word_target` |
| 11 | 导出文件章标题连写两遍 | 正文 md 自带 `# 第N章 标题`，导出又拼一次 | `_strip_leading_chapter_title` | `test_core.py::test_export_strips_duplicate_chapter_title` |
| 12 | `pacing.md` 写 2200 字、`structure.json` 写 366 字 | 模型返回的 `avg_words` 覆盖了启发式实测值 | 统计口径字段只信启发式 | `test_agents.py::test_deconstructor_model_cannot_override_measured_stats` |
| 13 | 拆书卷纲写「1 卷 60 章」，实际只有 10 章 | 同上，模型覆盖了 `volumes` | `volumes` 加入不可覆盖列表 | `test_agents.py::test_deconstructor_volume_structure_reflects_real_chapter_count` |
| 14 | 浏览器 404（favicon 缺失） | index.html 没引 favicon | 加 `favicon.svg` + manifest icon | `test_api.py::test_favicon_served` |
| 15 | 真相源/拆书页首次进入是空白 | 没有自动选中第一个文件 | 挂载后自动打开 | UI 冒烟 + 截图 |
| 16 | 导出预检没暴露给用户 | 有 `precheck()` 方法但没有 REST 端点，前端也没调 | 加 `GET /export/precheck` + 日更页「导出预检」按钮 | `test_api.py::test_export_precheck_endpoint` |
| 17 | 选中的筛选按钮文字消失 | `button.ghost` 排在 `button.primary` 之后，把背景覆盖成透明，深色文字压在深色底上 | CSS 顺序调整 + `button.ghost.primary` 显式规则 | 浏览器实测 `bg=rgb(201,162,39)` |
| 18 | 起名能生成四字怪名「穆北辰远」 | 双字名从含双字词的池里追加，拼成 4 字 | 双字池只留单字 | `test_naming.py::test_character_names_are_two_or_three_chars` |
| 19 | 起名用字池内有重复字（「岭」×3） | 手写池时重复录入 | 去重并加断言 | `test_naming.py::test_pools_have_no_duplicate_characters` |
| 20 | `.page-head` / `.page` 在 6 个视图各写一遍 | 复制粘贴 | 提到 `global.css`，各视图只留 `max-width` | 浏览器实测卡片间距 12px |
| 21 | 章 final 后索引不更新 | §8.5 要求增量更新，但 `confirm()` 里没接 | 加 `_update_index`，失败降级不阻断 | `test_agents.py::test_confirm_updates_index_after_final` |
| 22 | 审稿 category 从不沉淀 | §9.3 要求「反复出现 → 避雷条目」，代码里没有 | 加 `_distill_avoid_entries`（次数+占比+仅 high 三重门槛） | `test_agents.py::test_repeated_review_category_becomes_avoid_entry` |
| 23 | 避雷条目被 mid 级模板重复污染 | 阈值只看次数，且不限 severity，4 章书里立刻触发 | 提到 5 次 + 占比 ≥40% + 只认 high | `test_agents.py::test_mid_severity_never_becomes_avoid_entry` |
| 24 | mock 主角名每章都在变（楚澈→沈陌→林轩） | `hero` 用章级 seed 重抽，一本书读起来像短篇集 | 优先读角色卡名，其次书级常量 | `test_model.py::test_mock_hero_name_is_stable_across_chapters` |
| 25 | 章纲写「黑水渡口」，正文写矿洞 | mock 从不解析 prompt 里的「地点：」 | `_place_kind` + 分地点场景/体感池 | `test_model.py::test_mock_prose_uses_scene_location_from_outline` |
| 26 | 「山门」章节出现矿道专属描写 | `SENSES` 混了矿洞专用句 | 拆成通用 `SENSES` + `SENSES_BY_PLACE` | `test_model.py::test_mock_scenery_does_not_leak_across_places` |
| 27 | 一章塞 12 处日期句、只有 4 个不同日期 | 7 拍循环里每轮都放日期 | 整章只留一处时间锚点 | `test_model.py::test_mock_prose_has_single_date_anchor` |
| 28 | 相邻章整段重复（最长 67 字） | 节拍固定 i%7 + 池子只用洗牌不轮换 | 按章轮换节拍 + 池子按章旋转 + 第三成分 | `test_model.py::test_mock_prose_has_no_massive_sentence_repetition` |
| 29 | 审稿「采纳」不产生任何预览 | §13.2 要求 diff 预览，但前端只往 Set 里记一笔 | 新增 `FixSuggester` + REST + 审稿台 diff 卡片（-红/+绿） | `test_fix_suggest.py`(18) |
| 30 | diff 定位失败：evidence 带前句句末标点 | `text.find(evidence)` 直接匹配，`。林澈…` vs `\n\n林澈…` | 三级放宽：原样 → 去首尾标点 → 去空白并回映射 | `test_fix_suggest.py::test_evidence_with_leading_punctuation_still_locates` |
| 31 | 复读改写误删场景句 | 删「最长句」而非「evidence 指向的那句」 | 优先删 evidence 所在句 | `test_fix_suggest.py::test_repetition_deletes_evidence_sentence_not_longest` |
| 32 | 结构类问题报「evidence 找不到」 | 先定位再判类别，words/hook/title 本来就没有字面 evidence | 先判类别，给可操作的原因文案 | `test_fix_suggest.py::test_structural_issue_reason_is_meaningful` |
| 33 | 伏笔页没有时间线视图 | §13.2 要求「状态机总览 + 超期报警 + 时间线视图」，只做了前两个 | 加 tab + 时间轴（节点/日期/间隔天数） | `test_api.py::test_timeline_entries_have_days_since_start` |
| 34 | 时间线接口不返回 days_since_start | 前端算不出间隔 | 接口补该字段（首条 0，按虚构历法算天数） | 同上 |

## 7. 未验证 / 已知限制

如实声明，不掩饰：

1. **真实模型未联网验证**。所有端到端都用 `mock-local`。`openai_compat` provider 只测了 URL/payload/header 构造与错误分类，没有真实打通过 workbuddy/qoder/trae 桥或 DeepSeek 官方 API。

   **已离线证伪的部分（降级/红线路径，不依赖网络）**：
   - write 链终端降级：链尾非 terminal 时自动补 mock-local（`server/model/gateway.py:329-346 _resolve_hops`），由 `test_write_chain_appends_terminal_fallback` 覆盖四组对照（全廉价桥→硬补 / free 有 terminal→用配置 / 末位已 terminal→不补 / 非 write 任务→不补）。**此前零测试**，链尾被悄悄换成廉价模型会让成章质量静默下降且无人知晓。
   - provider 不可用/熔断/超时：走 `gateway.py:163-193` 的错误分类降级（ContentFiltered / RateLimited / BadRequest / BridgeDown / ModelError）。
   **仍未验证**：真实 API 的通联与计费，必须联网且有凭据。
2. **embedding / reranker 未验证**。没有配置真实 bge-m3 服务，检索始终跑 `mode=bm25`；`OpenAICompatEmbedder` 未联网。

   **已离线证伪的部分（降级路径）**：`get_embedder` 取不到配置时返回 None → `facade.mode` 自动降为 `bm25`、`degraded=True`（`server/retrieval/facade.py:149-154`）；`HybridSearch` 任何异常都收敛成 `mode="off"` 不抛（`facade.py:246/270` 铁律）。已由 `tests/test_retrieval.py` 覆盖（`get_embedder(None/{}) is None`、`mode == "bm25"`、异常→`degraded`）。**未验证**：真实向量召回与 rerank 的质量增益。
3. **多用户/鉴权未实现**。设计上单机单用户，只应绑 `127.0.0.1`。已由 `test_serve_defaults_to_loopback_only` 锁住默认值（此前没有任何测试看着它，改了 default 测试仍全绿——声明与实现分离就是隐患）。
4. ~~**PWA 离线队列未做真机验证**~~ —— **已补齐**，见 §11.1。真机 Chromium 断网→记录→联网同步全链路已跑通；过程中还挖出两个真实缺陷（首屏掉线状态灯卡在「在线」、favicon 404 + manifest 无图标）。
5. ~~**定时任务未实跑**~~ —— **已补齐**，见 §11.2。真起服务等过一次真实 cron 触发；此前「未实跑」不只是缺测试，而是开着就崩：`enabled: true` 时服务直接起不来（缺陷 #45）。
6. **跨章复读只做文本级判定**。做了语义级同义改写的复读检不出。**已用探针证实**：两章语义一致、逐句换词换序时，`AIPolish.repetition_check`（跨章 10-gram）与 `Reviewer._rule_repetition`（≥30 字连续片段）均 0 命中，而字面照抄对照组稳定命中——即限制属实，不是文档凭空写的。该限制已固化为 `test_polish_paraphrase_not_detected__known_limit`（断言「当前检不出」），将来真做了语义检测它会变红，提醒回来改本条。
7. **虚构历法按 360 天/年近似**，不解析真实农历。定位：`server/core/domain.py:233 date_to_days`（`y*360 + (mo-1)*30 + (dy-1)`，纯算术、无月份长度表，所以连「13 月」都照样接）。古历写法（腊月/正月、廿三/卅）是**字面映射**不是农历推算，且缺年号时直接返回 None。已由 `test_fiction_calendar_is_360_day_approximation` 钉死（月差恒 30、年差恒 360、13 月可解析、腊月廿三→(1024,12,23)、无年号→None）。只用于时间线单调/间隔的相对校验，别拿它算真实日期差。
8. **前端图表是手写 CSS**，没有交互缩放/提示浮层。**已用浏览器断言证实**（`scripts/ui-smoke.mjs` 末段「§7 第 8 条：图表形态」）：柱体由 `:style` 算高度、无 canvas/svg/legend/tooltip 节点 → 确实无缩放、无自绘浮层。⚠️ 更正：柱体**有原生 `:title` 提示**（第 N 章：N 字），此前文档写的「没有提示浮层」表述不准——自绘浮层没有，原生 tooltip 是有的。四项断言随 `verify-all` 第 5 步跑，引入图表库会变红。
9. **封面图像 API 未实现适配器**（降级路径已证）。`CoverGenerator` 预留了 `image_api` 参数与「请求了但没适配器就回退 SVG 并告警」的路径，但没有对接任何真实生图服务（DALL·E / SD / 即梦等），也未联网验证。

   **已离线证伪的部分**：`CoverGenerator.generate(use_image_api=True)` 在无适配器时**不报错**，回退程序化 SVG 并写 warning「已请求图像 API，但当前未配置可用的图像 API 适配器，回退为程序化 SVG」（`server/platform/cover.py:182-190`），由 `test_cover_image_api_falls_back_with_warning` 覆盖。**未验证**：真实生图服务的出图与计费。

## 8. M8 衍生层补充（本轮新增）

三项 M8 规格缺口已补齐，均有单测与真实 HTTP 验证：

| 能力 | 实现 | 验证 |
|---|---|---|
| 题材模板（§10.2 共享模板） | `server/core/templates.py` + `create_novel(template=…)` | 拆书产物自动进共享库；建书自动套用开局/金手指/升级/爽点基线；Planner 注入模板 brief。6 项单测 |
| 设定集导出 | `server/platform/setting_book.py`（11 节） | `POST /api/books/{b}/setting-book` → `exports/{date}/setting-book-*.md` + `.json`；标题层级降级正确。8 项单测 |
| 封面生成 | `server/platform/cover.py`（程序化 SVG） | 600×800 真实渲染截图 `docs/screenshots/16-cover.png`；书名含 `&`/`<` 时 XML 转义正确。6 项单测 |

新增缺陷（编号续前表）：

| # | 缺陷 | 根因 | 修复 | 测试 |
|---|---|---|---|---|
| 35 | 设定集把 `# 宪法` 原样嵌入，打断 `## 分节` 层级 | 直接拼接外部 Markdown | `_demote()` 整体降 2 级，且跳过 ``` 围栏内内容 | `test_setting_book_heading_hierarchy`、`test_demote_skips_fenced_code` |
| 36 | `book.target.words_total` 不存在，设定集直接崩 | 凭印象写了字段名，未核对 `BookTarget` | 改用实际的 `total_chapters` / `daily_chapters` / `words_per_chapter` | `test_setting_book_dry_run_has_all_sections` |
| 37 | 设定集把 `book.protagonist` 当成 dict 取 `.get()` | 该字段实际是**角色卡路径字符串**（`characters/mc.yaml`） | 走 `get_character()` 读卡，退回按 `role=="protagonist"` 查找 | `test_setting_book_includes_characters` |
| 38 | 作品集页一加载就写封面文件 | `onMounted` 里调了 `coverGenerate` | 改用只读的 `GET /covers`，写文件必须作者点按钮 | UI smoke 无副作用 |

## 9. M8 多书并行与共享模板端到端验证

| 验证项 | 结果 | 证据 |
|---|---|---|
| 两本书并行建书 | 通过 | `书甲`/`书乙` 各自生成 1 章，书架两条独立记录 |
| 两书正文独立 | 通过 | 正文内容不同（2088 vs 2083 字），各自伏笔池独立（各 2 条） |
| 各自走完 plan→write→review→curate→confirm | 通过 | 两书状态均推至 `final` |
| 拆书产物自动进共享模板库 | 通过 | `对标书A` 拆出 8 章 → 共享库出现同名模板 |
| 新书按题材自动套用模板 | 通过 | `套模板新书` 总纲含「题材模板基线：危机型 / 8 章」 |
| Planner 注入模板基线 | 通过 | `_template_brief()` 返回结构化节奏参数 |

**M8 验收标准全部满足**：两本书并行 ✅ 共享模板 ✅ PWA 录入灵感 ✅（此前已验证）定时夜审 ✅（配置默认关闭，需手动开启）

## 10. §11–§12 商业化与反馈闭环补验

| 验证项 | 实测结果 |
|---|---|
| 手动发布标记 | `POST /publish {chapters:[1]}` → final 8→7、published 0→1、存稿 4.0→3.5 天；撤销后恢复 |
| 非法发布迁移 | draft/drafted 不能直接 published；REST 返回 409 |
| 已发布章覆盖保护 | 四条落盘路径口径一致：`write()` / `write_stream()`（Writer 自带）、`polish(apply=True)` 与 `PUT /chapters/{ch}`（#75）、`adopt_version`（#76）均拒绝 published，前端保存按钮置灰，需先撤销标记 |
| 起点 DOCX | 真实导出 `凡人问道_qidian_2026-10-28.docx`；`zipfile.testzip()` 为 None，document.xml 含章题与正文，XML 特殊字符转义 |
| 反馈预览不落盘 | 即使旧客户端传 `apply:true`，响应仍 `applied:0` + `confirmation_required:true` |
| Reviewer 定向重审 | 预览 `review_source=fresh`；不是只读旧 review.json |
| 预览→确认提案 ID | 同一 ID `8b97e9ffdc07` 首次确认 `applied:1`，重复确认 `applied:0, skipped_duplicates:1` |
| Planner 避雷携带 | 单独提取「本章避雷」区，不受文风全文 1200 字截断影响 |

新增缺陷：

| # | 缺陷 | 根因 | 修复 | 测试 |
|---|---|---|---|---|
| 39 | 日更面板没有 published，final 被当作已发布 | 状态机缺发布态 | final ↔ published 手动迁移；published 排除存稿；Writer 禁止覆盖 | `test_published_transition_is_manual_and_reversible` 等 4 项 |
| 40 | 反馈复盘一键直接写真相源，且重复执行会重复追加 | 无确认步骤与幂等判断 | 预览 + accepted_ids 确认；条目级去重 | `test_feedback_apply_is_idempotent` |
| 41 | 预览用 fresh Reviewer，确认却用历史审稿，提案 ID 会失配 | 两侧证据来源不同 | `_fresh_feedback_recheck` 统一重算；`Reviewer.review()` 的返回值取 `.report` | 真实 HTTP 首确认 1、重复确认 0 |
| 42 | Planner 可能截掉反馈避雷区 | 整个文风文件统一截 1200 字 | 单独提取避雷区，最近 12 条强制注入 | `test_planner_extracts_avoidance_section_after_long_style` |
| 43 | 日更面板有节奏计算但无对比展示 | 只暴露 `/stats`，未并入面板 | `daily-panel.pacing` + 前端对比卡 | `test_daily_panel` 及 UI smoke |
| 44 | 起点 DOCX 规格未实现 | 只写 TXT | 标准库生成最小 WordprocessingML ZIP；仍走同一预检 | DOCX 3 项 |

## 11. 端到端补齐（PWA 离线队列 / 夜审 / 索引漂移 / 定时任务实跑）

§7 里第 4、5 条自认的缺口本轮补齐。补齐过程中**又挖出 4 个真实缺陷**——说明"没实跑过"从来不是"应该没问题"，而是"问题还藏在里面"。

两个入口，都已进 `scripts/verify-all.sh`，跑一次就是全套证据：

```bash
node scripts/pwa-smoke.mjs        # 35 项：索引漂移 / 夜审 / PWA 离线队列
bash scripts/scheduler-smoke.sh   # 15 项：真实 cron 触发
bash scripts/verify-all.sh        # 7 步全量，末行 RESULT=PASS|FAIL
```

两条脚本都跑在 `.runtime-test/` 下的 ws 副本上，结束时校验真实书库 git HEAD 与工作区逐字未变。**这一条是硬约束**：被测服务自己会调 `GitBot.commit_all`，直连真库会在书目录里造出 commit（首版脚本就踩了这个坑，已回滚）。

### 11.1 PWA 离线队列（§7 第 4 条）

| 验证项 | 结果 | 证据 |
|---|---|---|
| 索引漂移检测 | 通过 | 改 ch-0008 正文 → `/index/verify` 报 `drifted=[8]` → `/index/build` → 归零 |
| 夜审全链路 | 通过 | PUT outline/1 = drafted → POST /jobs/nightly → `reviewed[0].ch == 1` 无 error，内联 `index_verify.ok`，章纲变 reviewed |
| 断网记录灵感 | 通过 | 离线时点保存 → `#pendingCount` = 1，且 `materials/inspiration.md` **确实不存在**（真断网，不是假动作） |
| 联网自动同步 | 通过 | 上线 → 待同步归零 → 文件出现且含 `[伏笔]` → `#synced` 列表可见 |
| 状态灯随网络变化 | 通过 | 离线→「离线」、恢复→「在线」（首屏竞态场景单独断言） |
| 进度面板 | 通过 | 书名 / 第 N 章 / 存稿分档 / 已发布 / 可支撑天数 全部渲染 |
| 无 404、无 JS 报错 | 通过 | `/pwa/favicon.svg` 200、`/favicon.ico` 200、manifest icons 非空、全程 0 条 404 |

截图：`docs/screenshots/18-pwa-offline.png`（断网待同步）、`docs/screenshots/18-pwa.png`（同步后）。

### 11.2 定时任务实跑（§7 第 5 条）

真起 uvicorn、真等一次 cron（`* * * * *`）、真看副作用：

| 验证项 | 结果 | 证据 |
|---|---|---|
| `enabled: true` 时服务能起来 | 通过 | 这正是缺陷 #45；修好后 `/api/jobs/scheduler` 返回 `available:true, enabled:true` |
| 调度器真的 start | 通过 | 日志 `apscheduler.scheduler: Scheduler started` |
| 注册的 job 正确 | 通过 | `["nightly_review","backup"]`（index_rebuild 按要求关着） |
| **真实 cron 触发** | 通过 | 实测第 21 秒触发（另一次 22:10:00 触发），章纲 `drafted → reviewed` |
| 备份真落盘 | 通过 | `backups/20261028-221000/novels/` 0.3 MB |
| 成功也留日志 | 通过 | `定时任务完成：nightly_review` / `定时任务完成：backup` |
| 全程无崩溃 | 通过 | 日志 0 条 Traceback，服务始终响应 |

### 11.3 本轮新增缺陷

| # | 缺陷 | 根因 | 修复 | 测试 |
|---|---|---|---|---|
| 45 | **开启定时任务后服务直接起不来** | `app.add_event_handler` 在 fastapi 0.162 已移除 | 改 `lifespan` asynccontextmanager，构造 FastAPI 前建 scheduler | `test_app_boots_with_scheduler_enabled` |
| 46 | 同一秒内备份两次必失败 | 时间戳只到秒，`copytree` 撞已存在目录 | `_unique_dir()` 重名追加 `-2/-3`；失败清理半成品 | `test_backup_twice_in_same_second`、`test_unique_dir_appends_suffix`、`test_backup_keeps_only_n` |
| 47 | 定时任务失败完全静默 | 定义了 logger 名但全文无任何 logging 调用 | 三个 job 全部补 `logger.exception/warning`；`cli.cmd_serve` 配 `basicConfig` | `test_nightly_review_failure_is_logged_not_raised`、`test_index_rebuild_failure_is_logged`、`test_backup_failure_logged_and_no_leftover` |
| 48 | `load_scheduler_config` 泄露模块级 DEFAULTS | 无配置文件时直接 `return DEFAULTS` | 全路径 `copy.deepcopy` | `test_config_missing_file_returns_isolated_copy`、`test_config_file_overrides_defaults` |
| 49 | PWA 首屏掉线时状态灯卡在「在线」 | `online/offline` 监听在所有 `await` 之后才绑，事件被丢 | 监听移到最前；`openDB` 后补一次 `syncAll()` | `pwa-smoke` 首屏竞态断言 |
| 50 | 每次打开都 404，PWA 无可安装图标 | `/favicon.ico` 只查 `.ico`，仓库只有 `favicon.svg`；manifest icons 为空 | 回退链 `.ico → .svg`；补 `pwa/favicon.svg` 与 manifest icons；SW 版本升 v2 | `pwa-smoke` 三项图标断言 |
| 51 | **验证流程假绿：端口被占用时静默测了别人的服务** | `verify-all.sh` 第 3 步起服务前不查端口；绑定失败只在日志留一行 `Errno 98`，而随后的健康检查落到了**上一轮残留的 serve 进程**上并返回 `ok`（那个进程指向真实书库），于是第 4、5 步全程测错对象仍然 `RESULT=PASS` | 第 3 步前置端口预检（占用即 `RESULT=FAIL` 退出）+ 校验 `/api/health` 回显的 `root` 就是本次的 ws；第 3~5 步前后对真实书库做全文件 md5 校验，任何字节变化立刻判红。`pwa-smoke.mjs`、`scheduler-smoke.sh` 同样加端口预检 + root 断言 | 占位端口复跑 `verify-all.sh` → 第 3 步立即 `RESULT=FAIL`、退出码 1 |
| 52 | **订阅桥缺 `base_url` 时校验被整条跳过，失败推迟到发请求那一刻** | `registry.py` 原判断 `if spec.base_url and spec.is_local_bridge`：空地址让回环校验整个不执行，配置照常加载成功；真正发请求时 URL 退化成相对路径 `/v1/chat/completions`，才炸 `UnsupportedProtocol`（报错词不达意，且发生在写正文那一步） | 改成先判 `spec.is_local_bridge` 再要求 `base_url` 非空，缺地址直接在配置期抛 `ProviderConfigError` 并点名 `base_url`；官方 API（`is_local_bridge=false`）不受影响 | `test_bridge_missing_base_url_rejected_at_config_time` + 对照 `test_official_api_may_omit_base_url` |
| 53 | **提案白名单可被 `..` 绕过（白名单逃逸）** | `curator._allowed()` 只做字符串 `startswith`，不规范化路径：`memory/../evil.md` 通过白名单，而 `store.resolve()` 又把 `..` 折叠 → 实际写到书根的 `evil.md`，**逃出了 `memory/` 白名单目录**（不是逃出书根，但同样越权）。存储层的 `resolve()` 只挡逃出书根，挡不住逃出白名单目录，两层都没守住 | `_allowed()` 先用 `posixpath.normpath` 规范化再比对，并显式拒绝 `.` / `..` / `../` 开头 | `test_proposal_whitelist_rejects_path_traversal`（白名单内放行 + 穿越/擦边/越界全拒）+ `test_proposal_traversal_never_reaches_disk`（端到端兜底：逃出书根必抛 `StoreError`） |
| 54 | **架构铁律无人守护** | README:79 声明「依赖方向单向，`api → agents → context/model → core`，**禁止反向依赖**」，但 tests/ 下零覆盖——纯靠人看代码守。一旦有人让 core 反过来 import agents，分层就塌，且不会有任何测试变红 | 新增 `tests/test_architecture.py`：AST 扫全量 import（含相对导入解析），断言低层不 import 高层；另含 `test_layer_scan_actually_sees_files` 自检，防「扫描路径写错导致零违反」的假绿 | 实测 0 处反向依赖；反向验证：注入 `core → agents` 立即失败并报 `server/core/schema:408`，还原后归绿 |
| 55 | **防抄袭护栏无人守护** | QUICKSTART:152 承诺「范例只用于学句式与节奏，prompt 里明确禁止复用其人物、地名、情节」（实现在 `assembler.py` 系统提示的写作要求一节），但 tests/ 下零命中——这条护栏被顺手改成一句泛泛的「不要抄袭」也不会有任何测试变红，而它防的是把范例的人物/地名/情节直接搬进正文 | 新增 `tests/test_context.py::test_prompt_forbids_reusing_fewshot_content`：断言「不得复用」总述 + 人物/地名/情节三要素逐个点名 + 约束挂在「范例片段」上 + 句式骨架仍在 | 反向验证：把该句弱化为「不要抄袭它」→ 测试立即 FAILED 并打印真实 prompt 片段；还原后归绿 |
| 56 | **路径穿越测试是假绿** | `tests/test_api.py::test_truth_path_traversal_blocked` 原用 `/truth/../../../etc/passwd` 断言 `status in (400,404,422)`，实际拿到 404「未知 API 路径: /api/etc/passwd」——Starlette 在路由匹配前就把 `..` 折叠了，`store.resolve()` 的逃逸守卫（store.py:134）**根本没被调用**；把守卫删掉或短路成 `if False`，这条测试照样过 | 改用百分号编码 `%2e%2e`、`..%2f` 让 `..` 原样送达路由，断言必须是 400 且 detail 含 `escapes novel root`，并覆盖 PUT 写路径 + 断言 `/etc/passwd` 未被改写 | 反向验证：把 store.py:134 守卫短路为 `if False` → 测试立即 FAILED（`assert 404 == 400`）；还原后归绿 |
| 57 | **书目录名可越界（纵深防御缺失）** | `Workbench.get_store` 不校验目录名：`novels/../../outside` 只要里面摆了 book.json 就能被打开。当前 API 层碰不到（Starlette 先折叠路径，编码形式 `%2e%2e` 被 404 挡在路由外），**但守卫依赖调用方框架行为本身就不成立** | `server/core/store.py` 新增 `_assert_plain_dirname()`，守在 `get_store` 入口（`find_store` 的 key 可以是书名，书名含 `/` 合法，两条分支最终都过 get_store）：空/`.`/`..` 或有 `/`、反斜杠 → 抛 StoreError | 反向验证：把守卫短路为 `if False` → `test_store_rejects_book_dirname_traversal` 立即 FAILED（DID NOT RAISE）；还原后归绿 |
| 58 | **章节清单无上限（区间写法可拖死服务）** | `_parse_chapter_list("1-2000000")` 直接展开 200 万章：实测 `1-200000` → 6.5s、`1-2000000` → 70s 才返回 200。单机单用户场景下这就是 DoS，且 `POST /export` 走 `list[int]` 完全绕过它 | `server/api/routes.py`：`_parse_chapter_list` 加 `MAX_CHAPTER_LIST=5000` 硬上限（区间跨度/逗号项数/总数三处），超出直接 400 不静默截断；`POST /export` 单独拦 `len(chapters)`；`/review/{ch}/suggest` 的 indices 是问题索引不是章号，单独设 20 | 反向验证：三道守卫全短路为 `if False` → `test_chapter_list_is_capped` 立即 FAILED（DID NOT RAISE HTTPException）；还原后归绿 |
| 59 | **正文与清单没有资源上界（写章正文 / 灵感速记 / 索引重建总量）** | 切块代价是**超线性**的：单章 200KB→0.9s、500KB→5.5s、1MB→23s，一串大章能在索引重建里卡住几十秒，而重建挂在夜间定时任务里**卡住也没人看见**。同时 `PUT /chapters/{ch}` 的 `text`、`POST /inspiration` 的 `notes`（条数与单条长度）都完全无上限 | `server/retrieval/index.py` 新增 `MAX_INDEX_CHARS=3_000_000` + `IndexTooLargeError`，`_chapter_texts()` 在切块**之前**拦截（走 facade 既有的「失败返 error dict」降级路径，不静默截断正文）；`server/api/routes.py` 新增 `MAX_CHAPTER_CHARS=100_000` / `MAX_INSPIRATION_NOTES=200` / `MAX_NOTE_CHARS=5_000`，复用 `#58` 的 `_cap_text()` | 反向验证：上界压到 1 万字 + 守卫短路 → 2 万字正文**照常建索引**（changed=[1]，守卫被拿掉就失效）；守卫生效时同一份正文必报 `IndexTooLargeError`。写章与灵感三道短路 → `test_chapter_write_and_inspiration_are_capped` FAILED（assert 200 == 400） |
| 60 | **规模放大没有上界（书名长度 / 章号 / 章号路径）** | `safe_title()` 不设长度上限：500 字书名在建目录那刻炸 `OSError(Errno 36) File name too long`，HTTP 层变成一条看不懂的 500 英文。章号同样无上界：`ch_name()` 只格式化不校验，`write_chapter(10**9, ...)` 会写出 `ch-1000000000.md`（章号同时是章纲/审稿/检索的表主键，越界只会产出没人认领的文件，不报错） | `server/core/paths.py` 新增 `MAX_TITLE_CHARS=60`（ext4 单名 255 字节，中文 3 字节/字，目录名还要带 `novel-《》`）与 `MAX_CHAPTER_NO=100_000` + `_assert_chapter_no()`；六个章级路径构造器（chapter_md/outline_json/chapter_versions_dir/summary_md/assembled_md/review_json）全部过守卫；`create_novel` 把 `ValueError` 兜成 `StoreError`（HTTP 409 而不是 500） | 反向验证：书名上界短路 → `test_book_title_length_is_capped` FAILED（DID NOT RAISE ValueError）；章号上界短路 → `test_chapter_number_is_reasonable` FAILED。HTTP 实测：500/300/61 字 → 409 `书名过长：N 字（上限 60 字）`，60 字与正常书名 → 200 |
| 61 | **PROJECT.md §1「6 层上下文总量硬上限 16k」零测试守护** | 文档自称硬上限（`ContextAssembler.budget` 默认 16000，`_trim()` 会裁到预算内，裁不动才告警），但 tests/ 下**没有任何一条测试盯着它**。这是「文档写着、代码也在、但改坏了没人知道」：哪天 `_trim` 被改成静默截断（不写 warning），16k 就只是一句口号 | 不改代码（裁剪与告警的实现本身是对的），改的是「没人看着」：`tests/test_context.py::test_context_budget_is_hard_and_trim_reports` 灌满真相源后断言——预算内则不报、超了必须有含「预算」的 warning；极小预算（budget=1）时 L0/L1 不可裁，必须报「裁剪后仍超预算」且审计溯源里写明「超限」 | 反向验证：把「裁剪后仍超预算」的 `warnings.append` 短路掉 → 测试立即 FAILED（`AssertionError: ['L4: 未配置检索，跳过（降级）']`，test_context.py:396）；还原后归绿 |
| 62 | **真相源写入有两处没走 atomic** | 「文件即真相源」（PROJECT.md §1）的语义是：盘上写着什么就是真相。但 `writer.py` 写 `v{N}.meta.json` 与 `curator.py` 沉淀 `truth/style-fingerprint.md` 用的是裸 `write_text`（`gitbot.py` 的 `.gitignore` 也是）——**写到一半崩掉就留下半截文件，而半截文件会被当成真相读进来** | 三处全改成 `atomic.write_text_atomic`（tmp → replace）；`tests/test_core.py::test_truth_source_writes_are_atomic` 用 AST 式全仓扫描断言 server/ 下不出现裸 `write_text`（豁免 registry/exporter，它们写的是一次性生成物不是真相源，豁免理由写在测试里） | 反向验证 1：把 `writer.py` 的原子写改回裸 `write_text` → 测试立即 FAILED 并精确报 `agents/writer.py:233`；反向验证 2：把豁免名单短路成全放行 → 测试照样通过（证明豁免是真在收口，不是随手开的口子） |
| 63 | **原子写失败会留下 .tmp 垃圾文件** | `atomic.write_text_atomic` 的 `except BaseException: tmp.unlink(...)` 是唯一的清理点，但**没有任何测试盯着它**。哪天有人把那行删掉（看起来只是「清理而已」），每次写失败就往目录里丢一个 `.x.md.xxxx.tmp`——作者目录越用越脏，而且外部编辑器会把 `.tmp` 当候选正文挑出来 | 不改代码（清理逻辑本身是对的），改的是「没人看着」：`tests/test_core.py::test_atomic_write_cleans_up_tmp_on_failure` 用 monkeypatch 打爆 `os.replace` 制造写失败，断言目标文件**绝不被创建**（半截真相源）且目录里**一个残留都没有**；正常路径再断言目录里只剩目标文件 | 反向验证：把 `atomic.py:52` 的 `tmp.unlink(missing_ok=True)` 删掉 → 测试立即 FAILED 并精确报 `原子写失败留下了 tmp 残留：['.x.md.vz8y3pl3.tmp']`；还原后归绿 |
| 64 | **读路径扛不住半截文件（坏 json 会让整个 API 500）** | 原子写保证了「要么旧要么新」，但外部编辑器、同步盘、`git` 操作仍可能在目录里留下写了一半的 `json`/`yaml`。而 `atomic.read_json/read_yaml/read_text` **直接 `json.loads` 不兜底**：一本半截文件的存在就能让整页 API 报 500——书页打不开比内容少两条更糟 | 三个 read_* 全部加兜底：读不出/解析失败一律走 `default`（`OSError`/`UnicodeDecodeError`/`JSONDecodeError`/`YAMLError`）；`read_jsonl` 坏行跳过、好行照读，`read_text` 读不出走 default | 反向验证：把 `read_json` 的 `except` 去掉 → `test_atomic_readers_survive_half_written_files` 立即 FAILED（`json.decoder.JSONDecodeError: Expecting ',' delimiter: line 1 column 20`）；还原后归绿 |
| 65 | **未捕获异常是黑盒（报 500 但服务端什么都不留）** | `@app.exception_handler(Exception)` 只返回 `{"error": ...}`，**既不打日志也不留 traceback**。全仓 server/ 下只有 `scheduler.py` 与 `cli.py` 有 logging，`api` 层一条没有——线上报 500 之后事后根本没法追，而这恰恰是 PROJECT.md §6「失败不静默降级」最该守的地方 | `server/app.py` 新增 `logger = logging.getLogger("workbench.app")`，`unhandled` 走 `logger.exception(...)` 记录方法/路径/异常类型/消息与完整 traceback；**响应体仍然只有类型与消息**（不泄露内部路径） | 反向验证：把 `logger.exception(...)` 换成 `pass` → `test_unhandled_exception_is_logged` 立即 FAILED（`AssertionError: []`，test_api.py:665）；还原后归绿 |
| 66 | **参数守卫抛的 ValueError 全部变成 500** | #60 加的章号/书名守卫抛 `ValueError`，但 `routes.py` 里 10 个 `except ValueError` 只有 3 处手动转 400/404，其余全漏到 §65 的兜底 `Exception` handler。实测 `PUT /chapters/1000000000` → **500** `{"error":"ValueError: 章号超出范围..."}`——作者填错章号，服务端报 500，监控会把这当服务端故障 | `server/app.py` 新增 `@app.exception_handler(ValueError)` → 400，且 `logger.warning` 留痕；**只兜 ValueError**，KeyError/TypeError 仍然 500（`test_code_bug_still_500` 守着，避免把真服务端故障伪装成「用户填错了」） | 反向验证：把整个 ValueError handler 删掉 → `test_value_error_from_guards_is_400_not_500` 立即 FAILED（`ValueError: 章号超出范围：100001（上限 100000）` 直接穿透到客户端）；还原后 500/400/0/-5 全变 400，1200 仍 200 |
| 67 | **读者评论长度无上界（5MB 也照收）** | 扫 #58/#59/#60 那批上界时漏了读者评论这条：`POST /feedback/reviews/{ch}` 的 `FeedbackReviewReq.text` 是唯一**不带任何上界的自由文本写路径**。实测 5MB 评论 0.2s 照收进 `feedback/reviews`，磁盘与统计一起被灌爆。跟正文/速记/对标书不是一个量级的口子，但性质一样——「作者能用但没人在守」 | `server/api/routes.py` 新增 `MAX_REVIEW_CHARS=50_000`（长评也不该有 5 万字），`feedback_review` 在落盘前 `_cap_text(...)` | 反向验证：守卫短路成 `pass` → `test_feedback_review_text_is_capped` 立即 FAILED（`assert 200 == 400`，实收 50001 字）；还原后 50001 字 400、正常评论 200 |
| 68 | **上界逐个手加，新端点必然漏（机制缺失）** | #58/#59/#60/#67 是一条条给端点加 `_cap_text/_cap_chapters`，扫出来 46 个未封顶字段——实测 `PUT /threads` 能灌 1 万条 beats、`PUT /outline/{ch}` 能灌 5000 条 must_include，**照收**。逐个加是补丁：新加端点时没人会想起加，而每漏一个就多一条口子 | 改机制：`server/api/routes.py` 新增 `CappedModel` 基类（递归扫任意 str/list/嵌套 dict 字段，超限在 pydantic 校验阶段就拦），**25 个入站模型全部改继承它**——新模型只要继承就自动带上界。`server/app.py` 注册 `RequestValidationError -> 400`（FastAPI 默认 422，与全站 400 契约不一致） | 反向验证 1：`_over_capped` 短路成永不报超限 → `test_capped_model_guards_every_new_payload_model` FAILED（DID NOT RAISE）；反向验证 2：把 `ThreadReq` 改回继承 `BaseModel` → `test_all_payload_models_inherit_capped` FAILED 并点名 `['ThreadReq']`。另附 AST 自检盯着「有没有新模型裸奔」 |
| 69 | **文档自己也在漂移（清单是手抄的）** | §1 的单测清单手抄自某次跑批：实际 350 条，**文档顶着 305 条**，还漏记 `test_derivative` / `test_scheduler` / `test_architecture` 三个文件。手抄必然漂移——文档漏报覆盖范围，比代码少个测试更隐蔽：你以为盖住了，其实没有 | 修法：先把文档改回真值，再让**测试反过来盯着文档**——`tests/test_architecture.py` 新增三条：① 文档必须列出每个 `tests/test_*.py`（漏记/多记都红）；② 每条括号里的条数必须等于 AST 数出来的 `test_*` 函数数；③ 一句话结论的总数必须等于实际总数。口径统一为静态可数的函数条数（括号内），并注明 pytest 收集数多 3 项是 parametrize 展开 | 反向验证 1：删掉文档里 `test_derivative.py` 那行 → 立即报 `漏记了这些测试文件: ['test_derivative.py']`；反向验证 2：把 `test_naming.py` 的条数改成 99 → 报 `文档写 99 条，实际 16 条`；反向验证 3：把总数改回 305 → 报 `一句话结论写 305 条，实际 350 条`。三条还原后归绿 |
| 70 | **跳章发布被静默接受（读者看到的是断的）** | `POST /publish {chapters:[5]}` 在 1~4 章还是 `final` 时照样返回 **200 且一声不吭**——`updated: [5]`、`skipped: []`，`shortage_warning` 只报存稿不够，不报断层。作者端根本看不出来：他自己的书架上 1~5 章都在，断层只在读者端成立 | `server/platform/exporter.py`：`mark_published` 末尾算 `_published_gaps()`（已发布章之前、章号存在但未发布的章），有断层就在返回体里带 `gaps` 与 `warning: 第 N 章…读者会看到断层`；**不阻断**（本工具只记录作者在外部平台已做完的事实，报错不等于撤销） | 反向验证 1：`_published_gaps` 短路成永不报 → `test_publish_out_of_order_reports_gap` FAILED（`gaps: []`）；反向验证 2：算 gap 时不排除已发布的章 → `test_publish_in_order_has_no_gap` FAILED（顺序发布误报 `[1, 2]`）。还原后 3/3 归绿 |
| 71 | **章纲状态机只挂在 publish 路由上，通用写入口不查** | `PUT /outline/{ch}` 的 `OutlineUpdate.status` 是裸 `str`，`draft → published` 跳迁实测 **HTTP 200 静默接受**：章纲能过 `model_validate`（`published` 是合法枚举值），只有 `exporter.mark_published` 会查 `can_outline_transition`。作者端看不出异常，`daily-panel` 却把它算进已发布、存稿口径随之失真 | `server/api/routes.py`：`put_outline` 在 merge 前查同一张 `OUTLINE_TRANSITIONS`，非法跳迁 409 并给出合法推进顺序；**同状态重写放行**（幂等），不带 `status` 的普通编辑不受影响 | 反向验证：把守卫条件短路成 `if False and ...` → `test_outline_put_enforces_state_machine` FAILED（`drafted 应被拒，实际 200`）；还原后 0 处 `if False`、测试归绿 |
| 72 | **「确认章纲」能把终态倒回 planned** | `POST /outline/{ch}/confirm` 与 `planner.confirm_outline` 原先**无条件**把状态写成 `planned`：探针实测 `published → planned`、`final → planned` 全部 HTTP 200。「确认」的语义是向前推进，不是倒回——已发布/已定稿的章被拉回会让 `daily-panel` 的存稿口径凭空多出一章（`published` 被排除出 `stock`） | `server/agents/planner.py`：`confirm_outline` 落盘前查 `can_outline_transition`，非法跳迁抛 `ValueError`；`server/api/routes.py`：`confirm_outline_api` 同样查表，非法跳迁 **409**（同状态重写放行，幂等）。两处与 `PUT /outline` 共用同一张 `OUTLINE_TRANSITIONS` | 反向验证：把 planner 的守卫短路成 `if False and ...` → `test_confirm_outline_refuses_to_rewind_terminal_states` FAILED（DID NOT RAISE）；还原后归绿。另 `PUT /outline` 的守卫（#71）使得端到端 `pwa-smoke.mjs` 必须按迁移表逐级回退 |
| 73 | **重跑规划能把已发布章倒回草案** | `Planner.plan(save=True)` 原先无条件 `o.status = "draft"`：探针实测 7 章分别是 draft/planned/writing/drafted/reviewed/final/published 时跑一遍规划，`published` 章被改写成 `draft`（`SAVED 5`，第 7 章 `原题7` → `雪夜` 且状态回 draft）。`published → draft` 是非法迁移却没人拦；后果是 `daily-panel` 的存稿口径（`stock = draft+reviewed+final`，published 被排除）凭空多一章，读者端已经看过的章节在作者端变回未发布。已有的 reviewed/final 跳过逻辑（先于本条）只挡了中间态，没挡终态 | `server/agents/planner.py`：新增模块级 `draft_status(src)`（planned/writing/drafted → `drafted`，其余 → `draft`），保证重跑只降到 drafted 不倒回更「早」的状态；`plan()` 里新增 `existing.status == "published"` 分支，跳过并 warnings 追加「第 N 章已发布，跳过覆盖（已发布章不能倒回草案）」；`PlanResult` 新增 `rewritten: list[dict[str,str]]` 记录真实降级项，`run_plan` 路由一并透出 | 反向验证做了两处：`published` 守卫短路 → `test_replan_never_rewinds_published` FAILED（`after[7]` 变 draft，test_agents.py:870）；`draft_status` 分支短路 → 同一测试 FAILED（`第2章 planned -> draft`，:877）。还原后 `grep -c "if False"` = 0 |
| 74 | **CLI 的 `confirm` 是同一条命令的第三个裸奔入口** | 缺陷 #72 修的是 HTTP 路由与 `planner.confirm_outline` 两处，但 `server/cli.py:132 cmd_confirm` 仍是无条件 `o.status = "planned"`。探针实测七态各一章：ch-0001~0003 正常，ch-0004(drafted) / 0005(reviewed) / 0006(final) / 0007(published) **全部被改写成 planned**——同一条命令走 Web 被 409 拒绝，走终端却能一路放行 | `cmd_confirm` 改为调用 `planner.confirm_outline`（查 `OUTLINE_TRANSITIONS`），非法迁移打印「ch-XXXX 无法确认：非法状态迁移：X → Y」并以退出码 1 结束（有失败即非 0）；章纲不存在仍跳过 | 反向验证：把 `cmd_confirm` 还原成无条件赋值 → `test_cli_confirm_refuses_to_rewind_terminal_states` FAILED（`assert 0 == 1`，test_core.py:701）；还原后归绿 |
| 75 | **已发布章的正文能被直接改写（Reader 看到的内容会变）** || Writer 两条写路径（writer.py:73-74 / :124-125）早就拒绝覆盖 `published`，但另外两条写路径没查：① `AIPolish.polish(apply=True)`（polish.py:184-188）先 `write_version` 留档再 `write_chapter`——探针实测对 `status == "published"` 的章照样 `APPLIED True` / 正文被改写 / 章纲仍停在 published；② `PUT /chapters/{ch}`（routes.py:599-603）手工保存正文也无保护，前端 `WriteView.save()` 就是打它。作者端看不出异常：章还在书架上、字数也变了，但读者已经看过的章节内容被悄悄改掉，「已发布章覆盖保护」这条承诺实际上只守了两条路径 || 两条都补上 Writer 那句同款硬保护：`polish` 只在 `apply=True` 时拒绝（预览无害，照常出报告）；`put_chapter` 返回 409 且正文原样；前端 `WriteView` 加 `published` computed，按钮置灰显示「已发布·不可改」并提前 toast，不等服务端报错 || 反向验证：polish 的 `if apply:` 短路成 `if False:` → `test_polish_refuses_to_apply_on_published` FAILED（test_platform.py:319）；`put_chapter` 守卫短路成 `if False and ...` → `test_put_chapter_refuses_published` FAILED（`assert 200 == 409`，test_api.py:912）；还原后 `grep -c "if False"` = 0 归绿 |
| 76 | **切换版本能绕过「已发布章覆盖保护」** || 缺陷 #75 封了 `polish(apply=True)` 与 `PUT /chapters/{ch}`，但 `Writer.adopt_version`（writer.py:209-234）最终也是 `write_chapter`：探针实测章纲 `published` 时 `adopt_version(1,1)` 照样 `ADOPTED`、正文被换成旧版本内容、章纲仍停在 published——而同一章走 `write()` 却被 Writer 自己拒绝。三条落盘路径口径不一致，「已发布章覆盖保护」只守了其中两条 || `adopt_version` 开头补同一句硬保护（缺版本仍是原来的 `ValueError`）；`POST /chapters/{ch}/version/{v}/adopt` 把 `ValueError` 转 404 的逻辑不变，published 走同一条 404 路径并带「已发布」字样 || 反向验证：守卫短路成 `if False and ...` → `test_adopt_version_refuses_published` FAILED（`DID NOT RAISE ValueError`，test_agents.py:242）；还原后 `grep -c "if False"` = 0 归绿 |
| 77 | **线程 id / 平台名 / 角色 id 能拼出路径，写到书根甚至书外** || save_thread / save_profile / save_character 都是 `dir / f"{用户输入的 id}.json"`。探针实测：`save_thread(Thread(id="../../evil"))` 把文件写到**书根**（evil.json）；`id="../../book"` 直接**覆盖 book.json**，整本书从书架消失；`id="../../../../../tmp/xxx/evil2"` 能写出**书外**（文件确实落盘）。HTTP 层 `PUT /threads {"id":"../../evil"}` 返回 **200**，文件真的在书根 || store 层新增 `_assert_plain_stem(name, what)`（单段名：不含 `/` 或反斜杠、不是 `.` / `..` / 空 / 首尾空格），`_assert_plain_dirname` 改为复用它；三处 `f"{id}.json"` 全部过一遍；`put_thread` / `put_profile` 把 `StoreError` 转 **400**（原来会 500，且真穿透时文件已经落盘） || 反向验证：`_assert_plain_stem` 的分隔符分支短路成 `if False and ...` → `test_store_rejects_traversal_in_thread_and_profile_stems` FAILED（test_core.py:734）且 `test_put_thread_rejects_traversal_over_http` FAILED（`assert 200 == 400`，test_api.py:933）；还原后 `grep -c "if False"` = 0 归绿 |
| 78 | **建书时的平台名能拼出路径，profile 写到工作区根甚至书外** || `create_novel` 用 `f"{book.platform}.json"` 拼 profiles/ 与 workbench/profiles/ 两处文件名。探针实测：`platform="../../evil"` 建书**成功**，并在 ws/evil.json（工作区根）真的落盘；`platform="a/b"` 在书的 profiles/ 下造出子目录 a/b.json。HTTP 层 `POST /api/books` 返回 **200**，越界文件已经写完了（#77 守的是 save_* 三个写点，建书这条播种路径没过） || `create_novel` 在取到 dirname 之后补 `_assert_plain_stem(str(book.platform or ""), "平台名")`（HTTP 层转 409，和「书已存在」同码） || 反向验证：守卫换成 `pass` → `test_create_novel_rejects_traversal_in_platform` FAILED（test_core.py:788）且 `test_create_book_rejects_traversal_in_platform_over_http` FAILED（`assert 200 == 409`，test_api.py:949）；还原后归绿 |
| 79 | **对标书 id 能拼出路径，把 analysis/ 整层列出来** || `GET /books/{book}/benchmarks/{bid}` 把 `bid` 直接拼进 `analysis/benchmarks/{bid}`，然后 `for f in base.iterdir()` 把目录下所有文件正文读出来。唯一的守卫是「目录不存在则 404」，但 **bid=`..` 拼出的 analysis/ 本来就存在**，守卫形同虚设：探针实测 `bid=%2e%2e` 返回 **200** 且 files 里带出了 analysis/ 下作者没打算暴露的文件（模板、其他对标产物）。明文 `..` 会被 Starlette 在路由匹配前折叠成 404，所以这条只能靠编码形式打进来 || 复用 #77 引入的 `_assert_plain_stem(bid, "对标书 id")`，`StoreError` 转 **400**；正常 id 不变（不存在仍是 404） || 反向验证：把 `safe_bid = _assert_plain_stem(...)` 换成 `safe_bid = bid` → `test_benchmark_detail_rejects_traversal_in_bid` FAILED（`实际 200: {"id":"..","files":{}}`，test_api.py:976）；还原后归绿 |
| 80 | **平台名能拼出路径，把书外的 json 当平台配置读进来** || `load_profile` 是 `self.p.profiles / f"{platform}.json"`，platform 直接来自 HTTP query（`GET /export/precheck?platform=`）。探针实测：`platform=../../wsroot` 返回 200 且 echo 成 `wsroot`，预检的字数下限从书内的 2000 变成书外文件里写的 **10**——即**任意书外 json 都能被当成本书平台配置生效**（红线词库、字数区间、钩子要求全都会跟着变）。`redline_file` 外置词库同理，原本是 `self.p.root / prof.redline_file`，能指到书外任意 txt || `load_profile` 开头过 `_assert_plain_stem(platform, "平台名")`；`redline_file` 改为走 `self.resolve()`（逃出书根抛 StoreError）；`save_profile` 保存时也先 resolve 一遍，不等读取时才炸；`export_precheck` 把 `StoreError` 转 **400** || 反向验证：把 `stem = _assert_plain_stem(...)` 换成 `stem = platform` → `test_load_profile_rejects_traversal_in_platform` FAILED（`../../wsroot 应被拒绝，实际 200`，test_api.py:998）；还原后归绿 |
| 81 | **@引用能读出书内隐藏文件（`.git/config`、`.env`）** | 作者在 must_include 写 `@.env`，`_resolve_ref` 直接拼路径并读出正文，随 prompt 发给模型 | `_safe_ref_token()` 统一拒绝空/绝对/盘符/以点开头的路径段，`_resolve_ref` 与 `_resolve_snippet_ref` 共用 | `test_ref_resolution_refuses_dotfiles_and_absolute_paths`、`test_l5_never_leaks_dotfile_into_context`（test_context.py） |
| 82 | **对标书名没有长度上界（超长书名在 mkdir 那一刻炸成英文 500）** | `Deconstructor.analyze` 用 `_safe_dir(title)` 当目录名落 `analysis/benchmarks/{bid}`，`_safe_dir` 只做字符净化不做长度校验：实测 60 字书名 → `OSError(Errno 36) File name too long`，HTTP 层 `500 {"error":"OSError: [Errno 36]..."}`（200/400 字同样是 500） | `_safe_dir` 加 `MAX_BENCHMARK_TITLE_CHARS = 60` 上界，超限 `ValueError`（路由已 `except ValueError → 400`）；**不静默截断**——截断会让两本不同的对标书撞到同一个 bid 上，产物互相覆盖 | `test_benchmark_title_is_capped`、`test_benchmark_import_rejects_overlong_title`（test_api.py） |
| 83 | **半截 json 能让整个日更面板 500** | `WritingStats.pacing_vs_benchmark` 用裸 `json.loads(s.read_text())` 读对标产物：只要 `analysis/benchmarks/<bid>/structure.json` 写了一半（外部编辑器 / 同步盘 / 断电），`daily-panel` 就返回 500 `Unterminated string starting at...`，面板整块空白。同层 `list_benchmarks` 早已走 `atomic.read_json`，只有这一处漏 | 改用 `atomic.read_json(s, None) or {}`——缺陷 #64 定下的「读不出就走 default」语义要覆盖到每一条读路径，不覆盖一处就是一处 500 | `test_daily_panel_survives_corrupt_benchmark_json`（test_api.py） |
| 84 | **一个写坏的版本 meta 能堵死评分回流与切换版本** | `Writer.score_version` 与 `adopt_version` 用裸 `json.loads(mfile.read_text() or "{}")` 读 `chapters/ch-XXXX.versions/vN.meta.json`：实测半截 meta 让 `POST /chapters/1/version/1/score` 返回 **400 `Expecting value: line 1 column 10`**，作者补不了分也切不了版本。同一条链路的 `#83` 只修了 writing.py，这两处是漏网 | 两处都改用 `atomic.read_json(mfile, None) or {}`（meta 只是留档，读不出就当空字典，本次写入会顺手把它修好），并对非 dict 结果兜底；`import json` 提到模块头（adopt 里还要 dumps） | `test_score_and_adopt_survive_corrupt_version_meta`（test_api.py） |
| 85 | **写坏的 `feedback/stats.json` 让整条复盘链路 500** | `FeedbackIngest.top_drop_chapters` 假设 `days` 是 list、`chapters` 的值是 dict：实测六种畸形写法（`chapters` 值为 str/None/list、`days` 是 dict/str/元素是 str 或 None）**全部**让 `POST /feedback/recheck` 返回 500 `AttributeError: 'str' object has no attribute 'get'`。读者反馈是手抄/外部录入的数据，作者根本看不到 500 的真正原因 | 逐层 `isinstance` 校验后跳过不合法项：`stats` 非 dict、`days` 非 list、`day` 非 dict、`chapters` 非 dict、章值非 dict 一律跳过，数据结构不对就不参与 top-N | `test_feedback_recheck_survives_malformed_stats`（test_api.py） |
| 86 | **审稿报告被写成数组时复盘 500** | `NovelStore.load_review` 直接 `raw.setdefault("ch", ch)`：手工把 `memory/ch-0001.review.json` 写成 `[1,2,3]` 后 `POST /feedback/recheck` 返回 500 `AttributeError: 'list' object has no attribute 'setdefault'`——作者看不到任何复盘结果 | 加 `if not isinstance(raw, dict): return None`（读不出来就当没审过，categories 为空但不阻断） | `test_recheck_survives_corrupt_review_report`（test_api.py） |
| 87 | **真相源里混进二进制文件，报错是一串英文 codec 报错** | `GET /truth/{path}` 用 `p.read_text(encoding="utf-8")`，而 `UnicodeDecodeError` 是 `ValueError` 子类 → 被缺陷 #66 的全局 ValueError handler 接走，实测返回 400 `'utf-8' codec can't decode byte 0xff in position 0: invalid start byte`。真相源目录里混进截图/误拖素材时，作者看到的是完全看不懂的英文，且不知道是哪个文件 | 显式捕获 `UnicodeDecodeError`，换成中文提示「truth/xxx 不是 UTF-8 文本（二进制文件无法在此编辑）」，并补 `is_file()` 判断（目录不是文件） | `test_read_truth_binary_gives_chinese_message`（test_api.py） |
| 88 | **并发速记静默丢数据（响应全 200，文件里只剩几条）** | `add_inspiration` 是「读旧内容 → 拼接 → 原子写回」三段式：`atomic.write_text_atomic` 只保证**写入本身**原子，不保证整体原子。实测 20 个线程并发提交速记，**响应全部 200，文件里只剩 3–5 条**，其余永久丢失且没有任何报错 —— 移动端离线队列批量同步时正好会撞上 | 新增 `atomic.file_lock(path)`（系统临时目录里 `moran-lock-<路径哈希>.lock` 上的独占 flock，不落在书目录里以免被 git 提交）与 `atomic.append_text()`（读-改-写整体持锁）；灵感速记改用 `append_text`，`FeedbackIngest.save_stats` 同日覆盖那段也包上锁 | `test_concurrent_inspiration_notes_are_not_lost`（test_api.py） |
| 89 | **并发确认避雷提案会丢条目** | `FeedbackIngest.apply_proposals` 与 #88 同源：「读 style-fingerprint → 去重 → 写回」三段式没有锁。实测 20 个线程并发确认**不同**提案，最后只剩 **3 条**——丢的正是作者已经确认过的避雷条目（后来的写覆盖掉前一次写，且幂等判定用的是自己读到的旧内容） | 整段包进 `with atomic.file_lock(path):`，与 #88 共用同一把锁机制 | `test_concurrent_proposal_confirms_are_not_lost`（test_api.py） |
| 90 | **外置红线词库混进二进制字节，导出预检变 400** | 平台配置的 `redline_file` 指向的 txt 是作者手抄文件，`store.load_profile` 裸 `extra.read_text(encoding="utf-8")`：实测把它写成 `b"\xff\xfe\x00bad bytes"` 后 `GET /export/precheck` 返回 **400 `'utf-8' codec can't decode byte 0xff in position 0`**——UnicodeDecodeError 是 ValueError 子类，被 #66 的全局 handler 接走变成一串英文，作者完全看不懂也找不到是哪个文件 | 改用 `atomic.read_text(extra, "")`，词库读不出来就按「没有外置词库」处理，不阻断 | `test_binary_redline_file_does_not_break_precheck`（test_api.py） |
| 91 | **对标产物目录混进二进制，详情 400 / 章纲生成 502** | 两处裸 `read_text`：① `GET /benchmarks/{bid}` 遍历产物目录读全文，实测返回 400 `'utf-8' codec can't decode byte 0xff`；② `Planner._pacing_brief` 读 `hooks.md` / `pacing.md`，实测整条 `POST /plan` 502 `UnicodeDecodeError`——一本坏产物把章纲生成堵死。产物目录是作者自己拷贝进来的，混进截图/字体文件很常见 | 两处都改用 `atomic.read_text(..., None)`，读不出就跳过该文件（不是整段放弃） | `test_binary_benchmark_artifact_does_not_break_planning`（test_api.py） |
| 92 | **备份「只留最近 N 份」会删掉作者自己的文件夹** | `JobRunner.backup` 原用 `existing = sorted(dest_root.iterdir())` 把备份区里**所有**目录都当旧备份：实测在 `backups/` 下放一个 `0-我的重要资料`（字典序排在所有时间戳前面），下一次备份就被 `shutil.rmtree` 直接删掉——备份任务变成删数据任务 | 清理前先筛选「我们自己写的」：目录名必须匹配 `YYYYmmdd-HHMMSS`（含 `-2`/`-3` 变体）且里面有 `novels/` 子目录，只在这个集合里按字典序保留最近 N 份，并 `logger.info` 记清理数量 | `test_backup_retention_only_prunes_its_own_dirs`（test_scheduler.py） |
| 93 | **拆书写一半失败，留下「只有半截产物」的对标书** | `Deconstructor.analyze` 把 structure.json / pacing.md / hooks.md / template.json **分批**写：新书第一次拆书时若第 2、3 个写入失败（磁盘满/权限/文件被占用），目录里只剩 `structure.json` + `pacing.md`，而 `list_benchmarks` 只看 structure.json 存在就把它当合法对标书列出来——作者会拿一本残缺产物去做节奏对标 | 四个产物抽成 `_write_products()` 整体 try：本次新建的目录失败就 `shutil.rmtree` 整目录删掉；覆盖已有产物时保留旧文件（旧的至少是完整的，比一半新一半旧好） | `test_analyze_leaves_no_half_written_benchmark`（test_agents.py） |
| 94 | **真相源文件被外部写坏时整条读路径 500** | `load_outline` / `get_character` / `load_profile` 三处对 `read_json` / `read_yaml` 的返回值直接 `setdefault`：实测 `ch-0001.json` 写成 `"oops"` 后 `GET /outline/1` 返回 500 `AttributeError: 'str' object has no attribute 'setdefault'`，`profiles/tomato.json` 写成 `"str"` 或 `42` 后 `GET /export/precheck` 同样 500。写入方是同步盘/外部编辑器/断电，不是本服务，只能在读侧兜底 | 三处（含 `_seed_profile`）统一加 `if not isinstance(raw, dict)`：章纲当不存在（404）、角色卡当空卡、profile 走默认值；不做静默吞异常，返回的是「没有这份数据」而不是「数据读失败」 | `test_broken_truth_files_do_not_500`（test_api.py） |
| 95 | **模型吐长串符号时 `extract_json` 卡死（O(n²)）** | `normalizer.extract_json` 抠「第一个平衡的 `{...}`」时用 `s.find("{", start + 1)` 逐个花括号重试，最坏 O(n²)：**实测 1 万个 `{` 要 2.2s、5 万个要 53s**。而 `Gateway.complete_json` 解析失败后还会**再重试一次**（gateway.py:230），一次调用能卡近两分钟——模型抽风吐长串符号完全可能，不是理论风险 | 双重封顶：超 `MAX_JSON_SCAN_CHARS`(20000) 直接拒；长度合格但结构离谱时按 `MAX_JSON_SCAN_STEPS`(200000) 步预算拒。仍然是 `ValueError`（走既有的「再试一次」链路，不是 500） | `test_extract_json_is_bounded_on_garbage_input`（test_model.py，含 6 组正常写法不误伤的对照） |
| 96 | **超长路由表达式触发正则回溯（配置期卡顿）** | `parse_route_expr` 的 `\s*(?:→|->|>)\s*` **两侧都是 `\s*`**，纯空白输入会让 `re.split` 反复回溯：**实测 2 万个空格 1.3s、2 万个换行 1.3s**。路由表达式来自作者手写的 routing.yaml，手滑粘一长串空白完全可能，服务启动解析配置时就会卡住 | 新增 `MAX_ROUTE_EXPR_CHARS`(2000)：超限抛 `ProviderConfigError`（**配置期报错**，不是请求期卡顿）。正常表达式（`:` 精确 / `->` / `→` / list 形态）不受影响 | `test_route_expr_is_capped`（test_model.py） |
| 97 | **跨章复读检查是 O(n²)，10 万字正文审稿卡 6.6 秒** | `reviewer._shared_runs`（:335）每个窗口做一次 `in clean_b`（O(n) 子串查找）再逐字扩展，整体 O(n²)。**实测两章各 5 万字 1.6s、10 万字（`MAX_CHAPTER_CHARS` 上限）6.6s**——审稿是每次保存正文都要跑的路径，作者直接卡住 | 先把 b 的 min_len 窗口做成集合（O(1) 查），命中后**二分**求最长延伸（O(log n)）。**300 组随机对照 fuzz 与旧实现输出逐条一致**，提速不是靠改语义 | `test_shared_runs_is_not_quadratic`（test_agents.py，含 4 组语义不变对照 + 10 万字耗时断言） |
| 98 | **外部写坏的 yaml / 产物让服务起不来或整条 500** | `providers.yaml` 写一句 `- a` → `build_registry` 抛 `AttributeError: 'list' object has no attribute 'get'`，`AppContext.create` 直接崩；`scheduler.yaml` 同形抛 `TypeError: 'list' object is not a mapping`；`analysis/benchmarks/*/structure.json` 写成标量 → `list_benchmarks` 抛同型 AttributeError，`GET /benchmarks` 500；`proposals-XXXX.json` 写成标量 → `Curator.load_pack` 抛 pydantic ValidationError | 四处加形状守卫：registry 的 `build_registry`/`load_routing` 抛中文 `ProviderConfigError`；`load_scheduler_config` 顶层与 `jobs` 段都抛中文 ValueError（**不静默回退**——静默回退会让「配了夜审却从没跑过」无从察觉）；`list_benchmarks` 读不出 dict 就退回目录名；`load_pack` 读不出 dict 就当没有提案包。另给 `_wrap` 包 `try/except + logger.exception("定时任务异常：%s")`，`build_scheduler` 跳过非键值表的 job 配置 | `test_agents.py::test_yaml_top_level_must_be_mapping`、`test_agents.py::test_broken_benchmark_products_do_not_500`、`test_scheduler.py::test_job_wrapper_logs_unexpected_exceptions`、`test_scheduler.py::test_jobs_section_shape_is_defended` |
| 99 | **git 提交失败完全静默** | `commit_all` 的 except 只 `return CommitResult(False, error=...)`，全模块无一行 logging；`PATCH /foreshadow/{fid}` 更是把返回值整个丢掉。实测 `.git/HEAD` 被外部写坏 → `commit_all` 返回 ok=False，但**界面上什么都不显示**，作者以为自己的真相源进了版本库 | `gitbot.py` 加 `logger = logging.getLogger("workbench.git")`，`commit_all` / `init` 的失败分支各写 `logger.warning`；`PATCH /foreshadow/{fid}` 把 `commit` 结果带回响应体并在失败时再记一条「已落盘但 git 提交失败」；`routes.py` 补模块级 logger。**真相源照常落盘，不阻断**——git 挂了不能连正文一起丢 | `test_core.py::test_git_failure_is_logged_and_surfaced`、`test_api.py::test_truth_write_surfaces_git_failure` |
| 100 | **空内容的 `replace` / `create` 提案能把真相源整段清空** | `_compute_new_content` 写的是 `return prop.content or ""`，而 `Proposal.content` 缺省是 `None`。模型提案 `{"op":"replace"}` 只要漏带 content —— 实测 `truth/foreshadow.json` 被写成 **0 字节**，作者手上的伏笔台账凭空消失，且落盘那一刻没有任何报错 | `create`/`replace` 合并为一个分支：`content` 为 None 或纯空白就抛 `ValueError`，盘上文件保持原样；**要清空请用 `op=delete`**（那是显式意图）。`delete` / `patch` / `append` / 带内容的 `replace` 语义全部不变 | `test_agents.py::test_replace_and_create_refuse_empty_content` |
| 101 | **`op=append` 打 `.json` 真相源会把文件写成非 JSON** | `_compute_new_content` 的 append 分支只认 `.jsonl` 与 `.yaml`，`.json` 会掉进末尾的 md/txt 分支，把正文追加在 JSON 后面。实测 `truth/foreshadow.json` 变成 `{"items": [...]}` 再接一行中文，文件不再是合法 JSON，`load_foreshadow()` 退回空台账 —— **作者的全部伏笔凭空消失，而提案返回成功** | append 新增 `.json` 分支：带字典 `patch` 走深合并（文件仍合法），其余一律 `ValueError` 拒绝且盘上原样不动，报错里指明「请用 op=patch 或改用 .jsonl + lines」。`.jsonl` / `.yaml` 追加语义不变 | `test_agents.py::test_append_on_json_truth_file_stays_valid` |
| 102 | **md 分节替换被围栏代码块与同名标题写坏** | `replace_md_section` 用 `SECTION_RE` 只看行首 `#`：(a) 围栏代码块里的 `## 注释` 被当成标题，替换一个跟代码同名的分节会把围栏拦腰截断、代码块后半段被当成「分节正文」替换掉；(b) 同名标题出现两次时只替换第一个，第二个同名分节静默留在文档里，作者以为改过了其实没改 | 新增 `_md_headings()`：逐行跳过围栏代码块内的 `#`，且同名分节**从后往前全部替换**（下标不因前面改动作废）。找不到标题仍追加到文末 | `test_agents.py::test_replace_md_section_handles_fences_and_duplicates` |
| 103 | **提案的删除面没有白名单，能删掉 book.json 与正文** | `apply_proposal` 对 `op=delete` 只靠 `resolve()` 挡「逃出书根」，书根内不设限。实测删除 `book.json`（这本书从书架消失）、`chapters/ch-0001.md`（版本库那份成唯一副本）、`outline/chapters/ch-0001.json` 全部直接删掉 | `apply_proposal` 里 `op=delete` 先过 `_assert_proposal_file_allowed()`，复用 curator 的 `ALLOWED_PREFIXES` 做硬门槛，越界抛 `StoreError`。写（create/replace/patch/append）不受此限——作者改自己的正文与章纲是正常操作 | `test_agents.py::test_proposal_delete_is_restricted_to_truth_files` |
| 104 | **提案的写入面同样绕过白名单** | #103 只堵了删除，`op=replace` 照样能改不该改的：实测打 `book.json` 把这本书的 title 改成任意值（书架索引被改），打 `chapters/ch-0001.md` 把整章正文整段覆盖。PROJECT.md §2 承诺白名单是「物理保证，不是靠 prompt 约束」，而它此前只挂在 Curator 的**生成端**（`_to_proposals`），执行端不查 —— 绕过生成端就没人拦 | `apply_proposal` 里四类 op 统一先过 `_assert_proposal_file_allowed(file, op)`，越界抛 `StoreError`（删除/写入用词区分）。作者自己改正文走 `PUT /chapters`，不经提案，不受此限 | `test_agents.py::test_proposal_write_is_restricted_to_truth_files` |
| 105 | **发布断层告警在前端被丢弃** | #70 让服务端 `mark_published` 回传 `gaps` / `warning`，但 `DailyView.togglePublish` 只消费 `skipped`：作者点「标记发布」看到的永远是「已标记发布第 N 章」，读者端的断层无人知晓。后端把话说出口不等于作者看得见 | `togglePublish` 里补 `if (r.warning) store.toast(...)`；`App.vue` 补 `.toast.warn` 样式（否则带 kind 也只是普通灰条） | `tests/test_frontend.py::test_publish_result_warning_is_shown`、`test_toast_warn_kind_has_a_style` |
| 106 | **并发 commit 会静默丢提交（git 的 index.lock 不允许并发）** | `GitBot.commit_all` / `commit_paths` 完全没有互斥。实测 12 个线程并发提交：**9 个报 `.git/index.lock: File exists` 或 `Reference at refs/heads/master does already exist`**，而它们各自的文件其实都已落盘 —— 真相源写进去了却没进版本库，时光机上查不到。HTTP 层同样复现（15 个并发速记：响应全 200，git 日志只有 1 条） | `GitBot` 内部加 `_git_lock()`（复用 `atomic.file_lock`，锁落 `.git` 路径，不进书内容目录）；`commit_all`/`commit_paths` 拆出 `_commit_*` 内部实现并在外层持锁。`atomic.file_lock` 同时补**同线程可重入**（flock 对同进程的第二次 LOCK_EX 也会阻塞，否则嵌套会把自己锁死） | `test_core.py::test_concurrent_commits_are_not_lost`、`test_git_lock_is_reentrant_in_same_thread` |
| 107 | **truth 保存成功后无条件声称「已提交 git」** | 服务端把 commit 结果放进了响应体（`commit.ok` / `commit.error`），但 `TruthView.save()` 的 toast 是写死的字符串：git 挂了照样报「已提交 git」，作者以为真相源进了版本库，时光机上却查不到。同一页面上 CurateView 是读 `commit.ok` 的（显示「失败 + 原因」），两处口径不一致 | `save()` 里读 `r.commit`，按 `ok` 决定文案：成功说「已提交 git」，失败说「⚠ git 未提交：<原因>」并用 `warn` 样式 | `tests/test_frontend.py::test_truth_save_does_not_claim_git_success_blindly` |
| 108 | **CLI 的 `moran publish` 把发布断层告警吞了** | #70 让服务端 `mark_published` 回传 `gaps` / `warning`，#105 让 Web 端消费它，但 `cmd_publish` 只打印 `updated`：走终端连载的作者看到「标记发布: [5]」就以为一切正常，而读者端第 1~4 章是空的。同一条告警不能在某个入口被丢弃 | `cmd_publish` 补 `if res.get("warning"): print("  ⚠ ...")` | `test_core.py::test_cli_publish_prints_gap_warning` |
| 109 | **流式生成失败时 SSE 流里没有任何 error 事件** | 非流式路径 `except Exception -> 502` 早就把失败说出口，但 `stream=true` 时响应头在 Writer 真正开跑之前就发出去了，失败只留在服务端日志里。作者看到的是「进度条停在 0%，什么都没发生」 | `event_stream()` 的 `except` 里 `yield _sse("error", {"message": ...})` | `test_api.py::test_generate_stream_reports_writer_failure` |
| 110 | **夜审失败被「已触发 N 章」盖住** | `POST /jobs/nightly` 把失败埋在 `reviewed[i].error` 里，而 `reviewed` 的长度是**成功+失败**的总数；PWA 的 `triggerNightly` 只弹「夜审已触发：N 章」，作者看到「3 章」就以为三章都审过了，实际可能一章都没审成 | 响应顶层补 `ok` / `warnings`（中文「夜审有 N 章失败：第 X 章」、索引对账不通过），PWA 照实把 `error` 明细拼进弹窗 | `test_api.py::test_nightly_reports_failures_at_top_level` |
| 111 | **拆书「半成功」被报成全成功** | `Deconstructor.analyze` 在模型那一步挂掉时把原因塞进 `warnings`（实测`模型拆书失败（RuntimeError），仅启发式产物`），响应体也带回来了；但 `BenchmarkView.doImport` 只 toast「已拆解《X》：N 章 / M 字」——数字一样是好的，作者以为这是 AI 深度拆书的结果，照着它调节奏 | `doImport` 补 `(r.warnings || []).forEach((w) => store.toast(w, 'warn'))` | `tests/test_frontend.py::test_benchmark_import_shows_half_success_warnings` |
| 112 | **上传大小上限判在读之后** | `benchmark_upload` 是 `raw = await file.read()` 再比 `MAX_UPLOAD_BYTES`（8 MB）——先把整个文件吞进内存才拒，2 GB 上传会先吃掉 2 GB。Starlette 的 UploadFile 超过 1 MB 已落 `SpooledTemporaryFile`，seek+tell 就能知道大小（实测 9 MB 样本 tell()=9437184） | 读前先探 `file.size`，拿不到再 `seek(0, SEEK_END)+tell()` 后 seek 回 0；超限直接 400，read() 后的兜底保留 | `tests/test_api.py::test_benchmark_upload_rejects_oversize_before_reading_it` |
| 113 | **`moran pipeline` 有章失败仍返回 0** | 四个步骤（write/review/curate/confirm）的失败都被 `except Exception: print(...)` 吞掉，结尾无条件打「=== 完成 ===」并 `return 0`。实测把第 1 章置 published 再跑：输出里 6 行「失败」，退出码仍是 0 —— crontab / CI 只看退出码，半夜批量跑 20 章炸 19 章，收件箱里只有一句「完成」 | 每步 `failed += 1`；结尾按失败数打「完成（N 步失败）」并 `return 1 if failed else 0` | `tests/test_core.py::test_cli_pipeline_returns_nonzero_when_a_chapter_fails` |
| 114 | **`curate-confirm` 一条都没落下去仍返回 0** | `Curator.confirm` 只把落盘失败塞进 `warnings`，返回值里没有失败计数；CLI 打完「已应用 0 项，跳过 1 项」就 `return 0`。实测一条指向 `book.json`（白名单外）的提案：truth 一行没改，退出码仍是 0 —— 作者和套着它的脚本都以为沉淀完成了。和 #113 是同一类「告警没走到进程出口」 | `confirm` 返回值加 `failed` 计数；CLI 读 `out["failed"]`，非零时打「有提案落盘失败」并 `return 1` | `tests/test_core.py::test_curate_confirm_returns_nonzero_when_a_proposal_fails` |
| 115 | **truth 的「本章避雷」只增不减** | 每条避雷条目都带章号（实测 `- [第1章] 钩子弱 -> 后续章节避免：...`），去重按整行精确匹配，章号/日期不同就是新条目 —— 这一节随写作无限增长。`Planner._avoidance_brief` 只读最近 12 条，剩下的既不生效又永远躺在真相源文件里（作者打开 style-fingerprint.md 看到一屏陈年旧账）。与 #58/#68/#82 同源 | `MAX_AVOIDANCE_ENTRIES = 200`，超出丢最旧的；返回值加 `trimmed` / `limit`；`FeedbackView` 的确认 toast 按 `trimmed > 0` 转 warn 并告知丢了几条 | `tests/test_core.py::test_avoidance_section_is_capped_and_reports_trimming` |
| 116 | **起名词池并发首次写入会各写一份表头** | `NameGenerator._append_pool` 是「`exists()` 判表头 → `open('a')` 裸追加」，两步间无锁。实测 24 线程并发首次起名，约 15% 概率出现 2–3 份表头（更常见的是表头被别的线程的行挤到文件中间：`| 孤星府 |…\n# 起名词池\n…`），Markdown 渲染时表格断裂。与 #88 同源：当年只把灵感速记换成 `atomic.append_text`，词池漏了 | 表头提为模块级 `POOL_HEADER`，`_append_pool` 改用 `atomic.append_text(path, ..., header=POOL_HEADER)` 整体持锁 | `tests/test_core.py::test_names_pool_header_written_once_under_concurrency`（连跑 15 轮，单轮命中率约 15%） |
| 117 | **并发确认提案静默丢一半** | `store.apply_proposal()` 是「读旧内容 → 拼 → 写回」，全程不持锁。实测 12 条并发 append 到 `memory/entity-deltas.jsonl` 只活 6 条，`truth/timeline.yaml` 只活 4 条，**响应全 200 无异常**，Curator 把每条都记成 applied，作者以为 12 条伏笔/时间线都沉淀了 | `apply_proposal` 整段包进 `atomic.file_lock(target)`（按目标文件粒度，不影响别的文件并发）；新增 `tests/test_core.py::test_concurrent_proposal_appends_are_not_lost` | 反向验证：把锁换成 no-op → 真红 `实体增量丢了：期望 15 条，实际 4 条`；还原后归绿 |
| 118 | **实体增量损坏 → 图谱静默缩水，对账全绿** | `atomic.read_jsonl` 对坏行、git 冲突标记、整文件编码错乱一律 `except: continue`。而 `memory/entity-deltas.jsonl` 是真相源且**进版本库**：实测三种情况——尾部半行、冲突标记（实体 6→12 条重复）、整文件非 JSON（实体 13→4）——`build_index` 与 `verify` **全程 ok=True**。作者以为沉淀了，实际图谱已经塌了一半，而夜间对账每晚都报「一切正常」 | 新增 `atomic.read_jsonl_report() -> JsonlReport`（`rows` / `bad_lines` / `conflict_markers` / `unreadable` + 中文 `problem()`）；`store.read_entity_deltas_report()`；`BuildReport` / `DriftReport` 增 `entity_source` 并进 `as_dict()`；`verify()` 的 `ok` 把数据源问题并入 | `tests/test_retrieval.py::test_entity_delta_corruption_is_reported_not_swallowed`、`::test_build_report_carries_entity_source_warning`、`::test_entity_delta_read_keeps_valid_rows_when_partially_broken` |
| 119 | **`moran index verify` 对账不通过仍返回 0** | CLI 把 `bc.retrieval.verify()` 的 JSON 打出来就 `return 0`。这个命令是给 cron / CI 用的：漂移、缺章、孤儿、实体数据源损坏四种情况里任意一种发生，退出码依旧是 0 —— 半夜跑完没人看 JSON，等于没对账。与 #113/#114 同源（告警走到退出码） | `cmd_index` 的 verify 分支在 `ok=False` 时按四个字段逐条打中文告警（「正文已改未重索引」「正文有、索引没有」「索引有、正文已删」「实体数据源」）并 `return 1` | `tests/test_core.py::test_cli_index_verify_returns_nonzero_on_drift` |
| 120 | **调度器 start() 失败 →「已开启」是假的** | lifespan 里 `sched.start()` 的异常被静默 `pass`，服务照常起来；而 `/api/jobs/scheduler` 只回 `available` / `enabled`，这两个字段在 start 失败时**照样是 true**（实测返回体里没有 running 字段）。作者开了夜审以为每晚 3 点会跑，实际一次都不会触发，且没有任何地方能看出来。与 #45 是同一条链的两端：#45 崩得看得见，#120 没崩也没在跑且看不见 | 启动失败进 `sched_state["error"]` 并 `logger.exception`；状态端点补 `running` / `error`，并在「已开启但没在跑」时追加中文 `warning`（「定时任务已开启但调度器没在跑（原因），任务不会执行」） | `tests/test_scheduler.py::test_scheduler_start_failure_is_visible_in_status` |
| 121 | **索引对账结果在 Web 端完全没有出口** | `GET /books/{book}/index/verify` 一直在（漂移 / 缺章 / 孤儿 / #118 的实体数据源损坏），CLI 也会报警（#119），但整个 `web/src` 里从来没有一个调用点 —— `client.js` 只有 `indexStats`/`indexBuild` 两个封装且全项目无调用者。作者不开 CLI，实体图谱塌了一半，界面上依旧全绿 | `client.js` 增 `indexVerify` 封装；`DailyView.vue` 新增「索引对账」区：进页面就拉对账，四类问题（`error`/`drifted`/`missing`/`orphaned`）加 `entity_source` 逐条列出并给「重建索引 / 全量重建」按钮；`rebuildIndex` 消费 build 的 `entity_source`（还坏着就转 warn toast） | `tests/test_frontend.py::test_daily_panel_shows_index_verify_result`、`::test_index_rebuild_reports_entity_source_problem` |
| 122 | **流式生成的告警在 SSE 通道里被吞** | `Writer.write()` 会算 warnings（装配降级 + 字数偏少/偏多），非流式分支早就放进响应体了；但 `write_stream()` 产出的 `StreamChunk` 根本没有 warnings 字段，SSE 的 done 事件无从转发。实测 mock 网关返回 9 字正文：`write()` 拿到「L4 未配置检索（降级）」+「字数偏少：9 < 目标 2000–2400」，而流里只有 `[{done:false},{done:true,usage:{words:9}}]` —— 作者点完「生成」出来一章 9 个字，界面上一句提示都没有。与 #109 是同一条 SSE 通道的两类缺口（#109 管失败要有 error，这条管成功但有问题也要有 warnings） | `StreamChunk` 增 `warnings` 字段；`Writer._warnings()` 抽出来让两条路径共用同一口径；`write_stream` 的 done chunk 带上它；`routes.generate` 的 done 事件加 `"warnings"`；`WriteView.onDone` 逐条 `store.toast(..., 'warn')` | `tests/test_api.py::test_generate_stream_done_event_carries_warnings`、`tests/test_frontend.py::test_stream_done_warnings_are_shown_to_author` |
| 123 | **「发布前强制跑反 AI 味管线」只查开关位，从不真扫；Web 端还根本没有入口** | `Exporter.precheck` 从不调用规则层，只看 `book.ai_flavor_pipeline` / `book.repetition_check` 两个 bool（真值都 True）就放行 —— 实测正文里带着 `REPLACE_RULES` 首条「仿佛整个世界」，`POST /export` 返回 `blocked=False`，导出产物 `ch-0001.txt` 里套语原样在。README §9.2 承诺的「发布前强制」实际只查了个开关。同时 `client.js` 里有 `polish()` 封装却**全项目零调用点**：ReviewView 提示「去写作台跑反 AI 味润色」，写作台没有那个按钮（#123 的第二层）。第三层：`unresolved_high` 只在「导出被拦截」分支被渲染，而套语 / 复读**不拦导出** —— 成功导出时作者一句提示都收不到 | `AIPolish.scan(text, ch)` 新增只读扫描接口（规则层 + 排比 + 节奏 + 跨章复读，不改写不落盘）；`Exporter.__init__(store, polish=None)` 接收它，precheck 里按 kind 分级：套语 / 复读 → `high_issues(category="ai-flavor")`（需 `repetition_check`），排比 / 节奏 → `mid_issues`（需 `ai_flavor_pipeline`），两个开关各管各的；`deps.exporter` 把 polish 传进去。Web 端：`WriteView` 加「反 AI 味润色」按钮（先只读看 issues + diff，`window.confirm` 后才落盘，已发布章前端先拦），逐条 toast `r.warnings`；`DailyView` 导出成功分支消费 `r.unresolved_high` → warn toast + 成功卡片列出未处理项 | `tests/test_platform.py::test_export_precheck_scans_ai_flavor_smells`、`::test_export_precheck_ai_flavor_respects_switches`、`::test_export_report_lists_ai_flavor_issues`、`::test_polish_scan_is_read_only`、`tests/test_api.py::test_export_precheck_api_reports_ai_flavor`、`::test_polish_endpoint_returns_issues_and_diff`、`tests/test_frontend.py::test_polish_has_web_entry_point`、`::test_polish_apply_requires_confirmation_and_rejects_published`、`::test_export_success_reports_unresolved_high` |
| 124 | **PWA 首屏按钮在第一个 await 之前是死的** | `pwa/app.js` 的 `DOMContentLoaded` 里先 `await openDB()/render()/loadProgress()`，之后才给 `#save`/`#sync`/`#nightly` 绑 click。首屏那两个 `/api` 请求慢一点或断网，按钮就处于「看得见、点得动、一个字都不存」的状态 —— 作者只会以为自己没点对，一笔灵感就这么没了。与 #49（offline 监听挂在 await 后，状态灯卡在「在线」）是同一个首屏竞态的两端：一端是**看不到**掉线，一端是**写不进**东西 | 三个 `addEventListener` 与 `offline` 监听一起移到 `await openDB()` 之前；`save` 处理里补 `if (!db) db = await openDB()` 兜住「绑得早但 DB 还没好」 | `tests/test_frontend.py::test_pwa_first_screen_binds_input_before_the_first_await` |
| 125 | **PWA 端到端冒烟把「被测行为」当成 JS 报错，整条链路被判 FAIL** | `scripts/pwa-smoke.mjs` 用 `ctx.setOffline(true)` 模拟断网，期间页面自己发出的 `/api` 请求必然 `net::ERR_INTERNET_DISCONNECTED`，Chrome 记成一条 console error。这是**离线队列本该如此**（那个请求本来就不该发通），不是 JS 缺陷，却让「PWA 全程无 JS 报错」在离线步骤必红；`http404` 收集同理。实测连跑三次：20/19、35/34、35/35 —— 两种失败模式、同一个根因，验收脚本自己是不稳定的 | 引入 `offlineWindow` 开关，窗口内的 `ERR_INTERNET_DISCONNECTED`/`Failed to fetch`/`ERR_NETWORK_CHANGED`/`ERR_NAME_NOT_RESOLVED` 噪音不计入 `consoleErrors`，窗口内的 404 也不计入 `http404`；窗口只在 `setOffline(false)` 之后关闭 | `node scripts/pwa-smoke.mjs` 连跑 3 次均 35/35 `RESULT=PASS` |
| 126 | **commit 成功但 tag 没打上，返回值照写 tag=ch/0002（时光机查不到这一章，全程零日志）** | `GitBot._force_tag` 是 `except GitCommandError: pass`（只 catch 这一种），而 `_commit_all`/`_commit_paths` 打完标后无条件 `return CommitResult(..., tag=tag or "")`。探针实测：预置残留的 `.git/refs/tags/ch/0002.lock`（上次进程被 kill 的典型现场）后 `commit_chapter(2)` 返回 `ok=True, tag='ch/0002', error=''`，而 `tags()` 里只有 `ch/0001` 与 `ch/0002.lock`，`show_file_at("ch/0002", ...)` 返回 None —— **时光机按 ch/0042 索引，这一章等于没进版本库时光机**，且一行日志都没有。`docs/PROJECT.md:73` 承诺「每次章定稿自动提交并打 tag」 | `_force_tag` 改为返回错误文本（catch 放宽到 Exception），失败时 `logger.warning("git 打标失败：%s -> %s")`；两处打标点统一 `tag_err = ...`，返回 `tag="" if tag_err else (tag or "")` + 新增字段 `tag_error`。出口三层：CLI `moran pipeline` 打印「⚠ 打标失败（时光机查不到该章）」；`CurateView` 落盘结果卡片加一行 warn 文本；`TruthView.save()` 的 toast 拼 tagTail 并降级为 warn | `tests/test_core.py::test_commit_result_does_not_claim_a_tag_that_was_not_created`、`::test_commit_paths_does_not_claim_a_tag_that_was_not_created`（第二条打标路径单独一条：反向验证实测只摘 `_commit_paths` 时第一条全绿，说明那条路径无人看守）、`tests/test_frontend.py::test_tag_failure_has_a_frontend_exit`。反向验证三处均真红后还原 |
| 127 | **embedding 失败时索引静默缺向量，build / verify / 门面档位三处同时说谎** | `IndexBuilder._embed_pending` 是 `except Exception: return 0`（注释写着「不影响 BM25 主链路」），异常串直接丢。探针实测（embedder 抛 RuntimeError 模拟上游 502）：`build` 返回 `{embedded: 0, error: '', chunks: 12, vectors: 0}`、`stats` 报 `mode='hybrid'`、`verify` 返回 `ok=True` —— **三处全在说谎**：检索实际已退回纯 BM25，而 README:73 承诺的「FTS5(BM25) + 向量 + RRF(k=60)」此刻只有前者。真实书库现场：演示书 `index.db` 实测 chunks=39 / **vectors=0** / `meta.embedder=''`，一直在「hybrid 门面 + 零向量」状态下跑 | `_embed_pending` 把异常记进 `self._embed_error`；新增 `_vector_source(stats)` 把「向量缺失」翻成中文并区分成因（embedding 跑挂 → 带异常原文；压根没配 → 只陈述事实）；`build` 报告新增 `vector_source` 字段；`verify` 新增 `vector_source` 并**按成因**并入 `ok`（配了 embedder 却零向量 → 不通过；没配 → 只提示不判红，否则对账永远红没人看）；`facade.mode` 不再只看「有没有 embedder」，零向量一律报 `bm25`；`facade` 六处早退分支补 `vector_source`/`entity_source` 键（缺键会让前端读 undefined 后静默跳过）。出口三层：CLI `moran index verify` 新增「向量缺失」告警且退出码非 0；`DailyView.indexProblems` push 该说明；`rebuildIndex` 命中时弹 warn toast | `tests/test_retrieval.py::test_build_reports_vector_source_when_embedding_fails`、`::test_verify_flags_missing_vectors`、`::test_verify_ok_when_vectors_present`、`::test_verify_tolerates_unconfigured_embedding`、`::test_facade_mode_does_not_claim_hybrid_without_vectors`、`::test_facade_early_returns_carry_vector_source_key`、`tests/test_core.py::test_cli_index_verify_reports_missing_vectors`、`tests/test_frontend.py::test_index_verify_reports_missing_vectors_to_author`、`::test_index_rebuild_reports_missing_vectors`。反向验证五处（摘错误记录 / 摘 verify 并入 / 摘 facade.mode / 两处前端分支短路）均真红后还原 |
| 128 | **只读打开 WAL 库凭空造出 `-wal` / `-shm`，基线校验假红** | `IndexBuilder.connect(create=False)` 用 `mode=ro` 打开索引库。WAL 模式的库哪怕只读打开，SQLite 也会新建这两个旁文件（正文一个字节没变、`git status` 干净）。`verify-all.sh` 第 3~5 步的防线按「书库目录所有文件 md5 汇总」做基线，UI 冒烟进书架页（DailyView 挂载即调 `indexVerify`）就多出 32KB `index.db-shm` → 报「真实书库被改写」整轮 `RESULT=FAIL`。假红和假绿一样致命：这道防线第一次哭狼之后就没人信了 | 只读连接改为 `mode=ro&immutable=1`：告诉 SQLite 该文件此刻不会变，不加锁、不建旁文件（实测读到内容一致、库文件字节前后相同）；写连接不受影响仍走 WAL+NORMAL | `tests/test_retrieval.py::test_read_only_connect_does_not_create_wal_sidecars`、`::test_read_only_connect_still_sees_the_data`（后者是对照组：immutable 不能把库读空）。反向验证：去掉 `immutable=1` → 真红 `['index.db'] -> ['index.db', 'index.db-shm', 'index.db-wal']`，还原后转绿 |
| 129 | **PWA 夜审只消费 warnings，漏掉 index_verify.vector_source** | `POST /jobs/nightly` 返回 `{reviewed, ok, warnings, index, index_verify}`。PWA `triggerNightly` 只读了 `warnings`，但 embedding 没跑成时 `vector_source` 在 `index_verify` 里而不在 `warnings` 里 —— 作者点了「夜审」却不知道索引悄悄退回纯 BM25，还以为一切正常 | `triggerNightly` 补读 `r.index_verify.vector_source` 与 `r.index.vector_source`（去重后拼进告警），作者才能看到「索引对账：向量缺失…」 | `tests/test_frontend.py::test_pwa_nightly_alerts_on_index_vector_source`。反向验证：删掉 `index_verify` 消费代码 → 真红，还原后转绿 |
| 130 | **load_book 失败静默降级，作者改的 context_budget 被悄悄忽略** | server/api/deps.py:57 与 server/context/assembler.py:146 两处 except Exception: pass 后静默用默认 16000，零告警 | 破坏 book.json 为非法 YAML，assemble() 后 ctx.warnings 无任何提示 | 两处 except 改为记 warning「book.yaml 读取失败：{类型}，上下文预算已降级为默认 16000」 | assembler.warnings 直接注入 + deps.py 同步，两条守卫测试（含对照组） | 反向验证：摘 assembler warning → test_load_book_failure_warns_about_context_budget 真红；还原后转绿 |

**单测与端到端的分工**（别把两者混为一谈）：`tests/test_scheduler.py` 21 项覆盖 job 函数、配置解析、注册逻辑与 lifespan 装配；但「cron 到点会不会真的把任务调起来」只能靠 `scripts/scheduler-smoke.sh` 真等服务跑——两者都跑才算闭环。

**结论**：§7 第 4、5 条已从「未验证」转为「已验证」，且两条链路都不只是"缺测试"，而是各藏着真实缺陷（#45 尤其严重：配置一开服务就崩）。全量 `bash scripts/verify-all.sh` → `RESULT=PASS`。

### 11.4 缺陷 #51 的教训：假绿比红更危险

补这两条链路的过程中，真实书库 `ws/novels/novel-《凡人问道》` 被写入了 5 个新章纲和 3 个改动的 memory 文件。查下来**不是被测代码的问题**，而是验证脚本自己：

1. 更早一轮我起过一个 `serve --port 8791` 的后台任务，它一直没停，且直接指向真实 ws；
2. `verify-all.sh` 第 3 步的 `serve` 因此绑定失败，只在 `ws/serve.log` 留了 `Errno 98`；
3. 第 3 步判断「服务起来没有」只看 `curl /api/health` 是否成功——残留服务健康地回了 `ok`；
4. 于是第 4、5 步打的是那个旧服务，`RESULT=PASS` 只证明了「8791 上有个服务」。

**这套流程的缺陷在于：它没有任何一处校验「我测的到底是不是我刚起的那个服务」。** 修复后有两道闸：端口预检（有人占着就立刻红）与 `health.root` 身份断言（起来了也要证明是本次的 ws）；再加一道真实书库全文件 md5 护栏，把「静默污染」变成「响亮的失败」。

**反向验证已做**：用一个监听器占住端口后复跑，第 3 步立即输出 `RESULT=FAIL`、退出码 1，不再进入第 4 步。只写代码不验证修复等于没修。

被污染的真实书库已还原：`git checkout -f` 三个 memory 文件 + 删除 `ch-0009..0013.json`，还原后全文件哈希 `c69f19f9bec404c1403b0057e86fa961`，`git status` 只剩开跑前就存在的 `?? feedback/`。

