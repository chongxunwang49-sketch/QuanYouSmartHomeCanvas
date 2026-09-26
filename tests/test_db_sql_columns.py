"""
`repository.py` 里的 SQL 列名 vs 迁移 DDL —— **静态比对**。

══════════════════════════════════════════════════════════════════
为什么要有这个文件：同一个错犯了两次
══════════════════════════════════════════════════════════════════

**第一次** —— 001 迁移漏了 `space_plan` 列。当时的记录写着：
「列清单是照着**已有的表**写的，不是照着**方案要落的字段**写的」。

**第二次**（2026-09-24 实测发现）—— `load_plan` 的 SELECT 里写了
`budget` 这一列，而 `design_plans` **没有这一列**（真正的列是
`total_budget_min` / `total_budget_max` / `breakdown` / `budget_computed_by`）。

第二次的后果值得完整记下来，因为它演示了"看起来合理的错误"是怎么长出来的：

    psycopg 抛 UndefinedColumn
      → `pool.fetch_all` 按契约返回 None（"失败返回 None"）
      → `load_plan` 的 `if not rows: return None`
      → 接口回 **4004**「找不到方案 plan_xxx（户型 layout_xxx）。
         可能是还没生成过、或数据库暂时不可用 —— 此时**不会退回默认摆放**」

    而方案**就在库里**。用户在 3D 页面上看到的是一间空房子，
    配一句语气笃定、位置合理的解释。没有栈、没有 5xx、没有红色。
    实测证据（容器日志）：

        [db] 查询 失败（连续 1 次，30s 内不再重试）：UndefinedColumn:
        column "budget" does not exist
        LINE 1: SELECT plan_id, style, budget_grade, space_plan, budget, ris...

**雪上加霜**：连接池的熔断让这一次坏查询把**整个持久化层停了 30 秒**
（`_COOLDOWN_SECONDS`）—— 审计落库也跟着停。一个列名的错，代价远不止一次查询。

══════════════════════════════════════════════════════════════════
这个测试做什么
══════════════════════════════════════════════════════════════════
把 `migrations/*.sql` 里的 `CREATE TABLE` / `ALTER TABLE ADD COLUMN` 解析成
`{表: {列}}`，再把 `repository.py` 里的 SQL 里出现在**列位置**的标识符抽出来，
逐个比对：

  · `INSERT INTO t (a, b, …)`  —— 括号里的每个名字，**按表比对**
  · `UPDATE t SET a = …, b = …` —— `SET` 里等号左边的名字
  · `SELECT a, b FROM t`      —— `FROM` 之前那些名字

它们是**静态**比对，不连数据库 —— 所以它能在迁移还没跑到、
或者数据库根本没起来的机器上发现这类错误。

⚠️ 反过来它也检查一次：迁移里**声明了却从没被用过**的列不报错（那是正常的，
   比如 `quanyou_products` 现在没人读）。只查单向。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "backend" / "app" / "db" / "migrations"
REPOSITORY = REPO / "backend" / "app" / "db" / "repository.py"

#: SQL 里合法但**不是列名**的东西：关键字、函数、占位符、类型转换。
_NOT_A_COLUMN = {
    "now", "conflict", "excluded", "coalesce", "count", "value", "values",
    "set", "where", "from", "into", "on", "do", "update", "insert", "select",
    "table", "jsonb", "text", "int", "integer", "boolean", "timestamptz",
    "as", "and", "or", "not", "null", "true", "false", "returning",
}


def _strip_sql_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def _split_top_level(body: str) -> list[str]:
    """按**顶层**逗号切分（括号内的逗号不算）。"""
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _matching_paren(sql: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise AssertionError("SQL 里的括号不配对 —— 这个解析器需要跟着改")


def _ddl_tables() -> dict[str, set[str]]:
    """
    从迁移里解析出 `{表名: {列名}}`。
    `CREATE TABLE` 建表 + `ALTER TABLE … ADD COLUMN` 补列都要算上。
    """
    ddl = "\n".join(
        _strip_sql_comments(p.read_text(encoding="utf-8"))
        for p in sorted(MIGRATIONS.glob("*.sql"))
    )
    tables: dict[str, set[str]] = {}

    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\(",
                         ddl, re.I):
        name = m.group(1)
        body = ddl[m.end(): _matching_paren(ddl, m.end() - 1)]
        cols: set[str] = set()
        for part in _split_top_level(body):
            head = part.strip().split()
            if not head:
                continue
            # 表级约束不是列
            if head[0].upper() in {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK",
                                   "CONSTRAINT", "EXCLUDE", "LIKE"}:
                continue
            cols.add(head[0].strip('"').lower())
        tables.setdefault(name, set()).update(cols)

    for m in re.finditer(
        r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
        ddl, re.I,
    ):
        tables.setdefault(m.group(1), set()).add(m.group(2).lower())

    return tables


def _sql_blobs() -> list[str]:
    """
    把 `repository.py` 里的 SQL 文本拼出来。

    ⚠️ 用 `tokenize` 拿**字符串字面量的位置**，只把**源码里相邻**的字面量
       接起来（Python 的隐式拼接：`"SELECT a, " "b FROM t"`）。
       无脑把全文所有字符串拼成一坨的话，会把两个不相干的语句接在一起，
       抽出来的"列名"就全是假的 —— 那种测试红得莫名其妙。
    """
    import io
    import tokenize

    src = REPOSITORY.read_text(encoding="utf-8")
    tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    strings: list[tuple[int, int, str]] = []
    for tok in tokens:
        if tok.type == tokenize.STRING:
            text = tok.string
            for prefix in ('"""', "'''", '"', "'"):
                if text.startswith(prefix):
                    text = text[len(prefix):-len(prefix)]
                    break
            strings.append((tok.start[0], tok.end[0], text))

    # ⚠️ 两个分隔符**不能混用**：相邻字面量要**真的接起来**（Python 的隐式
    #    拼接就是一次字符串拼接），不相邻的段之间才插一个不可打印的哨兵。
    #    第一版两种都用 `\x00`，于是 `"SELECT … space_plan, "` 和
    #    `"risks FROM …"` 被硬生生拆成两段 —— `SELECT … FROM` 那条正则
    #    永远匹配不到，SELECT 形态一个都没抽到（而测试还是绿的）。
    blobs: list[str] = []
    buf: list[str] = []
    prev_end = -10
    for start, end, text in strings:
        if buf and start - prev_end > 1:
            blobs.append("".join(buf))
            buf = []
        buf.append(text)
        prev_end = end
    if buf:
        blobs.append("".join(buf))
    return blobs


