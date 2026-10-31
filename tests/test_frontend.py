"""前端契约单测：服务端回传的告警必须真的显示出来。

后端把话说出口不等于作者看得见。这一文件盯的是「响应体里有 warning，前端却
把它丢掉」这类断链 —— Python 单测摸不到浏览器，但可以守住源码层面的契约：
调用方必须消费那个字段，且 toast 的 kind 有对应样式（否则带 kind 也只是个普通灰条）。
"""
from __future__ import annotations

import pathlib

WEB_ROOT = pathlib.Path(__file__).resolve().parent.parent / "web" / "src"

PWA_ROOT = pathlib.Path(__file__).resolve().parent.parent / "pwa"


def _read(rel: str) -> str:
    return (WEB_ROOT / rel).read_text(encoding="utf-8")


def _pwa(rel: str = "app.js") -> str:
    return (PWA_ROOT / rel).read_text(encoding="utf-8")


def _code(src: str) -> str:
    """去掉整行 `//` 注释再断言。

    缺陷 #111 的反向验证暴露了这个坑：注释里写了「deconstructor.warnings」，
    而 `"r.warnings" in body` 会被 `to` + `r.warnings` 这段注释命中 —— 把修复
    代码删掉测试照样绿，等于没有守卫。断言必须只看真代码。
    """
    return "\n".join(l for l in src.splitlines() if l.strip()[:2] != "//")

def _block(src: str, marker: str, limit: int = 2400) -> str:
    """从 `marker` 起切出一个函数体，直到下一个顶层语句。

    缺陷 #123 反向验证留下的教训：固定长度窗口（src[i:i+1200]）会把
    下一个函数也圈进来 —— runPolish 里删掉 warnings 消费，applyPolish 里的
    r.warnings 照样把断言喂饱，守卫就等于没有。切到下一个顶层声明为止，
    才能保证断言只针对这一个函数。
    """
    i = src.find(marker)
    if i < 0:
        return ""
    tail = src[i + len(marker) : i + len(marker) + limit]
    ends = [tail.find(k) for k in ("\nasync function ", "\nfunction ", "\nconst ", "\nonMounted(", "\nwatch(")]
    ends = [e for e in ends if e >= 0]
    if ends:
        tail = tail[: min(ends)]
    return _code(tail)


def test_publish_result_warning_is_shown():
    """缺陷 #105：`mark_published` 会回传断层告警（#70），前端必须显示它。

    修复前 DailyView.togglePublish 只认 `skipped`，作者点了「标记发布」看到的
    永远是「已标记发布第 N 章」，而读者那边是断的。
    """
    src = _read("views/DailyView.vue")
    i = src.find("togglePublish")
    assert i > 0, "DailyView.vue 里找不到 togglePublish"
    body = _code(src[i : i + 1200])
    assert "api.publish(" in body, "togglePublish 里没有 publish 调用"
    assert "r.warning" in body, (
        "togglePublish 没有消费 mark_published 回传的 warning —— "
        "断层告警会被静默吞掉（缺陷 #105）"
    )
    assert "store.toast" in body, "告警必须走 toast，不能只 console.log"


def test_toast_warn_kind_has_a_style():
    """toast 的 kind=warn 必须有样式，否则告警块长得和普通提示一样。

    store.toast 的第二个参数是 kind，App.vue 用 :class="t.kind" 渲染。
    少了 .toast.warn 这条规则，warn 就是个没有边框色的普通条 —— 等于没强调。
    """
    app = _read("App.vue")
    assert "t.kind" in app, "App.vue 不再按 kind 渲染 toast"
    assert ".toast.warn" in app, "App.vue 缺少 .toast.warn 样式：warn 级别的提示看不出来"

def test_truth_save_does_not_claim_git_success_blindly():
    """缺陷 #107：TruthView.save 不能无条件说「已提交 git」。

    服务端把 commit 结果放进响应体（commit.ok / commit.error）。修复前这句
    toast 是写死的字符串 —— git 挂了照样报「已提交 git」，作者以为真相源
    进了版本库，时光机上却查不到。
    """
    src = _read("views/TruthView.vue")
    i = src.find("async function save()")
    assert i > 0, "TruthView.vue 里找不到 save()"
    body = _code(src[i : i + 900])
    assert "r.commit" in body, "save() 没有读服务端回传的 commit 结果"
    # 注意：判据必须是「真代码里判了 ok」，不能写死 `commit.ok` ——
    # 这个字符串只出现在注释里，照注释断言会把修复删掉还绿（缺陷 #111 反向验证踩到）。
    assert "git.ok" in body, "save() 没有判断 commit 的 ok —— 会无条件声称已提交 git"
    assert "已提交 git" in body, "git 成功时的正常文案不应被改掉"

