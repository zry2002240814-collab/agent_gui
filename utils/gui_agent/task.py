"""Task constraints that a model or recovery strategy may not silently relax."""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from device_controller import APP_PACKAGE_MAP

APP_ALIASES = {"b站": "哔哩哔哩", "bilibili": "哔哩哔哩", "小破站": "哔哩哔哩",
               "滴滴": "滴滴出行", "高德": "高德地图", "携程": "携程旅行",
               "网易云": "网易云音乐", "头条": "今日头条", "wps": "WPS Office",
               "qqmusic": "QQ音乐", "微信": "微信", "12306": "铁路12306"}
CHINESE_NUMBERS = dict(zip("一二三四五六七八九十", range(1, 11)))


def _count(token):
    if token.isdigit():
        return int(token)
    if "十" in token:
        tens, ones = token.split("十", 1)
        return CHINESE_NUMBERS.get(tens, 1) * 10 + CHINESE_NUMBERS.get(ones, 0)
    return CHINESE_NUMBERS[token]


def normalize_app(name):
    compact = name.strip().replace(" ", "").lower()
    for alias, target in APP_ALIASES.items():
        if alias.lower() == compact:
            return target
    for known in sorted(APP_PACKAGE_MAP, key=len, reverse=True):
        if known.replace(" ", "").lower() == compact:
            return known
    return name.strip()


@dataclass
class TaskSpec:
    instruction: str
    apps: List[str] = field(default_factory=list)
    requirements: Dict[str, str] = field(default_factory=dict)
    target_count: Optional[int] = None
    rating_threshold: Optional[float] = None
    rating_inclusive: bool = False
    collection: bool = False
    plan: List[str] = field(default_factory=list)

    def qualify(self, rating):
        if self.rating_threshold is None:
            return True
        if isinstance(rating, bool) or not isinstance(rating, (int, float)):
            return False
        return rating >= self.rating_threshold if self.rating_inclusive else rating > self.rating_threshold

    def snapshot(self):
        return dict(instruction=self.instruction, apps=self.apps, requirements=self.requirements,
                    target_count=self.target_count, rating_threshold=self.rating_threshold,
                    rating_inclusive=self.rating_inclusive, collection=self.collection, plan=self.plan)


def compile_task(instruction):
    task = TaskSpec(instruction=instruction,
                    requirements={"task_goal": "满足原始指令中的全部具体条件，提供完成证据"})
    matches = []
    for name in APP_PACKAGE_MAP:
        position = instruction.lower().find(name.lower())
        if position >= 0:
            matches.append((position, -len(name), normalize_app(name)))
    for alias, name in APP_ALIASES.items():
        position = instruction.lower().find(alias.lower())
        if position >= 0:
            matches.append((position, -len(alias), name))
    for _, _, name in sorted(matches):
        if name not in task.apps and not (name == "QQ" and "QQ音乐" in instruction):
            task.apps.append(name)
    if not task.apps and re.search(r"导航|附近.*咖啡|公交路线", instruction):
        task.apps = ["高德地图"]
    task.collection = bool(re.search(
        r"(?:加入|添加).*(?:书单|歌单|收藏)|放(?:进|到).*歌单|搬运|"
        r"(?:把|将).*(?:收藏|书单|歌单)|收藏(?:这|此|该|课程|视频|歌曲)", instruction))
    if re.search(r"播放(?!量|最多|最少|次数|数)|开始听", instruction):
        task.requirements["playback"] = "指定内容、版本和播放状态得到界面证据确认"
    if task.collection:
        task.requirements["collection"] = "符合条件的条目已加入指定目标清单，并去重核验"
        count = re.search(r"(\d+|[一二三四五六七八九十]{1,3})\s*"
                          r"(?:门|首|条|本|款|个(?!\s*(?:新?歌单|新?书单|列表|收藏夹|文件夹)))", instruction)
        ranking = re.search(r"前\s*(\d+|[一二三四五六七八九十]{1,3})", instruction)
        value = (count or ranking)
        if value:
            token = value.group(1)
            task.target_count = _count(token)
    if re.search(r"筛选|最便宜|评分最高|点评最高|播放最多|播放量最高|换乘.*最少", instruction):
        task.requirements["selection"] = "筛选与选择标准实际生效，并与原始指令一致"
    if re.search(r"导航|路线|途经|经过", instruction):
        task.requirements["route"] = "起点、终点、途经点和要求的路线结果均核实"
    rating = re.search(r"评分\s*(超过|大于|不低于|至少|>=|≥|>)\s*(\d+(?:\.\d+)?)", instruction)
    if rating:
        task.rating_threshold = float(rating.group(2))
        task.rating_inclusive = rating.group(1) in {"不低于", "至少", ">=", "≥"}
    return task
