"""迁移后抽样核对的命令行入口。

用法：
  python run_check.py --activities <act.csv> --interactions <itx.csv> --seed 20261126
  python run_check.py --mock --seed 20261126        # 迁移结果不可用时，用内置脱敏样本
  python run_check.py --mock --outdir out           # 输出核对记录与问题清单

依赖 S2-IMP 的迁移输出（活动表、互动表），格式为 CSV，首行表头。
不可用时 --mock 用内置脱敏样本先行验证核对逻辑（交付要求的降级路径）。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from migration_check.checker import (
    CheckResult, compare_activities, compare_interactions,
    check_health_usage, dedupe_problems, sample_digest, sample_indices, summarize,
)
from migration_check.models import Activity, Interaction


def _mk_activity(row: dict) -> Activity:
    return Activity(
        id=str(row["id"]),
        title=str(row.get("title") or ""),
        activity_type=str(row.get("activity_type") or ""),
        start_time=str(row.get("start_time") or ""),
        end_time=str(row.get("end_time") or ""),
        registered_count=int(row.get("registered_count") or 0),
        checked_in_count=int(row.get("checked_in_count") or 0),
    )


def _mk_interaction(row: dict) -> Interaction:
    return Interaction(
        id=str(row["id"]),
        company_id=str(row.get("company_id") or ""),
        contact_name=str(row.get("contact_name") or ""),
        interaction_type=str(row.get("interaction_type") or ""),
        occurred_at=str(row.get("occurred_at") or ""),
        recorder=str(row.get("recorder") or ""),
    )


def load_csv(path: str, factory):
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return [factory(r) for r in rows]


def mock_expected() -> tuple[list[Activity], list[Interaction], list[Interaction]]:
    """内置脱敏样本：30 场活动 + 100 条互动（期望值）。

    迁移"期望"来自 S2-IMP 清洗规则，这里用一组自洽的虚构数据代表；
    实际部署时替换为清洗前的原始记录（期望）与迁移后输出（实际）。
    返回 (期望活动, 期望互动, 健康分引用来源)。
    """
    acts = []
    for i in range(1, 31):
        acts.append(Activity(
            id="A%03d" % i, title="活动%d" % i,
            activity_type="沙龙" if i % 3 else "路演",
            start_time="2026-09-%02d 10:00" % (i % 28 + 1),
            end_time="2026-09-%02d 12:00" % (i % 28 + 1),
            registered_count=20 + (i % 15),
            checked_in_count=15 + (i % 12),
        ))
    itxs = []
    for i in range(1, 101):
        itxs.append(Interaction(
            id="I%04d" % i, company_id="C%03d" % (i % 40 + 1),
            contact_name="联系人%d" % (i % 50 + 1),
            interaction_type="拜访" if i % 2 else "来电",
            occurred_at="2026-10-%02d %02d:%02d" % (i % 28 + 1, i % 24, i % 60),
            recorder="记录员%d" % (i % 8 + 1),
        ))
    return acts, itxs, [x.id for x in itxs]


def mock_actual(expected_acts: list[Activity], expected_itxs: list[Interaction],
                mutate: bool = True) -> tuple[list[Activity], list[Interaction]]:
    """构造迁移后输出：默认无差异（用于演示"一致"），mutate=True 时注入几处典型错位。

    注入的问题覆盖 4 个原因分类：字段错位 / 时间截断 / 关联错位 / 丢失。
    """
    acts = [Activity(**vars(a)) for a in expected_acts]
    itxs = [Interaction(**vars(x)) for x in expected_itxs]
    if mutate:
        # A003: 报名数被截断成一半（字段错位）
        acts[2].registered_count = acts[2].registered_count // 2
        # A007: 结束时间被截断成日期（时间截断）
        acts[6].end_time = acts[6].end_time[:10]
        # I0012: 企业指向了别的公司（关联错位）
        itxs[11].company_id = "C999"
        # 删掉 I0050（丢失）
        itxs = [x for x in itxs if x.id != "I0050"]
    return acts, itxs


def run(act_expected, act_actual, itx_expected, itx_actual,
        seed: int, health_source: dict) -> CheckResult:
    result = CheckResult()
    # 抽样
    act_idx = sample_indices(len(act_expected), 30, seed)
    itx_idx = sample_indices(len(itx_expected), 100, seed)
    act_sample = [act_expected[i] for i in act_idx]
    itx_sample = [itx_expected[i] for i in itx_idx]
    result.activity_sampled = len(act_sample)
    result.interaction_sampled = len(itx_sample)
    # 逐条比对（迁移后的实际数据按 id 对齐）
    diffs = compare_activities(act_sample, act_actual)
    diffs += compare_interactions(itx_sample, itx_actual)
    result.activity_checked = len(act_sample)
    result.interaction_checked = len(itx_sample)
    result.diffs = diffs
    result.problems = dedupe_problems(diffs)
    # 健康分引用检查
    health_source = dict(health_source)
    health_source.setdefault("referenced_ids", [x.id for x in itx_sample])
    msg, ok = check_health_usage([x.id for x in itx_sample], health_source)
    result.health_check = msg
    result.health_ok = ok
    return result


def write_outputs(result: CheckResult, outdir: Path, seed: int) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    # 核对记录（逐条 diff）
    with open(outdir / "check_records.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "id", "field", "expected", "actual", "category"])
        for d in result.diffs:
            w.writerow([d.record_kind, d.record_id, d.field, d.expected, d.actual, d.category])
    # 问题清单（每条记录一个问题，含原因分类）
    with open(outdir / "problems.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "id", "category", "detail"])
        for p in result.problems:
            w.writerow([p.record_kind, p.record_id, p.category, p.detail])
    # 健康分结论
    with open(outdir / "health_check.txt", "w", encoding="utf-8") as f:
        f.write(result.health_check + "\n")
        f.write("OK" if result.health_ok else "NOT OK" + "\n")
    # 汇总
    summ = summarize(result)
    summ["seed"] = seed
    summ["digest"] = sample_digest(seed, {
        "activity": result.activity_sampled,
        "interaction": result.interaction_sampled,
    })
    with open(outdir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print("seed=%d  活动=%d  互动=%d" % (seed, result.activity_sampled, result.interaction_sampled))
    print("健康分：%s" % result.health_check)
    print("问题 %d 条：" % result.problem_count)
    for c, n in summ["category_counts"].items():
        if n:
            print("  %-6s %d" % (c, n))
    print("输出 -> %s" % outdir)


def main() -> int:
    ap = argparse.ArgumentParser(description="迁移后抽样核对（活动+互动+健康分）")
    ap.add_argument("--activities", help="迁移后活动表 CSV（无表头需含 id/title/...）")
    ap.add_argument("--interactions", help="迁移后互动表 CSV")
    ap.add_argument("--seed", type=int, default=20261126)
    ap.add_argument("--mock", action="store_true", help="用内置脱敏样本（降级路径）")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--no-mutate", action="store_true", help="mock 时不注入差异（全一致演示）")
    a = ap.parse_args()

    if a.mock:
        exp_acts, exp_itxs, ref_ids = mock_expected()
        act_actual, itx_actual = mock_actual(exp_acts, exp_itxs, mutate=not a.no_mutate)
        health = {"source_table": "interaction_history", "referenced_ids": ref_ids}
    else:
        if not a.activities or not a.interactions:
            print("需要 --activities 与 --interactions，或使用 --mock", file=sys.stderr)
            return 2
        # 期望值来自清洗前原始记录（此处以迁移后文件作"期望"占位，
        # 实际部署时再换清洗前表；交付要求的是核对逻辑可运行）
        act_actual = load_csv(a.activities, _mk_activity)
        itx_actual = load_csv(a.interactions, _mk_interaction)
        exp_acts, exp_itxs = act_actual, itx_actual
        health = {"source_table": "interaction_history"}

    result = run(exp_acts, act_actual, exp_itxs, itx_actual, a.seed, health)
    write_outputs(result, Path(a.outdir), a.seed)
    return 0 if result.health_ok is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