def test_benchmark_import_shows_half_success_warnings():
    """缺陷 #111：拆书「半成功」时不能只报章数字数。

    Deconstructor.analyze 在模型那一步挂掉时会把原因塞进 warnings
    （实测：`模型拆书失败（RuntimeError），仅启发式产物`），响应体也带回来了。
    修复前 doImport 只 toast「已拆解《X》：N 章 / M 字」——数字一样是好的，
    于是作者以为这是 AI 深度拆书的结果，拿它去调节奏。
    """
    src = _read("views/BenchmarkView.vue")
    i = src.find("async function doImport()")
    assert i > 0, "BenchmarkView.vue 里找不到 doImport()"
    body = src[i : i + 900]
    assert "api.benchmarkImport(" in body, "doImport 里没有拆书调用"
    assert "r.warnings" in body, (
        "doImport 没有消费拆书回传的 warnings —— 模型拆书失败会被静默吞掉（缺陷 #111）"
    )
    assert "store.toast" in body, "告警必须走 toast，不能只 console.log"
    assert "已拆解《" in body, "拆书成功的正常文案不应被改掉"



def test_daily_panel_shows_index_verify_result():
    """缺陷 #121：索引对账结果在 Web 端必须有出口。

    `GET /books/{book}/index/verify` 一直存在（漂移 / 缺章 / 孤儿 / #118 的
    实体数据源损坏），CLI 也会报警（#119），但整个 web/src 里从来没有一个
    调用点 —— 作者不开 CLI，实体图谱塌了一半，界面上依旧全绿。
    日更面板是每天必看的一页，对账结果挂这里。
    """
    src = _read("views/DailyView.vue")
    assert "api.indexVerify(" in src, "日更面板没有拉索引对账结果（缺陷 #121）"
    assert "indexProblems" in src, "拿到了对账结果却没算问题列表"

    # 四类问题都必须真的被 push 进列表。注意不能拿 find() 之后的全文做断言 ——
    # 那会把文件后半段任何一处同名字段也算进去（反向验证时删掉 push 照样绿）。
    i = src.find("const indexProblems")
    assert i > 0, "找不到 indexProblems 计算属性"
    body = _code(src[i : src.find("async function loadIndexHealth")])
    # 判据收紧到「同一行里既提到字段又真的 push」：只写 `if (v.drifted)`
    # 却不 push（或被提前 `return []` 短路）在语义上等于没展示，光看字段存在
    # 抓不到——这是缺陷 #111 反向验证留下的教训（注释/死代码会骗过 in 断言）。
    for field in ("error", "drifted", "missing", "orphaned", "entity_source"):
        hit = [l for l in body.splitlines() if field in l and "out.push" in l]
        assert hit, f"对账字段 {field} 没有被真的 push 进问题列表（缺陷 #121）"

    # 必须能一键重建，否则看见了也修不了
    assert "api.indexBuild(" in src, "对账没通过却不能一键重建索引"

    # client.js 得先有这个封装
    assert "indexVerify:" in _read("api/client.js"), "api/client.js 缺 indexVerify 封装"


def test_index_rebuild_reports_entity_source_problem():
    """重建索引时如果数据源还坏着，不能只报「已重建 N 章」。

    #118：entity-deltas.jsonl 损坏时 build() 照样成功、照样返回 entities，
    只是图谱是残的。build 的 `entity_source` 必须被消费，否则作者点完
    「重建索引」看到章数就以为修好了。
    """
    src = _read("views/DailyView.vue")
    i = src.find("async function rebuildIndex")
    assert i > 0, "DailyView.vue 里找不到 rebuildIndex()"
    body = _code(src[i : i + 900])
    assert "api.indexBuild(" in body, "rebuildIndex 里没有 indexBuild 调用"
    assert "entity_source" in body, (
        "rebuildIndex 没有消费 build 回传的 entity_source —— "

        "实体数据源还坏着却只报「已重建 N 章」（缺陷 #118/#121）"
    )


