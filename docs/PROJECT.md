# 项目结构与设计要点

## 目录

```
moran/
├── server/                     后端（46 个模块）
│   ├── core/                   无 IO 的领域逻辑 + 文件真相源
│   │   ├── paths.py            NovelPaths / WorkbenchPaths：所有路径的唯一来源
│   │   ├── atomic.py           原子写（mkstemp→fsync→os.replace→fsync 目录）
│   │   ├── schema.py           pydantic v2 模型（extra="allow" 宽容读取）
│   │   ├── store.py            NovelStore（唯一文件读写入口，resolve 拒逃逸）
│   │   ├── domain.py           纯逻辑：伏笔状态机 / 时间线校验 / 战力校验
│   │   └── gitbot.py           每书一 repo，失败不抛
│   ├── model/                  模型层
│   │   ├── base.py             请求/结果/异常层级
│   │   ├── gateway.py          路由 fallback + 熔断 + 脱敏重试
│   │   ├── normalizer.py       JSON 抽取 / 消息归一 / token 估算
│   │   ├── usage.py            调用记录 + 成本统计（SQLite）
│   │   └── providers/          openai_compat / mock / registry
│   ├── context/assembler.py    6 层上下文装配 + 预算裁剪 + 审计落盘
│   ├── retrieval/              可替换检索（分块/FTS5/向量/RRF/GraphRAG/门面）
│   ├── agents/                 planner / writer / reviewer / curator / deconstructor / naming
│   ├── platform/               导出（预检+红线）/ 反 AI 味润色 / 改写建议（diff）
│   ├── feedback/               读者反馈录入 + 高弃章归因
│   ├── stats/                  统计 + APScheduler 定时任务
│   ├── api/                    deps（惰性构造）+ routes（60+ 端点）
│   ├── cli.py                  init/new/plan/write/review/curate/export/panel/timeline/foreshadow/pipeline/serve
│   └── app.py                  FastAPI 装配（路由顺序 + 静态 + SPA fallback）
├── web/                        Vue 3 + Vite（14 个视图，无 UI 库）
├── pwa/                        移动端灵感速记（IndexedDB 离线队列）
├── scripts/                    make-demo / verify-all / ui-smoke / click-precheck / make-benchmark
├── tests/                      269 项测试 / 11 个文件
├── docs/                       QUICKSTART / VERIFICATION / PROJECT / screenshots
└── ws/                         运行工作区（不入库）
```

## 一本书的骨架

```
ws/
├── novels/
│   └── novel-《凡人问道》/       ← 一本书 = 一个 git 仓库
│       ├── book.json           元数据 + 目标（题材/平台/字数/主角）
│       ├── truth/              7 份真相文件（唯一事实来源）
│       │   ├── constitution.md        项目宪法（世界观/叙事/文风红线）
│       │   ├── style-fingerprint.md   文风指纹（含「忌」清单）
│       │   ├── world/                 世界设定（力量体系/阵营/地理/规则）
│       │   ├── world-state.md         当前世界状态（动态）
│       │   ├── characters/*.yaml      角色卡（含 power_scale）
│       │   ├── foreshadow.json        伏笔池（状态机）
│       │   ├── timeline.yaml          时间线（单调性校验）
│       │   ├── resources.json         资源账本
│       │   └── arcs.md                卷/弧
│       ├── outline/
│       │   ├── master-outline.md      总纲
│       │   ├── volume-01.md           卷纲
│       │   ├── threads/*.yaml         主线/支线
│       │   └── chapters/ch-0001.json  章纲
│       ├── chapters/ch-0001.md        正文
│       ├── memory/                    章摘要 / 审稿报告 / 装配审计 / 提案包
│       ├── analysis/benchmarks/       拆书产物（只存结构，不存原文）
│       ├── exports/                   导出（gitignore）
│       └── .rag-index/                FTS5 索引（gitignore）
└── workbench-config/          providers.yaml / routing.yaml / .env
```

## 关键设计决策（以及为什么）

### 1. 文件即真相源，git 即审计

不引入数据库存内容。一本书就是一个目录，任何编辑器可开、任何 diff 工具可比。
`GitBot` 在每次「确认沉淀」和每次「章定稿」时自动提交并打 tag（`ch/0001`），
所以**任何历史状态都可回溯**，`git show ch/0003:chapters/ch-0003.md` 就能拿到当时正文。

代价：并发写不安全（单用户场景可接受），大书（万章级）git 会变慢。

### 2. 唯一写入口：提案 → 确认

Curator 只**产出提案**（`memory/proposals-XXXX.json`），不直接改真相源。
作者勾选后调 `confirm()`，按白名单逐条 `apply_proposal`，然后 git 提交。
`PROPOSAL_FILE_PREFIXES`（`curator.ALLOWED_PREFIXES` 是它的别名）之外的提案在生成时就被丢弃——模型不能写任意文件。

