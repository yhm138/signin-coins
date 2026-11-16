#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端验证 + 基准测试（需要一台 MySQL 8.0 / MariaDB 10.2+）

对每条规则：
  ① DSL 参考解释器逐行跑（语义基准）
  ② 编译器自动选路产出的 SQL
  ③ 强制 Tier 2（递归 CTE）产出的 SQL
三者结果必须完全一致；并与原始 @用户变量 SQL 对账。

连接配置（环境变量，任选其一）：
  MYSQL_SOCKET=/tmp/mysql.sock MYSQL_USER=root
  MYSQL_HOST=127.0.0.1 MYSQL_PORT=3306 MYSQL_USER=root MYSQL_PASSWORD=secret
  MYSQL_DATABASE=signin_dsl_test        # 默认值，脚本会自动 CREATE DATABASE

用法：
  python3 tests/e2e_verify.py                    # 全量（含基准）
  python3 tests/e2e_verify.py --skip-bench       # 只验正确性
  python3 tests/e2e_verify.py --bench-users 200,1000
"""
from __future__ import annotations

import argparse
import datetime as D
import hashlib
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dslc  # noqa: E402

RULES = ROOT / "rules"
FIXTURES = ROOT / "tests" / "fixtures"


# ------------------------------------------------------------------ 连接
def mysql_argv(database: str | None):
    argv = ["mysql", "--default-character-set=utf8mb4", "--batch", "--raw",
            "--skip-column-names"]
    sock = os.getenv("MYSQL_SOCKET")
    if sock:
        argv += [f"--socket={sock}"]
    else:
        argv += ["-h", os.getenv("MYSQL_HOST", "127.0.0.1"),
                 "-P", str(os.getenv("MYSQL_PORT", "3306"))]
    argv += ["-u", os.getenv("MYSQL_USER", "root")]
    if database:
        argv += [database]
    return argv


DB = os.getenv("MYSQL_DATABASE", "signin_dsl_test")
SESSION_PREFIX = ""   # 按服务端方言在 main() 里设置：每次调用都是新会话，必须逐次下发


def q(sql, timed=False, database=DB):
    sql = SESSION_PREFIX + sql
    env = dict(os.environ)
    if os.getenv("MYSQL_PASSWORD"):
        env["MYSQL_PWD"] = os.environ["MYSQL_PASSWORD"]
    t0 = time.perf_counter()
    p = subprocess.run(mysql_argv(database), input=sql, capture_output=True,
                       text=True, env=env)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[:1500])
    rows = [tuple(l.split("\t")) for l in p.stdout.splitlines() if l != ""]
    return (rows, time.perf_counter() - t0) if timed else rows


def try_q(sql):
    """可能因权限/方言失败语句：失败就跳过"""
    try:
        q(sql)
        return True
    except RuntimeError:
        return False


def norm(rows):
    out = []
    for r in rows:
        rr = []
        for v in r:
            try:
                rr.append(round(float(v), 6))
            except ValueError:
                rr.append(v)
        out.append(tuple(rr))
    return sorted(out)


# ------------------------------------------------------------------ 造数
def gen_data(seed=7, nusers=40, dup=False):
    """覆盖：全勤 / 偶尔断签 / 稀疏 / 整月不签 / 规律 7 连 2 断 / 隔天签 / 脏数据"""
    rnd = random.Random(seed)
    start, end = D.date(2021, 7, 1), D.date(2021, 11, 3)
    rows = []
    for uid in range(1, nusers + 1):
        mode = uid % 8
        d = start
        while d <= end:
            if mode == 0:
                p = 1.0
            elif mode == 1:
                p = 0.93
            elif mode == 2:
                p = 0.55
            elif mode == 3:
                p = 1.0 if d.month in (7, 9) else 0.0
            elif mode == 4:
                p = 1.0 if (d - start).days % 9 < 7 else 0.0
            elif mode == 5:
                p = 1.0 if (d - start).days % 2 == 0 else 0.0
            elif mode == 6:
                p = 0.8
            else:
                p = 0.3
            if rnd.random() < p:
                t = D.datetime.combine(d, D.time(rnd.randint(0, 23), rnd.randint(0, 59)))
                rows.append((uid, 0, t, t, 1))
                if dup and rnd.random() < 0.25:
                    rows.append((uid, 0, t + D.timedelta(minutes=5), t, 1))
                if rnd.random() < 0.3:
                    rows.append((uid, rnd.randint(1, 99), t, t, 0))
                if rnd.random() < 0.1:
                    rows.append((uid, rnd.randint(1, 99), t, t, 1))
            d += D.timedelta(days=1)
    return rows


def load(rows, table="tb_user_log"):
    q(f"DROP TABLE IF EXISTS {table}; "
      f"CREATE TABLE {table}(uid INT, artical_id INT, in_time DATETIME, out_time DATETIME, "
      f"sign_in TINYINT, KEY(uid, in_time)) ENGINE=InnoDB;")
    B = 4000
    for i in range(0, len(rows), B):
        vals = ",".join(
            f"({u},{a},'{t:%Y-%m-%d %H:%M:%S}','{o:%Y-%m-%d %H:%M:%S}',{s})"
            for u, a, t, o, s in rows[i:i + B])
        q(f"INSERT INTO {table} VALUES {vals};")


# ---------------------------------------------------- 参考实现（逐行解释）
def ref_table(prog, rows, name):
    daily, emits = dslc.reference_daily(
        prog, [(u, t.date()) for u, a, t, o, s in rows if s == 1 and a == 0])
    cols = []
    for i, n in enumerate(emits):
        sample = next((r[2 + i] for r in daily if r[2 + i] is not None), 0)
        cols.append(f"{n} " + ("BIGINT" if isinstance(sample, int) else
                               "DECIMAL(24,6)" if isinstance(sample, float) else "VARCHAR(64)"))
    q(f"DROP TABLE IF EXISTS {name}; CREATE TABLE {name}(uid INT, dt DATE, {', '.join(cols)});")
    for i in range(0, len(daily), 4000):
        vals = ",".join(
            f"({r[0]},'{r[1]}'," + ",".join(
                "NULL" if v is None else (f"'{v}'" if isinstance(v, str) else str(v))
                for v in r[2:]) + ")"
            for r in daily[i:i + 4000])
        q(f"INSERT INTO {name} VALUES {vals};")
    return re.sub(r"\bdaily\b", name, prog.report), len(daily)


def compile_rule(path, **kw):
    return dslc.gen_sql(dslc.Parser(Path(path).read_text(encoding="utf-8")).parse_program(), **kw)


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-bench", action="store_true")
    ap.add_argument("--bench-users", default="200,1000,4000")
    a = ap.parse_args()

    print(f"连接: {'socket=' + os.environ['MYSQL_SOCKET'] if os.getenv('MYSQL_SOCKET') else os.getenv('MYSQL_HOST', '127.0.0.1')}  库={DB}")
    q(f"CREATE DATABASE IF NOT EXISTS {DB} DEFAULT CHARSET utf8mb4;", database=None)
    ver = q("SELECT VERSION();")[0][0]
    print("服务端版本:", ver)
    global SESSION_PREFIX
    if "MariaDB" in ver:
        SESSION_PREFIX = ("SET SESSION max_recursive_iterations=1000000;\n"
                          "SET SESSION max_statement_time=300;\n")
    else:
        SESSION_PREFIX = ("SET SESSION cte_max_recursion_depth=1000000;\n"
                          "SET SESSION max_execution_time=300000;\n")
    print("会话前缀:", SESSION_PREFIX.replace("\n", " ").strip())

    clean = gen_data(seed=7, nusers=40, dup=False)
    dirty = gen_data(seed=7, nusers=40, dup=True)
    load(clean)
    print(f"\n造数：干净数据 {len(clean)} 行日志")

    results = []
    for path in sorted(RULES.glob("*.dsl")):
        sql_auto, info = compile_rule(path)
        sql_t2, _ = compile_rule(path, force_tier=2)
        prog = dslc.Parser(path.read_text(encoding="utf-8")).parse_program()
        report, ndaily = ref_table(prog, clean, "ref_" + path.stem)
        want = norm(q(report))
        got_auto, got_t2 = norm(q(sql_auto)), norm(q(sql_t2))
        ok1, ok2 = got_auto == want, got_t2 == want
        results.append((path.name, ok1, ok2))
        print(f"  {path.name:24} tier={info['tier_used']} "
              f"日程表={info.get('schedule_len')}行 周期={info.get('cycle_period')} "
              f"常量折叠={info.get('const_folded')}  自动={'PASS' if ok1 else 'FAIL'} "
              f"强制Tier2={'PASS' if ok2 else 'FAIL'}  ({ndaily} 人日, {len(want)} 组)")
        for label, got, ok in (("自动", got_auto, ok1), ("Tier2", got_t2, ok2)):
            if not ok:
                for x, y in zip(want, got):
                    if x != y:
                        print(f"    {label} 首个差异 want={x} got={y}")
                        break

    # ---------------- 与原始 @用户变量 SQL 对账 ----------------
    print("\n与原始 @用户变量 SQL 对账（规则 1）:")
    orig = (FIXTURES / "original_uservars.sql").read_text(encoding="utf-8")
    prog = dslc.Parser((RULES / "r1_original.dsl").read_text(encoding="utf-8")).parse_program()
    report, _ = ref_table(prog, clean, "ref_r1_clean")
    want = norm(q(report))
    try:
        print(f"  干净数据: {'一致' if norm(q(orig)) == want else '不一致'}  ({len(want)} 组)")
    except RuntimeError as e:
        print("  跳过（服务端不支持用户变量写法）:", str(e)[:120])

    load(dirty)
    print(f"  脏数据（同日重复签到日志，{len(dirty)} 行）:")
    report, _ = ref_table(prog, dirty, "ref_r1_dirty")
    want = norm(q(report))
    sql_auto, _ = compile_rule(RULES / "r1_original.dsl")
    print(f"    DSL 编译产物 : {'一致' if norm(q(sql_auto)) == want else '不一致'}")
    try:
        got_orig = norm(q(orig))
    except RuntimeError:
        got_orig = None
    if got_orig is not None:
        print(f"    @用户变量原版: {'一致' if got_orig == want else '不一致'}")
        if got_orig != want:
            d1 = {(r[0], r[1]): r[2] for r in want}
            d2 = {(r[0], r[1]): r[2] for r in got_orig}
            diff = [(k, d1[k], d2.get(k)) for k in d1 if d1[k] != d2.get(k)]
            print(f"    不一致组数 {len(diff)}/{len(want)}；样例 (uid,月份) 正确 vs 原版 → {diff[:4]}")
        # 硬证据 A：行集合完全相同，只改插入顺序
        print("  插入顺序实验（同一份行集合，只改物理插入顺序）:")
        for label, data in (("原始顺序", dirty),
                            ("打乱 seed=42", random.Random(42).sample(dirty, len(dirty))),
                            ("打乱 seed=99", random.Random(99).sample(dirty, len(dirty)))):
            load(data)
            rep, _ = ref_table(prog, data, "ref_r1_order")
            w = norm(q(rep))
            try:
                g = norm(q(orig))
                nd = sum(1 for x, y in zip(w, g) if x != y)
                msg = f"指纹 {hashlib.md5(repr(g).encode()).hexdigest()[:10]}，错 {nd}/{len(w)} 组"
            except RuntimeError as e:
                msg = "直接报错 → " + str(e).split("\n")[0][:90]
            c = norm(q(sql_auto))
            print(f"    {label:14} 原版: {msg}")
            print(f"    {'':14} DSL : 指纹 {hashlib.md5(repr(c).encode()).hexdigest()[:10]}，"
                  f"与参考一致={c == w}")
        load(dirty)
        # 硬证据 B：只改优化器开关（是否触发取决于计划，故为可选观察）
        print("  优化器开关实验（需要 SUPER 权限；无权限会自动跳过）:")
        if try_q("SET GLOBAL optimizer_switch='derived_merge=off';"):
            for sw in ("on", "off"):
                q(f"SET GLOBAL optimizer_switch='derived_merge={sw}';")
                g = norm(q(orig))
                fp = hashlib.md5(repr(g).encode()).hexdigest()[:10]
                nd = sum(1 for x, y in zip(want, g) if x != y) if len(g) == len(want) else -1
                print(f"    derived_merge={sw:3} → 指纹 {fp}  与正确值不一致 {nd}/{len(want)}")
            q("SET GLOBAL optimizer_switch='derived_merge=on';")
        else:
            print("    跳过")
        fp = [hashlib.md5(repr(norm(q(sql_auto))).encode()).hexdigest()[:10] for _ in range(3)]
        print(f"    DSL 产物连跑 3 次指纹: {fp}  稳定={len(set(fp)) == 1}")

    # ---------------- 基准 ----------------
    if not a.skip_bench:
        print("\n基准测试（同数据、同规则、三条路径）:")
        for nusers in [int(x) for x in a.bench_users.split(",")]:
            rows = gen_data(seed=11, nusers=nusers)
            load(rows)
            nd = len({(u, t.date()) for u, _a, t, _o, s in rows if s == 1 and _a == 0
                      and D.date(2021, 7, 7) <= t.date() <= D.date(2021, 10, 31)})
            s1, _ = compile_rule(RULES / "r1_original.dsl")
            s2, _ = compile_rule(RULES / "r1_original.dsl", force_tier=2)
            r1, t1 = q(s1, timed=True)
            try:
                _ro, to = q(orig, timed=True)
                t_orig = f"{to*1000:8.1f} ms"
            except RuntimeError:
                t_orig = "     n/a"
            try:
                r2, t2 = q(s2, timed=True)
                print(f"  {nusers:5} 用户 / {len(rows):7} 行 / {nd:6} 人日: "
                      f"Tier1 {t1*1000:7.1f} ms | Tier2 {t2*1000:8.1f} ms | @变量原版 {t_orig} | "
                      f"Tier1 加速 {t2/t1:4.1f}x | 一致={norm(r1) == norm(r2)}")
            except RuntimeError as e:
                print(f"  {nusers} 用户: Tier1 {t1*1000:.1f} ms, Tier2 失败: {str(e)[:120]}")

    bad = [r for r in results if not (r[1] and r[2])]
    print(f"\n汇总: {len(results)-len(bad)}/{len(results)} 条规则、两种后端全部与参考实现一致")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
