"""
业务数据落库（`backend/app/db/`）。

═══════════════════════════════════════════════════════════════════
为什么这批用例不标 `integration`
═══════════════════════════════════════════════════════════════════
`qy-postgres` 就在 compose 里跑着、并且**没有它应用也照常工作**（落库是
加固不是依赖）。所以这里沿用 `test_checkpoint.py` 的做法：连不上就
`pytest.skip`，而不是打成 integration 让默认跑根本不覆盖 —— 那样
这层代码在 99% 的运行里是没人看的。

═══════════════════════════════════════════════════════════════════
为什么这批用例是**同步**函数、内部自己跑事件循环
═══════════════════════════════════════════════════════════════════
psycopg 的异步模式在 Windows 上**不能用默认的 ProactorEventLoop**：

    Psycopg cannot use the 'ProactorEventLoop' to run in async mode

`pytest.ini` 是 `asyncio_mode = auto`，如果把这些写成 `async def`，
它们会被丢进 pytest-asyncio 自己建的循环里 —— 而那个循环是本进程默认的
Proactor。改全局策略又会波及别的用例（`asyncio` 的子进程支持**只能**用
Proactor，`test_mcp_servers.py` 要起子进程）。

所以这里的做法是：**临时把事件循环策略换成 Selector，跑完再换回来**，
只给这几条用例换循环，进程里其它东西不受影响。具体写法与它踩过的坑见 `_run()`。
"""

from __future__ import annotations

import asyncio
import selectors
import sys
from typing import Any, Coroutine

import pytest

from backend.app.core.config import settings
from backend.app.db import audit_sink, migrations, pool, repository

_TEST_LAYOUT_PREFIX = "layout_dbtest_"


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    """
    在 Selector 循环里跑一段协程（见模块说明）。

    ⚠️ **不能用 `asyncio.run(coro, loop_factory=...)`。**

    `loop_factory` 是 **Python 3.12** 才有的参数，而本项目（ADR-05）
    与容器都是 **3.11**。这句在 3.11 上抛：

        TypeError: run() got an unexpected keyword argument 'loop_factory'

    而它**只在装了 psycopg3 的机器上才会被执行到** —— 本机 conda 环境
    只有 psycopg2-binary（另一个包），这批用例是 skip；容器里有 psycopg3
    但**没装 pytest**。两头都不执行，于是这 37 条用例**一次都没真正跑过**，
    而它们看起来是绿的。

    ⚠️ 这是本项目反复出现的同一种失败：**"看起来有、其实永远不执行"** ——
    比没有更难查，因为它贡献的是"覆盖率"的错觉。

    3.11 的等价写法是换策略、跑、再换回来，同样只影响这几条用例。
    """
    if sys.platform != "win32":
        return asyncio.run(coro)

    policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_cls is None:  # 该策略被移除后的版本（3.16+）
        return asyncio.run(
            coro,
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )

    prev = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(policy_cls())
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop_policy(prev)


@pytest.fixture(autouse=True)
def _need_postgres():
    """
    连不上就跳过，并说明为什么（与 test_checkpoint 同一个手法）。

    ⚠️ **`import psycopg` 必须走 `importorskip`，不能裸 import。**
    本意是"库连不上就跳过"，而裸 import 写在 try **外面** ——
    于是当环境里根本没有 psycopg3 时（比如本机的 conda env 只装了
    psycopg2-binary，那是另一个包），37 条用例会**报 ERROR 而不是 SKIP**。
    后果是本地全量跑永远是红的，而红的原因与代码无关 ——
    看久了就会开始忽略它，而那时它真坏掉也没人知道。
    """
    psycopg = pytest.importorskip(
        "psycopg", reason="本地环境没有 psycopg3（容器里有），跳过落库用例"
    )

    try:
        with psycopg.connect(settings.checkpoint_dsn, connect_timeout=3):
            pass
    except Exception as e:
        pytest.skip(f"PostgreSQL 不可用（{type(e).__name__}），跳过落库用例")
    pool.reset()
    yield
    _run(_cleanup())
    pool.reset()