def test_index_verify_reports_missing_vectors_to_author():
    """缺陷 #127：embedding 没跑成时 hash 对账照样全绿，作者必须能看见。

    判据钉在「生效条件行」上（#126 的教训）：只要求源码里出现过 vector_source
    会被渲染体的 `{{ }}` 那行喂饱，把 v-if 改成 false 反向验证照样绿。
    """
    src = _read("views/DailyView.vue")
    i = src.find("const indexProblems")
    assert i > 0, "找不到 indexProblems 计算属性"
    body = _code(src[i : src.find("async function loadIndexHealth")])
    # 判据必须排除短路成 false 的分支：#126 反向验证时 `if (false && v.x)` 那行
    # 照样含字段名和 out.push，宽断言会被自己的死代码喂饱。
    hit = [
        l
        for l in body.splitlines()
        if "vector_source" in l and "out.push" in l and "false" not in l
    ]
    assert hit, "对账结果里的 vector_source 没有被 push 进问题列表（缺陷 #127）"


def test_index_rebuild_reports_missing_vectors():
    """重建索引后没有向量，不能只报「已重建 N 章」（缺陷 #127）。

    同样钉在生效条件上：只查字段名会被 `r?.entity_source` 那支分支喂饱。
    """
    src = _read("views/DailyView.vue")
    i = src.find("async function rebuildIndex")
    assert i > 0, "DailyView.vue 里找不到 rebuildIndex()"
    body = _code(src[i : i + 1200])
    branch = [
        l
        for l in body.splitlines()
        if "vector_source" in l and ("else if" in l or "if (" in l) and "false" not in l
    ]
    assert branch, "rebuildIndex 没有消费 build 回传的 vector_source（缺陷 #127）"
    assert "warn" in body, "向量缺失只是 warn，不该报成失败，但也不能是 ok"

def test_stream_done_warnings_are_shown_to_author():
    """缺陷 #122：流式生成的 done 事件带了 warnings，前端必须真的弹出来。

    服务端把告警塞进 SSE 的 done 事件只是第一步 —— WriteView.onDone 原先只弹
    「第 N 章生成完成，X 字」，字段名多了也没人读。告警链在最后一米断开的话，
    服务端改得再对作者依旧看不见。
    """
    src = _read("views/WriteView.vue")
    i = src.find("onDone")
    assert i > 0, "WriteView.vue 里找不到 onDone 回调"
    body = _code(src[i : i + 700])
    assert "generateStream(" in src, "WriteView 没有走流式生成"
    # 判据收紧到同一行：只出现 `p.warnings` 却不弹（比如只 console.log）等于没展示，
    # 这是缺陷 #111 反向验证留下的教训（注释 / 死代码会骗过 in 断言）。
    hit = [l for l in body.splitlines() if "p.warnings" in l]
    assert hit, "onDone 没有读 done 事件回传的 warnings（缺陷 #122）"
    # ws 是从 p.warnings 取出来的，必须被逐条弹出去（不能只是算出来放着）
    tail = body[body.find("p.warnings") :]
    assert [l for l in tail.splitlines() if "store.toast" in l], (
        "读到了 warnings 却不走 toast —— 作者看不到（缺陷 #122）"
    )


def test_polish_has_web_entry_point():
    """缺陷 #123：反 AI 味管线在 Web 端必须真的够得着。

    修复前 client.js 里有 polish() 封装，却没有任何 view 调用它 ——
    §9.2 承诺的「发布前强制跑反 AI 味管线」在浏览器里根本没法触发，
    ReviewView 的提示「去写作台跑反 AI 味润色」指向一个不存在的按钮。
    """
    src = _read("views/WriteView.vue")
    i = src.find("async function runPolish")
    assert i > 0, "WriteView.vue 里找不到 runPolish() —— 反 AI 味管线在 Web 端没有入口（缺陷 #123）"
    body = _block(src, "async function runPolish")
    assert "api.polish(" in body, "runPolish 里没有调用 api.polish（缺陷 #123）"
    # 告警必须逐条弹出来：模型层挂掉时服务端只回 warnings（规则层结果已保留），
    # 前端不消费就等于作者以为「润色过」，实际只跑了规则层。
    hit = [l for l in body.splitlines() if "r.warnings" in l]
    assert hit, "runPolish 没有读 polish 回传的 warnings（缺陷 #123）"
    tail = body[body.find("r.warnings") :]
    assert [l for l in tail.splitlines() if "store.toast" in l], (
        "读到了 warnings 却不走 toast —— 作者看不到（缺陷 #123）"
    )


