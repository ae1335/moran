# 网文通用工作流工作台

[![GitHub](https://img.shields.io/badge/GitHub-ae1335%2Fmoran-blue)](https://github.com/ae1335/moran)

> 本地优先 · 文件即真相源 · BYOK 多模型的个人长篇网文创作工作台
>
> 按《网文工作台架构方案计划书 v2.0》实现。**M1–M8 全部落地**，离线可跑通。

---

## 30 秒启动

```bash
# 1) 依赖（受限环境装到 vendor/pylib，不污染系统 Python）
pip install --target vendor/pylib -r requirements.txt

# 2) 建工作区 + 建书 + 跑 3 章（离线 mock 模型，不花钱）
PYTHONPATH=vendor/pylib:. python -m server.cli --root ws init
PYTHONPATH=vendor/pylib:. python -m server.cli --root ws new "凡人问道"
PYTHONPATH=vendor/pylib:. python -m server.cli --root ws pipeline --chapters 1-3

# 3) 起 Web UI
PYTHONPATH=vendor/pylib:. python -m server.cli --root ws serve
#    API 文档  http://127.0.0.1:8765/docs
#    移动 PWA  http://127.0.0.1:8765/pwa/index.html

# 4) 前端（可选，开发模式）
cd web && npm install && npm run dev     # http://127.0.0.1:5173
cd web && npx vite build                 # 构建后由主服务直接托管
```

`make demo` 等价于上面第 2 步（跑 8 章 + 导出）。
接真实模型、配检索、排错：见 [`docs/QUICKSTART.md`](docs/QUICKSTART.md)。
目录结构、设计决策与扩展点：见 [`docs/PROJECT.md`](docs/PROJECT.md)。
`make verify`（=`bash scripts/verify-all.sh`）一条命令跑完全部验证，末行 `RESULT=PASS` 才算过：

| 步骤 | 内容 |
|---|---|
| 1–2 | Python 单测（446 项）+ 前端构建 |
| 3–4 | 起服务 + API 冒烟（含未知 `/api` 必须 404） |
| 5 | 浏览器 UI 冒烟（15 个页面，收集控制台错误） |
| 6 | **PWA 离线队列 / 夜审 / 索引漂移** 端到端（35 项，真断网） |
| 7 | **定时任务实跑**（15 项，真等一次 cron 触发） |

第 6、7 步都跑在 `.runtime-test/` 下的 ws 副本上，结束时校验真实书库 git 逐字未变
（被测服务自己会调 `GitBot.commit_all`，直连真库会在书目录里造出 commit）。
只跑单条：`make smoke-pwa` / `make smoke-scheduler`。

---

## 它解决什么

| 痛点 | 本工作台的做法 |
|---|---|
| 长篇一致性崩坏（人设/时间线/战力/伏笔吃书） | 7 份真相文件 + 6 层上下文装配，按章选择性注入，**总量硬上限 16k tokens** |
| 伏笔管理靠脑子 | 伏笔全生命周期状态机 `draft→planted→hinted→resolved\|dropped`，超期自动报警 |
| 平台绑定、迁移成本 | 一本书 = 一个纯文件目录 + git 仓库；任何编辑器可开 |
| AI 成本与质量平衡 | BYOK + 订阅桥复用 + 任务级路由 + 链尾强制官方 API |
| AIGC 检测风险 | 文风指纹 + 反 AI 味后处理（规则层 + 模型层 + 复读检测），发布前强制 |
| 写崩了不知道为什么 | 读者反馈回流：弃书率 top-5% → 定向重审 → 归因 → 避雷条目 → 影响后续章纲 |

---

## 架构

```
UI 层    本地 Web UI（三栏） · 移动 PWA · 任意编辑器直开文件
  ↓
服务层   FastAPI：/api/generate(SSE) · /chapters · /truth · /foreshadow · /export …
  ↓
Agent    Planner → Writer → Reviewer → Curator（+ 拆书 Agent 离线）
  ↓
装配引擎 6 层：L0 宪法 · L1 契约 · L2 伏笔 · L3 前情 · L4 检索 · L5 @引用
  ↓
检索     FTS5(BM25) + 向量 + RRF(k=60) + rerank + GraphRAG 一跳 + 出处标注
模型层   ProviderRegistry · Router+fallback · Normalizer · HealthMonitor · Usage
  ↓
数据层   truth/ outline/ chapters/ memory/ analysis/ materials/ feedback/ → git
```

依赖方向单向：`api → agents → context/model → core`，**禁止反向依赖**。 由 `tests/test_architecture.py` 用 AST 全量扫描守护（129 个缺陷已修复）；实测当前 0 处反向依赖，注入 `core → agents` 会立即失败并报出精确行号。

---

## 目录结构（代码）

```
server/
├── core/         paths 目录约定 · atomic 原子写 · schema 全部 pydantic 模型
│                 store 真相源读写门面 · domain 伏笔/时间线/战力校验 · gitbot
├── model/        base 协议 · gateway 路由+fallback+熔断 · normalizer 三桥差异
│                 usage SQLite 用量/健康/事故 · providers/{openai_compat,mock,registry}
├── context/      assembler 6 层装配 + 预算裁剪 + 审计落盘
├── retrieval/    chunker · index(FTS5/向量/实体图) · embedder · search(BM25+向量+RRF+rerank)
│                 graph(GraphRAG) · facade(降级封装)
├── agents/       prompts 全部模板 · planner · writer · reviewer · curator · deconstructor
├── platform/     exporter 导出+预检 · polish 反 AI 味管线
├── feedback/     ingest 反馈录入 + 复盘归因
├── stats/        writing 写作统计 · scheduler 夜审/索引/备份
├── api/          deps 依赖注入 · routes 全部 REST
├── app.py        FastAPI 装配
└── cli.py        命令行（M1：无前端也能跑通）
web/              Vue 3 + Vite 三栏工作台
pwa/              移动端灵感速记（离线 queue → 联网同步）
tests/            216 项单测 + 集成测试
```

---

## 数据模型（真相源）

一本书 = 一个目录：

```
novels/novel-《书名》/
├── book.json                 书籍元信息（target/current_chapter/预算/管线开关）
├── truth/                    ★ 每章生成前强制装配
│   ├── constitution.md       世界/叙事/文风红线（L0，缺失即拒绝生成）
│   ├── style-fingerprint.md  文风指纹 + 避雷条目
│   ├── characters/*.yaml     角色卡（boundaries / power_scale.forbidden 是审稿依据）
│   ├── world/*.md            力量体系 / 势力 / 地理 / 世界规则
│   ├── foreshadow.json       伏笔状态机
│   ├── timeline.yaml         虚构历法时间线
│   ├── world-state.md        当前世界状态快照
│   ├── resources.json        资源账本
│   └── arcs.md               情感弧线与支线
├── outline/
│   ├── master.md · volume-01.md
│   ├── threads/*.json        故事线
│   └── chapters/ch-0042.json 章纲（全流水线枢纽契约）
├── chapters/ch-0042.md + ch-0042.versions/
├── memory/                   章摘要 · 装配审计 · 审稿报告 · 实体变化流 · 提案包
├── analysis/benchmarks/      拆书产物（只存抽象结构，不存原文）
├── materials/                素材 / 片段 / 词表 / inspiration.md（PWA 回流）
├── feedback/                 stats.json · reviews/
├── profiles/                 平台 profile（番茄/起点/七猫）
├── exports/                  导出产物（gitignore）
└── .rag-index/               检索缓存（gitignore，可随时重建）
```

工作区根另有 `workbench/`（书架、共享素材、SQLite 用量库）与 `workbench-config/`（providers/routing/.env）。

---

## 关键机制

### 6 层上下文装配（§5）

| 层 | 内容 | 预算 | 缺失时 |
|---|---|---|---|
| L0 | 宪法 + 文风指纹 | ~1k | **硬错误，拒绝生成** |
| L1 | 章纲 + 出场人物卡全文 | ~3k | **硬错误** |
| L2 | 活跃伏笔（本章涉及 ∪ 到期 30 章内） | ~0.5k | 跳过 |
| L3 | 前 3 章摘要 + 上章尾部 500 字 | ~3k | 按实际裁剪 |
| L4 | 检索片段（带 `[ch-XXXX:段N]` 出处） | ~6k | 降级 BM25 → 跳过 |
| L5 | `@路径` 显式引用卡片 | 剩余 | 跳过 |

超预算按 **L4 → L3 → L2** 顺序裁剪，L0/L1 不可裁。装配结果完整落盘 `memory/ch-XXXX.assembled.md`，含**实际发送的 System/User Prompt**——任何一句话被写出，都能回答"它当时看到了什么"。

### 伏笔状态机

```
draft ──plant──> planted ──hint──> hinted ──resolve──> resolved
  └──────────────┴────────drop────────┴───> dropped
```

非法迁移被拒绝（API 409），超期（`current_ch > deadline_ch` 且未回收）在 Planner 阶段报警并给回收建议。

### 模型路由与 fallback

```yaml
plan:    [qoder-ultimate → glm-5.2 → official]
write:   [trae:pro → workbuddy:deepseek-v4-pro → deepseek-official]
review:  [workbuddy:kimi-k2.7 → deepseek-official]
```

- `429` → 熔断该桥 **10 分钟**，切下一跳
- `content_filter` → **先脱敏重试**（同 provider），再切跳
- `4001`/区域错配 → **不重试**
- `write` 链**禁止廉价模型收尾**，链尾自动补官方/本地
- 全链失败 → `HardError`，**不静默降级**
- **订阅桥必须 127.0.0.1**，公网地址在 Pydantic 层直接拒绝（防把凭据发给第三方中转）

### 反 AI 味（发布前强制）

1. **规则层**（零成本）：16 条套语替换 + 排比三连 + 句长方差
2. **模型层**（可选）：按文风指纹重写标记段落，产出 diff 待确认
3. **复读检测**：跨章 10-gram 指纹比对

关闭 2/3 时，导出会拦截并要求一次显式风险确认。

---

## CLI（无前端也能跑完整个闭环）

```bash
python -m server.cli --root ws init                # 初始化
python -m server.cli --root ws new "凡人问道"       # 建书（自动生成全部种子文件 + git）
python -m server.cli --root ws books               # 书架
python -m server.cli --root ws plan --count 5      # 章纲草案
python -m server.cli --root ws confirm 1 2 3       # 确认章纲
python -m server.cli --root ws write 1             # 写正文
python -m server.cli --root ws assemble 1 --show-prompt   # 只看装配
python -m server.cli --root ws review 1            # 审稿
python -m server.cli --root ws curate 1            # 沉淀提案
python -m server.cli --root ws curate-confirm 1    # 确认落盘 + git
python -m server.cli --root ws export --mode 合集   # 导出 TXT
python -m server.cli --root ws export --file-format docx  # 起点 DOCX
python -m server.cli --root ws publish 1 2         # 手动标记已发布（不代发）
python -m server.cli --root ws panel               # 日更面板
python -m server.cli --root ws usage --days 7      # 用量
python -m server.cli --root ws index build         # 建检索索引
python -m server.cli --root ws timeline            # 时间线（顺带校验单调性）
python -m server.cli --root ws foreshadow          # 伏笔池 + 超期报警
python -m server.cli --root ws name character -n 10  # 起名（skill/sect/place 同理）
python -m server.cli --root ws setting-book         # 设定集（M8）
python -m server.cli --root ws cover --width 600    # 封面 SVG（M8）
python -m server.cli --root ws pipeline --chapters 1-3   # 一条命令跑完
python -m server.cli --root ws serve               # 起 Web 服务
```

---

## REST API

完整列表见 `/docs`（自动生成）。主要端点：

```
POST /api/books                          建书
POST /api/books/{book}/plan              Planner 批次章纲
POST /api/books/{book}/generate          {task:"write",ch:42,stream:true} → SSE
POST /api/books/{book}/review            审稿
POST /api/books/{book}/curate            Curator 提案包
POST /api/books/{book}/curate/confirm    逐项确认落盘
GET  /api/books/{book}/truth/{path}      真相源读写
GET  /api/books/{book}/foreshadow        伏笔总览 + 超期
PATCH /api/books/{book}/foreshadow/{id}  改状态（非法迁移 409）
GET  /api/books/{book}/chapters/{ch}     正文 + 版本
POST /api/books/{book}/chapters/{ch}/version/{v}/adopt
GET  /api/providers · /api/providers/health · POST /api/routing/reload
GET  /api/usage?days=7
GET  /api/books/{book}/export/precheck   导出前预检（不落盘，供 UI 展示）
POST /api/books/{book}/export            导出 + 预检报告
GET  /api/books/{book}/daily-panel       日更面板
POST /api/books/{book}/feedback/*        反馈录入 + 复盘
POST /api/books/{book}/feedback/recheck  定向重审预览（不直接写入）
POST /api/books/{book}/feedback/recheck/confirm  按提案 ID 确认避雷条目
POST /api/books/{book}/publish            手动发布/撤销发布（不代发）
POST /api/books/{book}/benchmarks/*      拆书
GET  /api/books/{book}/review/{ch}/suggest   审稿改写预览（diff，不落盘）
POST /api/books/{book}/review/{ch}/suggest   只预览选中项
POST /api/books/{book}/names             起名（character/skill/sect/place）
GET  /api/books/{book}/names             起名词池
POST /api/books/{book}/inspiration       PWA 灵感到流
GET  /api/books/{book}/setting-book      设定集预览（11 节，不落盘）
POST /api/books/{book}/setting-book      生成设定集 → exports/{date}/
POST /api/books/{book}/cover             生成封面（默认程序化 SVG）
GET  /api/books/{book}/covers            已生成封面列表（只读）
GET  /api/books/{book}/cover/file        最新封面字节（供 <img>）
GET  /api/books/{book}/cover/prompt      外部生图提示词
GET  /api/books/{book}/templates         书内 + 共享题材模板库
GET  /api/templates                      工作区级模板库
```

---

## 测试

```bash
PYTHONPATH=vendor/pylib:. python3 -m pytest tests/ -q    # 305 项单测全绿
bash scripts/verify-all.sh 8793                          # 全量：单测 + 构建 + 服务 + API + 浏览器
bash scripts/make-demo.sh ws                             # 重建 8 章离线 demo
```

覆盖：路径/原子写/提案合并/伏笔状态机/时间线校验/git · 模型路由+熔断+脱敏+JSON 抽取+凭据边界 ·
6 层装配+预算裁剪+审计 · Planner/Writer/Reviewer/Curator/拆书 · 导出预检+反 AI 味+反馈复盘+统计 ·
REST+SSE 集成（含 M1 全链路验收）。

---

## 安全与合规

- 单账号，**不实现账号池**（封号红线）
- 订阅桥强制回环地址，凭据只走 `.env`（`.gitignore` 已含）
- 拆书只存抽象结构，不存原文
- 不做平台爬虫；反馈数据仅本地
- 自动发布**不做**，只导出

---

## 验证

完整验证过程与结果见 [`docs/VERIFICATION.md`](docs/VERIFICATION.md)，含：
- 213 项单测分布、前端构建产物
- 8 章端到端流水线产物核对表
- 15 个 REST 端点真实 HTTP 状态
- 14 个页面在真实 Chromium 的渲染结果与截图
- **开发中修复的 34 个真实缺陷**（可复现 + 有回归测试）
- 8 条未验证项（真实模型/embedding/PWA 真机等）

---

## 已知限制（如实声明）

1. **embedding/reranker 需要外部服务**（bge-m3 / bge-reranker）。未配置时检索自动降级为纯 BM25，流水线不中断。`HashEmbedder` 只是无依赖兜底，质量远低于真 embedding。
2. **虚构历法按 360 天/年、30 天/月**近似做单调性与差值校验，不解析真实农历。
3. **文档级真相源**（`world-state.md` 等）的分节替换按 Markdown 标题匹配，标题改名会让对应 patch 变成新增小节（不会丢内容，但会重复）。
4. **审稿规则层**只覆盖可量化项（字数/红线词/套语/复读/POV 标记/战力禁词）；OOC、剧情合理性依赖模型层 + 人工终审。
5. **定时任务默认关闭**，需在 `workbench-config/scheduler.yaml` 里显式 `enabled: true`。
6. `serve --reload` 在部分环境下与 APScheduler 的 reloader 冲突，生产用不带 reload。
7. **`mock-local` 是离线占位生成器**，正文由模板池拼装：句式重复率高、无剧情逻辑，只用于跑通链路与回归测试，**不要用它评估文笔**。真实写作必须在 `workbench-config/routing.yaml` 里配实际模型。
8. **跨章复读检测按「连续共同片段 ≥30 字」判定**，不做语义级改写识别；同义改写造成的隐性复读仍需人工/模型复审。
9. **前端无 UI 库**（手写 CSS），复杂表格/图表能力有限；字数曲线是 CSS 柱状图，不做交互缩放。
9b. **封面默认是程序化 SVG**（几何排版 + 题材配色），不是 AI 生图。要真图请用「作品集」页复制的提示词到外部生图工具，或自行在 `CoverGenerator` 里接图像 API——本实现不内置任何第三方生图 SDK。
10. **单机单用户**：没有鉴权层，只应绑 `127.0.0.1`。多设备请自行加反代与认证。