async def _cleanup() -> None:
    """删掉本文件造的数据。**不留下痕迹** —— 否则 `audit_rows`
    这类计数会被上一次跑测试的行污染，看指标的人分不清是应用写的还是测试写的。

    ⚠️ `audit_logs` 也要清。第一版漏了，于是端到端验收时 `audit_rows`
    里躺着 `dbtest_sink|6` —— 那不是应用写的，但看指标的人无从分辨。
    清理范围按**事件名前缀**（`dbtest_`）而不是全表：这个文件之外的行
    是真实的运行记录，删了就毁了 AC-14 的意义。
    """
    await pool.execute(
        "DELETE FROM design_plans WHERE layout_id LIKE %s", (_TEST_LAYOUT_PREFIX + "%",)
    )
    await pool.execute(
        "DELETE FROM house_layouts WHERE id LIKE %s", (_TEST_LAYOUT_PREFIX + "%",)
    )
    await pool.execute("DELETE FROM audit_logs WHERE action LIKE 'dbtest_%'")
    await pool.close_pool()


def _layout(n: int = 1) -> dict[str, Any]:
    return {
        "layout_id": f"layout_dbtest_{n}",
        "mode": "full",
        "rooms": [{"name": "客厅", "area": 28.5}, {"name": "主卧", "area": 16.2}],
        "total_area": 44.7,
        "confidence": 0.85,
    }


def _plan(plan_id: str = "plan_modern_economy") -> dict[str, Any]:
    return {
        "plan_id": plan_id,
        "style": "modern",
        "budget_grade": "economy",
        # ⚠️ 分项明细在运行时的字段名是 `lines`（BudgetBreakdown），
        #    而表列叫 `breakdown` —— 映射见 repository._plan_row 的说明。
        "budget": {
            "total_min": 88000.0, "total_max": 112000.0,
            "lines": [{"name": "水电改造", "min": 8000, "max": 11000}],
            "computed_by": "rule_engine_v1",
        },
        "materials": {
            "items": [
                {"id": "QY-FL-101", "name": "全友地板", "is_quanyou": True, "category": "floor"},
                {"id": "XY-FL-301", "name": "圣象地板", "is_quanyou": False, "category": "floor"},
            ],
            "quanyou_coverage": 0.5,
        },
        "risks": {"items": []},
        "environment": {"formaldehyde": {"risk": "low"}},
    }


# ══════════════════════════════════════════════════════════════════
# 迁移
# ══════════════════════════════════════════════════════════════════


class TestMigrations:
    def test_文件名不合规直接抛错(self):
        """
        ⚠️ 不静默跳过。一个没被识别的迁移文件等于一段永远不执行的 DDL，
        而"表没建出来"要到运行时报错才被发现，那时已经离现场很远了。
        """
        import re

        pattern = migrations._VERSION_RE
        assert pattern.match("001_business_tables.sql")
        assert not pattern.match("business_tables.sql")     # 缺版本号
        assert not pattern.match("1_business.sql")          # 版本号不是三位
        assert not pattern.match("001_Business.sql")        # 大写
        assert not re.match(r"^\d{3}_[a-z0-9_]+\.sql$", "001_.sql")

    def test_按版本号升序(self):
        files = migrations.discover()
        versions = [v for v, _, _ in files]
        assert versions == sorted(versions)
        assert versions, "一个迁移文件都没有 —— 表是建不出来的"

    def test_已应用的版本进得了报告(self):
        async def _go():
            done = await migrations.applied()
            report = await migrations.migrate()
            await pool.close_pool()
            return done, report

        done, report = _run(_go())
        assert done is not None, "数据库应当可用（fixture 已经验过）"
        assert report["ok"] is True
        # 幂等：再跑一次不会重复应用
        assert report["applied"] == []

    def test_表确实建出来了(self):
        """读 `schema_migrations` **不能证明** DDL 生效了 —— 只证明那一行在。

        这两件事会分开：迁移文件里某个 `CREATE TABLE` 写错、而记录行插进去了，
        看起来就是"迁移成功但表不存在"。所以直接问 `to_regclass`。
        """
        async def _go():
            rows = await pool.fetch_all(
                "SELECT to_regclass('house_layouts') AS a, "
                "to_regclass('design_plans') AS b, "
                "to_regclass('audit_logs') AS c"
            )
            await pool.close_pool()
            return rows

        rows = _run(_go())
        assert rows and all(rows[0][k] for k in ("a", "b", "c")), (
            f"表不存在：{rows} —— 跑 `python scripts/init_db.py`"
        )


# ══════════════════════════════════════════════════════════════════
# 户型与方案
# ══════════════════════════════════════════════════════════════════


