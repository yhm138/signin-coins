# DSL 语言参考

一个只有四个块的小语言。**状态推导用 DSL，聚合用原生 SQL** —— 因为易变的是前者。

```
source { ... }   # 数据从哪来、按谁分组、什么算"一天"
state  { ... }   # 跨行状态（等价于 @用户变量），必须声明 + 字面量初值
scan   { ... }   # 组内按天升序，一天一行，顺序执行
report { ... }   # 原生 SQL，关系名固定为 daily
```

---

## 1. 词法

| 元素 | 写法 |
|---|---|
| 注释 | `# ...` 或 `-- ...`（到行尾） |
| 标识符 | `[A-Za-z_][A-Za-z_0-9]*` |
| 整数 / 小数 | `7`、`1.5` |
| 字符串 | `'abc'`，内部 `''` 表示单引号 |
| 日期字面量 | `2021-07-07`（裸写，不加引号） |
| 空值 / 布尔 | `null`、`true`、`false`（`true/false` 等价于 `1/0`） |
| 赋值 | `:=`（和 MySQL 一致；`=` 只用于比较） |

块用 `{}`，语句不需要分号。

---

## 2. `source` 块

```
source {
    from    tb_user_log
    where   sign_in = 1 and artical_id = 0
    entity  uid
    day     date(in_time)
    between 2021-07-07, 2021-10-31
}
```

| 字段 | 必填 | 语义 |
|---|---|---|
| `from` | ✅ | 源表名 |
| `where` | | 行过滤表达式，**引用源表列**（不是 DSL 变量） |
| `entity` | | 状态分组键（列名）。不同 entity 之间状态完全隔离，可并行 |
| `day` | ✅ | "一天"的表达式，通常是 `date(in_time)` |
| `between` | | 闭区间 `[start, end]`，按 `day` 过滤 |

**编译器保证**：按 `(entity, day)` **去重**后一天一行、按 `day` 升序。
所以同一天多条签到日志不会重复发币 —— 这正是手写 `@变量` 版本最容易踩的坑。

**优化**：当 `day` 形如 `date(col)` 时，日期谓词会被自动改写成
`col >= 'start 00:00:00' AND col < 'end+1 00:00:00'`，让 `(entity, col)` 索引可用。

---

## 3. `state` 块

```
state { consec := 0   f_prev := 0   f_cur := 1 }
```

- 声明**跨行状态**，初值必须是字面量（可折叠的常量表达式如 `1 + 1` 也接受）。
- 每个 `entity` 的第一行之前，状态 = 这里声明的初值。
- **未在这里声明、却在 `scan` 里被赋值的名字 = 行内临时变量**，不跨行保留。
  这个区分是编译器判定可优化性的关键输入，所以必须显式声明。

---

## 4. `scan` 块

### 语句

```
name := expr                          # 赋值
if expr { ... } else if expr { ... } else { ... }
emit expr as name [, expr as name]*   # 产出度量列
```

### 执行语义（**有定义**，不依赖数据库的求值顺序）

1. 按 `entity` 分组，组内按 `day` 升序，一天一行。
2. 语句**自上而下顺序执行**；后面的表达式看得见前面刚赋的值（和 `@变量` 的直觉一致）。
3. `if` 的条件为 `null` 时走 `else` 分支（SQL `IF` 语义）。
4. `emit` 可以出现在任意位置，读的是**该位置**的变量值。
5. 组内第一行：`prev(...)` 与 `gap()` 均为 `null`。

### 内置取值

| 写法 | 含义 |
|---|---|
| `day` | 当前行的日期 |
| `prev(day)` | 上一行的日期；首行 `null` |
| `prev(x)`（`x` 为状态变量） | 上一行**结束时**的 `x`；首行 `null` |
| `gap()` | 与上一行相差的天数；首行 `null` |

`gap()` 是原语。下面三种常见写法会被**去糖**成它，语义完全等价：

```
day = prev(day) + 1
prev(day) + 1 = day
datediff(day, prev(day)) = 1
```

`prev()` 只接受变量名，`prev(day + 1)` 会报编译错误。行内临时变量不能 `prev()`。

---

## 5. 表达式

### 运算符优先级（低 → 高）

| 级别 | 运算符 |
|---|---|
| 1 | `or` |
| 2 | `and` |
| 3 | `not`（前缀） |
| 4 | `=` `!=` `<>` `<` `<=` `>` `>=` |
| 5 | `+` `-` |
| 6 | `*` `/` `%`、一元 `-` |

- `and` / `or` 是 **SQL 三值逻辑**：`null and false = false`、`null and true = null`。
- 任何算术/比较遇到 `null` 结果为 `null`。
- **日期运算**：`day + 1` / `prev(day) - 1` 编译为 `DATE_ADD/DATE_SUB(... INTERVAL n DAY)`；
  `day - prev(day)` 编译为 `DATEDIFF`。识别是语法级的（`day`、`prev(day)`、日期字面量、
  `date_add/date_sub/date` 的返回值已知为 DATE）。状态变量里存日期时请显式用函数。