这是「AI 提议、人来拍板」的物理保证，不是靠 prompt 约束。

> ⚠️ 白名单不只挂在**生成端**：此前只靠 `_to_proposals` 丢弃，执行端（`store.apply_proposal`）
> 不查，于是绕过生成端的提案能把 `book.json` 的标题改掉、把正文整章覆盖、把整本书删掉。
> 缺陷 #103/#104 后，四类 op 在**落盘那一步**统一过白名单（越界抛 `StoreError`）。
> 守卫：`tests/test_agents.py::test_proposal_delete_is_restricted_to_truth_files`
> 与 `test_proposal_write_is_restricted_to_truth_files`。作者自己改正文走
> `PUT /chapters`，不经提案，不受此限。

### 3. 6 层上下文，硬上限 16k
> 守卫：`tests/test_context.py::test_context_budget_is_hard_and_trim_reports` —— 超预算必须告警（不许静默截断），L0/L1 不可裁。

| 层 | 内容 | 预算 | 裁剪优先级 |
|---|---|---|---|
| L0 | 宪法 + 文风指纹 | 1000 | 不裁（空了拒生成） |
| L1 | 本章章纲 | 3000 | 不裁（空了拒生成） |
| L2 | 活跃伏笔提醒 | 500 | 第 3 个被裁 |
| L3 | 前 3 章摘要 + 上章尾部 | 3000 | 第 2 个被裁 |
| L4 | 检索素材 | 6000 | **第 1 个被裁** |
| L4f | few-shot 范例片段（§5.3） | 共用 L4 的 1/3 | 第 2 个被裁 |
| L5 | `@路径` 显式引用 | 余量 | 不裁 |

L0/L1 空时**直接抛 `AssemblyError`**，不静默生成——没有宪法的正文不该产出。
L4 检索任何失败都降级为 warning，永不阻断（检索是增强，不是前提）。

每章装配结果落 `memory/ch-XXXX.assembled.md`，含分层用量、告警、**实际发送的完整 prompt**。

### 4. 审稿必须带证据

`LITERAL_EVIDENCE_CATEGORIES`（OOC/战力/复读/AI味/红线/时间线）强制要求模型给的
`evidence` 是正文里的字面子串，否则整条丢弃。

⚠️ **实现比这里写的宽松**（`reviewer._verify_evidence`）：只取 evidence 的**前 8 个非空白字符**
做包含判定，且 `len(probe) < 2` 直接豁免。所以「前 8 字真 + 后半编造」仍会放行——
这是有意的容错（模型常截断/加省略号），但别指望它能挡住拼接式幻觉。
边界由 `test_verify_evidence_only_checks_first_8_chars` 钉住，改动实现会变红。

这条规则来自实测：文风指纹写着「忌：眼中闪过一丝」，模型把它当成正文命中，
报出 high 级问题，而正文里根本没有这个词。

### 5. 订阅桥强制回环

§7.3 的安全红线：`is_local_bridge: true` 的 provider 必须指向 `127.0.0.1`/`localhost`/`::1`，
否则 `ProviderConfigError` 拒绝启动。官方 API（`is_local_bridge: false`）才允许公网。

用意：订阅桥是把网页版额度转成 API 的本地代理，暴露到公网等于把账号送人。

### 6. 失败不静默降级

- 模型全链失败 → `HardError`，不偷偷用 mock 顶替
- 检索不可用 → `mode="off"`，但装配继续（记 warning）
- git 失败 → `CommitResult(ok=False, error=...)`，不抛；正文照样保存（由 `test_git_failure_returns_commit_result_not_raise` 钉住：此前零测试，一旦改成抛异常，写章流程会整段崩、正文跟着丢）
- 定时任务失败 → 记日志，不阻断服务

区别在「什么是核心」（生成/装配）和「什么是增强」（检索/提交/定时）。

## 扩展点

| 想做什么 | 改哪里 |
|---|---|
| 加新模型供应商 | `providers.yaml` 加一条（`kind: openai_compat` 即可） |
| 换 embedding | `providers.yaml` 的 `embedding` 段 + `retrieval/embedder.py` |
| 加审稿规则 | `agents/reviewer.py` 的 `_rule_*` + `VALID_CATEGORIES` |
| 加平台 profile | `core/store.py` 的 `PROFILE_PRESETS` 或直接写 `ws/workbench-config` |
| 换前端 | 后端是纯 REST，`server/api/routes.py` 就是契约 |
| 加 CLI 子命令 | `cli.py` 加 `cmd_*` + `sub.add_parser` |
| 改起名用字 | `agents/naming.py` 的 `SURNAMES` / `GIVEN_*` / `SKILL_*` / `SECT_*` / `PLACE_*` |