class TestLayoutRepository:
    def test_存取回环(self):
        async def _go():
            lid = _TEST_LAYOUT_PREFIX + "roundtrip"
            ok = await repository.save_layout(lid, _layout(), user_id=7)
            got = await repository.load_layout(lid)
            await pool.close_pool()
            return ok, got

        ok, got = _run(_go())
        assert ok is True, "写库失败应当是显式的 False，而不是静默"
        assert got == _layout()

    def test_重复写同一个id是覆盖而不是报错(self):
        """
        同一个 layout_id 会被解析两次（用户重传同一张图）。
        INSERT 会直接抛唯一约束冲突，把一次正常的重传变成失败。
        """
        async def _go():
            lid = _TEST_LAYOUT_PREFIX + "resave"
            await repository.save_layout(lid, _layout())
            second = dict(_layout(), total_area=52.0)
            ok = await repository.save_layout(lid, second)
            got = await repository.load_layout(lid)
            await pool.close_pool()
            return ok, got

        ok, got = _run(_go())
        assert ok is True
        assert got and got["total_area"] == 52.0

    def test_取不存在的id返回None(self):
        async def _go():
            got = await repository.load_layout(_TEST_LAYOUT_PREFIX + "不存在")
            await pool.close_pool()
            return got

        assert _run(_go()) is None

    def test_空id不写也不读(self):
        async def _go():
            wrote = await repository.save_layout("", _layout())
            got = await repository.load_layout("")
            await pool.close_pool()
            return wrote, got

        assert _run(_go()) == (False, None)


class TestPlanRepository:
    def test_方案落库(self):
        async def _go():
            lid = _TEST_LAYOUT_PREFIX + "plans"
            await repository.save_layout(lid, _layout())
            n = await repository.save_plans(lid, [_plan()])
            rows = await pool.fetch_all(
                "SELECT plan_id, total_budget_min, budget_computed_by, "
                "jsonb_array_length(breakdown) AS lines, "
                "jsonb_array_length(quanyou_products) AS qy "
                "FROM design_plans WHERE layout_id = %s", (lid,),
            )
            await pool.close_pool()
            return n, rows

        n, rows = _run(_go())
        assert n == 1
        assert rows[0]["plan_id"] == "plan_modern_economy"
        assert float(rows[0]["total_budget_min"]) == 88000.0
        assert rows[0]["budget_computed_by"] == "rule_engine_v1"
        # 运行时的 `lines` 落进了表列 `breakdown`
        assert rows[0]["lines"] == 1
        # `quanyou_products` 只存全友那部分（1 件），不是整份 items（2 件）
        assert rows[0]["qy"] == 1

    def test_同一个plan_id在不同户型下互不覆盖(self):
        """
        ⚠️ **这条守的是需求文档 §5.2 里一处建不起来的设计。**

        文档把 `design_plans.id` 写成主键，而运行时的 plan_id 是
        `plan_{style}_{grade}`（workflow.py:215）——**不含 layout**。
        两个用户都生成"现代简约·经济型"，plan_id 完全相同。

        按文档建表的话，第二个人的方案要么插不进去、要么把第一个人的覆盖掉。
        所以主键是 `(layout_id, plan_id)`。这条用例就是证明这个修正真的生效。
        """
        async def _go():
            a, b = _TEST_LAYOUT_PREFIX + "a", _TEST_LAYOUT_PREFIX + "b"
            await repository.save_layout(a, _layout(1))
            await repository.save_layout(b, _layout(2))
            await repository.save_plans(a, [_plan()])
            await repository.save_plans(b, [_plan()])
            rows = await pool.fetch_all(
                "SELECT layout_id FROM design_plans WHERE plan_id = %s "
                "AND layout_id LIKE %s ORDER BY layout_id",
                ("plan_modern_economy", _TEST_LAYOUT_PREFIX + "%"),
            )
            await pool.close_pool()
            return rows

        rows = _run(_go())
        assert len(rows) == 2, (
            f"同一个 plan_id 在两个户型下应当各存一行，实际 {len(rows)} 行 —— "
            f"主键退回单列 `id` 了？"
        )

    def test_户型不在库里时方案写不进去(self):
        """
        `design_plans.layout_id` 有外键。**不做隐式的自动补写** ——
        那会掩盖"方案比户型先到"这个真实异常。
        调用方拿到 0，自己去记 warning。
        """
        async def _go():
            n = await repository.save_plans(
                _TEST_LAYOUT_PREFIX + "不存在", [_plan()],
            )
            await pool.close_pool()
            return n

        assert _run(_go()) == 0

    def test_热区从参数来而不是从plan里取(self):
        """
        ⚠️ 第一版在这里错过：热区存在 `state["hotspots"]`（按 plan_id 分键），
        而 `plans[]` 是 `assemble_plans` 从 `plan_bundles` 汇出来的 ——
        两者在 state 里不在同一个地方。在 `plan.get("hotspots")` 上取值
        **永远是 None**，而且不报错，只会安静地存一列空值。
        """
        async def _go():
            lid = _TEST_LAYOUT_PREFIX + "hotspot"
            await repository.save_layout(lid, _layout())
            await repository.save_plans(
                lid, [_plan()],
                hotspots_by_plan={"plan_modern_economy": [{"room": "客厅", "x": 1}]},
            )
            rows = await pool.fetch_all(
                "SELECT hotspots FROM design_plans WHERE layout_id = %s", (lid,),
            )
            await pool.close_pool()
            return rows[0]["hotspots"]

        assert _run(_go()) == [{"room": "客厅", "x": 1}]

    def test_重复写同一套方案是覆盖(self):
        """同一个户型重跑生成，plan_id 与上次完全相同。"""
        async def _go():
            lid = _TEST_LAYOUT_PREFIX + "rerun"
            await repository.save_layout(lid, _layout())
            await repository.save_plans(lid, [_plan()])
            changed = _plan()
            changed["budget"]["total_min"] = 99000.0
            await repository.save_plans(lid, [changed])
            rows = await pool.fetch_all(
                "SELECT total_budget_min FROM design_plans WHERE layout_id = %s", (lid,),
            )
            await pool.close_pool()
            return rows

        rows = _run(_go())
        assert len(rows) == 1
        assert float(rows[0]["total_budget_min"]) == 99000.0

    def test_部分成功会如实返回条数(self):
        """一套写失败不该把另外两套一起回滚 —— 部分成功比全无更接近真实。"""
        async def _go():
            lid = _TEST_LAYOUT_PREFIX + "partial"
            await repository.save_layout(lid, _layout())
            n = await repository.save_plans(
                lid, [_plan("plan_a_x"), _plan("plan_b_y")],
            )
            await pool.close_pool()
            return n

        assert _run(_go()) == 2


