"""造一本「对标书」样本（用于演示与测试拆书功能）。纯原创文本，不抄任何真实作品。"""
from __future__ import annotations

import sys
from pathlib import Path

CHAPTER = """第{n}章 {title}

{n}个月前，{hero}还没想过自己会站在这里。

雾气贴着地面漫上来，把石阶一级一级吞掉。{hero}把手里的东西掂了掂，又放回怀里。

「你确定要走这条路？」身后有人问。

{hero}没有回答。有些话说出来就轻了，不如留在心里压着。

前方的门半开着，缝里透出一点光，不亮，但稳。

第{n}段台阶走完，{hero}停下来喘了口气。汗从鬓角下来，落到地上，很快就没了痕迹。

「我数过。」{hero}终于开口，「从山门到这里，一共一千三百二十七级。」

那人愣了一下：「你数这个做什么？」

「因为我想记住，」{hero}说，「我是怎么上来的。」

风忽然停了。

门那边传来一声很轻的响动，像是有人把手放到了门框上。

{hero}抬手，推门。

门后的光一下子涌出来，把所有影子都拉得很长。

「来了。」里面有人说。

「来了。」{hero}答。

{n}年之后，这一夜会被人反复提起。有人说那是运气，有人说那是命。

但{hero}知道，那只是一千三百二十七级台阶中的第一级，被终于走完了。

「从明天起，」{hero}在门口回头看了一眼来路，「就不一样了。」

远处的钟响了三下。夜还没过去，但天边已经有了第一道白。
"""

PLAN = [
    ("入门", "秦九", "山门试炼，第一次登阶"),
    ("问路", "秦九", "在集市上遇到说书人，听到旧传闻"),
    ("旧伤", "秦九", "旧伤复发，被迫停下休整"),
    ("同门", "苏晚", "与同门起冲突，第一次有人替他说话"),
    ("夜巡", "秦九", "夜巡时发现墙上的刻痕"),
    ("试锋", "秦九", "第一次与人正面交手"),
    ("残页", "苏晚", "残页上的字开始显形"),
    ("破境", "秦九", "在最不该的时候突破"),
    ("雪夜", "秦九", "雪夜里有人送来一句话"),
    ("横刀", "秦九", "横刀拦路，退无可退"),
]


def main(out_dir: str, chapters: int = 10) -> None:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []
    for i, (title, hero, _note) in enumerate(PLAN[:chapters], start=1):
        parts.append(CHAPTER.format(n=i, title=title, hero=hero))
    body = "\n\n".join(parts)
    (root / "benchmark-sample.txt").write_text(body, encoding="utf-8")
    print(f"已生成 {root / 'benchmark-sample.txt'}：{chapters} 章 / {len(body)} 字符")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ws/shared-materials", int(sys.argv[2]) if len(sys.argv) > 2 else 10)