def _column_positions() -> list[tuple[str, str, str]]:
    """
    抽出 `(表名, 列名, 上下文)`。表名拿不到时用 `''`（只能做全集比对）。
    """
    found: list[tuple[str, str, str]] = []
    for blob in _sql_blobs():
        for stmt in blob.split("\x00"):
            if not re.search(r"\b(SELECT|INSERT\s+INTO|UPDATE)\b", stmt, re.I):
                continue

            # INSERT INTO t (a, b, c)。表名同时留给后面的 `DO UPDATE SET` 用
            insert_table = ""
            for m in re.finditer(r"INSERT\s+INTO\s+(\w+)\s*\(([^)]*)\)", stmt, re.I):
                insert_table = m.group(1)
                for col in _split_top_level(m.group(2)):
                    found.append((m.group(1).lower(), col.strip().lower(), "INSERT"))

            # UPDATE t SET … 与 ON CONFLICT … DO UPDATE SET …（后者不带表名）
            #
            # ⚠️ 两种形态都要覆盖。本项目用的是 `INSERT … ON CONFLICT DO UPDATE
            #    SET` —— 写 UPSERT 就是这个形状，而它里面**没有** `UPDATE t`
            #    这一段表名。第一版只认 `UPDATE\s+(\w+)\s+SET`，于是 SET 形态
            #    一个都没抽到（37 个 INSERT、13 个 SELECT、0 个 SET），
            #    而测试当时还是绿的 —— 是"每种形态都得抽到"那条把它逼出来的。
            for m in re.finditer(
                r"(?:UPDATE\s+(\w+)\s+)?SET\s+(.*?)(?=\bWHERE\b|\bRETURNING\b|$)",
                stmt, re.I | re.S,
            ):
                table = (m.group(1) or insert_table or "").lower()
                for cm in re.finditer(r"(\w+)\s*=", m.group(2)):
                    found.append((table, cm.group(1).lower(), "SET"))

            # SELECT a, b, c FROM t
            for m in re.finditer(r"SELECT\s+(.*?)\s+FROM\s+(\w+)", stmt, re.I | re.S):
                for col in _split_top_level(m.group(1)):
                    col = col.strip()
                    if "(" in col:          # 函数调用，例如 count(*) AS n
                        continue
                    name = col.split()[0].strip('"').lower()
                    found.append((m.group(2).lower(), name, "SELECT"))

    return [
        (t, c, ctx) for t, c, ctx in found
        if c and c not in _NOT_A_COLUMN and not c.startswith("%")
    ]