# ══════════════════════════════════════════════════════════════════
# 审计
# ══════════════════════════════════════════════════════════════════


class TestAuditRowMapping:
    """
    事件字段 → 表列的映射。**不发 SQL，纯函数级断言** ——
    这样失败信息能直接指出是哪一处映射错了。
    """

    def test_已知字段落进对应的列(self):
        row = repository._audit_row("task_finished", {
            "user_id": 3, "task_id": "t-1", "kind": "generate",
            "status": "completed", "degraded": True, "elapsed_seconds": 12.5,
        })
        # 列顺序见 _INSERT_AUDIT：action, user_id, resource_type, resource_id,
        # model_used, degraded, degrade_reason, token_usage, latency_ms,
        # ip_address, payload
        assert row[0] == "task_finished"
        assert row[1] == 3
        assert row[2] == "generate"
        assert row[3] == "t-1"
        assert row[5] is True
        assert row[8] == 12500, "elapsed_seconds 应当换算成 latency_ms"

    def test_没有列的字段进payload(self):
        """
        `username` 与 `status` 在 §5.5 的列里没有位置，而它们恰恰是查询时要用的：

        `login_failed` 是**唯一没有 user_id 的事件**（账号可能根本不存在），
        丢掉 username，"有没有人在撞密码"就答不了了。
        """
        row = repository._audit_row("login_failed", {
            "username": "someone", "reason": "bad_credentials",
        })
        payload = row[10]
        assert payload is not None and "someone" in payload
        assert row[6] == "bad_credentials"      # reason → degrade_reason

    def test_超长字段被截断而不是让整条写入失败(self):
        row = repository._audit_row("task_finished", {"task_id": "x" * 200})
        assert len(row[3]) == 64

    def test_空事件名不写空字符串(self):
        """`action` 是 NOT NULL —— 空字符串能写进去，但按它聚合就毫无意义。"""
        assert repository._audit_row("", {})[0] == "unknown"


class TestAuditCounting:
    def test_落库之后数得出来(self):
        """
        ⚠️ **这是 AC-14「可查询」的判据。** 在此之前只能对日志文件 grep。
        """
        async def _go():
            before = await repository.count_audit_events(action="dbtest_probe")
            await repository.insert_audit_events([
                ("dbtest_probe", {"user_id": 1}), ("dbtest_probe", {"user_id": 2}),
            ])
            after = await repository.count_audit_events(action="dbtest_probe")
            await pool.execute("DELETE FROM audit_logs WHERE action = %s",
                               ("dbtest_probe",))
            await pool.close_pool()
            return before, after

        before, after = _run(_go())
        assert before == 0
        assert after == 2

    def test_空批次不产生SQL(self):
        assert _run(repository.insert_audit_events([])) == 0


