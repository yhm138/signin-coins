#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单元测试：不需要数据库，纯标准库 unittest。

覆盖：词法/语法、去糖、Tier 判定、轨迹展开与 ρ 环压缩的数学正确性、
      参考语义（对手算期望值，不是拿编译器验编译器）、代码生成不变量、产物漂移。

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import datetime as D
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dslc  # noqa: E402

RULES = ROOT / "rules"
GEN = ROOT / "generated"


def parse(name):
    return dslc.Parser((RULES / name).read_text(encoding="utf-8")).parse_program()


def days(*specs):
    return [D.date.fromisoformat(s) for s in specs]


def run_user(prog, dates, uid=1):
    """把单个用户的一串日期喂给参考解释器，返回每日 emit 字典列表"""
    rows = [(uid, d) for d in dates]
    out, emits = dslc.reference_daily(prog, rows)
    return [dict(zip(emits, r[2:])) for r in out]


class TestParsing(unittest.TestCase):
    def test_all_rules_parse(self):
        for p in sorted(RULES.glob("*.dsl")):
            prog = dslc.Parser(p.read_text(encoding="utf-8")).parse_program()
            self.assertEqual(prog.table, "tb_user_log", p.name)
            self.assertEqual(prog.entity, "uid", p.name)
            self.assertTrue(prog.body, p.name)
            self.assertIn("daily", prog.report, p.name)

    def test_error_reports_line_number(self):
        bad = "source { from t }\nstate { x := 1 }\nscan { x := }\n"
        with self.assertRaises(dslc.DslError) as cm:
            dslc.Parser(bad).parse_program()
        self.assertIn("第 3 行", str(cm.exception))

    def test_unknown_function_rejected(self):
        src = (RULES / "r1_original.dsl").read_text(encoding="utf-8").replace(
            "coin := 1", "coin := nosuchfn(1)")
        with self.assertRaises(dslc.DslError):
            dslc.gen_sql(dslc.Parser(src).parse_program())

    def test_state_init_must_be_literal(self):
        src = (RULES / "r1_original.dsl").read_text(encoding="utf-8").replace(
            "state { consec := 0 }", "state { consec := 1 + 1 }")
        # 常量表达式可折叠为字面量，应当接受
        prog = dslc.Parser(src).parse_program()
        self.assertEqual(prog.init["consec"], 2)

    def test_prev_requires_variable(self):
        src = (RULES / "r1_original.dsl").read_text(encoding="utf-8").replace(
            "prev(day)", "prev(day + 1)")
        with self.assertRaises(dslc.DslError):
            dslc.Parser(src).parse_program()


class TestDesugar(unittest.TestCase):
    def test_three_spellings_of_gap_are_equivalent(self):
        base = (RULES / "r1_original.dsl").read_text(encoding="utf-8")
        variants = [
            base,
            base.replace("day = prev(day) + 1", "gap() = 1"),
            base.replace("day = prev(day) + 1", "datediff(day, prev(day)) = 1"),
            base.replace("day = prev(day) + 1", "prev(day) + 1 = day"),
        ]
        ds = days("2021-07-07", "2021-07-08", "2021-07-09", "2021-07-11",
                  "2021-07-12", "2021-07-13", "2021-07-14")
        ref = None
        for v in variants:
            got = run_user(dslc.Parser(v).parse_program(), ds)
            ref = ref if ref is not None else got
            self.assertEqual(got, ref)


