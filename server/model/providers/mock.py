"""本地 mock provider —— 无网、无 Key 也能把整条流水线跑通。

存在意义：
1. 集成测试必须能在离线下验证「建书→章纲→生成→审稿→Curator→导出」全链路；
2. 作者没有配好 Key 时，UI 仍可演示与验收；
3. 它是**确定性**的（同一 prompt 同一输出），所以可以作为回归基线。

它按 task 分支产出结构化数据，尽力模仿真实模型的输出形状。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, AsyncIterator

from ..base import CompletionRequest, CompletionResult, StreamChunk

SURNAMES = ["林", "萧", "叶", "秦", "顾", "沈", "江", "楚", "云", "陆"]
GIVEN = ["澈", "尘", "寒", "轩", "渊", "霄", "岚", "川", "陌", "然"]
SCENE_LOCS = ["青岩矿脉", "落霞城集", "青云山门", "断崖栈道", "藏经阁三层", "黑水渡口"]
CHAPTER_TITLES = [
    "淬骨", "试锋", "矿脉", "夜行", "破境", "横刀", "雪夜", "问心",
    "旧账", "断崖", "换命", "空棺", "石语", "残页", "风口", "落锁",
]
#: 时间线索模板：_extract_date 只认「XX历 N年 N月 N日」这种写法
DATE_LINES = [
    "青云历{year}年{month}月{day}，矿上照例要清点人数。",
    "墙上刻着青云历{year}年{month}月{day}的字样，笔画很浅。",
    "他记得清楚，青云历{year}年{month}月{day}，那天下的是雨。",
]
_CN_NUM = "零一二三四五六七八九十"


def cn_day(n: int) -> str:
    """1–30 → 中文日（初一…初十、十一…二十、廿一…三十）。"""
    n = max(1, min(30, n))
    if n <= 10:
        return "初" + _CN_NUM[n]
    if n < 20:
        return "十" + _CN_NUM[n - 10]
    if n == 20:
        return "二十"
    if n < 30:
        return "廿" + _CN_NUM[n - 20]
    return "三十"

#: 场景池按地点分类（章纲给了地点就用对应池，没给就用通用池）
SCENERY_BY_PLACE = {
    "mine": [
        "矿洞深处滴水声一下一下，像有人在数息。",
        "矿灯的火苗歪向一侧，说明前面有风。",
        "石壁上留着上一班的刻痕，歪歪扭扭记到十七。",
        "矿道里有股潮冷的铁腥味，",
        "头顶的岩层在渗水，一滴一滴，落在同一处。",
        "脚下的碎石硌得生疼，",
        "井口的辘轳空转了半圈，绳子垂着，没人接。",
    ],
    "cliff": [
        "山风从崖口灌进来，带着碎雪的味道。",
        "远处的山脊压着云，像有人把刀横在那儿。",
        "碎石从崖边滚下去，很久才听见回响。",
        "风从更深的谷底翻上来，卷着土腥味。",
    ],
    "ferry": [
        "渡口的缆绳绷得很紧，船身贴着岸一沉一浮。",
        "水面上浮着一层薄雾，对岸的灯只剩一个亮点。",
        "船工把竹篙插进水里，抽出来时带起一串泥。",
        "岸边的木桩被水泡得发黑，拴着七八根绳。",
    ],
    "town": [
        "暮色把青岩染成暗红，远处宗门钟声敲了七下。",
        "集市上的叫卖声一层压一层，混着油炸的味道。",
        "巷子两边的屋檐几乎碰到一起，只漏下一线天。",
        "摊子收了一半，剩下的在暗处等着客人。",
        "青石板被踩得发亮，缝隙里嵌着菜叶和鱼鳞。",
    ],
    "gate": [
        "石阶上结着薄冰，每一步都要先用脚尖试过。",
        "山门的匾额被雨水磨得发白，字迹还认得出。",
        "两尊石狮蹲在门侧，爪子上积着落叶。",
        "门轴锈住了，推开时声音拖得很长。",
    ],
    "snow": [
        "雪落在石面上不化，被风一层层压实。",
        "呼出的气立刻变成白，转眼就散了。",
        "脚印踩出去不到十步就被雪填平。",
    ],
    "forest": [
        "林子里的光被枝叶切成碎片，落在肩上一晃一晃。",
        "脚下积着厚厚的腐叶，踩下去软得没底。",
        "有鸟突然飞起来，翅膀扑得很急。",
    ],
}

#: 细节收束语：作为第三独立成分追加到叙述段，把两池组合（数百种）放大到数万种。
#: 相邻章共用同一批池子时，没有第三成分会撞出 60+ 字的整段重复（实测过）。
DETAILS = [
    "袖口磨破了边。", "靴底沾着一层泥。", "指缝里有洗不掉的黑。",
    "后颈的汗凉了。", "腰间的绳子勒得发紧。", "衣角被风掀起又落下。",
    "肩上的旧伤隐隐发酸。", "舌根泛着苦味。", "耳后有一道浅疤。",
    "腰牌贴着皮肉，是凉的。", "手里的东西比看上去沉。", "呼吸慢慢匀了。",
    "头发被汗黏在额角。", "膝盖上还留着旧淤。", "掌心的茧又厚了一层。",
    "靴子里的沙子硌脚。", "喉咙里像卡了根刺。", "背后一片湿。",
    "刀鞘上的漆掉了一块。", "衣襟上沾了灰。", "手腕转了两圈。",
    "脚下换了重心。", "他数了数自己的心跳。", "牙齿咬得很紧。",
    "额角的青筋跳了一下。", "手指在袖子里收紧。", "衣摆扫过地面。",
    "他舔了舔干裂的嘴唇。", "目光在四周扫了一圈。", "耳朵动了一下。",
    "他把气缓缓吐尽。", "肩上落了点灰。", "他往前挪了半步。",
    "脚趾在靴子里蜷起。", "手上的力道松了一分。", "他微微低下头。",
    "影子被拉得很长。", "呼吸停了半拍。", "他咬了咬牙。",
    "眼睛适应了暗。", "他侧过身。", "腰背挺直了。",
    "他眯起眼睛。", "指尖压出白痕。", "他慢慢松开手。",
    "他垂下手。", "他站定了。", "他轻轻吐了口气。",
]

#: 内心戏的收束尾句：与 INTERIORS 组合，把单池采样变成双池
INNER_TAILS = [
    "他没让自己多想。",
    "这句话他说不出口。",
    "想明白这点，脚步反而慢了。",
    "可他知道，想归想。",
    "最后他什么都没做。",
    "他把这个念头压回去了。",
    "这大概就是所谓的成长。",
    "但他还是记住了。",
    "至少现在还不能。",
    "他忽然觉得有点好笑。",
    "有些事只能往后放。",
    "他决定先不看。",
    "这一次他选择相信。",
    "他把这句话留给了自己。",
]

HOOKS = [
    "矿洞深处传来诵经声，{hero}停住了脚。",
    "那张黄纸上的名字被水泡开了——只剩一个姓。",
    "他数到第十七个刻痕时，石壁后面有人敲了三下。",
    "灯笼灭了，但脚步声没停，反而更近了。",
    "师父的剑，怎么会插在这里？",
    "远处的钟声只响了六下。有人改了规矩。",
    "他忽然想起，这个人三年前就该死了。",
    "门开了一条缝，里面递出来的不是矿牌，是一只手。",
    "矿灯照到尽头，是一堵不该存在的墙。",
    "有人在他背后，用他自己的声音说了一句话。",
    "血滴在地上，没有渗进去，反而浮了起来。",
    "那块矿牌上的编号，和他腰间的一模一样。",
    "他伸手去扶，摸到的却是冷的。",
    "名字被划掉了，但墨迹是新的。",
    "回到住处时，桌上多了一碗还热着的面。",
    "所有人都跪下了，只有他站着。",
]
ACTIONS = [
    "握紧了拳", "缓缓吐出一口气", "抬眼看向远处", "把剑收了半寸", "指尖敲了敲石壁",
    "压低声音", "侧身让开半步", "把矿镐换了只手", "垂眼看着地面", "退到石壁的阴影里",
    "抬手抹掉额角的灰", "把腰间的矿牌按了按", "停住脚步", "回头看了一眼来路",
]
LINES = [
    "谁？", "让开。", "这话你也信？", "再往前一步试试。", "矿牌呢？",
    "你的名字，写在这张纸上。", "这条道，今天不通。", "把东西放下，人可以走。",
    "我要见你们管事的。", "三年前那笔账，该算了。",
    "你手里的东西，不属于你。", "我数到三。", "别点灯。",
    "下面还有人吗？", "这话我只说一次。", "你走错道了。",
]
WEAPONS = ["剑", "矿镐", "短刀", "石斧", "断矛", "铁尺"]
#: 通用体感（任何场景都能用）
SENSES = [
    "风从更深的地方灌上来，", "灯芯爆了一下，", "背后的衣料湿了一片，",
    "指尖的旧伤又开始跳，", "空气忽然变稠，", "有人在极近的地方咳了一声，",
    "手心的汗渗进掌心，", "远处传来一声闷响，", "呼吸在耳边显得很响，",
    "有什么在看不见的地方移动，", "凉意从脚底往上爬，", "嗓子干得发紧，",
]

#: 分地点体感（章纲给了地点就用对应池，避免「渡口」写矿道）
SENSES_BY_PLACE = {
    "mine": [
        "矿道里有股潮冷的铁腥味，", "脚下的碎石硌得生疼，",
        "石壁上渗出的水珠落在肩上，", "远处传来有节奏的凿击声，", "矿灯忽然矮了半寸，",
    ],
    "cliff": [
        "风从崖下翻上来，带着石粉的味道，", "脚下的碎石松动了一下，",
        "耳膜被高处的气压压得发闷，",
    ],
    "ferry": [
        "船板在脚下轻轻一沉，", "水汽扑在脸上，凉得很快，",
        "缆绳摩擦渡桩，发出干涩的声响，",
    ],
    "town": [
        "油炸的味道混着人汗，", "脚下的青石板被踩得发亮，",
        "有人在很近的地方讨价还价，",
    ],
    "gate": [
        "石阶的凉意透过鞋底，", "香火的味道从门里飘出来，", "檐角的铁马响了一声，",
    ],
    "snow": ["雪粒打在脸上，像细沙，", "睫毛上结了霜，", "脚陷进雪里，拔出来费劲，"],
    "forest": ["腐叶的气味很重，", "有枝条擦过衣袖，", "鸟被惊起来，扑棱一声，"],
}
REACTIONS = [
    "没有出声", "把呼吸压得很慢", "数了三下才开口", "手背绷起一道筋", "让开了半步",
    "把背贴紧石壁", "没有退", "反手按住腰侧", "把重心压到前脚", "抬眼看了一圈",
    "没有看那把刀", "把矿牌塞进怀里", "向前迈了半步", "把袖口拉紧",
]
INTERIORS = [
    "{hero}想起师父说过的话：矿里最危险的不是塌方，是人。",
    "{hero}知道这一趟回不去了，但他还是往前走。",
    "这条路{hero}走过七次，只有这一次，石头是热的。",
    "{hero}在心里把每个人的位置重新排了一遍，然后选了最坏的那个。",
    "如果现在退，前面的三年就白熬了——{hero}想。",
    "{hero}忽然明白那张黄纸为什么是湿的。",
    "{hero}数了数面前的人，比预想的少两个。少的两个在哪，才是要紧事。",
    "矿牌上的编号错了。{hero}记得清清楚楚，这一批不该有这个号。",
    "{hero}没有回头看。回头这个动作，会告诉对方他怕了。",
    "{hero}想起三天前那个雨夜，也是这么静，静得能听见自己的心跳。",
    "{hero}把要说的话在心里过了一遍，然后删掉了所有解释。",
    "有些账不是不算，是没到算的时候。{hero}把这句话又默念了一遍。",
    "{hero}没有急着开口。有些话一出口，就没有回头的余地了。",
    "这事要是搁在去年，{hero}早就动手了。今年他学会了等。",
    "{hero}把矿灯往前递了递，光只照到三步远，剩下的得自己走。",
    "疼是真的，但还能忍。{hero}把这点疼记在心里，像记一笔账。",
    "{hero}忽然想笑。走了这么远，原来绕回的原点。",
    "{hero}知道自己该说点什么，可话到嘴边，发现哪句都不对。",
    "有人在背后议论，声音不大，刚好能让人听见。{hero}没回头。",
    "{hero}把每一步都走得很快，快到没时间害怕。",
    "这条路他走过无数遍，闭着眼也不会错——可今天他觉得陌生。",
    "{hero}想起小时候第一次下矿，也是这么黑，那时他哭过。",
    "手心的汗渗进矿镐的木柄，{hero}得换只手才握得住。",
    "不该是这样。{hero}又把整件事从头到尾捋了一遍，还是不对。",
    "{hero}在心里给每个人排了序，谁先动，谁后动，谁不动。",
    "天快亮了。{hero}知道再耗下去，先撑不住的会是自己。",
]
SCENERY = [
    "山风从崖口灌进来，带着碎雪的味道。",
    "矿洞深处滴水声一下一下，像有人在数息。",
    "暮色把青岩染成暗红，远处宗门钟声敲了七下。",
    "石阶上结着薄冰，每一步都要先用脚尖试过。",
    "炉火噼啪响了一声，映得墙上人影摇晃。",
    "天光从井口斜斜地落下来，切开一线浮尘。",
    "雨停在半空，落到石面上才散成一团白汽。",
    "矿灯的火苗歪向一侧，说明前面有风。",
    "远处有人在磨刀，声音很慢，像在数日子。",
    "石壁上留着上一班的刻痕，歪歪扭扭记到十七。",
    "井口的辘轳空转了半圈，绳子垂着，没人接。",
    "远处的山脊压着云，像有人把刀横在那儿。",
    "石缝里的苔藓被踩塌了，脚印一直通到暗处。",
    "灯油快要烧尽，焰心开始发红。",
    "风里混着烧纸的味道，一阵有一阵无。",
    "台阶尽头是黑的，回头的路也是黑的。",
    "水滴落在铁器上，响得比别处脆。",
    "墙根堆着新土，掩得不太仔细。",
    "有人把一块布挂在钉子上，还湿着。",
    "石台上摆着七盏灯，只有六盏亮。",
    "门轴锈住了，推开时声音拖得很长。",
    "头顶的岩层在渗水，一滴一滴，落在同一处。",
    "他踩到什么东西，软的，立刻松开。",
]
CONFLICTS = [
    "王长老的弟子横剑拦在路中央，剑尖挑着半块矿牌。",
    "有人在暗处扣动了机括，碎石从头顶塌下来。",
    "对方一句话没说，先递过来一张写着名字的黄纸。",
    "地面突然震了一下，矿壁上的纹路亮起一道细线。",
    "三个穿灰衣的人从岔道口转出来，谁也没看谁。",
    "链条绷紧的声音从背后传来，接着是闸门落下的闷响。",
    "有人在他脚边扔下一枚断成两半的铜钱。",
    "领头的那个抬手示意，后面的人立刻散开了。",
    "一支火把被扔进积水里，嗤地一声灭了。",
    "石缝里渗出的水忽然变红，又很快淡下去。",
    "对面的呼吸声乱了半拍，随即有人后退。",
    "一柄刀从侧面递过来，停在离喉咙三寸的地方。",
    "有人低声报出他的旧名，那个名字早该没人记得。",
    "脚下的地面开始缓慢倾斜，像有什么在下面翻身。",
    "围过来的人里，有一个他从前救过。",
    "对面把矿牌翻过来，背面刻着一行他不认识的字。",
    "有人从背后按住他的肩，力气大得不像常人就该有。",
    "岔道口同时转出三个人，走的是同一个步子。",
    "对方把刀往地上一插，刀身没入半尺。",
    "石板被掀开一角，下面是空的。",
    "他听见自己的名字被念了两遍，第二遍不是同一个人。",
    "对面那个忽然跪下，额头磕在石头上，磕出了血。",
    "一支箭钉在门框上，箭尾还缠着红布。",
    "所有人同时停手，像被谁按住了。",
    "有人在数数，从一个很长的地方往回数。",
    "对面的手在抖，抖得握不住东西。",
    "铜牌掉在地上，滚了两圈，停在他脚前。",
    "对方把火折子凑近油布，等他开口。",
    "有东西从头顶掠过，带下的风是暖的。",
    "他在人群里看见一张和记忆里一模一样的脸。",
    "对方把头盔摘了，他认出了那双眼睛。",
]


def _pick(seed: int, pool: list[str]) -> str:
    return pool[seed % len(pool)]


def _shuffled(pool: list[str], seed: int) -> list[str]:
    """确定性洗牌（不用 random，保证同一 seed 每次结果一致）。"""
    out = list(pool)
    n = len(out)
    for k in range(n - 1, 0, -1):
        j = (seed * 1103515245 + k * 12345 + 7) % (k + 1)
        out[k], out[j] = out[j], out[k]
    return out


def _hero_for(book_title: str) -> str:
    """书级主角名：同一本书恒定，不同书不同。"""
    seed = _seed(f"hero::{book_title}")
    surname = SURNAMES[seed % len(SURNAMES)]
    given = GIVEN[(seed // 7) % len(GIVEN)]
    return surname + given


def _foreshadow_for(ch: int, count: int, start: int) -> list[dict]:
    """按固定节奏产出伏笔动作，让 demo 真正跑起状态机。

    节奏：每批第 1 章埋 2 条，第 2 章提示，第 4 章回收——只在「批内」成环，
    这样任意长度的一批都能看到 plant→hint→resolve 的全过程。
    """
    idx = ch - start  # 批内序号
    out: list[dict] = []
    if idx % 4 == 0:
        out.append({"ref": f"F-{ch:03d}a", "action": "plant", "note": HOOKS[(ch - 1) % len(HOOKS)].split("，")[0]})
        out.append({"ref": f"F-{ch:03d}b", "action": "plant", "note": "矿牌编号与腰间那枚相同"})
    elif idx % 4 == 1:
        out.append({"ref": f"F-{ch - 1:03d}a", "action": "hint", "note": "旧名被叫破时对手停顿了半拍"})
    elif idx % 4 == 3:
        out.append({"ref": f"F-{ch - 3:03d}a", "action": "resolve", "note": "黄纸上的姓被认了出来"})
        out.append({"ref": f"F-{ch - 3:03d}b", "action": "hint", "note": "腰间矿牌发烫"})
    return out


def _power_for(ch: int, start: int) -> list[dict]:
    """每 4 章一次境界推进，供战力校验与章纲展示。"""
    if (ch - start) % 4 == 2:
        return [{"char": "mc", "field": "rank", "from": "淬体九层", "to": "凝气一层"}]
    return []


def _body_section(text: str) -> str:
    """取出 ```<<< ... >>>``` 之间的正文；没有则原样返回。"""
    start = text.find("<<<")
    end = text.rfind(">>>")
    if start != -1 and end != -1 and end > start:
        return text[start + 3 : end]
    return text


def _seed(text: str) -> int:
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:8], 16)


class MockProvider:
    """确定性 mock。name 固定为 'mock-local'。"""

    name = "mock-local"
    bridge = "mock"

    def __init__(
        self,
        latency_ms: int = 0,
        fail_times: int = 0,
        fail_with: str = "rate_limit",
        name: str = "mock-local",
        bridge: str = "mock",
    ):
        self.name = name
        self.bridge = bridge
        self.latency_ms = latency_ms
        self._fail_times = fail_times
        self._fail_with = fail_with
        self.calls: list[CompletionRequest] = []

    # ---------------- 对外 ----------------
    def supports(self, model: str) -> bool:
        return True

    async def health(self) -> tuple[bool, str]:
        return True, "mock ok"

    async def complete(self, req: CompletionRequest, model: str) -> CompletionResult:
        started = time.monotonic()
        if self._fail_times > 0:
            self._fail_times -= 1
            from ..base import BadRequest, BridgeDown, ContentFiltered, RateLimited

            mapping = {
                "rate_limit": RateLimited,
                "bridge_down": BridgeDown,
                "content_filter": ContentFiltered,
                "bad_request": BadRequest,
            }
            cls = mapping.get(self._fail_with, BridgeDown)
            raise cls(f"mock 注入故障: {self._fail_with}", provider=self.name, model=model)

        self.calls.append(req)
        if self.latency_ms:
            import asyncio

            await asyncio.sleep(self.latency_ms / 1000.0)

        text = self.render(req)
        prompt_tokens = sum(len(m.content) for m in req.messages)
        completion_tokens = len(text)
        return CompletionResult(
            text=text,
            provider=self.name,
            model=model or "mock-1",
            bridge=self.bridge,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            latency_ms=int((time.monotonic() - started) * 1000),
            finish_reason="stop",
            raw={"mock": True},
        )

    async def stream(self, req: CompletionRequest, model: str) -> AsyncIterator[StreamChunk]:
        res = await self.complete(req, model)
        # 按句切块，模拟流式
        buf = ""
        for ch in res.text:
            buf += ch
            if ch in "。！？\n" and len(buf) >= 12:
                yield StreamChunk(delta=buf, done=False, provider=self.name, model=res.model)
                buf = ""
        if buf:
            yield StreamChunk(delta=buf, done=False, provider=self.name, model=res.model)
        yield StreamChunk(
            delta="",
            done=True,
            provider=self.name,
            model=res.model,
            usage={
                "prompt_tokens": res.prompt_tokens,
                "completion_tokens": res.completion_tokens,
                "total_tokens": res.total_tokens,
            },
        )

    # ---------------- 内容生成 ----------------
    def render(self, req: CompletionRequest) -> str:
        # 审稿/沉淀只看正文段（<<< ... >>>），避免把 prompt 里的「禁用词清单」当成正文命中
        full = "\n".join(m.content for m in req.messages)
        blob = _body_section(full) if req.task in ("review", "curator") else full
        task = req.task
        if task == "plan":
            return self._plan(blob)
        if task == "review":
            return self._review(blob)
        if task == "curator":
            return self._curator(blob)
        if task == "summarize":
            return self._summary(blob)
        if task == "deconstruct":
            return self._deconstruct(blob)
        if task == "polish":
            return self._polish(blob)
        return self._prose(blob, req)

    # ---- planner ----
    def _plan(self, blob: str) -> str:
        start = self._int_after(blob, r"从第[\s*`]*(\d+)[\s*`]*章")
        count = self._int_after(blob, r"生成[\s*`]*(\d+)[\s*`]*章") or 3
        title_hint = self._first(r"《(.+?)》", blob) or "无名"
        chapters = []
        for i in range(count):
            ch = start + i
            rnd = _seed(f"{title_hint}-{ch}")
            hero = SURNAMES[rnd % len(SURNAMES)] + GIVEN[(rnd // 7) % len(GIVEN)]
            chapters.append(
                {
                    "ch": ch,
                    # 用章号取模而不是 hash：保证任意连续一批（≤池长度）章名互不相同
                    "title": CHAPTER_TITLES[(ch - 1) % len(CHAPTER_TITLES)],
                    "pov": "mc",
                    "threads": ["main"],
                    "scenes": [
                        {
                            "loc": f"world/geography#{SCENE_LOCS[rnd % len(SCENE_LOCS)]}",
                            "chars": ["mc"] + (["su"] if rnd % 4 == 0 else []),
                            "goal": f"取得{['玄铁矿', '淬体液', '矿牌', '旧账册', '阵图残页'][rnd % 5]}",
                            "conflict": CONFLICTS[rnd % len(CONFLICTS)],
                            "outcome": ["险胜但暴露实力", "以巧破局", "退一步换取情报", "被逼立下口头承诺"][rnd % 4],
                        }
                    ],
                    "must_include": ["@characters/mc"],
                    "foreshadow_actions": _foreshadow_for(ch, count, start),
                    "power_events": _power_for(ch, start),
                    "hook": HOOKS[(ch - 1) % len(HOOKS)].format(hero=hero),
                    "target_words": [2000, 2400],
                    "status": "draft",
                    "beat": ["冲突", "奖励", "期待"][rnd % 3],
                }
            )
        return json.dumps({"chapters": chapters}, ensure_ascii=False)

    # ---- writer ----
    def _prose(self, blob: str, req: CompletionRequest) -> str:
        ch = self._int_after(blob, r"第[\s*`]*(\d+)[\s*`]*章") or 1
        title = self._first(r"标题[：:]\s*(.+?)[\n，,]", blob) or self._first(r"「(.+?)」", blob) or "无名"
        target = self._int_after(blob, r"字数[\s*`]*(\d+)") or 2000
        hook = self._first(r"结尾钩子[：:]\s*(.+?)\n", blob) or "远处传来一声钟响。"
        pov = self._first(r"视角[：:]\s*(\S+)", blob) or "mc"
        rnd = _seed(f"{ch}-{title}")

        # 主角名优先级：
        # 1) prompt 里角色卡给出的名字（角色卡是唯一事实来源）
        # 2) 书级常量（同一本书恒定；跨书不同）
        # 早先按章 seed 重抽，同一本书里主角从楚澈变沈陌变林轩，读起来像短篇集。
        book_title = self._first(r"《(.+?)》", blob) or title
        hero = self._card_hero(blob) or _hero_for(book_title)

        # 场景句按章纲给的地点选池：否则「黑水渡口」也写成矿洞（实测过）
        loc = self._place_kind(blob)
        paras: list[str] = [f"# 第{ch}章 {title}"]
        # 用「洗牌袋」保证同章内台词/动作/景物不扎堆重复，而不是简单地 rnd+i 取模
        bags: dict[str, list[str]] = {}
        rounds: dict[str, int] = {}

        def draw(kind: str, pool: list[str], salt: int) -> str:
            if kind not in bags or not bags[kind]:
                rounds[kind] = rounds.get(kind, 0) + 1
                # 整池洗牌不够：池子只有几十条，每章要抽十几条，相邻章必然大面積重叠。
                # 先按章号**旋转**再洗牌，让每章优先使用池子里不同的一段。
                n = len(pool)
                offset = (ch * 7 + salt) % n if n else 0
                rotated = pool[offset:] + pool[:offset]
                bags[kind] = _shuffled(rotated, rnd + salt * 7 + rounds[kind] * 101)
            return bags[kind].pop()

        body_words = 0
        i = 0
        date_done = False
        # 必须写满 target（而不是提前 200 字收手）：平台字数下限是按 target 下限定的，
        # 生成不足会让导出预检把 demo 的每一章都判成「低于下限」（实测出现过）。
        while body_words < max(600, target + 60):
            # 节拍顺序按章轮换：固定 i%7 会让相邻章的段落在同一位置对齐，
            # 有限池下同位置必然撞（实测 ch3/ch4 有 67 字整段重复）。
            beat = (i + ch * 3) % 7
            # 开场第一段不放时间锚点：正文第一句就是「青云历…那天下的是雨」很突兀
            if i == 0 and beat == 6:
                beat = 0
            if beat == 0:
                pool = SCENERY_BY_PLACE.get(loc) or SCENERY
                para = (
                    f"{draw('scenery', pool, 11)}{hero}{draw('action', ACTIONS, 17)}。"
                    f"{draw('detail_a', DETAILS, 13)}"
                )
            elif beat == 1:
                para = f"「{draw('line', LINES, 23)}」他{draw('action', ACTIONS, 29)}。"
            elif beat == 2:
                para = (
                    f"{draw('conflict', CONFLICTS, 37)}{hero}{draw('reaction', REACTIONS, 59)}。"
                    f"{draw('detail_b', DETAILS, 67)}"
                )
            elif beat == 3:
                sense_pool = (SENSES_BY_PLACE.get(loc) or []) + SENSES
                para = f"{draw('sense', sense_pool, 43)}{hero}{draw('reaction', REACTIONS, 47)}。"
            elif beat == 6:
                # 时间锚点：整章只给一处，重复出现会让时间线看起来像刷屏
                if date_done:
                    para = draw('interior', INTERIORS, 53).format(hero=hero) + draw('tail', INNER_TAILS, 61)
                else:
                    date_done = True
                    tpl = DATE_LINES[ch % len(DATE_LINES)]
                    para = tpl.format(
                        year=1024 + ch // 12,
                        month=(ch % 12) + 1,
                        day=cn_day((ch * 3) % 28 + 1),
                    )
            else:
                para = f"{draw('interior', INTERIORS, 53).format(hero=hero)}{draw('tail', INNER_TAILS, 61)}"
            paras.append(para)
            body_words += len(re.findall(r"[\u4e00-\u9fff]", para))
            i += 1

        paras.append(hook)
        return "\n\n".join(paras)

    # ---- reviewer ----
    def _review(self, blob: str) -> str:
        issues: list[dict[str, Any]] = []
        ch = self._int_after(blob, r"第[\s*`]*(\d+)[\s*`]*章") or 1
        rnd = _seed(f"review-{ch}")
        # 依据注入内容做真实可核查的判定（不是纯随机）
        if "仿佛" in blob or "宛如" in blob:
            issues.append(
                {
                    "severity": "mid",
                    "category": "ai-flavor",
                    "evidence": "仿佛/宛如",
                    "suggestion": "改为具体动作或感官描写，删掉这类比喻引导词",
                }
            )
        if "眼中闪过一丝" in blob:
            issues.append(
                {
                    "severity": "high",
                    "category": "ai-flavor",
                    "evidence": "眼中闪过一丝",
                    "suggestion": "套语，直接写眼神动作（眯起/垂下去/移开）",
                }
            )
        if rnd % 3 == 0:
            issues.append(
                {
                    "severity": "mid",
                    "category": "repetition",
                    "evidence": "指节泛白",
                    "suggestion": "同一章内出现 3 次以上，删到 1 次",
                }
            )
        if rnd % 4 == 0:
            issues.append(
                {
                    "severity": "low",
                    "category": "hook",
                    "evidence": "章末",
                    "suggestion": "钩子偏弱，可在最后一句前加一句未解信息",
                }
            )
        return json.dumps({"issues": issues}, ensure_ascii=False)

    # ---- curator ----
    def _curator(self, blob: str) -> str:
        ch = self._int_after(blob, r"第[\s*`]*(\d+)[\s*`]*章") or 1
        name = SURNAMES[_seed(f"cur-{ch}") % len(SURNAMES)] + GIVEN[_seed(f"cur2-{ch}") % len(GIVEN)]
        return json.dumps(
            {
                "proposals": [
                    {
                        "id": f"p-{ch}-1",
                        "file": f"memory/ch-{ch:04d}.summary.md",
                        "op": "create",
                        "content": f"# 第{ch}章 摘要\n\n{name}在青岩矿脉与人冲突，夺得玄铁矿，暴露了部分实力。结尾矿洞传出诵经声。",
                        "reason": "章后沉淀",
                    },
                    {
                        "id": f"p-{ch}-2",
                        "file": "memory/entity-deltas.jsonl",
                        "op": "append",
                        "lines": [
                            json.dumps(
                                {"ch": ch, "entity": "mc", "kind": "state", "op": "update", "note": "获得玄铁矿"},
                                ensure_ascii=False,
                            )
                        ],
                        "reason": "实体变化流",
                    },
                ]
            },
            ensure_ascii=False,
        )

    # ---- summarize ----
    def _summary(self, blob: str) -> str:
        ch = self._int_after(blob, r"第[\s*`]*(\d+)[\s*`]*章") or 1
        return f"第{ch}章：主角在矿脉冲突中获胜，实力暴露，结尾出现异常线索。"

    # ---- polish ----
    def _polish(self, blob: str) -> str:
        text = blob
        for bad, good in (
            ("仿佛", "像"),
            ("宛如", "如同"),
            ("不由得", ""),
            ("眼中闪过一丝", "眼神一沉，"),
        ):
            text = text.replace(bad, good)
        return text

    # ---- deconstruct ----
    def _deconstruct(self, blob: str) -> str:
        return json.dumps(
            {
                "structure": {
                    "volumes": [{"index": 1, "chapters": 60, "note": "开局卷"}],
                    "coolpoint_curve": [0.3, 0.5, 0.7, 0.6, 0.8],
                },
                "pacing": {"avg_words": 2200, "dialogue_ratio": 0.42, "info_release": "每 2 章一个信息点"},
                "hooks": {"types": {"危机型": 0.4, "悬念型": 0.3, "反转型": 0.2, "情绪型": 0.1}},
            },
            ensure_ascii=False,
        )

    # ---------------- 小工具 ----------------
    @staticmethod
    def _card_hero(blob: str) -> str | None:
        """从角色卡段里取主角名（``**林澈**（mc，protagonist）``）。"""
        m = re.search(r"\*\*([\u4e00-\u9fff]{2,3})\*\*\s*[（(]\s*mc\b", blob)
        if m:
            return m.group(1)
        m = re.search(r"\*\*([\u4e00-\u9fff]{2,3})\*\*\s*[（(][^)）]*protagonist", blob)
        return m.group(1) if m else None

    @staticmethod
    def _place_kind(blob: str) -> str:
        """章纲地点 → 场景池类型。"""
        m = re.search(r"地点[：:]\s*(\S+)", blob)
        loc = m.group(1) if m else ""
        for kw, kind in (
            ("矿", "mine"), ("崖", "cliff"), ("渡", "ferry"), ("城", "town"),
            ("集市", "town"), ("山门", "gate"), ("阁", "gate"), ("殿", "gate"),
            ("雪", "snow"), ("林", "forest"),
        ):
            if kw in loc:
                return kind
        return "generic"

    @staticmethod
    def _first(pattern: str, text: str) -> str | None:
        m = re.search(pattern, text)
        return m.group(1).strip() if m else None

    @staticmethod
    def _int_after(text: str, pattern: str) -> int | None:
        m = re.search(pattern, text)
        if not m:
            return None
        try:
            return int(m.group(1))
        except (ValueError, IndexError):
            return None