class TestAuditSink:
    """
    队列 + 后台刷盘。守三条：入队不阻塞、堆积会被计数、失败会被计数。
    """

    def teardown_method(self):
        audit_sink.reset()

    def test_入队与刷盘(self):
        async def _go():
            audit_sink.reset()
            await audit_sink.start()
            assert audit_sink.enqueue("dbtest_sink", {"user_id": 1})
            for _ in range(40):                     # 等后台任务把它刷掉
                await asyncio.sleep(0.1)
                if audit_sink.stats()["written"]:
                    break
            stats = audit_sink.stats()
            await audit_sink.stop()
            await pool.close_pool()
            return stats

        stats = _run(_go())
        assert stats["enqueued"] == 1
        assert stats["written"] == 1, f"审计没落库：{stats}"
        assert stats["failed"] == 0

    def test_队列满时丢最老的并计数(self):
        """
        ⚠️ 队列**有界**。数据库长时间不通时它会满，此时丢最老的并计数 ——
        「最近的事件更可能被查」。计数会经 `/system/metrics` 暴露出来。

        这条测试在**不启动后台任务**的情况下跑，队列才有机会真的满。
        """
        audit_sink.reset()
        overflow = 50
        for i in range(audit_sink.MAXSIZE + overflow):
            audit_sink.enqueue("dbtest_overflow", {"i": i})
        stats = audit_sink.stats()
        assert stats["dropped_full"] == overflow, stats
        # `enqueued` 是"投递成功的次数"（含把别人挤掉的那次），
        # 所以不变量是 enqueued - dropped_full == 队列容量。
        # ⚠️ 第一版断言写的是 `enqueued == MAXSIZE`，把 enqueued 当成了
        #    "留在队列里的条数" —— 两个含义混在一个计数器里，读的人
        #    没法判断"少了 50 条"到底是丢了还是没投。
        assert stats["enqueued"] - stats["dropped_full"] == audit_sink.MAXSIZE, stats

    def test_落库失败被计数(self, monkeypatch):
        """
        **失败必须被看见**（见 audit_sink.py 的说明）：一条悄悄没写进去的
        降级记录，会让"昨天有几次降级"的答案是错的，而错得没有迹象。
        """
        async def _fail(events):
            return 0

        monkeypatch.setattr(repository, "insert_audit_events", _fail)

        async def _go():
            audit_sink.reset()
            await audit_sink.start()
            audit_sink.enqueue("dbtest_fail", {})
            for _ in range(40):
                await asyncio.sleep(0.1)
                if audit_sink.stats()["failed"]:
                    break
            stats = audit_sink.stats()
            await audit_sink.stop()
            return stats

        stats = _run(_go())
        assert stats["failed"] == 1, f"落库失败没有被计数：{stats}"

    def test_不下于最大批次(self):
        """一批不超过 BATCH 条 —— 否则单条 SQL 的参数会无限增长。"""
        assert 0 < audit_sink.BATCH <= 500


# ══════════════════════════════════════════════════════════════════
# store 的三层读写
# ══════════════════════════════════════════════════════════════════


class TestLayoutStoreLayers:
    """
    `api/store.py` 是内存 → Redis → PG 三层。落库这一层要证明的是
    **"进程重启后还在"** —— 所以这里必须绕过内存与 Redis 去读。
    """

    def test_写进库(self):
        async def _go():
            from backend.app.api import store as layout_store

            lid = _TEST_LAYOUT_PREFIX + "layer_save"
            await layout_store.save(lid, _layout(), user_id=9, confidence=0.9)
            got = await repository.load_layout(lid)
            await pool.close_pool()
            return got

        got = _run(_go())
        assert got == _layout()

    def test_内存与Redis都没有时从库捞回(self):
        """
        ⚠️ 这是落库要解决的**唯一**那个场景：一小时前解析的户型、
        而进程已经重启过。在此之前它只剩一句"可能已过期（暂存 1 小时）"。
        """
        async def _go():
            from backend.app.api import store as layout_store
            from backend.app.core.redis_client import get_redis

            lid = _TEST_LAYOUT_PREFIX + "layer_load"
            await layout_store.save(lid, _layout())

            # 模拟"进程重启"：清掉内存，删掉 Redis 键，只留库里的那份
            layout_store.clear()
            client = await get_redis().client()
            if client is not None:
                await client.delete(f"{layout_store.LAYOUT_PREFIX}:{lid}")

            got = await layout_store.load(lid)
            await pool.close_pool()
            return got

        assert _run(_go()) == _layout()

    def test_库里也没有就是真的没有(self):
        """控制项：三层都查不到时返回 None，不能凭空造一份。"""
        async def _go():
            from backend.app.api import store as layout_store

            layout_store.clear()
            got = await layout_store.load(_TEST_LAYOUT_PREFIX + "真不存在")
            await pool.close_pool()
            return got

        assert _run(_go()) is None