class TestTierDecision(unittest.TestCase):
    def test_tier1_rules(self):
        for name in ("r1_original.dsl", "r2_fib_cycle7.dsl", "r3_fib_cap.dsl",
                     "r4_fib_noreset.dsl", "r5_weekend.dsl", "r8_rule_changed.dsl"):
            _sql, info = dslc.gen_sql(parse(name), verbose_info={})
            self.assertEqual(info["tier"], 1, name)
            self.assertEqual(info["reasons"], [], name)

    def test_tier2_rules_have_reasons(self):
        for name in ("r6_break_keep.dsl", "r7_makeup_card.dsl"):
            _sql, info = dslc.gen_sql(parse(name), verbose_info={})
            self.assertEqual(info["tier"], 2, name)
            self.assertTrue(info["reasons"], name)

    def test_calendar_dependent_payout_stays_tier1(self):
        _sql, info = dslc.gen_sql(parse("r5_weekend.dsl"), verbose_info={})
        self.assertEqual(info["tier"], 1)
        self.assertFalse(info["const_folded"])
        self.assertTrue(info["cond_hoisted"])

    def test_forcing_unsound_tier1_raises(self):
        with self.assertRaises(dslc.DslError) as cm:
            dslc.gen_sql(parse("r6_break_keep.dsl"), force_tier=1)
        self.assertIn("不能强制 Tier 1", str(cm.exception))

    def test_calendar_dependent_state_falls_back(self):
        src = (RULES / "r1_original.dsl").read_text(encoding="utf-8").replace(
            "if consec = 3 { coin := coin + 2 }",
            "if dayofweek(day) = 1 { consec := 1 }")
        _sql, info = dslc.gen_sql(dslc.Parser(src).parse_program(), verbose_info={})
        self.assertEqual(info["tier"], 2)
        self.assertTrue(any("绝对日期" in r for r in info["reasons"]))


class TestUnroll(unittest.TestCase):
    def coins(self, name):
        prog = parse(name)
        _sql, info = dslc.gen_sql(prog, verbose_info={})
        bound = (prog.end - prog.start).days + 2
        sched, prefix, period = dslc.unroll(prog, info, bound)
        vals = []
        for k, st in sched:
            day = prog.start + D.timedelta(days=k - 1)
            prev = None if k == 1 else day - D.timedelta(days=1)
            _post, em = dslc.concrete_run(prog, st, day, prev, first=(k == 1))
            vals.append(em["coin"])
        return vals, prefix, period, info

    def test_r1_schedule(self):
        vals, prefix, period, _ = self.coins("r1_original.dsl")
        self.assertEqual(vals, [1, 1, 3, 1, 1, 1, 7, 1])
        self.assertEqual((prefix, period), (2, 7))

    def test_r2_fibonacci_cycle(self):
        vals, prefix, period, _ = self.coins("r2_fib_cycle7.dsl")
        self.assertEqual(vals, [1, 1, 2, 3, 5, 8, 13, 1])
        self.assertEqual((prefix, period), (2, 7))

    def test_r3_capped_fib_reaches_fixed_point(self):
        vals, prefix, period, _ = self.coins("r3_fib_cap.dsl")
        self.assertEqual(vals[:16], [1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233, 377, 610, 987])
        self.assertEqual(vals[-3:], [1000, 1000, 1000])
        self.assertEqual(period, 1)
        self.assertEqual(prefix, len(vals))

    def test_r4_uncapped_fib_has_no_cycle(self):
        vals, prefix, period, info = self.coins("r4_fib_noreset.dsl")
        self.assertIsNone(period)
        self.assertIsNone(prefix)
        self.assertEqual(len(vals), 31)
        self.assertEqual(vals[:12], [1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144])
        self.assertTrue(info["truncated"])

    def test_r8_rule_change(self):
        vals, prefix, period, _ = self.coins("r8_rule_changed.dsl")
        self.assertEqual(vals, [1, 2, 1, 1, 4, 1, 1, 1, 9, 1])
        self.assertEqual((prefix, period), (2, 9))

    def test_index_math_matches_simulation_for_every_day(self):
        """ρ 压缩的核心不变量：任意真实第 k 天，映射到的日程表行必须给出同一天的正确金额"""
        for name in ("r1_original.dsl", "r2_fib_cycle7.dsl", "r3_fib_cap.dsl",
                     "r4_fib_noreset.dsl", "r5_weekend.dsl", "r8_rule_changed.dsl"):
            prog = parse(name)
            _sql, info = dslc.gen_sql(prog, verbose_info={})
            bound = (prog.end - prog.start).days + 2
            sched, prefix, period = dslc.unroll(prog, info, bound)
            day0 = prog.start
            row_coin = []
            for k, st in sched:
                day = day0 + D.timedelta(days=k - 1)
                prev = None if k == 1 else day - D.timedelta(days=1)
                _p, em = dslc.concrete_run(prog, st, day, prev, first=(k == 1))
                row_coin.append(em["coin"])
            state, prev = dict(prog.init), None
            for k in range(1, bound + 1):
                day = day0 + D.timedelta(days=k - 1)
                state, em = dslc.concrete_run(prog, state, day, prev, first=(k == 1))
                row = k if period is None else (k if k < prefix else prefix + (k - prefix) % period)
                self.assertEqual(row_coin[row - 1], em["coin"], f"{name} 第 {k} 天")
                prev = day

    def test_unroll_bound_guard(self):
        with self.assertRaises(dslc.DslError) as cm:
            dslc.gen_sql(parse("r4_fib_noreset.dsl"), max_unroll=5)
        self.assertIn("展开上界", str(cm.exception))


