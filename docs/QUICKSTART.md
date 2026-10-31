# 快速上手（10 分钟）

## 0. 前提

- Python 3.11+、Node 18+（只用 Python 也能跑完全部功能，前端可选）
- 本机受限环境（无 venv、`/home/yan/.cache` 只读）时依赖要装到 `vendor/pylib`

## 1. 装依赖

```bash
pip install --target vendor/pylib -r requirements.txt
cd web && npm install && cd ..
```

## 2. 建工作区并跑一本 8 章的书（不花钱）

```bash
export PYTHONPATH=vendor/pylib:.
python3 -m server.cli --root ws init
python3 -m server.cli --root ws new "凡人问道"
python3 -m server.cli --root ws pipeline --chapters 1-8
```

`pipeline` 会一次跑完 **章纲 → 确认 → 正文 → 审稿 → 沉淀 → 确认落盘 + git 提交**。

> 默认路由指向 `mock-local`（离线占位生成器，不联网、不花钱）。
> **要看真实文笔，必须换真实模型**，见第 5 步。

## 3. 起 Web UI

```bash
python3 -m server.cli --root ws serve --port 8765
# 打开 http://127.0.0.1:8765
```

前端没构建时 `/` 会返回 JSON 提示；`/pwa/index.html` 是可用的移动端灵感速记页。

## 4. 看看能做什么

| 页面 | 用途 |
|---|---|
| 书架 | 多书管理、新建书 |
| 章纲 | 批量生成章纲草案 → 逐章校对 → 确认后才能写作 |
| 写正文 | 6 层上下文装配 + 流式生成 + 版本留存 |
| 审稿 | 规则层（字数/红线/套语/复读/POV/战力）+ 模型层，**每条问题都带正文证据** |
| 沉淀 | 模型产出提案 → 你勾选 → 确认后才落盘（唯一写入口） |
| 伏笔 | 状态机 `draft→planted→hinted→resolved\|dropped`，超期报警 |
| 日更 | 存稿箱三级缓冲、断更预警、字数曲线、**导出预检** |
| 真相源 | 7 份真相文件的在线编辑 + git 历史回溯 |
| 拆书 | 导入对标书 → 产出节奏/钩子/模板（只存抽象结构，不存原文） |
| 起名 | 角色/功法/宗门/地名，规则生成、seed 可复现、产物进 `materials/dictionary/` |
| 反馈 | 日数据 + 章评 → 高弃章归因 → 避雷建议 |
| 用量 | 调用次数/token/成本/订阅池占比 |
| 模型路由 | 任务级 fallback 链、探活、热重载 |

## 5. 接真实模型

`ws/workbench-config/providers.yaml` 在 `init` 时已生成，默认含 5 个 provider
（`mock-local` / `workbuddy-local:8787` / `qoder-local:8963` / `trae-local:19950` /
`deepseek-official`），按需改 `enabled` 和模型名即可。

`ws/workbench-config/providers.yaml`：

```yaml
providers:
  - name: workbuddy-local
    kind: openai_compat
    base_url: http://127.0.0.1:8787
    bridge: workbuddy2api
    is_local_bridge: true          # §7.3：订阅桥必须回环（默认已满足）
    models: ["glm-5.2", "kimi-k2.7", "deepseek-v4-pro"]
    first_system_only: true        # 国际版接口只接受首条 system
    enabled: true

  - name: deepseek-official
    kind: openai_compat
    base_url: https://api.deepseek.com
    bridge: deepseek
    api_key_env: DEEPSEEK_API_KEY
    is_local_bridge: false         # 官方 API 允许公网
    models: ["deepseek-chat"]
    enabled: true
```

`ws/workbench-config/.env`：

```
DEEPSEEK_API_KEY=sk-...
```

`ws/workbench-config/routing.yaml`（**链尾必须是官方 API 或本地桥**，不能停在订阅桥上）：

```yaml
routes:
  plan:       ["workbuddy-local:glm-5.2", "deepseek-official:deepseek-chat"]
  write:      ["workbuddy-local:glm-5.2", "deepseek-official:deepseek-chat"]
  review:     ["deepseek-official:deepseek-chat"]
  curator:    ["deepseek-official:deepseek-chat"]
  polish:     ["deepseek-official:deepseek-chat"]
  summarize:  ["deepseek-official:deepseek-chat"]
  deconstruct:["deepseek-official:deepseek-chat"]
  free:       ["deepseek-official:deepseek-chat"]
```

改完在「模型路由」页点 **热重载配置**，或 `POST /api/routing/reload`。

## 6. 接检索（可选）

```bash
# 建索引（FTS5，无需外部服务）
python3 -m server.cli --root ws index build

# 配 embedding 后会升级为混合检索（RRF 融合）
```

```yaml
# ws/workbench-config/providers.yaml 追加
embedding:
  provider: openai
  base_url: http://127.0.0.1:9997
  api_key: local
  model: bge-m3
```

未配置时检索自动降级为纯 BM25，**流水线不中断**。

## 7. 日常命令

```bash
python3 -m server.cli --root ws write 9          # 写第 9 章
python3 -m server.cli --root ws review 9         # 审稿
python3 -m server.cli --root ws curate 9         # 出提案
python3 -m server.cli --root ws curate-confirm 9 # 确认落盘（会 git commit）
python3 -m server.cli --root ws panel            # 今天该写几章
python3 -m server.cli --root ws timeline         # 时间线有没有矛盾
python3 -m server.cli --root ws foreshadow       # 哪些伏笔快到期
python3 -m server.cli --root ws export --mode 合集
python3 -m server.cli --root ws name character -n 10 --seed 42   # 起名（--no-save 只看不存）
```

## 7.5 给文风加范例片段（可选）

§5.3 的 few-shot：把**你自己写的**满意片段放进 `materials/snippets/<类型>/`，
装配时按场景类型自动取 1–2 条注入（与检索共用预算，最多占 1/3）。

```bash
mkdir -p ws/novels/novel-《书名》/materials/snippets/{combat,emotion,scene,dialogue}
# 每个文件就是一段正文（.md），建议 200–800 字
```

章纲里写 `style_ref: combat/my-fight.md` 可强制指定某条（优先级最高）。
范例只用于学句式与节奏，prompt 里明确禁止复用其人物、地名、情节。 由 `test_prompt_forbids_reusing_fewshot_content` 钉住（此前 tests/ 下零命中——防抄袭护栏被顺手删掉一行也不会有任何测试变红）；反向验证：把该句弱化为「不要抄袭它」测试立即失败。

## 8. 出问题怎么查

| 现象 | 检查 |
|---|---|
| 生成报「全部路由失败」 | `GET /api/providers/health?probe=true`；看 `ws/serve.log` |
| 装配报「L0 宪法为空」 | 打开「真相源」填 `constitution.md` |
| 装配报「L1 章纲为空」 | 章纲必须先 `confirm`（`draft → planned`） |
| 检索一直 `mode=off` | `python3 -m server.cli --root ws index build` |
| 导出被拦截 | 看响应里的 `block_reason`；`allow_risk: true` 可强制 |
| 前端白屏 | `cd web && npx vite build`；看浏览器 console |