# ══════════════════════════════════════════════════════════════════
# 不可用时的行为
# ══════════════════════════════════════════════════════════════════


class TestDegradesGracefully:
    """
    数据库不通时**不能把主链路拖下水**。这是整套设计的前提，
    所以要有用例守着，而不是只在注释里声明。
    """

    def test_开关关掉时直接返回None(self, monkeypatch):
        monkeypatch.setattr(settings, "ENABLE_DB", False)
        pool.reset()

        async def _go():
            ok = await pool.execute("SELECT 1")
            n = await repository.count_audit_events()
            await pool.close_pool()
            return ok, n

        ok, n = _run(_go())
        assert ok is None and n is None
        assert pool.stats()["skipped_unavailable"] >= 1

    def test_写失败返回None而不是抛(self, monkeypatch):
        """
        ⚠️ 与 Redis 同一立场：**库挂了不该让接口挂**。

        `execute()` 返回 None（没执行成）与 0（执行了、没有行受影响）
        是两件事，调用方靠这个区分"库没通"与"写成功但没变化"。
        """
        pool.reset()

        async def _boom(*a, **k):
            raise RuntimeError("模拟连接中断")

        monkeypatch.setattr(pool, "get_pool", _boom)

        async def _go():
            got = await pool.execute("SELECT 1")
            await pool.close_pool()
            return got

        assert _run(_go()) is None
        assert pool.stats()["failed"] >= 1

    def test_冷却期内不建池直接返回None(self):
        """
        连续失败后暂停重试。否则每次请求都要等一次连接超时 ——
        把"数据库慢"放大成"接口慢"。

        ⚠️ **不能把 `get_pool` 换成一个会抛的桩来测这条**（第一版就是
        那么写的）：熔断逻辑就在 `get_pool` **里面**，换掉它等于把要测的
        东西一起换掉了 —— 那次 5 次调用试了 5 次连接，报出来的却是
        "熔断没生效"。要测它就得真的制造一次失败记录，再问它下一次
        还试不试。
        """
        pool.reset()
        pool._record_failure("测试注入", RuntimeError("模拟连接中断"))
        assert pool._cooling_down() is True

        async def _go():
            got = await pool.get_pool()      # 冷却期内：不建池，直接 None
            stats = pool.stats()
            await pool.close_pool()
            return got, stats

        got, stats = _run(_go())
        assert got is None
        assert stats["skipped_unavailable"] >= 1, stats

    def test_冷却期过后恢复重试(self):
        """熔断是**暂停**不是**关闭** —— 永久熔断会让一次抖动的网络
        变成"这个功能以后都不能用了"。"""
        pool.reset()
        pool._record_failure("测试注入", RuntimeError("模拟连接中断"))
        import time as _t

        pool._last_failure_at = _t.monotonic() - pool._COOLDOWN_SECONDS - 1
        assert pool._cooling_down() is False
        pool.reset()

    def test_连不上时真的记了一次失败(self, monkeypatch):
        """
        用**真实**的失败路径：把 DSN 指到一个没人监听的端口。
        与上面那条的区别是，这条走的是"池建起来了、但连不上"，
        也就是生产里最常见的那种故障。
        """
        pool.reset()
        # ⚠️ 改的是**端口字段**，不是 `checkpoint_dsn` —— 那是个只读
        #    property，setattr 会在**拆夹具时**才报
        #    `property ... has no setter`，失败信息指向 teardown，
        #    与真正的问题隔了一层。
        monkeypatch.setattr(settings, "POSTGRES_PORT", 1)

        async def _go():
            got = await pool.fetch_all("SELECT 1")
            stats = pool.stats()
            await pool.close_pool()
            return got, stats

        got, stats = _run(_go())
        assert got is None
        assert stats["failed"] >= 1, f"连接失败没有被计数：{stats}"
        pool.reset()
