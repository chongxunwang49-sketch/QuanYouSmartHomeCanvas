"""
极简迁移执行器：按序号跑 `migrations/*.sql`，已跑过的跳过。

═══════════════════════════════════════════════════════════════════
为什么不用 alembic
═══════════════════════════════════════════════════════════════════
见 `pool.py` 的模块说明。这里补一句关于**迁移本身**的：

alembic 的核心能力是「从模型反向生成迁移」和「降级路径」，两者都要求
先把表描述成 Python 对象。本项目只有 3 张表、没有模型层，于是它的收益
只剩下"版本记录"这一项 —— 而这一项用一张 `schema_migrations` 表加
二十行代码就够了。

**代价要说清楚：没有自动降级。** 改表要么加一个新的 `00N_*.sql`
（向前兼容，推荐），要么手写降级 SQL 并由人执行。这是一个真实的取舍，
不是"不需要迁移"。

═══════════════════════════════════════════════════════════════════
为什么迁移语句整体执行、而不是按分号切
═══════════════════════════════════════════════════════════════════
按 `;` 切开是最容易想到的做法，也最容易错：本项目上游禁用项里
（`requirements.txt` 的说明）就有过"正则切 SQL 把函数体切碎"的先例。
这里用 psycopg 的**一次 multistatement 执行**：它把整段交给服务器，
由服务器解析。少一层自作聪明的切分，就少一类只在怪异 SQL 上才炸的 bug。

⚠️ 代价：一个文件里的语句是**同一个隐式事务**（psycopg 对多语句执行的处理），
   所以文件里**不要写** `BEGIN` / `COMMIT` —— 会与外层事务冲突。
"""

from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from . import pool

__all__ = ["MIGRATIONS_DIR", "discover", "applied", "migrate", "pending"]

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_VERSION_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")

_CREATE_TRACKING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    VARCHAR(8) PRIMARY KEY,
    name       VARCHAR(128) NOT NULL,
    applied_at TIMESTAMP DEFAULT NOW()
)
"""


def discover() -> list[tuple[str, str, Path]]:
    """
    列出迁移文件，按版本号升序。返回 `[(version, name, path)]`。

    ⚠️ 文件名不合规**直接抛错**，不静默跳过。一个没被识别的迁移文件
    等于一段永远不执行的 DDL —— 而"表没建出来"要到运行时报错才被发现，
    那时已经离现场很远了。
    """
    out: list[tuple[str, str, Path]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _VERSION_RE.match(path.name)
        if not m:
            raise ValueError(
                f"迁移文件名不合规：{path.name}（应为 NNN_小写名.sql）"
            )
        out.append((m.group(1), m.group(2), path))

    versions = [v for v, _, _ in out]
    if len(set(versions)) != len(versions):
        raise ValueError(f"迁移版本号重复：{versions}")
    return out


async def applied() -> set[str] | None:
    """已应用的版本号。**表不存在时返回空集**（首次运行就是这个情况）。"""
    await pool.execute(_CREATE_TRACKING)
    rows = await pool.fetch_all("SELECT version FROM schema_migrations")
    if rows is None:
        return None          # 数据库不通 —— 与"没有迁移"是两回事
    return {str(r["version"]) for r in rows}


def pending() -> list[tuple[str, str, Path]]:
    """同步版：列出全部迁移文件（不含"已应用"判断，那个要连库）。"""
    return discover()


async def migrate(*, dry_run: bool = False) -> dict[str, object]:
    """
    应用所有未执行的迁移。返回一份可直接打印的报告。

    幂等：已记录的版本会跳过；SQL 里也统一用 `CREATE TABLE IF NOT EXISTS`
    之类的写法，所以"记录丢了但表在"的意外情况不会炸。
    """
    files = discover()
    done = await applied()
    if done is None:
        return {"ok": False, "reason": "数据库连接不可用", "applied": [],
                "already": [], "total": len(files), "dry_run": dry_run}

    todo = [(v, n, p) for v, n, p in files if v not in done]
    report: dict[str, object] = {
        "ok": True,
        "already": sorted(done),
        "applied": [],
        "total": len(files),
        "dry_run": dry_run,
    }
    if dry_run or not todo:
        report["pending"] = [v for v, _, _ in todo]
        return report

    for version, name, path in todo:
        sql = path.read_text(encoding="utf-8")
        if await pool.execute(sql) is None:
            # 一条失败就停下：后面的迁移可能依赖它，接着跑只会制造
            # 更难查的半成品状态。已经成功的那几条**不回滚** ——
            # 它们是幂等的，下次跑会跳过。
            report["ok"] = False
            report["reason"] = f"{version}_{name} 执行失败（详见日志）"
            break
        await pool.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s) "
            "ON CONFLICT (version) DO NOTHING",
            (version, name),
        )
        report["applied"].append(f"{version}_{name}")  # type: ignore[union-attr]
        logger.info(f"[db] 迁移已应用：{version}_{name}")

    return report