class TestReferenceSemantics(unittest.TestCase):
    """期望值全部手算，独立于编译器实现"""

    def test_r1_ten_day_streak_then_gap(self):
        ds = days(*[f"2021-07-{d:02d}" for d in range(7, 17)]) + \
            days("2021-07-20", "2021-07-21", "2021-07-22")
        got = [r["coin"] for r in run_user(parse("r1_original.dsl"), ds)]
        self.assertEqual(got, [1, 1, 3, 1, 1, 1, 7, 1, 1, 3, 1, 1, 3])

    def test_r1_monthly_totals(self):
        ds = days(*[f"2021-07-{d:02d}" for d in range(7, 17)]) + \
            days("2021-07-20", "2021-07-21", "2021-07-22", "2021-08-01", "2021-08-02")
        rows, emits = dslc.reference_daily(parse("r1_original.dsl"), [(1, d) for d in ds])
        totals = {}
        for uid, dt, coin in rows:
            totals[f"{dt:%Y%m}"] = totals.get(f"{dt:%Y%m}", 0) + coin
        self.assertEqual(totals, {"202107": 25, "202108": 2})  # 20 + 5

    def test_r2_fibonacci_ten_days(self):
        ds = days(*[f"2021-07-{d:02d}" for d in range(7, 17)])
        got = [r["coin"] for r in run_user(parse("r2_fib_cycle7.dsl"), ds)]
        self.assertEqual(got, [1, 1, 2, 3, 5, 8, 13, 1, 1, 2])

    def test_r6_break_keeps_half(self):
        ds = days("2021-07-07", "2021-07-08", "2021-07-09", "2021-07-10",
                  "2021-07-11", "2021-07-13", "2021-07-14", "2021-07-15")
        got = [r["coin"] for r in run_user(parse("r6_break_keep.dsl"), ds)]
        # 1,2,3,4,5 → 断签 floor(5/2)=2 → 3 → 4
        self.assertEqual(got, [1, 1, 3, 1, 1, 1, 3, 1])

    def test_r7_makeup_cards_and_month_reset(self):
        ds = days("2021-07-07", "2021-07-08", "2021-07-09", "2021-07-11",
                  "2021-07-12", "2021-07-15", "2021-08-01", "2021-08-02")
        got = run_user(parse("r7_makeup_card.dsl"), ds)
        self.assertEqual([r["coin"] for r in got], [1, 1, 3, 1, 1, 1, 1, 1])
        self.assertEqual([r["cards_left"] for r in got], [2, 2, 2, 1, 1, 1, 2, 2])

    def test_makeup_card_spec(self):
        """docs/RULES.md §1 的逐条口径，期望值由参考解释器实跑核对过"""
        prog = parse("r7_makeup_card.dsl")

        # §1.3 恰好漏 1 天才可补；§1.4 漏签 >=2 天即使有卡也断签
        got = run_user(prog, days("2021-07-07", "2021-07-08", "2021-07-11"))
        self.assertEqual([r["coin"] for r in got], [1, 1, 1])       # 7/11 天序回到 1
        self.assertEqual([r["cards_left"] for r in got], [2, 2, 2])  # 未消耗

        # §1.4 跨月 gap=1：连续天数延续，卡补满 2
        got = run_user(prog, days("2021-07-30", "2021-07-31", "2021-08-01"))
        self.assertEqual([r["coin"] for r in got], [1, 1, 3])
        self.assertEqual([r["cards_left"] for r in got], [2, 2, 2])

        # §1.3 卡不结转不累计：7 月一张没用，8 月仍是 2 张
        got = run_user(prog, days("2021-07-07", "2021-08-05", "2021-08-07"))
        self.assertEqual([r["cards_left"] for r in got], [2, 2, 1])

        # §0 活动期外的签到不作为"上一次签到"：7/7 仍是首签
        got = run_user(prog, days("2021-07-06", "2021-07-07", "2021-07-09"))
        self.assertEqual([r["coin"] for r in got], [1, 1])
        self.assertEqual([r["cards_left"] for r in got], [2, 1])

        # §1.3 每次最多 1 张，用光后同样漏 1 天也断签
        got = run_user(prog, days("2021-07-07", "2021-07-09", "2021-07-11", "2021-07-13"))
        self.assertEqual([r["coin"] for r in got], [1, 1, 3, 1])
        self.assertEqual([r["cards_left"] for r in got], [2, 1, 0, 0])

    def test_makeup_card_worked_example(self):
        """docs/RULES.md §1.6 的逐日算例整表锁定（含口径 B：漏签日不计入连续天数）"""
        ds = days(*[f"2021-07-{d:02d}" for d in
                    (7, 8, 9, 11, 12, 13, 14, 15, 16, 18, 20, 21, 24, 25, 26, 27, 28, 29, 30, 31)],
                  "2021-08-02", "2021-08-03")
        got = run_user(parse("r7_makeup_card.dsl"), ds)
        self.assertEqual([r["coin"] for r in got],
                         [1, 1, 3, 1, 1, 1, 7, 1, 1, 3, 1, 1, 1, 1, 3, 1, 1, 1, 7, 1, 1, 3])
        self.assertEqual([r["cards_left"] for r in got],
                         [2, 2, 2, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1])
        # 口径 B 的判别点：7/11 用卡后天序是 4 不是 5，所以 7 金币落在 7/14 而非 7/13
        self.assertEqual(got[6]["coin"], 7)    # 07-14
        self.assertEqual(got[5]["coin"], 1)    # 07-13
        totals = {}
        for d, r in zip(ds, got):
            totals[f"{d:%Y%m}"] = totals.get(f"{d:%Y%m}", 0) + r["coin"]
        self.assertEqual(totals, {"202107": 38, "202108": 4})

    def test_users_are_isolated(self):
        prog = parse("r1_original.dsl")
        ds = days("2021-07-07", "2021-07-08", "2021-07-09")
        rows, _ = dslc.reference_daily(prog, [(1, d) for d in ds] + [(2, d) for d in ds])
        self.assertEqual([r[2] for r in rows], [1, 1, 3, 1, 1, 3])

    def test_same_day_duplicates_are_deduped(self):
        prog = parse("r1_original.dsl")
        ds = days("2021-07-07", "2021-07-08", "2021-07-09")
        rows, _ = dslc.reference_daily(prog, [(1, d) for d in ds] + [(1, ds[1]), (1, ds[1])])
        self.assertEqual([r[2] for r in rows], [1, 1, 3])

    def test_window_filter(self):
        prog = parse("r1_original.dsl")
        rows, _ = dslc.reference_daily(prog, [(1, D.date(2021, 7, 6)), (1, D.date(2021, 7, 7)),
                                              (1, D.date(2021, 11, 1))])
        self.assertEqual([r[1] for r in rows], [D.date(2021, 7, 7)])

    def test_else_if_form_is_equivalent(self):
        src = """
source { from tb_user_log where sign_in = 1 and artical_id = 0 entity uid
         day date(in_time) between 2021-07-07, 2021-10-31 }
state { c := 0 }
scan {
  if gap() = 1 { c := c + 1 } else if gap() = 2 { c := 1 } else { c := 1 }
  if c > 7 { c := 1 }
  coin := 1
  if c = 3 { coin := 3 } else if c = 7 { coin := 7 }
  emit coin as coin
}
report { select uid, sum(coin) as coin from daily group by uid }
"""
        ds = days(*[f"2021-07-{d:02d}" for d in range(7, 25)])
        a = run_user(parse("r1_original.dsl"), ds)
        b = run_user(dslc.Parser(src).parse_program(), ds)
        self.assertEqual(a, b)