### 函数表

| DSL | MySQL | 说明 |
|---|---|---|
| `mod(a,b)` | `MOD(a,b)` | `b=0` 时 `null` |
| `least(...)` / `greatest(...)` | 同名 | 任一参数 `null` → `null` |
| `ifnull(a,b)` / `coalesce(...)` | 同名 | |
| `if(c,a,b)` | `IF(c,a,b)` | |
| `abs` / `floor` / `ceil` | 同名 | |
| `datediff(a,b)` | `DATEDIFF(a,b)` | 相差天数 |
| `date_add(d,n)` / `date_sub(d,n)` | `DATE_ADD/DATE_SUB` | 按天 |
| `date(x)` | `DATE(x)` | |
| `day(d)` / `month(d)` / `year(d)` | 同名 | |
| `dayofweek(d)` | `DAYOFWEEK(d)` | **1=周日 … 7=周六**（与 MySQL 一致） |
| `weekday(d)` | `WEEKDAY(d)` | 0=周一 … 6=周日 |
| `date_format(d,fmt)` | `DATE_FORMAT` | 仅 `report` 层常用 |

加函数 = 在 `dslc.py` 里加两行：`FUNCS_CONCRETE` 一个 Python 实现 + `FUNCS_SQL` 一个 SQL 模板。
两份实现由单元测试和端到端交叉验证保证一致。

---

## 6. `report` 块

块内是**原生 SQL**（按花括号配对整体截取），关系名固定为 `daily`，列为：

```
daily(<entity 名>, dt, <emit 的各个名字...>)
```

例如 `entity uid` + `emit coin as coin, cards as cards_left` → `daily(uid, dt, coin, cards_left)`。

```
report {
    select uid, date_format(dt, '%Y%m') as month, sum(coin) as coin
    from daily
    group by uid, month
    order by month, uid
}
```

聚合、窗口、自联结、`HAVING` 随便写 —— 这一层本来就不易变，用 SQL 最省事。
编译器把它原样拼在 `WITH ... daily AS (...)` 之后。

---

## 7. 完整示例

### 原题

```
source {
    from tb_user_log where sign_in = 1 and artical_id = 0
    entity uid day date(in_time) between 2021-07-07, 2021-10-31
}
state { consec := 0 }
scan {
    if day = prev(day) + 1 and consec < 7 { consec := consec + 1 } else { consec := 1 }
    coin := 1
    if consec = 3 { coin := coin + 2 }
    if consec = 7 { coin := coin + 6 }
    emit coin as coin
}
report {
    select uid, date_format(dt,'%Y%m') as month, sum(coin) as coin
    from daily group by uid, month order by month, uid
}
```

### 斐波那契（行内临时变量 + 顺序赋值）

```
state { n := 0   f_prev := 0   f_cur := 1 }
scan {
    if gap() = 1 and n < 7 {
        n      := n + 1
        t      := f_cur          # t 是行内临时变量
        f_cur  := f_prev + f_cur
        f_prev := t
    } else {
        n := 1   f_prev := 0   f_cur := 1
    }
    emit f_cur as coin
}
```

### 每月 2 张补签卡（真运行时状态 → 自动 Tier 2）

完整活动规则口径见 [RULES.md §1](RULES.md)。

```
state { consec := 0   cards := 2   m := 0 }
scan {
    if month(day) <> m { m := month(day)   cards := 2 }
    if gap() = 1 {
        if consec < 7 { consec := consec + 1 } else { consec := 1 }
    } else {
        if gap() = 2 and cards > 0 {
            cards := cards - 1
            if consec < 7 { consec := consec + 1 } else { consec := 1 }
        } else { consec := 1 }
    }
    coin := 1
    if consec = 3 { coin := coin + 2 }
    if consec = 7 { coin := coin + 6 }
    emit coin as coin, cards as cards_left
}
```

---

## 8. 常见坑

| 现象 | 原因 / 处理 |
|---|---|
| 被判成 Tier 2，但我以为能优化 | 断签分支里有依赖旧状态的赋值（如"保留一半进度"），或状态转移读了 `month(day)`。`--explain` 会给出具体变量名 |
| `--tier 1` 报错 | 判定不通过时禁止强制展开，这是安全护栏而不是限制 |
| `展开上界 N < 窗口内最长可能连续天数` | 无环轨迹 + `--max-unroll` 给太小。去掉该参数或加大 |
| 产物 SQL 巨大 | 顺序赋值靠内联，嵌套深会膨胀。看 `info.sql_chars`，考虑把中间量提升为状态变量 |
| MySQL 8 上递归 CTE 报错 | `SET SESSION cte_max_recursion_depth = 1000000;`（默认 1000） |
| 金额溢出 | 不封顶的指数/斐波那契增长极快，注意 BIGINT / DECIMAL 上限 |
