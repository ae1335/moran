"""起名生成器（§10.3，Planner 附属工具，非 Agent）。

约束来自 `truth/world/power-system.md` 的用字风格与 `materials/dictionary/` 词表，
产物追加到 `materials/dictionary/names-pool.md`，作者挑用。

设计取舍：
- **纯规则、零模型调用**：起名要的是可控与可复现，不是"创意"。同一 seed 同一结果。
- **不落真相源**：names-pool 是素材（materials/），不参与一致性校验，也不进 Curator 白名单。
- **去重**：对全书已有角色名 + 池内已有名字做排重，避免起出第二个"林昭"。
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..core.store import NovelStore
from ..core import atomic


#: 词池文件表头（缺陷 #116：建表头必须整体持锁，不能 exists() 之后再裸追加）
POOL_HEADER = (
    "# 起名词池\n\n"
    "> 由 Planner 附属起名器生成，作者挑用；**不是真相源**，删改随意。\n\n"
    "| 名字 | 类别 | 备注 |\n"
    "| --- | --- | --- |"
)

#: 起名类别 → 用字池
#:
#: 池子按「网文常见姓氏 / 雅正字 / 器物字 / 地理字」粗分，不求全，
#: 求的是组合空间够大（姓 40×名 2 字位 × 60 字 ≈ 十万级）且不生成谐音梗。
SURNAMES = [
    "林", "萧", "苏", "叶", "秦", "顾", "陆", "楚", "沈", "云",
    "谢", "江", "裴", "宋", "温", "柳", "白", "姜", "霍", "燕",
    "薛", "程", "许", "罗", "孟", "韩", "何", "曾", "岑", "柯",
    "祝", "闻", "穆", "宗", "连", "薄", "戚", "单", "颜", "应",
]

GIVEN_MALE = [
    "昭", "珩", "渊", "屿", "临", "野", "肃", "决", "持", "衍",
    "尘", "戈", "戟", "砚", "铮", "彻", "朔", "岭", "桓", "晏",
    "澈", "骁", "默", "樵", "钺", "隼", "歧", "烬", "樾", "戬",
]

GIVEN_FEMALE = [
    "婳", "妗", "婉", "棠", "薜", "茴", "荔", "纭", "纾", "绢",
    "绫", "绡", "璃", "璎", "筠", "笙", "箬", "绮", "絮", "胧",
    "苓", "荞", "菀", "葭", "蕖", "蘋", "黛", "黎", "姒", "姝",
]

#: 中性名（不分性别）
GIVEN_NEUTRAL = [
    "川", "舟", "霜", "野", "白", "青", "远", "行", "止", "言",
    "墨", "书", "琴", "灯", "秋", "冬", "南", "微", "玄", "渡",
]

#: 功法 / 器物：动词-意象两段式，读起来像招式名
SKILL_HEADS = [
    "斩", "破", "碎", "焚", "裂", "沉", "问", "叩", "断", "锁",
    "引", "御", "化", "定", "照", "洗", "开", "闭", "镇", "压",
]
SKILL_TAILS = [
    "岳", "渊", "锋", "岚", "河", "海", "灯", "钟", "碑", "爻",
    "玄", "枢", "极", "阳", "阴", "煞", "罡", "冥", "虚", "决",
]

#: 宗门 / 势力：前缀 + 后缀，避开真实宗教名
SECT_PREFIX = [
    "青云", "太玄", "北冥", "落霞", "寒山", "焚天", "流萤", "孤星",
    "白鹿", "赤霄", "墨羽", "长夜", "听雪", "无相", "斩月", "枯荣",
]
SECT_SUFFIX = ["宗", "门", "阁", "殿", "府", "院", "盟", "教", "观", "堂"]

#: 地名：地形字 + 修饰字
PLACE_MOD = [
    "青岩", "落霞", "黑水", "断崖", "枯骨", "长风", "无名", "碎星",
    "沉沙", "白骨", "赤水", "寒潭", "南疆", "北荒", "云梦", "白帝",
]
PLACE_KIND = ["城", "镇", "关", "渡", "谷", "岭", "渊", "原", "洲", "窟", "峡", "海"]

KIND_LABELS = {
    "character": "角色",
    "skill": "功法/招式",
    "sect": "宗门/势力",
    "place": "地名",
}


@dataclass
class NameCandidate:
    name: str
    kind: str
    note: str = ""


@dataclass
class NamePool:
    """一次生成的结果集。"""

    kind: str
    seed: int
    candidates: list[NameCandidate] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # 因重名被跳过的

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "seed": self.seed,
            "names": [c.name for c in self.candidates],
            "candidates": [{"name": c.name, "kind": c.kind, "note": c.note} for c in self.candidates],
            "skipped": self.skipped,
        }


class NameGenerator:
    """按 world/power-system 的风格起名，产物写 materials/dictionary/names-pool.md。"""

    def __init__(self, store: NovelStore):
        self.store = store

    # ---------------- 对外 ----------------
    def generate(
        self,
        kind: str = "character",
        count: int = 10,
        seed: int | None = None,
        gender: str = "",
        save: bool = True,
    ) -> NamePool:
        if kind not in KIND_LABELS:
            raise ValueError(f"未知起名类别：{kind}（可选 {', '.join(KIND_LABELS)}）")
        count = max(1, min(int(count), 50))
        seed = int(seed) if seed is not None else random.randrange(1, 10**6)
        rng = random.Random(seed)

        existing = self._existing_names()
        pool = NamePool(kind=kind, seed=seed)
        seen: set[str] = set()

        guard = 0
        while len(pool.candidates) < count and guard < count * 40:
            guard += 1
            name, note = self._make(kind, rng, gender)
            if not name or name in seen:
                continue
            seen.add(name)
            if name in existing:
                pool.skipped.append(name)
                continue
            pool.candidates.append(NameCandidate(name=name, kind=kind, note=note))

        if save and pool.candidates:
            self._append_pool(pool)
        return pool

    # ---------------- 内部 ----------------
    def _make(self, kind: str, rng: random.Random, gender: str) -> tuple[str, str]:
        if kind == "character":
            surname = rng.choice(SURNAMES)
            if gender == "female":
                given = rng.choice(GIVEN_FEMALE)
            elif gender == "male":
                given = rng.choice(GIVEN_MALE)
            else:
                given = rng.choice(GIVEN_MALE + GIVEN_FEMALE + GIVEN_NEUTRAL)
            # 10% 概率双字名（追加**单字**，否则会拼出「穆北辰远」这类四字怪名）
            if rng.random() < 0.10:
                given += rng.choice(GIVEN_NEUTRAL)
            return surname + given, self._style_note()

        if kind == "skill":
            return rng.choice(SKILL_HEADS) + rng.choice(SKILL_TAILS), "动词+意象"

        if kind == "sect":
            return rng.choice(SECT_PREFIX) + rng.choice(SECT_SUFFIX), "前缀+建制后缀"

        if kind == "place":
            return rng.choice(PLACE_MOD) + rng.choice(PLACE_KIND), "地形修饰+地貌"

        return "", ""

    def _style_note(self) -> str:
        """从 power-system 里抠出境界词，给候选名一个"符合世界观"的备注。"""
        try:
            text = self.store.read_truth_text("world/power-system.md")
        except Exception:
            return ""
        m = re.findall(r"淬体|凝气|筑基|金丹|元婴|化神|炼气|辟谷|心动|灵寂", text)
        uniq = list(dict.fromkeys(m))[:3]
        return f"契合境界线：{'/'.join(uniq)}" if uniq else ""

    def _existing_names(self) -> set[str]:
        """全书已有名字（角色卡 + 已有池 + 章纲标题），避免起重复。"""
        names: set[str] = set()
        try:
            for c in self.store.list_characters():
                names.add(c.name)
                names.add(c.id)
        except Exception:
            pass
        pool_file = self.store.p.materials / "dictionary" / "names-pool.md"
        try:
            if pool_file.exists():
                for line in pool_file.read_text(encoding="utf-8").splitlines():
                    m = re.match(r"^\|\s*([^\s|]+)\s*\|", line)
                    if m:
                        names.add(m.group(1))
        except Exception:
            pass
        return names

    def _append_pool(self, pool: NamePool) -> Path:
        path = self.store.p.materials / "dictionary" / "names-pool.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"| {c.name} | {KIND_LABELS.get(c.kind, c.kind)} | {c.note} |" for c in pool.candidates]
        # 缺陷 #116：原实现是「exists() 判表头 -> open("a") 裸追加」，两步之间无锁 ——
        # 并发首次写入时每个线程都看到文件不存在，于是各写一份表头（实测 24 线程并发
        # 约 15% 概率出现 2 份表头，Markdown 中段表格渲染断裂）。
        # 与 #88 同源：当年只把灵感速记换成了 append_text，起名词池漏了。
        atomic.append_text(path, "\n".join(lines), header=POOL_HEADER)
        return path

    # ---------------- 读 ----------------
    def read_pool(self) -> list[dict]:
        """读取已有词池（前端展示用）。"""
        path = self.store.p.materials / "dictionary" / "names-pool.md"
        if not path.exists():
            return []
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\|\s*([^\s|]+)\s*\|\s*([^|]+?)\s*\|\s*([^|]*)\|\s*$", line)
            if not m or m.group(1) in ("名字", "---"):
                continue
            out.append({"name": m.group(1).strip(), "kind": m.group(2).strip(), "note": m.group(3).strip()})
        return out
