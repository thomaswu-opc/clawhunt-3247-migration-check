"""迁移前后数据模型：活动、互动、健康分引用来源。

迁移核对的本质是"迁移值 vs 期望值"的逐字段比对，两侧字段用 dataclass 显式声明，
避免位置参数错位；不一致的判定集中在 DIFF_KEYS_ 常量里，一处改、全部跟随。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Activity:
    """历史活动：核对 报名数 / 签到数 / 活动类型 / 时间。"""

    id: str
    title: str
    activity_type: str
    start_time: str
    end_time: str
    registered_count: int
    checked_in_count: int


@dataclass
class Interaction:
    """历史互动：核对 企业 / 联系人 / 类型 / 时间 / 记录人。"""

    id: str
    company_id: str
    contact_name: str
    interaction_type: str
    occurred_at: str
    recorder: str


# 逐条比对时使用的字段白名单。迁移可能引入新列或改列名，但核对只认这些
# "业务语义"字段——新增/重命名不进核对集，避免误报。
ACTIVITY_DIFF_KEYS = ("activity_type", "start_time", "end_time",
                      "registered_count", "checked_in_count")
INTERACTION_DIFF_KEYS = ("company_id", "contact_name", "interaction_type",
                         "occurred_at", "recorder")