class TestCodegen(unittest.TestCase):
    def test_no_session_variables_anywhere(self):
        for p in sorted(RULES.glob("*.dsl")):
            for tier in (None, 2):
                sql, _ = dslc.gen_sql(parse(p.name), force_tier=tier)
                self.assertNotIn("@", sql, p.name)
                self.assertNotIn(":=", sql, p.name)

    def test_tier1_is_set_based(self):
        sql, info = dslc.gen_sql(parse("r1_original.dsl"), verbose_info={})
        self.assertNotIn("RECURSIVE", sql)
        self.assertIn("ROW_NUMBER() OVER", sql)
        self.assertIn("TO_DAYS(dt)", sql)
        self.assertTrue(info["const_folded"])
        self.assertIn("JOIN sched s", sql)

    def test_tier2_is_recursive_and_carries_depth_hint(self):
        sql, _ = dslc.gen_sql(parse("r7_makeup_card.dsl"))
        self.assertIn("WITH RECURSIVE", sql)
        self.assertIn("cte_max_recursion_depth", sql)
        self.assertIn("s.i = w.i + 1", sql)

    def test_sargable_pushdown(self):
        sql, info = dslc.gen_sql(parse("r1_original.dsl"), verbose_info={})
        self.assertEqual(info["sargable_pushdown"], "in_time")
        self.assertIn("in_time >= '2021-07-07 00:00:00'", sql)
        self.assertIn("in_time < '2021-11-01 00:00:00'", sql)

    def test_mode_b_hoists_condition_and_drops_sched(self):
        sql, info = dslc.gen_sql(parse("r5_weekend.dsl"), verbose_info={})
        self.assertIn("DAYOFWEEK(n.dt)", sql)
        self.assertIn("ELT(", sql)
        self.assertNotIn("sched AS", sql)
        self.assertEqual(info["schedule_cols"], [])

    def test_generated_artifacts_are_up_to_date(self):
        for p in sorted(RULES.glob("*.dsl")):
            sql, _ = dslc.gen_sql(parse(p.name))
            artifact = GEN / (p.stem + ".sql")
            self.assertTrue(artifact.exists(), f"缺少产物 {artifact.name}")
            self.assertEqual(artifact.read_text(encoding="utf-8").rstrip(), sql.rstrip(),
                             f"{artifact.name} 已过期，请跑 make sql")
        forced = GEN / "r1_original.forced_tier2.sql"
        sql, _ = dslc.gen_sql(parse("r1_original.dsl"), force_tier=2)
        self.assertEqual(forced.read_text(encoding="utf-8").rstrip(), sql.rstrip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