TABLES = _ddl_tables()
ALL_COLUMNS = {c for cols in TABLES.values() for c in cols}
POSITIONS = _column_positions()


def test_解析器本身是有效的():
    """
    ⚠️ **先证明这个测试不是空跑的。**
    正则写歪了的话，`POSITIONS` 会是空的，下面那条就会一路绿灯 ——
    而它本该抓到的错误照样溜过去。
    """
    assert set(TABLES) >= {"house_layouts", "design_plans", "audit_logs"}, (
        f"迁移里只解析出这些表：{sorted(TABLES)}"
    )
    assert "space_plan" in TABLES["design_plans"], (
        "design_plans 的列没解析对（003 迁移里加了 space_plan）"
    )
    # ⚠️ 这里不写"至少 40 个"这种数 —— 那个数字是我拍的，加一句 SQL 就得改。
    #    真正要保证的是**每种形态都抽到了**：缺一种就说明对应的正则没生效，
    #    而那一类错误会静默溜过去。
    by_kind: dict[str, int] = {}
    for _t, _c, ctx in POSITIONS:
        by_kind[ctx] = by_kind.get(ctx, 0) + 1
    for kind in ("INSERT", "SET", "SELECT"):
        assert by_kind.get(kind, 0) >= 3, (
            f"`{kind}` 形态只抽到 {by_kind.get(kind, 0)} 个列位置 —— "
            f"对应的正则没生效，这一类错误会溜过去。全部形态：{by_kind}"
        )


def test_repository里的列名都在迁移DDL里存在():
    """
    **这条就是那个 bug 的守门人。**
    `budget` 不在任何一张表的列里 —— 它会让这条立刻变红，
    而不是等到有人在 3D 页面上看到"找不到方案"。
    """
    missing = [
        (t, c, ctx) for t, c, ctx in POSITIONS if c not in ALL_COLUMNS
    ]
    assert not missing, (
        "repository.py 里的 SQL 引用了迁移 DDL 里不存在的列：\n"
        + "\n".join(
            f"  [{ctx}] {t or '(表名未知)'}.{c}" for t, c, ctx in missing
        )
        + "\n\n这类错误的后果不是报错，而是**一个看起来很合理的错误**："
        "\npsycopg 抛 UndefinedColumn → pool.fetch_all 按契约返回 None →"
        "\n调用方当成'没有这一行' → 接口回一个真假难辨的 4004，"
        "\n而且连接池会因此熔断 30 秒。详见本文件头部。"
    )


def test_INSERT的列名属于它自己那张表():
    """
    上一条件只比"在所有表的列里存在"，抓不到"用错了表"。
    `INSERT INTO t (…)` 的表名是**写在语句里**的，所以这一形态可以精确绑定。
    """
    bad = []
    for table, col, ctx in POSITIONS:
        if ctx != "INSERT":
            continue
        if table not in TABLES:
            bad.append((table, col, "表本身不在迁移里"))
        elif col not in TABLES[table]:
            bad.append((table, col, "不属于这张表"))
    assert not bad, (
        "INSERT 的列与它自己那张表对不上：\n"
        + "\n".join(f"  {t}.{c} —— {why}" for t, c, why in bad)
    )
