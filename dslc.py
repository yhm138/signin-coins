#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dslc.py —— 签到规则 DSL 编译器（零依赖，纯标准库）

设计目标
--------
人写的：过程式、逐行、顺序赋值语义（和 MySQL 用户变量写法一样直观）
机器产的：集合式 SQL（窗口函数），只在必要时退化为递归 CTE

管线
----
  DSL 文本
    ↓ Lexer / Parser（Pratt 表达式解析）
  AST
    ↓ Desugar（把 `day = prev(day)+1` / `datediff(day,prev(day))` 归一成 gap()）
    ↓ 抽象解释（SymbolicAlg）：
        ① 断签/首行是否把状态重置为「与旧状态无关的常量」？
        ② 状态转移是否与绝对日历日期无关？
    ↓ 若 ①② 均成立 → Tier 1：把状态机沿「连续签到」转移展开成有限日程表
         （做 ρ-环检测：前缀 + 周期），数据侧只剩 islands + ROW_NUMBER + JOIN
      否则         → Tier 2：递归 CTE 逐日状态机（语义完全一致，慢但通用）
    ↓ SQL（MySQL 8.0 / MariaDB 10.2+）

同一套语句执行器 + 三种「值代数」：
  ConcreteAlg  —— 具体求值（参考实现 / 展开日程表）
  SymbolicAlg  —— 符号求值（可优化性判定，带路径分叉与合并）
  SqlAlg       —— 求值即生成 SQL 表达式（顺序赋值用内联消解，分支用 IF 合并）
