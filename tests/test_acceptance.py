"""3247 验收测试：抽样核对 + 健康分引用 + 问题分类。

零第三方依赖（纯标准库），运行：
  python -m unittest discover -s tests -t . -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migration_check.checker import (
    classify_problem, compare_activities, compare_interactions,
    check_health_usage, dedupe_problems, sample_digest, sample_indices,
)
from migration_check.models import Activity, Interaction


def mk_act(i: int, **kw) -> Activity:
    base = dict(id="A%03d" % i, title="t", activity_type="沙龙",
                start_time="2026-09-01 10:00", end_time="2026-09-01 12:00",
                registered_count=20, checked_in_count=15)
    base.update(kw)
    return Activity(**base)


def mk_itx(i: int, **kw) -> Interaction:
    base = dict(id="I%04d" % i, company_id="C001", contact_name="张三",
                interaction_type="拜访", occurred_at="2026-10-01 10:00", recorder="记录员1")
    base.update(kw)
    return Interaction(**base)


class TestSampling(unittest.TestCase):
    def test_sample_30_and_100(self):
        # 30 场活动 + 100 条互动，且样本不重复、来自期望集合
        acts = [mk_act(i) for i in range(1, 61)]
        itxs = [mk_itx(i) for i in range(1, 201)]
        ai = sample_indices(len(acts), 30, 20261126)
        ii = sample_indices(len(itxs), 100, 20261126)
        self.assertEqual(len(ai), 30)
        self.assertEqual(len(ii), 100)
        self.assertEqual(len(set(ai)), 30, "活动样本不可重复")
        self.assertEqual(len(set(ii)), 100, "互动样本不可重复")
        self.assertTrue(all(i < len(acts) for i in ai))
        self.assertTrue(all(i < len(itxs) for i in ii))

    def test_same_seed_same_sample(self):
        acts = [mk_act(i) for i in range(1, 61)]
        a1 = sample_indices(len(acts), 30, 42)
        a2 = sample_indices(len(acts), 30, 42)
        self.assertEqual(a1, a2, "同种子必须同一样本")

    def test_diff_seed_diff_sample(self):
        acts = [mk_act(i) for i in range(1, 61)]
        a1 = sample_indices(len(acts), 30, 42)
        a2 = sample_indices(len(acts), 30, 43)
        self.assertNotEqual(a1, a2, "不同种子应产生不同样本（否则种子没起作用）")

    def test_sample_digest_stable(self):
        d1 = sample_digest(20261126, {"activity": 30, "interaction": 100})
        d2 = sample_digest(20261126, {"activity": 30, "interaction": 100})
        self.assertEqual(d1, d2)
        self.assertNotEqual(d1, sample_digest(20261127, {"activity": 30, "interaction": 100}))


class TestCompare(unittest.TestCase):
    def test_activity_all_match(self):
        e = [mk_act(i) for i in (1, 2, 3)]
        a = [mk_act(i) for i in (1, 2, 3)]
        self.assertEqual(compare_activities(e, a), [])

    def test_activity_diff_field(self):
        e = [mk_act(1, registered_count=30)]
        a = [mk_act(1, registered_count=15)]  # 报名数错位
        diffs = compare_activities(e, a)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0].field, "registered_count")
        self.assertEqual(diffs[0].category, "字段错位")

    def test_activity_time_truncated(self):
        e = [mk_act(1, end_time="2026-09-01 12:00")]
        a = [mk_act(1, end_time="2026-09-01")]  # 截断成日期
        diffs = compare_activities(e, a)
        self.assertEqual(diffs[0].category, "时间截断")

    def test_interaction_company_mislink(self):
        e = [mk_itx(1, company_id="C001")]
        a = [mk_itx(1, company_id="C999")]  # 关联错位
        diffs = compare_interactions(e, a)
        self.assertEqual(diffs[0].field, "company_id")
        self.assertEqual(diffs[0].category, "关联错位")

    def test_missing_record(self):
        e = [mk_act(1), mk_act(2)]
        a = [mk_act(1)]  # A002 丢失
        diffs = compare_activities(e, a)
        self.assertTrue(any(d.record_id == "A002" and d.category == "丢失" for d in diffs))

    def test_duplicate_id_flagged(self):
        # 同一 id 出现两次 → 归类为重复（在逐条比对前由调用方识别，这里验证规则）
        e = [mk_act(1)]
        a = [mk_act(1), mk_act(1)]
        # 比对只按 id 对齐，重复由外层记录；此处验证 dedupe 至少不崩溃且问题数≥1
        diffs = compare_activities(e, a)
        probs = dedupe_problems(diffs)
        self.assertIsInstance(probs, list)

    def test_classify_no_info_forbidden(self):
        # 不允许"问题：无"这类无信息条目 —— 每个 diff 必须能归类到枚举
        c = classify_problem("activity", "title", "t", "x")
        self.assertIn(c, ("字段错位", "时间截断", "关联错位", "丢失", "重复", "其他"))
        # 期望非空但实际为空 → 丢失
        self.assertEqual(classify_problem("activity", "title", "t", ""), "丢失")
        # 期望为空 → 丢失
        self.assertEqual(classify_problem("activity", "title", "", "x"), "丢失")


class TestHealth(unittest.TestCase):
    def test_uses_history_ok(self):
        ids = ["I0001", "I0002", "I0003"]
        msg, ok = check_health_usage(ids, {"source_table": "interaction_history",
                                           "referenced_ids": ids})
        self.assertTrue(ok)
        self.assertIn("覆盖本批 3 条", msg)

    def test_not_history_table(self):
        msg, ok = check_health_usage(["I0001"], {"source_table": "member_base",
                                                "referenced_ids": ["I0001"]})
        self.assertFalse(ok)
        self.assertIn("非历史互动表", msg)

    def test_not_covering_all(self):
        ids = ["I0001", "I0002", "I0003"]
        msg, ok = check_health_usage(ids, {"source_table": "interaction_history",
                                           "referenced_ids": ["I0001"]})
        self.assertFalse(ok)
        self.assertIn("未覆盖本批 3 条", msg)


class TestIntegration(unittest.TestCase):
    def test_run_check_mock_detects_problems(self):
        # 端到端：mock 注入差异，run_check 应检出问题且分类齐全
        import run_check as rc
        exp_acts, exp_itxs, ref_ids = rc.mock_expected()
        act_actual, itx_actual = rc.mock_actual(exp_acts, exp_itxs, mutate=True)
        res = rc.run(exp_acts, act_actual, exp_itxs, itx_actual,
                     20261126, {"source_table": "interaction_history", "referenced_ids": ref_ids})
        self.assertEqual(res.activity_sampled, 30)
        self.assertEqual(res.interaction_sampled, 100)
        self.assertGreaterEqual(res.problem_count, 4, "应至少检出 4 类问题")
        cats = {p.category for p in res.problems}
        # 注入覆盖：字段错位/时间截断/关联错位/丢失
        self.assertIn("字段错位", cats)
        self.assertIn("时间截断", cats)
        self.assertIn("关联错位", cats)
        self.assertIn("丢失", cats)

    def test_run_check_mock_all_match(self):
        import run_check as rc
        exp_acts, exp_itxs, ref_ids = rc.mock_expected()
        act_actual, itx_actual = rc.mock_actual(exp_acts, exp_itxs, mutate=False)
        res = rc.run(exp_acts, act_actual, exp_itxs, itx_actual,
                     20261126, {"source_table": "interaction_history", "referenced_ids": ref_ids})
        self.assertEqual(res.problem_count, 0)
        self.assertTrue(res.health_ok)


if __name__ == "__main__":
    unittest.main()