def test_polish_apply_requires_confirmation_and_rejects_published():
    """润色落盘必须先确认，且已发布章不许改（与后端一致）。

    后端 polish(apply=True) 对 published 章抛 ValueError → 400；前端也要提前拦，
    否则作者点了「应用到正文」只看到一句一闪而过的错误 toast。
    """
    src = _read("views/WriteView.vue")
    i = src.find("async function applyPolish")
    assert i > 0, "WriteView.vue 里找不到 applyPolish()（缺陷 #123）"
    body = _block(src, "async function applyPolish")
    assert "window.confirm(" in body, "润色落盘没有二次确认 —— 改写正文不该是一键的（缺陷 #123）"
    assert "published" in body, "applyPolish 没有拦已发布章（缺陷 #123）"
    assert "api.polish(" in body, "applyPolish 里没有调用 api.polish（缺陷 #123）"


def test_export_success_reports_unresolved_high():
    """缺陷 #123：导出成功时也要把未处理的高优问题说出来。

    修复前 unresolved_high 只在「导出被拦截」分支被渲染 —— 而套语 / 复读不会
    拦截导出，于是成功导出的产物里带着套语，作者却一句提示都没收到。
    """
    src = _read("views/DailyView.vue")
    i = src.find("async function doExport")
    assert i > 0, "DailyView.vue 里找不到 doExport()"
    body = _block(src, "async function doExport")
    assert "api.exportBook(" in body, "doExport 里没有 exportBook 调用"
    # 关键：unresolved_high 在「被拦截」分支里本来就出现过（blockedInfo.unresolved），
    # 所以只断言「body 里有 unresolved_high」是假绿 —— 删掉成功分支照样绿。
    # 判据必须钉在成功分支上：导出成功的那句 toast 之后必须再读一次
    # unresolved_high，并再走一次 toast。
    ok_at = body.find("已导出")
    assert ok_at > 0, "doExport 里找不到导出成功的 toast（缺陷 #123）"
    tail = body[ok_at:]
    hit = [l for l in tail.splitlines() if "unresolved_high" in l]
    assert hit, (
        "导出成功分支没有读 unresolved_high —— 套语 / 复读不会被拦下，"
        "作者却一句提示都收不到（缺陷 #123）"
    )
    assert [l for l in tail.splitlines() if "store.toast" in l and "'warn'" in l], (
        "读到了 unresolved_high 却不走 warn toast —— 作者看不到（缺陷 #123）"
    )




# 缺陷 #126：打标失败必须有前端出口（否则作者只在响应体里能看到，界面照说「已提交」）
def test_tag_failure_has_a_frontend_exit():
    """服务端说了真话没用——前端不展示就等于没说。

    实测：修复前全仓 ``grep tag`` 在 web/src 下没有任何 commit 结果消费点展示 tag，
    CurateView 只显示 ``commit.ok ? sha : '失败 ' + error``，TruthView 只判 ``git.ok``。
    新加的 tag_error 就算进了响应体，界面上也照样写着「已提交」。

    判据必须钉在**渲染条件**上，不能钉在「文件里出现过 tag_error」。
    反向验证实测过两版假守卫：
    - 只要『含 tag_error 且含 {{ 』→ v-if 改成 false 后，渲染体那行照样命中，假绿；
    - 只要『函数体里出现过 tag_error』→ 赋值改成 `false && ...` 后照样命中，假绿。
    所以这里一条查 v-if 条件本身，一条查赋值右值不能被 false 短路。
    """
    cur = _code(_read('views/CurateView.vue'))
    blk = _block(cur, 'result-box', limit=900)
    cond = [ln for ln in blk.splitlines() if 'v-if' in ln and 'tag_error' in ln]
    assert cond, f"CurateView 的 v-if 条件里没有 tag_error（渲染不出来）：{blk[:400]}"
    assert 'false' not in cond[0], f"v-if 被短路成 false：{cond[0]}"

    # TruthView 走 toast：赋值右值必须真的读 tag_error，不能被 `false &&` 短路，
    # 且拼出来的提示必须是 warn 级（打标失败用绿字 = 把告警当喜报）
    truth = _block(_code(_read('views/TruthView.vue')), 'function save(', limit=1400)
    assign = [ln for ln in truth.splitlines() if 'tagTail' in ln and '=' in ln]
    assert assign, f"TruthView 的 save() 没有消费 tag_error：{truth[:400]}"
    rhs = assign[0].split('=', 1)[1]
    assert 'tag_error' in rhs and not rhs.lstrip().startswith('false'), \
        f"tagTail 的右值被短路或没读 tag_error：{assign[0]}"
    assert 'warn' in truth, "打标失败却没走 warn 级提示（缺陷 #126）"

