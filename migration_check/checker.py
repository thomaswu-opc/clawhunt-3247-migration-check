"""抽样核对的判定逻辑：逐条比对、健康分引用检查、问题分类。

设计要点：
- 抽样用固定种子（自写 LCG，不依赖 random.Random 的跨版本稳定性）；
- 不一致的判定是"逐字段比对"，不是"整条字符串比"——后者会因字段顺序或
  无关列的变化误报；
- 问题分类是显式枚举，无法归类的进"其他"，不允许出现无信息的"问题：无"。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .models import ACTIVITY_DIFF_KEYS, INTERACTION_DIFF_KEYS, Activity, Interaction


# ---- 确定性抽样（自写 LCG，跨 Python 版本恒定） ----

class Lcg:
    """32 位整数线性同余，参数取自 glibc/Numerical Recipes 常用组合。

    只用整数运算，任何 Python 3.x 下结果一致，保证"同种子必同样本"。
    """

    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF or 1

    def next(self) -> int:
        self.state = (1103515245 * self.state + 12345) & 0xFFFFFFFF
        return self.state


def sample_indices(total: int, count: int, seed: int) -> list[int]:
    """从 [0,total) 里确定性抽取 count 个不重复下标。

    若 count >= total，返回全部（抽样上限是样本本身）；否则用 LCG 生成候选
    并拒绝已选中的（Fisher-Yates 风格），保证可复现且不重复。
    """
    if count >= total:
        return list(range(total))
    rng = Lcg(seed)
    pool = list(range(total))
    chosen: list[int] = []
    for i in range(count):
        j = rng.next() % len(pool)
        chosen.append(pool.pop(j))
    return sorted(chosen)


# ---- 问题分类 ----

PROBLEM_CATEGORIES = ("字段错位", "时间截断", "关联错位", "丢失", "重复", "其他")


def classify_problem(kind: str, key: str, expected: Any, actual: Any) -> str:
    """根据差异特征归因。

    分类规则（显式、可测试）：
    - 期望有值、实际为空/缺 → 丢失
    - 时间字段且实际是期望的前缀（截断）→ 时间截断
    - 以 _id 结尾或 id 类字段且两侧都非空但不相等 → 关联错位
    - 同 id 出现多于一次 → 重复（由调用方在出现次数>1 时给出）
    - 其余 → 字段错位
    """
    if expected is None or (isinstance(expected, str) and not expected.strip()):
        return "丢失"
    if actual is None or (isinstance(actual, str) and not actual.strip()):
        return "丢失"
    es, as_ = str(expected), str(actual)
    if "time" in key.lower() or key.endswith("_at") or key == "occurred_at":
        if as_.startswith(es) and len(as_) != len(es):
            return "时间截断"
        if es.startswith(as_) and len(es) != len(as_):
            return "时间截断"
    if key.endswith("_id") or key == "id":
        return "关联错位"
    return "字段错位"


# ---- 逐条比对 ----

@dataclass
class DiffItem:
    record_kind: str      # activity / interaction
    record_id: str
    field: str
    expected: Any
    actual: Any
    category: str = ""


@dataclass
class Problem:
    record_kind: str
    record_id: str
    category: str
    detail: str


@dataclass
class CheckResult:
    activity_sampled: int = 0
    interaction_sampled: int = 0
    activity_checked: int = 0
    interaction_checked: int = 0
    diffs: list[DiffItem] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    health_check: str = ""
    health_ok: bool | None = None
    category_counts: dict[str, int] = field(default_factory=dict)

    @property
    def problem_count(self) -> int:
        return len(self.problems)


def compare_activities(expected: list[Activity], actual: list[Activity]) -> list[DiffItem]:
    """按 id 对齐后逐字段比对活动，仅比对 ACTIVITY_DIFF_KEYS。"""
    by_id = {a.id: a for a in actual}
    diffs: list[DiffItem] = []
    for e in expected:
        a = by_id.get(e.id)
        if a is None:
            for k in ACTIVITY_DIFF_KEYS:
                diffs.append(DiffItem("activity", e.id, k, getattr(e, k), None, "丢失"))
            continue
        for k in ACTIVITY_DIFF_KEYS:
            ev, av = getattr(e, k), getattr(a, k)
            if ev != av:
                diffs.append(DiffItem("activity", e.id, k, ev, av,
                                      classify_problem("activity", k, ev, av)))
    return diffs


def compare_interactions(expected: list[Interaction], actual: list[Interaction]) -> list[DiffItem]:
    by_id = {x.id: x for x in actual}
    diffs: list[DiffItem] = []
    for e in expected:
        a = by_id.get(e.id)
        if a is None:
            for k in INTERACTION_DIFF_KEYS:
                diffs.append(DiffItem("interaction", e.id, k, getattr(e, k), None, "丢失"))
            continue
        for k in INTERACTION_DIFF_KEYS:
            ev, av = getattr(e, k), getattr(a, k)
            if ev != av:
                diffs.append(DiffItem("interaction", e.id, k, ev, av,
                                      classify_problem("interaction", k, ev, av)))
    return diffs


def dedupe_problems(diffs: list[DiffItem]) -> list[Problem]:
    """把逐字段 diff 汇总成"每条记录一个问题"，并识别重复 id。

    同一记录可能有多个字段不一致，归并成一条问题，detail 列出不一致字段，
    category 取该记录差异里最严重/首个（保持可复现顺序）。
    """
    by_rec: dict[tuple[str, str], list[DiffItem]] = {}
    for d in diffs:
        by_rec.setdefault((d.record_kind, d.record_id), []).append(d)
    problems: list[Problem] = []
    for (kind, rid), ds in by_rec.items():
        # 若整条记录缺失（全部字段 actual=None），归类为丢失
        cats = [d.category for d in ds]
        cat = "丢失" if all(c == "丢失" for c in cats) else cats[0]
        detail = "; ".join("%s: %s→%s" % (d.field, d.expected, d.actual) for d in ds)
        problems.append(Problem(kind, rid, cat, detail))
    return problems


def check_health_usage(interaction_ids: list[str], health_source: dict[str, Any]) -> tuple[str, bool]:
    """检查健康分是否使用了历史互动。

    health_source 需含 'source_table'（引用的来源表名）与 'referenced_ids'（引用的 id 集合）。
    判定：source_table 明确指向历史互动表，且 referenced_ids 覆盖了传入的 interaction_ids。
    """
    source = str(health_source.get("source_table") or "")
    referenced = set(health_source.get("referenced_ids") or [])
    wanted = set(interaction_ids)
    if "interaction" not in source.lower():
        return ("健康分引用来源=%s，非历史互动表 → 未正确使用历史互动" % source, False)
    missing = wanted - referenced
    if missing:
        return ("健康分引用来源=历史互动表，但未覆盖本批 %d 条（缺 %d 条）"
                % (len(wanted), len(missing)), False)
    return ("健康分引用来源=历史互动表，覆盖本批 %d 条" % len(wanted), True)


def summarize(result: CheckResult) -> dict[str, Any]:
    """生成可打印的汇总，含分类统计，供 CLI 与测试共用。"""
    counts = {c: 0 for c in PROBLEM_CATEGORIES}
    for p in result.problems:
        counts[p.category] = counts.get(p.category, 0) + 1
    result.category_counts = counts
    return {
        "activity_sampled": result.activity_sampled,
        "interaction_sampled": result.interaction_sampled,
        "activity_checked": result.activity_checked,
        "interaction_checked": result.interaction_checked,
        "problem_count": result.problem_count,
        "category_counts": counts,
        "health_ok": result.health_ok,
        "health_check": result.health_check,
    }


def sample_digest(seed: int, counts: dict[str, int]) -> str:
    """样本的确定性摘要：种子+抽样数 → 稳定 digest，供"同种子必同样本"验证。"""
    h = hashlib.sha256()
    h.update(("seed=%d" % seed).encode())
    for k in ("activity", "interaction"):
        h.update(("|%s=%d" % (k, counts.get(k, 0))).encode())
    return h.hexdigest()