"""
from __future__ import annotations

import argparse
import datetime as D
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any

# =====================================================================
# 1. 词法
# =====================================================================
TOKEN_RE = re.compile(
    r"""(?P<ws>\s+)
      | (?P<comment>\#[^\n]*|--[^\n]*)
      | (?P<date>\d{4}-\d{2}-\d{2})
      | (?P<num>\d+\.\d+|\d+)
      | (?P<str>'(?:[^']|'')*')
      | (?P<assign>:=)
      | (?P<op><>|!=|<=|>=|=|<|>|\+|-|\*|/|%|\(|\)|\{|\}|\.|,)
      | (?P<ident>[A-Za-z_][A-Za-z_0-9]*)
    """,
    re.X,
)


class DslError(Exception):
    pass


def lex(src: str):
    toks, i = [], 0
    while i < len(src):
        m = TOKEN_RE.match(src, i)
        if not m:
            line = src.count("\n", 0, i) + 1
            raise DslError(f"第 {line} 行无法识别的字符: {src[i:i+20]!r}")
        kind, val = m.lastgroup, m.group()
        if kind not in ("ws", "comment"):
            toks.append((kind, val, i))
        i = m.end()
    toks.append(("eof", "", len(src)))
    return toks


# =====================================================================
# 2. AST
# =====================================================================
@dataclass(frozen=True)
class Lit:
    v: Any


@dataclass(frozen=True)
class Ident:
    name: str


@dataclass(frozen=True)
class Prev:
    name: str


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple


@dataclass(frozen=True)
class Bin:
    op: str
    l: Any
    r: Any


@dataclass(frozen=True)
class Un:
    op: str
    e: Any


@dataclass(frozen=True)
class Sym:
    """仅抽象解释期使用的符号值"""
    name: str
    ne1: bool = False  # 已知 != 1（断签时的 gap）


@dataclass
class Assign:
    name: str
    expr: Any


@dataclass
class If:
    cond: Any
    then: list
    els: list


@dataclass
class Emit:
    items: list  # [(name, expr)]


@dataclass
class Program:
    table: str = ""
    where: Any = None
    entity: str = "uid"
    day: Any = None
    start: D.date = None
    end: D.date = None
    init: dict = field(default_factory=dict)
    body: list = field(default_factory=list)
    report: str = ""


# =====================================================================
# 3. 语法分析
# =====================================================================
PREC = {"or": 1, "and": 2, "=": 4, "!=": 4, "<>": 4, "<": 4, "<=": 4, ">": 4, ">=": 4,
        "+": 5, "-": 5, "*": 6, "/": 6, "%": 6}
KEYWORDS = {"and", "or", "not", "null", "true", "false"}


class Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = lex(src)
        self.p = 0

    # --- token helpers ---
    def peek(self, k=0):
        return self.toks[self.p + k]

    def kind(self):
        return self.peek()[0]

    def val(self):
        return self.peek()[1]

    def at(self, val):
        k, v, _ = self.peek()
        return v.lower() == val.lower() and k in ("ident", "op", "assign")

    def take(self):
        t = self.peek()
        self.p += 1
        return t

    def expect(self, val):
        if not self.at(val):
            self.err(f"期望 {val!r}")
        return self.take()

    def err(self, msg):
        line = self.src.count("\n", 0, self.peek()[2]) + 1
        raise DslError(f"第 {line} 行: {msg}（读到 {self.val()!r}）")

    # --- program ---
    def parse_program(self) -> Program:
        prog = Program()
        while self.kind() != "eof":
            if self.at("source"):
                self.take()
                self.parse_source(prog)
            elif self.at("state"):
                self.take()
                self.expect("{")
                while not self.at("}"):
                    name = self.take()[1]
                    self.expect(":=")
                    e = self.expr()
                    v = ConcreteAlg().eval(e, {}, Ctx())
                    if isinstance(v, (Bin, Call, Un, Sym, Ident)):
                        self.err("state 初值必须是字面量")
                    prog.init[name] = v
                self.expect("}")
            elif self.at("scan"):
                self.take()
                self.expect("{")
                prog.body = self.stmts()
                self.expect("}")
            elif self.at("report"):
                self.take()
                prog.report = self.raw_block()
            else:
                self.err("期望 source / state / scan / report 块")
        if not prog.table or prog.day is None or not prog.body:
            raise DslError("source.from / source.day / scan 块不能为空")
        return prog

    def raw_block(self) -> str:
        """report 块里是原生 SQL，按花括号配对整体截取"""
        self.expect("{")
        depth, start = 1, self.peek()[2]
        while True:
            k, v, pos = self.take()
            if k == "eof":
                self.err("report 块缺少 }")
            if v == "{":
                depth += 1
            elif v == "}":
                depth -= 1
                if depth == 0:
                    return self.src[start:pos].strip()

    def parse_source(self, prog: Program):
        self.expect("{")
        while not self.at("}"):
            key = self.take()[1].lower()
            if key == "from":
                prog.table = self.take()[1]
            elif key == "where":
                prog.where = self.expr()
            elif key == "entity":
                prog.entity = self.take()[1]
            elif key == "day":
                prog.day = self.expr()
            elif key == "between":
                prog.start = self.date_lit()
                self.expect(",")
                prog.end = self.date_lit()
            else:
                self.err(f"source 块未知字段 {key}")
        self.expect("}")

    def date_lit(self) -> D.date:
        k, v, _ = self.take()
        if k != "date":
            self.err("期望 YYYY-MM-DD 日期")
        return D.date.fromisoformat(v)

    # --- statements ---
    def stmts(self) -> list:
        out = []
        while not self.at("}") and self.kind() != "eof":
            out.append(self.stmt())
        return out

    def stmt(self):
        if self.at("if"):
            self.take()
            cond = self.expr()
            self.expect("{")
            then = self.stmts()
            self.expect("}")
            els = []
            if self.at("else"):
                self.take()
                if self.at("if"):
                    els = [self.stmt()]
                else:
                    self.expect("{")
                    els = self.stmts()
                    self.expect("}")
            return If(cond, then, els)
        if self.at("emit"):
            self.take()
            items = []
            while True:
                e = self.expr()
                self.expect("as")
                items.append((self.take()[1], e))
                if self.at(","):
                    self.take()
                    continue
                break
            return Emit(items)
        if self.kind() == "ident":
            name = self.take()[1]
            self.expect(":=")
            return Assign(name, self.expr())
        self.err("期望 if / emit / 赋值语句")

    # --- expressions (Pratt) ---
    def expr(self, min_prec=1):
        left = self.atom()
        while True:
            k, v, _ = self.peek()
            op = v.lower()
            if k not in ("op", "ident") or op not in PREC or PREC[op] < min_prec:
                break
            self.take()
            right = self.expr(PREC[op] + 1)
            left = Bin(op, left, right)
        return left

    def atom(self):
        k, v, _ = self.peek()
        if self.at("("):
            self.take()
            e = self.expr()
            self.expect(")")
            return e
        if self.at("not"):
            self.take()
            return Un("not", self.expr(3))
        if v == "-" and k == "op":
            self.take()
            return Un("-", self.expr(6))
        if k == "num":
            self.take()
            return Lit(float(v) if "." in v else int(v))
        if k == "date":
            self.take()
            return Lit(D.date.fromisoformat(v))
        if k == "str":
            self.take()
            return Lit(v[1:-1].replace("''", "'"))
        if k == "ident":
            low = v.lower()
            if low in ("null", "true", "false"):
                self.take()
                return Lit({"null": None, "true": 1, "false": 0}[low])
            self.take()
            if self.at("("):
                self.take()
                args = []
                if not self.at(")"):
                    while True:
                        args.append(self.expr())
                        if self.at(","):
                            self.take()
                            continue
                        break
                self.expect(")")
                if low == "prev":
                    if len(args) != 1 or not isinstance(args[0], Ident):
                        self.err("prev() 只接受一个变量名，如 prev(day)")
                    return Prev(args[0].name)
                return Call(low, tuple(args))
            return Ident(v)
        self.err("期望表达式")


# =====================================================================
# 4. Desugar：把常见的「连续判定」写法归一为 gap()
#    gap() = 与上一行的天数差；组内首行为 NULL
# =====================================================================
def desugar(e):
    if isinstance(e, Bin):
        l, r = desugar(e.l), desugar(e.r)
        # datediff(day, prev(day)) -> gap()
        if (isinstance(l, Ident) and l.name == "day" and isinstance(r, Prev)
                and r.name == "day" and e.op == "-"):
            return Call("gap", ())
        # day = prev(day) + 1  /  prev(day) + 1 = day   ->  gap() = 1
        for a, b in ((l, r), (r, l)):
            if (e.op == "=" and isinstance(a, Ident) and a.name == "day"
                    and isinstance(b, Bin) and b.op == "+"
                    and isinstance(b.l, Prev) and b.l.name == "day"
                    and isinstance(b.r, Lit) and b.r.v == 1):
                return Bin("=", Call("gap", ()), Lit(1))
        return Bin(e.op, l, r)
    if isinstance(e, Call):
        args = tuple(desugar(a) for a in e.args)
        if (e.name == "datediff" and len(args) == 2
                and isinstance(args[0], Ident) and args[0].name == "day"
                and isinstance(args[1], Prev) and args[1].name == "day"):
            return Call("gap", ())
        return Call(e.name, args)
    if isinstance(e, Un):
        return Un(e.op, desugar(e.e))
    return e


def desugar_stmts(ss):
    out = []
    for s in ss:
        if isinstance(s, Assign):
            out.append(Assign(s.name, desugar(s.expr)))
        elif isinstance(s, If):
            out.append(If(desugar(s.cond), desugar_stmts(s.then), desugar_stmts(s.els)))
        else:
            out.append(Emit([(n, desugar(x)) for n, x in s.items]))
    return out


# =====================================================================
# 5. 值代数 + 语句执行器
# =====================================================================
@dataclass
class Ctx:
    day: Any = None
    prev_day: Any = None
    gap: Any = None
    prev_state: dict = field(default_factory=dict)


def is_date(v):
    return isinstance(v, D.date)


class Interp:
    """共用的语句执行器；分支语义由 truth()/merge() 决定"""

    def run(self, stmts, env, ctx):
        for s in stmts:
            if isinstance(s, Assign):
                env[s.name] = self.eval(s.expr, env, ctx)
            elif isinstance(s, If):
                c = self.eval(s.cond, env, ctx)
                t = self.truth(c)
                if t is True:
                    env = self.run(s.then, env, ctx)
                elif t is False:
                    env = self.run(s.els, env, ctx)
                else:  # 条件未知：两路都走，再合并
                    a = self.run(s.then, dict(env), ctx)
                    b = self.run(s.els, dict(env), ctx)
                    env = self.merge_env(c, a, b, env)
            elif isinstance(s, Emit):
                for name, x in s.items:
                    env["\0emit_" + name] = self.eval(x, env, ctx)
        return env

    null_value = None

    def merge_env(self, cond, a, b, old):
        out = dict(old)
        for k in set(a) | set(b):
            av = a.get(k, old.get(k, self.null_value))
            bv = b.get(k, old.get(k, self.null_value))
            if av is None:
                av = self.null_value
            if bv is None:
                bv = self.null_value
            out[k] = self.merge(cond, av, bv)
        return out

    def emits(self, env):
        return {k[6:]: v for k, v in env.items() if k.startswith("\0emit_")}


# ---------- 5.1 具体求值（SQL NULL 语义） ----------
def sql_and(a, b):
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def sql_or(a, b):
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def as_bool(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v != "" and v != "0"
    return bool(v)


FUNCS_CONCRETE = {
    "mod": lambda a, b: None if a is None or b is None or b == 0 else a % b,
    "least": lambda *a: None if any(x is None for x in a) else min(a),
    "greatest": lambda *a: None if any(x is None for x in a) else max(a),
    "ifnull": lambda a, b: b if a is None else a,
    "coalesce": lambda *a: next((x for x in a if x is not None), None),
    "if": lambda c, a, b: a if as_bool(c) is True else b,
    "abs": lambda a: None if a is None else abs(a),
    "floor": lambda a: None if a is None else int(a // 1),
    "ceil": lambda a: None if a is None else -int(-a // 1),
    "datediff": lambda a, b: None if a is None or b is None else (a - b).days,
    "date_add": lambda a, n: None if a is None or n is None else a + D.timedelta(days=int(n)),
    "date_sub": lambda a, n: None if a is None or n is None else a - D.timedelta(days=int(n)),
    "dayofweek": lambda a: None if a is None else (a.weekday() + 1) % 7 + 1,  # MySQL: 1=周日
    "weekday": lambda a: None if a is None else a.weekday(),                  # 0=周一
    "day": lambda a: None if a is None else a.day,
    "month": lambda a: None if a is None else a.month,
    "year": lambda a: None if a is None else a.year,
    "date": lambda a: a,
}


class ConcreteAlg(Interp):
    def truth(self, v):
        return as_bool(v) is True

    def merge(self, cond, a, b):
        return a  # 具体模式下不会走到

    def eval(self, e, env, ctx):
        if isinstance(e, Lit):
            return e.v
        if isinstance(e, Ident):
            if e.name == "day":
                return ctx.day
            if e.name in env:
                return env[e.name]
            raise DslError(f"未定义的变量 {e.name}")
        if isinstance(e, Prev):
            if e.name == "day":
                return ctx.prev_day
            return ctx.prev_state.get(e.name)
        if isinstance(e, Call):
            if e.name == "gap":
                return ctx.gap
            args = [self.eval(a, env, ctx) for a in e.args]
            if e.name not in FUNCS_CONCRETE:
                raise DslError(f"未知函数 {e.name}")
            return FUNCS_CONCRETE[e.name](*args)
        if isinstance(e, Un):
            v = self.eval(e.e, env, ctx)
            if v is None:
                return None
            return -v if e.op == "-" else as_bool(not as_bool(v))
        if isinstance(e, Bin):
            a, b = self.eval(e.l, env, ctx), self.eval(e.r, env, ctx)
            op = e.op
            if op == "and":
                return sql_and(as_bool(a), as_bool(b))
            if op == "or":
                return sql_or(as_bool(a), as_bool(b))
            if a is None or b is None:
                return None
            if is_date(a) and isinstance(b, int) and op in ("+", "-"):
                return a + D.timedelta(days=b if op == "+" else -b)
            if is_date(a) and is_date(b) and op == "-":
                return (a - b).days
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op == "/":
                return a / b
            if op == "%":
                return a % b if b else None
            if op == "=":
                return a == b
            if op in ("!=", "<>"):
                return a != b
            if op == "<":
                return a < b
            if op == "<=":
                return a <= b
            if op == ">":
                return a > b
            if op == ">=":
                return a >= b
        raise DslError(f"无法求值 {e}")


# ---------- 5.2 符号求值（可优化性判定） ----------
def free_syms(e, acc=None):
    acc = set() if acc is None else acc
    if isinstance(e, Sym):
        acc.add(e.name)
    elif isinstance(e, Bin):
        free_syms(e.l, acc)
        free_syms(e.r, acc)
    elif isinstance(e, Un):
        free_syms(e.e, acc)
    elif isinstance(e, Call):
        for a in e.args:
            free_syms(a, acc)
    return acc


class SymbolicAlg(ConcreteAlg):
    """值 = 具体 Python 值 或 AST（含 Sym）。条件未知则分叉，用 IF 树合并。"""

    def truth(self, v):
        if isinstance(v, (Bin, Un, Call, Sym, Ident, Prev)):
            return None
        return as_bool(v) is True

    def merge(self, cond, a, b):
        if a == b:
            return a
        return Call("if", (cond, a, b))

    def eval(self, e, env, ctx):
        if isinstance(e, (Lit, Ident, Prev)):
            return super().eval(e, env, ctx)
        if isinstance(e, Call):
            if e.name == "gap":
                return ctx.gap
            if e.name not in FUNCS_CONCRETE:
                raise DslError(f"未知函数 {e.name}")
            args = [self.eval(a, env, ctx) for a in e.args]
            if all(not isinstance(a, (Bin, Un, Call, Sym)) for a in args):
                return FUNCS_CONCRETE[e.name](*args)
            return Call(e.name, tuple(args))
        if isinstance(e, Un):
            v = self.eval(e.e, env, ctx)
            if not isinstance(v, (Bin, Un, Call, Sym)):
                return super().eval(Un(e.op, Lit(v)), {}, ctx)
            return Un(e.op, v)
        if isinstance(e, Bin):
            a, b = self.eval(e.l, env, ctx), self.eval(e.r, env, ctx)
            sa = isinstance(a, (Bin, Un, Call, Sym))
            sb = isinstance(b, (Bin, Un, Call, Sym))
            if not sa and not sb:
                return super().eval(Bin(e.op, Lit(a), Lit(b)), {}, ctx)
            # 三值逻辑短路化简：能定死就定死，避免无谓分叉
            if e.op in ("and", "or"):
                av = UNK if sa else as_bool(a)
                bv = UNK if sb else as_bool(b)
                if e.op == "and":
                    if av is False or bv is False:
                        return False
                    if av is None or bv is None:   # NULL AND x 永不为真
                        return None
                    if av is True:
                        return b
                    if bv is True:
                        return a
                else:
                    if av is True or bv is True:
                        return True
                    if (av is None and bv is False) or (bv is None and av is False):
                        return None
                    if av is False:
                        return b
                    if bv is False:
                        return a
                return Bin(e.op, a, b)
            # 断签模式：gap 已知 != 1（但 gap 到底是多少仍未知）
            for x, y in ((a, b), (b, a)):
                if isinstance(x, Sym) and x.ne1 and isinstance(y, int):
                    if e.op == "=" and y == 1:
                        return False
                    if e.op in ("!=", "<>") and y == 1:
                        return True
            return Bin(e.op, a, b)
        raise DslError(f"符号求值失败 {e}")


# ---------- 5.3 求值即生成 SQL ----------
UNK = object()   # 编译期未知


@dataclass(frozen=True)
class Sql:
    t: str
    d: bool = False      # 是否 DATE 类型（决定 + - 是日期运算还是算术）
    c: Any = UNK         # 编译期已知常量值（用于常量折叠）

    def __str__(self):
        return self.t


def sql_lit(v):
    if v is None:
        return Sql("NULL", False, None)
    if isinstance(v, D.date):
        return Sql(f"DATE '{v.isoformat()}'", True, v)
    if isinstance(v, str):
        return Sql("'" + v.replace("'", "''") + "'", False, v)
    if isinstance(v, bool):
        return Sql("1" if v else "0", False, int(v))
    if isinstance(v, float) and v.is_integer():
        return Sql(str(int(v)), False, int(v))
    return Sql(str(v), False, v)


def _fold(e, vals):
    if any(v is UNK for v in vals):
        return UNK
    try:
        return ConcreteAlg().eval(e, {}, Ctx())
    except Exception:
        return UNK


# DSL 函数 -> SQL 片段模板
FUNCS_SQL = {
    "mod": lambda a: f"MOD({a[0]}, {a[1]})",
    "least": lambda a: "LEAST(" + ", ".join(a) + ")",
    "greatest": lambda a: "GREATEST(" + ", ".join(a) + ")",
    "ifnull": lambda a: f"IFNULL({a[0]}, {a[1]})",
    "coalesce": lambda a: "COALESCE(" + ", ".join(a) + ")",
    "if": lambda a: f"IF({a[0]}, {a[1]}, {a[2]})",
    "abs": lambda a: f"ABS({a[0]})",
    "floor": lambda a: f"FLOOR({a[0]})",
    "ceil": lambda a: f"CEIL({a[0]})",
    "datediff": lambda a: f"DATEDIFF({a[0]}, {a[1]})",
    "date_add": lambda a: f"DATE_ADD({a[0]}, INTERVAL {a[1]} DAY)",
    "date_sub": lambda a: f"DATE_SUB({a[0]}, INTERVAL {a[1]} DAY)",
    "dayofweek": lambda a: f"DAYOFWEEK({a[0]})",
    "weekday": lambda a: f"WEEKDAY({a[0]})",
    "day": lambda a: f"DAY({a[0]})",
    "month": lambda a: f"MONTH({a[0]})",
    "year": lambda a: f"YEAR({a[0]})",
    "date": lambda a: f"DATE({a[0]})",
    "date_format": lambda a: f"DATE_FORMAT({a[0]}, {a[1]})",
}
DATE_FUNCS = {"date_add", "date_sub", "date"}


class SqlAlg(Interp):
    null_value = Sql("NULL")

    def __init__(self, allow_columns=False):
        self.allow_columns = allow_columns

    def truth(self, v):
        return None  # 永远两路都走，用 IF 合并

    def merge(self, cond, a, b):
        if a.t == b.t:
            return a
        if cond.c is not UNK:                      # 常量折叠：条件编译期已知
            return a if as_bool(cond.c) is True else b
        return Sql(f"IF({cond.t}, {a.t}, {b.t})", a.d and b.d)

    def eval(self, e, env, ctx):
        if isinstance(e, Lit):
            return sql_lit(e.v)
        if isinstance(e, Ident):
            if e.name == "day":
                return ctx.day
            if e.name in env:
                return env[e.name]
            if self.allow_columns:
                return Sql(e.name)
            raise DslError(f"未定义的变量 {e.name}")
        if isinstance(e, Prev):
            return ctx.prev_day if e.name == "day" else ctx.prev_state[e.name]
        if isinstance(e, Call):
            if e.name == "gap":
                return ctx.gap
            args = [self.eval(a, env, ctx) for a in e.args]
            if e.name not in FUNCS_SQL:
                raise DslError(f"函数 {e.name} 没有 SQL 映射")
            cv = UNK
            if all(a.c is not UNK for a in args) and e.name in FUNCS_CONCRETE:
                cv = _fold(Call(e.name, tuple(Lit(a.c) for a in args)), [a.c for a in args])
            return Sql(FUNCS_SQL[e.name]([a.t for a in args]), e.name in DATE_FUNCS, cv)
        if isinstance(e, Un):
            v = self.eval(e.e, env, ctx)
            cv = _fold(Un(e.op, Lit(v.c)), [v.c])
            return Sql(f"(-{v.t})", False, cv) if e.op == "-" else Sql(f"(NOT {v.t})", False, cv)
        if isinstance(e, Bin):
            a, b = self.eval(e.l, env, ctx), self.eval(e.r, env, ctx)
            op = e.op
            cv = _fold(Bin(op, Lit(a.c), Lit(b.c)), [a.c, b.c])
            if op in ("and", "or"):
                return Sql(f"({a.t} {op.upper()} {b.t})", False, cv)
            if op in ("+", "-") and a.d and not b.d:
                fn = "DATE_ADD" if op == "+" else "DATE_SUB"
                return Sql(f"{fn}({a.t}, INTERVAL {b.t} DAY)", True, cv)
            if op == "-" and a.d and b.d:
                return Sql(f"DATEDIFF({a.t}, {b.t})", False, cv)
            sql_op = "=" if op == "=" else ("<>" if op in ("!=", "<>") else op)
            return Sql(f"({a.t} {sql_op} {b.t})", False, cv)
        raise DslError(f"无法生成 SQL: {e}")


# =====================================================================
# 6. 编译
# =====================================================================
def expr_sql(e, allow_columns=True):
    return SqlAlg(allow_columns).eval(e, {}, Ctx(day=Sql("dt", True)))


def state_names(prog):
    return list(prog.init)


def row_local_names(prog):
    """scan 里赋值但未在 state 声明的名字 = 行内局部变量"""
    names = []

    def walk(ss):
        for s in ss:
            if isinstance(s, Assign) and s.name not in prog.init and s.name not in names:
                names.append(s.name)
            elif isinstance(s, If):
                walk(s.then)
                walk(s.els)
    walk(prog.body)
    return names


def symbolic_run(prog, mode):
    """mode: first / break / continue —— 返回 (状态表达式, emit 表达式)"""
    st = {k: Sym("st_" + k) for k in prog.init}
    day = Sym("day")
    if mode == "first":
        ctx = Ctx(day=day, prev_day=None, gap=None, prev_state={k: None for k in prog.init})
    elif mode == "break":
        ctx = Ctx(day=day, prev_day=Sym("prev_day"), gap=Sym("gap", ne1=True),
                  prev_state=dict(st))
    else:
        ctx = Ctx(day=day, prev_day=Sym("prev_day"), gap=1, prev_state=dict(st))
    alg = SymbolicAlg()
    env = alg.run(prog.body, dict(st), ctx)
    return {k: env[k] for k in prog.init}, alg.emits(env)


def concrete_run(prog, state, day, prev_day, first=False):
    gap = None if prev_day is None else (day - prev_day).days
    ctx = Ctx(day=day, prev_day=prev_day, gap=gap,
              prev_state=({k: None for k in prog.init} if first else dict(state)))
    alg = ConcreteAlg()
    env = alg.run(prog.body, dict(state), ctx)
    return {k: env[k] for k in prog.init}, alg.emits(env)


def analyze(prog):
    """判定能否走 Tier 1，并给出理由"""
    info = {"state": state_names(prog), "locals": row_local_names(prog)}
    post_first, _ = symbolic_run(prog, "first")
    post_break, _ = symbolic_run(prog, "break")
    post_cont, _ = symbolic_run(prog, "continue")

    def concrete(v):
        return not isinstance(v, (Bin, Un, Call, Sym, Ident, Prev))

    reasons = []
    for label, post in (("首行", post_first), ("断签", post_break)):
        bad = [k for k, v in post.items() if not concrete(v)]
        if bad:
            reasons.append(f"{label}后状态 {bad} 仍依赖旧状态/日历（不是常量重置）")
    if not reasons and post_first != post_break:
        reasons.append("首行与断签的重置状态不一致")
    dep = set()
    for v in post_cont.values():
        dep |= {s for s in free_syms(v) if s in ("day", "prev_day")}
    if dep:
        reasons.append(f"连续签到的状态转移依赖绝对日期 {sorted(dep)}")
    info["tier"] = 2 if reasons else 1
    info["reasons"] = reasons
    info["reset_state"] = post_break if not reasons else None
    return info


def unroll(prog, info, bound):
    """沿「连续签到」转移展开行入口状态，ρ-环检测压缩成 前缀+周期

    注意：第 1 天走的是「首行转移」(gap=NULL)，第 2 天起走「续签转移」(gap=1)，
    两者是不同的函数，所以环检测只能从 k=2 开始，首行永远是纯前缀。
    """
    day0 = prog.start or D.date(2021, 7, 7)
    entry = dict(prog.init)                       # 第 1 天的入口状态 = 声明初值
    seq = [(1, dict(entry))]
    entry, _ = concrete_run(prog, entry, day0, None, first=True)   # 首行转移
    seen = {}
    prefix, period = None, None
    k = 2
    while k <= bound:
        key = tuple(entry[n] for n in prog.init)
        if key in seen:
            prefix, period = seen[key], k - seen[key]
            break
        seen[key] = k
        seq.append((k, dict(entry)))
        entry, _ = concrete_run(prog, entry, day0 + D.timedelta(days=k - 1),
                                day0 + D.timedelta(days=k - 2))
        k += 1
    info["schedule_len"] = len(seq)
    info["cycle_prefix"] = prefix
    info["cycle_period"] = period
    info["truncated"] = period is None
    return seq, prefix, period


def reify(v):
    """把符号求值的结果还原成普通 DSL AST，便于交给 SqlAlg 渲染 + 常量折叠"""
    if isinstance(v, Sym):
        return Ident("__sym_" + v.name)
    if isinstance(v, Bin):
        return Bin(v.op, reify(v.l), reify(v.r))
    if isinstance(v, Un):
        return Un(v.op, reify(v.e))
    if isinstance(v, Call):
        return Call(v.name, tuple(reify(a) for a in v.args))
    return Lit(v)


def compile_body_sql(prog, entry_map, ctx):
    """顺序赋值 → 内联；分支 → IF 合并。返回 (post 状态 SQL, emit SQL)"""
    alg = SqlAlg()
    env = alg.run(prog.body, dict(entry_map), ctx)
    return {k: env[k] for k in prog.init}, alg.emits(env)


def cast_sql(v_expr, sample):
    if isinstance(sample, D.date):
        return f"CAST({v_expr} AS DATE)"
    if isinstance(sample, float):
        return f"CAST({v_expr} AS DECIMAL(24,6))"
    if isinstance(sample, str):
        return f"CAST({v_expr} AS CHAR(255))"
    return f"CAST({v_expr} AS SIGNED)"


def emit_names(prog):
    names = []

    def collect(ss):
        for s in ss:
            if isinstance(s, Emit):
                names.extend(n for n, _ in s.items if n not in names)
            elif isinstance(s, If):
                collect(s.then)
                collect(s.els)
    collect(prog.body)
    return names


def gen_sql(prog, force_tier=None, max_unroll=None, verbose_info=None):
    prog.body = desugar_stmts(prog.body)
    info = analyze(prog)
    tier = force_tier or info["tier"]
    if force_tier == 1 and info["tier"] == 2:
        raise DslError("不能强制 Tier 1：本规则不满足常量重置/日期无关条件，"
                       "强行展开会算错。原因: " + "; ".join(info["reasons"]))
    ent = prog.entity
    day_sql = expr_sql(prog.day).t
    where_sql = expr_sql(prog.where).t if prog.where is not None else "1=1"
    # 优化：day = date(col) 时把日期谓词改写成 col 的范围条件，让索引能用上
    sarg = ""
    if (isinstance(prog.day, Call) and prog.day.name == "date"
            and len(prog.day.args) == 1 and isinstance(prog.day.args[0], Ident)):
        col = prog.day.args[0].name
        nxt = prog.end + D.timedelta(days=1)
        sarg = (f"      AND {col} >= '{prog.start} 00:00:00' AND {col} < '{nxt} 00:00:00'\n")
        info["sargable_pushdown"] = col
    d_cte = (
        f"d AS (\n"
        f"    SELECT DISTINCT {ent} AS uid, {day_sql} AS dt\n"
        f"    FROM {prog.table}\n"
        f"    WHERE {where_sql}\n"
        + (sarg if sarg else
           f"      AND {day_sql} >= DATE '{prog.start}' AND {day_sql} <= DATE '{prog.end}'\n")
        + f")"
    )
    emits = emit_names(prog)
    info["emits"] = emits
    info["tier_used"] = tier

    if tier == 1:
        bound = max_unroll or ((prog.end - prog.start).days + 2)
        sched, prefix, period = unroll(prog, info, bound)
        max_island = (prog.end - prog.start).days + 1
        if period is None and bound < max_island:
            raise DslError(f"状态轨迹无环且展开上界 {bound} < 窗口内最长可能连续天数 "
                           f"{max_island}，会算错；请增大 --max-unroll")

        # ---- 部分求值：把每个 k 的 emit 在编译期算成常量（只要与日历无关）----
        const_rows, all_const = [], True
        for k, st in sched:
            sctx = Ctx(day=Sym("day"), prev_day=Sym("prev_day"),
                       gap=(None if k == 1 else 1),
                       prev_state={n: (None if k == 1 else st[n]) for n in prog.init})
            alg = SymbolicAlg()
            em = alg.emits(alg.run(prog.body, dict(st), sctx))
            if any(isinstance(v, (Bin, Un, Call, Sym)) for v in em.values()):
                all_const = False
                break
            const_rows.append((k, em))
        # k=1 与 k=1+period 的 gap 不同，若首行落在环里则不能折叠
        if period is not None and prefix == 1:
            all_const = False
        info["const_folded"] = all_const

        if period is None:
            idx = "n.k"
        elif prefix == 1:
            idx = f"MOD(n.k - 1, {period}) + 1"
        else:
            idx = f"IF(n.k < {prefix}, n.k, {prefix} + MOD(n.k - {prefix}, {period}))"
        info["index_expr"] = idx

        if all_const:
            # ===== 模式 A：规则整条被编译成一张常量日程表 =====
            used = []
            rows = ["SELECT " + ", ".join([str(k)] + [sql_lit(em[n]).t for n in emits])
                    for k, em in const_rows]
            header = "SELECT " + ", ".join(
                [f"{const_rows[0][0]} AS k"] +
                [f"{sql_lit(const_rows[0][1][n]).t} AS {n}" for n in emits])
            sched_cte = "sched AS (\n    " + "\n    UNION ALL ".join([header] + rows[1:]) + "\n)"
            emit_cols = ", ".join(f"s.{n}" for n in emits)
            info["schedule_cols"] = emits
        else:
            # ===== 模式 B：逐行部分求值，状态全部折叠，只剩日期相关表达式 =====
            day_sql_v = Sql("n.dt", True)
            prev_sql_v = Sql("LAG(n.dt) OVER (PARTITION BY n.uid ORDER BY n.dt)", True)
            senv = {"__sym_day": day_sql_v, "__sym_prev_day": prev_sql_v}
            sym_vals = {n: [] for n in emits}
            for k, st in sched:
                sctx = Ctx(day=Sym("day"), prev_day=Sym("prev_day"),
                           gap=(None if k == 1 else 1),
                           prev_state={n2: (None if k == 1 else st[n2]) for n2 in prog.init})
                alg = SymbolicAlg()
                em = alg.emits(alg.run(prog.body, dict(st), sctx))
                for n in emits:
                    sym_vals[n].append(em[n])

            def render(v):
                return SqlAlg().eval(reify(v), dict(senv), Ctx()).t

            def render_list(vs):
                txts = [render(v) for v in vs]
                return txts[0] if len(set(txts)) == 1 else f"ELT({idx}, " + ", ".join(txts) + ")"

            parts, hoisted = [], False
            for n in emits:
                vs = sym_vals[n]
                if (all(isinstance(v, Call) and v.name == "if" and len(v.args) == 3 for v in vs)
                        and all(v.args[0] == vs[0].args[0] for v in vs)):
                    # 条件外提：IF(cond, ELT(...), ELT(...)) 而不是 ELT(..., IF(cond,..), ...)
                    cond = render(vs[0].args[0])
                    txt = (f"IF({cond}, {render_list([v.args[1] for v in vs])}, "
                           f"{render_list([v.args[2] for v in vs])})")
                    hoisted = True
                else:
                    txt = render_list(vs)
                parts.append(txt + f" AS {n}")
            emit_cols = ",\n           ".join(parts)
            sched_cte = None
            info["schedule_cols"] = []
            info["cond_hoisted"] = hoisted

        sql = "\n".join([
            "WITH " + d_cte + ",",
            "g AS (   -- 连续日期分段：日期 - 行号 = 常量",
            "    SELECT uid, dt, TO_DAYS(dt) - ROW_NUMBER() OVER (PARTITION BY uid ORDER BY dt) AS grp",
            "    FROM d",
            "),",
            "n AS (   -- 段内第几天",
            "    SELECT uid, dt, ROW_NUMBER() OVER (PARTITION BY uid, grp ORDER BY dt) AS k",
            "    FROM g",
            "),",
            f"-- 编译器展开的状态轨迹：{len(sched)} 行"
            + (f"（前缀 {prefix-1} + 周期 {period}）" if period else "（无环，按窗口上界展开）"),
        ] + ([sched_cte + ","] if sched_cte else []) + [
            "daily AS (",
            f"    SELECT n.uid AS {ent}, n.dt, {emit_cols}",
            ("    FROM n JOIN sched s ON s.k = " + idx) if sched_cte else "    FROM n",
            ")",
            prog.report,
        ])
    else:
        seq_cte = ("seq AS (\n    SELECT uid, dt, ROW_NUMBER() OVER (PARTITION BY uid ORDER BY dt) AS i\n"
                   "    FROM d\n)")
        base_entry = {k: sql_lit(v) for k, v in prog.init.items()}
        base_ctx = Ctx(day=Sql("s.dt", True), prev_day=Sql("NULL", True), gap=Sql("NULL"),
                       prev_state={k: Sql("NULL") for k in prog.init})
        base_post, base_emit = compile_body_sql(prog, base_entry, base_ctx)
        day0 = prog.start
        sample_post, sample_emit = concrete_run(prog, prog.init, day0, None, first=True)
        rec_entry = {k: Sql(f"w.st_{k}", isinstance(prog.init[k], D.date)) for k in prog.init}
        rec_ctx = Ctx(day=Sql("s.dt", True), prev_day=Sql("w.dt", True),
                      gap=Sql("DATEDIFF(s.dt, w.dt)"), prev_state=dict(rec_entry))
        rec_post, rec_emit = compile_body_sql(prog, rec_entry, rec_ctx)
        base_cols = ["s.uid", "s.dt", "s.i"] + [
            f"{cast_sql(base_post[k].t, sample_post[k])} AS st_{k}" for k in prog.init] + [
            f"{cast_sql(base_emit[n].t, sample_emit.get(n))} AS {n}" for n in emits]
        rec_cols = ["s.uid", "s.dt", "s.i"] + [rec_post[k].t for k in prog.init] + [
            rec_emit[n].t for n in emits]
        sql = "\n".join([
            "WITH RECURSIVE " + d_cte + ",",
            seq_cte + ",",
            "walk AS (",
            "    SELECT " + ",\n           ".join(base_cols),
            "    FROM seq s WHERE s.i = 1",
            "    UNION ALL",
            "    SELECT " + ",\n           ".join(rec_cols),
            "    FROM walk w JOIN seq s ON s.uid = w.uid AND s.i = w.i + 1",
            "),",
            f"daily AS (SELECT uid AS {ent}, dt, {', '.join(emits)} FROM walk)",
            prog.report,
        ])
    head = [f"-- 由 dslc.py 自动生成 | Tier {tier}"
            + ("（集合式：窗口函数 + 常量日程表）" if tier == 1 else "（递归 CTE 状态机）")]
    if tier == 1:
        head.append(f"-- 状态变量 {info['state']} | 日程表 {info['schedule_len']} 行"
                    + (f" | 前缀 {prefix-1} + 周期 {period}" if period else " | 无环")
                    + f" | 编译期常量折叠={info['const_folded']}")
    else:
        head.append("-- 降级原因: " + "; ".join(info["reasons"]))
        head.append("-- MySQL 8.0 请先放开递归深度: SET SESSION cte_max_recursion_depth = 1000000;")
        head.append("-- MariaDB 对应参数: SET SESSION max_recursive_iterations = 1000000;")
    sql = "\n".join(head) + "\n" + sql
    info["sql_chars"] = len(sql)
    if verbose_info is not None:
        verbose_info.update(info)
    return sql, info


# =====================================================================
# 7. 参考执行器：直接按 DSL 语义逐行跑（用于验证编译结果）
# =====================================================================
def reference_daily(prog, rows):
    """rows: [(uid, date)]，未去重未排序；返回 [(uid, date, emit...)]"""
    prog.body = desugar_stmts(prog.body)
    emits = emit_names(prog)
    groups = {}
    for uid, day in rows:
        if prog.start <= day <= prog.end:
            groups.setdefault(uid, set()).add(day)
    out = []
    for uid in sorted(groups):
        state = dict(prog.init)
        prev_day = None
        for i, day in enumerate(sorted(groups[uid])):
            state, em = concrete_run(prog, state, day, prev_day, first=(i == 0))
            out.append((uid, day) + tuple(em[n] for n in emits))
            prev_day = day
    return out, emits


# =====================================================================
# 8. CLI
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="签到规则 DSL 编译器")
    ap.add_argument("file")
    ap.add_argument("--out", help="把生成的 SQL 写到文件")
    ap.add_argument("--explain", action="store_true", help="打印编译决策")
    ap.add_argument("--tier", type=int, choices=[1, 2], help="强制某条路径")
    ap.add_argument("--max-unroll", type=int)
    ap.add_argument("--json", action="store_true", help="以 JSON 输出 sql+info")
    a = ap.parse_args()
    src = open(a.file, encoding="utf-8").read()
    prog = Parser(src).parse_program()
    info = {}
    sql, info = gen_sql(prog, force_tier=a.tier, max_unroll=a.max_unroll, verbose_info={})
    if a.json:
        print(json.dumps({"sql": sql, "info": info}, ensure_ascii=False, default=str, indent=2))
        return
    if a.explain:
        print("-- ===== 编译决策 =====", file=sys.stderr)
        for k in ("state", "locals", "emits", "tier", "tier_used", "reasons",
                  "reset_state", "schedule_len", "cycle_prefix", "cycle_period",
                  "schedule_cols", "index_expr", "sql_chars"):
            if k in info:
                print(f"-- {k:15}= {info[k]}", file=sys.stderr)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(sql + "\n")
        print(f"已写入 {a.out}", file=sys.stderr)
    else:
        print(sql)


if __name__ == "__main__":
    try:
        main()
    except DslError as e:
        print(f"编译错误: {e}", file=sys.stderr)
        sys.exit(1)