def test_pwa_first_screen_binds_input_before_the_first_await():
    """缺陷 #124：PWA 首屏必须在第一个 await 之前就绑好输入。

    修复前 DOMContentLoaded 里先 `await openDB()/render()/loadProgress()`，
    之后才给 #save/#sync/#nightly 绑 click。首屏那两个 /api 请求慢一点（或断网），
    按钮就处于「看得见、点得动、一个字都不存」的状态 —— 作者只会以为自己没点对，
    灵感就这么丢了。offline 监听（#49）是同一个首屏竞态的另一端，一并守住。
    """
    body = _code(_pwa())
    i = body.find("DOMContentLoaded")
    assert i > 0, "pwa/app.js 里找不到 DOMContentLoaded 入口"
    entry = body[i:]

    # 注意：必须锚定**顶层**那句 `db = await openDB()`。
    # save 处理里也有一句 `if (!db) db = await openDB();`（绑得早之后的兜底），
    # 只 find("await openDB()") 会命中它 —— 那会让守卫在修复正确的代码上也报错。
    db_at = entry.find("\n  db = await openDB();")
    prog_at = entry.find("await loadProgress()")
    save_at = entry.find("getElementById('save').addEventListener")
    sync_at = entry.find("getElementById('sync').addEventListener")
    night_at = entry.find("getElementById('nightly').addEventListener")
    offline_at = entry.find("addEventListener('offline'")

    for name, at in (("save", save_at), ("sync", sync_at), ("nightly", night_at),
                     ("offline", offline_at)):
        assert at > 0, f"PWA 入口里没有绑 {name}（缺陷 #124）"

    assert db_at > 0, "PWA 入口里找不到 await openDB()"
    assert max(save_at, sync_at, night_at, offline_at) < db_at, (
        "输入绑定排在了 await openDB() 之后 —— 首屏请求没回来之前按钮是死的（缺陷 #124）"
    )
    if prog_at > 0:
        assert max(save_at, sync_at, night_at) < prog_at, (
            "按钮绑在了 await loadProgress() 之后 —— 服务端一慢，作者点不动（缺陷 #124）"
        )



def test_pwa_nightly_alerts_on_index_vector_source():
    """缺陷 #129：PWA triggerNightly 必须把 index_verify.vector_source 也弹出来。

    服务端 nightly 返回 {reviewed, ok, warnings, index, index_verify}。
    前端只消费了 warnings，但 embedding 没跑成时 vector_source 在
    index_verify 里而不在 warnings 里 —— 作者点了「夜审」却不知道
    索引悄悄退回纯 BM25。
    """
    src = _pwa()
    # 找 triggerNightly 函数体
    idx = src.find("async function triggerNightly")
    assert idx > 0, "找不到 triggerNightly"
    body = src[idx:idx + 3000]
    # 必须消费 index_verify.vector_source
    assert "index_verify" in body, "triggerNightly 没读 index_verify"
    assert "vector_source" in body, "triggerNightly 没消费 vector_source"
    assert "iv.vector_source" in body or "index_verify.vector_source" in body, \
        "triggerNightly 没读 index_verify.vector_source"
