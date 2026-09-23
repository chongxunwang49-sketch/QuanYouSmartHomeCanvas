"""
图片质量预检（AC-27）。

═══════════════════════════════════════════════════════════════════
这个文件守的是什么
═══════════════════════════════════════════════════════════════════
需求文档 2.2.4 的阶段表里有一行 `prechecking`（"正在检查图片质量…"），
而在这之前**代码里根本没有这一步** —— 任务启动时无条件写那个阶段，
界面显示它的时候什么都没发生。属于「UI 声称做了一件没做的事」。

所以这里既测**功能**（不合格的图真的被拦），也测**诚实性**
（`prechecking` 阶段确实由一次真实的节点执行产生，不是写死的）。

最关键的一条是 `test_不合格图片时模型一次都没被调用` ——
AC-27 的原话是「Token 消耗为 0」。只断言"返回了错误"是不够的：
错误可能是在调用模型**之后**才产生的，那 Token 已经花出去了。
"""

from __future__ import annotations

import asyncio
import base64
import io

import pytest
from PIL import Image, ImageDraw

from backend.app.graph import workflow
from backend.app.graph.state import initial_state
from backend.app.services.image.precheck import (
    MAX_ASPECT,
    MIN_SHORT_SIDE,
    PrecheckResult,
    decode_input,
    precheck_image,
    precheck_ref,
)


# ══════════════════════════════════════════════════════════════════
# 造图工具
# ══════════════════════════════════════════════════════════════════


def _floorplan_png(width: int = 1600, height: int = 1200, *, blur: bool = False) -> bytes:
    """
    画一张"像户型图"的图：白底 + 黑线框 + 若干房间矩形。

    **不能直接用纯白图** —— 那会被"近乎纯色"那条规则拦掉，
    于是"正常图应当通过"的用例永远测不到真正想测的路径。
    """
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    m = int(min(width, height) * 0.08)

    # 外墙
    d.rectangle([m, m, width - m, height - m], outline="black", width=4)
    # 内部隔墙，切出几个"房间"
    d.line([width // 2, m, width // 2, height - m], fill="black", width=3)
    d.line([m, height // 2, width - m, height // 2], fill="black", width=3)
    # 门洞与窗（留白的一段）
    d.rectangle([width // 2 - 8, height // 2 - 40, width // 2 + 8, height // 2 - 8],
                fill="white")
    # 尺寸标注文字（用短线模拟，不去依赖字体）
    for i in range(6):
        y = m + 40 + i * 30
        d.line([m + 20, y, m + 200, y], fill="black", width=2)

    if blur:
        from PIL import ImageFilter

        img = img.filter(ImageFilter.GaussianBlur(radius=max(2, min(width, height) // 120)))

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _data_uri(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode()


# ══════════════════════════════════════════════════════════════════
# 纯函数层
# ══════════════════════════════════════════════════════════════════


class TestPrecheckFunction:
    def test_正常户型图通过(self):
        r = precheck_image(_floorplan_png())
        assert r.ok, f"正常图不该被拦：{r.rejections}"
        assert r.width == 1600 and r.height == 1200
        assert "可用" in r.advice

    def test_清晰度检测缺失时优雅降级(self):
        """
        `cv2` 是**可选依赖** —— 本机两个 Python 环境里只有一个装了它
        （base 环境有、pytorch 环境没有）。所以这里断言的是**两种情况下都成立**
        的性质，而不是"blur_score 必须有值"。

        ⚠️ 第一版这条断言写成 `blur_score is not None`，于是在 pytorch 环境
        下挂掉 —— 而那个环境才是 README 里文档化的启动环境。
        断言不该耦合到一个按设计就可选的依赖上。
        """
        try:
            import cv2  # noqa: F401

            has_cv2 = True
        except ImportError:
            has_cv2 = False

        r = precheck_image(_floorplan_png())
        assert r.ok, "缺 cv2 不该影响放行结论"

        if has_cv2:
            assert r.blur_score is not None and r.blur_score > 0
            assert not any("未安装 OpenCV" in w for w in r.warnings)
        else:
            assert r.blur_score is None
            assert any("未安装 OpenCV" in w for w in r.warnings), (
                "缺 cv2 时必须**说出来**，不能静默跳过 —— 静默跳过等于假装检查过了"
            )

    def test_分辨率过低被拒(self):
        r = precheck_image(_floorplan_png(width=300, height=200))
        assert not r.ok
        assert any("分辨率过低" in x for x in r.rejections)
        # 提醒里要给出实际尺寸，用户才知道差多少
        assert any(str(MIN_SHORT_SIDE) in x for x in r.rejections)

    def test_非图片被拒(self):
        r = precheck_image(b"this is definitely not an image")
        assert not r.ok
        assert any("无法解码" in x for x in r.rejections)

    def test_近乎纯色被拒(self):
        """传了一张白纸 —— 没有可识别内容，不该浪费一次模型调用。"""
        buf = io.BytesIO()
        Image.new("RGB", (1200, 900), "white").save(buf, "PNG")
        r = precheck_image(buf.getvalue())
        assert not r.ok
        assert any("近乎纯色" in x for x in r.rejections)

    def test_极端长宽比被拒(self):
        r = precheck_image(_floorplan_png(width=4000, height=500))
        assert not r.ok
        assert any("长宽比异常" in x for x in r.rejections)
        assert str(MAX_ASPECT) in r.advice or any("长宽比" in x for x in r.rejections)

    def test_模糊只提醒不拒收(self):
        """
        ⚠️ 这条守的是一条**刻意的设计决定**。

        模糊阈值（Laplacian 方差）**没有用真实户型图标定过**。
        用一个没标定的阈值去拒收，会把好图误杀，而用户看到的理由是
        「图片模糊」—— 他会以为是自己拍得不好，反复重拍，
        永远不知道是阈值错了。

        所以模糊只提醒。这条断言防的是将来有人"顺手"把它改成拒收。
        """
        r = precheck_image(_floorplan_png(blur=True))
        assert not any("清晰度" in x for x in r.rejections), (
            "模糊度不能参与拒收 —— 阈值未标定，见 precheck.py 模块说明"
        )

    def test_图像库不可用时放行而不是拦下(self, monkeypatch):
        """
        图像库抽风时不能把用户的解析请求一起弄停 ——
        一个质检环节坏掉却让主流程挂掉，比没有质检更糟。

        用 `sys.modules` 里塞 None 的方式模拟 ImportError：
        `import PIL` 拿到 None 之后取 `Image` 会抛 TypeError，
        而模块里那条 try/except 正好覆盖它。
        """
        import builtins

        real_import = builtins.__import__

        def _fail(name, *a, **kw):
            if name in ("PIL", "PIL.Image", "numpy"):
                raise ImportError(f"模拟 {name} 不可用")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _fail)
        r = precheck_image(_floorplan_png())
        assert isinstance(r, PrecheckResult)
        assert r.ok, "预检自己坏掉时必须放行，而不是把用户的图拦在门外"
        assert any("跳过预检" in w for w in r.warnings)

    def test_分析过程中抛异常也不阻断(self, monkeypatch):
        """
        覆盖另一半：库能导入，但解码/计算过程中炸了。
        同样必须放行 —— 理由同上。
        """
        import numpy as np

        real_asarray = np.asarray

        def _boom(*a, **kw):
            raise RuntimeError("模拟分析阶段炸了")

        monkeypatch.setattr(np, "asarray", _boom)
        try:
            r = precheck_image(_floorplan_png())
        finally:
            monkeypatch.setattr(np, "asarray", real_asarray)

        assert isinstance(r, PrecheckResult)
        assert r.ok, "预检内部异常时必须放行"


class TestDecodeInput:
    def test_接受_data_uri(self):
        raw = _floorplan_png(600, 500)
        assert decode_input(_data_uri(raw)) == raw

    def test_接受裸_base64(self):
        raw = _floorplan_png(600, 500)
        assert decode_input(base64.b64encode(raw).decode()) == raw

    def test_坏_base64_抛错(self):
        with pytest.raises(ValueError):
            decode_input("!!!! not base64 !!!!")

    def test_precheck_ref_把解码失败也当不合格(self):
        """解码失败也是一种"不合格"，不该抛异常穿透到调用方。"""
        r = precheck_ref("data:image/png;base64,%%%%")
        assert not r.ok
        assert r.rejections


# ══════════════════════════════════════════════════════════════════
# 节点层
# ══════════════════════════════════════════════════════════════════


class TestPrecheckNode:
    def test_合格图片写入_precheck_结果(self):
        out = workflow.precheck_image(
            initial_state(task_id="t", image_ref=_data_uri(_floorplan_png()))
        )
        assert out["precheck"]["ok"] is True
        assert out["precheck"]["width"] == 1600

    def test_不合格图片抛_PrecheckError(self):
        from PIL import Image as _I

        buf = io.BytesIO()
        _I.new("RGB", (200, 150), "white").save(buf, "PNG")
        with pytest.raises(workflow.PrecheckError) as ei:
            workflow.precheck_image(
                initial_state(task_id="t", image_ref=_data_uri(buf.getvalue()))
            )
        # advice 是可操作的一句话，不带异常类名
        assert "分辨率过低" in ei.value.advice
        assert "PrecheckError" not in ei.value.advice
        assert ei.value.result.ok is False


# ══════════════════════════════════════════════════════════════════
# 图集成 —— AC-27 的核心断言
# ══════════════════════════════════════════════════════════════════


class _RecordingLLM:
    """记录每一次调用。用来断言"一次都没被调用"。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete_json(self, schema, **kw):  # pragma: no cover
        self.calls.append(schema.__name__)
        raise AssertionError("预检未通过的请求不该走到模型调用")

    async def complete(self, **kw):  # pragma: no cover
        self.calls.append("complete")
        raise AssertionError("预检未通过的请求不该走到模型调用")


class TestZeroTokenOnRejection:
    def test_不合格图片时模型一次都没被调用(self, monkeypatch):
        """
        ⚠️ **AC-27 的核心断言。**

        「Token 消耗为 0」不能只断言"返回了错误" —— 错误完全可能是在
        调用模型**之后**才产生的，那时 Token 已经花出去了。
        所以这里断的是 `llm.calls == []`。
        """
        from backend.app.agents.layout_parser import LayoutParserAgent

        llm = _RecordingLLM()
        monkeypatch.setitem(workflow._AGENTS, "parse_layout",
                            LayoutParserAgent(llm=llm))

        # 一张 200×150 的图 —— 不合格
        buf = io.BytesIO()
        Image.new("RGB", (200, 150), "white").save(buf, "PNG")

        graph = workflow.build_graph(with_checkpointer=False, stages="parse")
        with pytest.raises(workflow.PrecheckError):
            asyncio.run(graph.ainvoke(
                initial_state(task_id="t", image_ref=_data_uri(buf.getvalue()))
            ))

        assert llm.calls == [], f"模型被调用了 {llm.calls} —— AC-27 要求零 Token"

    def test_合格图片会继续走到解析(self, monkeypatch, stub_knowledge):
        """反向确认：预检不是把什么都拦掉。"""
        from backend.app.agents.layout_parser import LayoutParserAgent
        from backend.app.core.llm_client import LLMResult

        called = {"n": 0}

        class _OK:
            async def complete_json(self, schema, **kw):
                called["n"] += 1
                payload = {
                    "rooms": [{"name": "客厅", "type": "living_room", "area": 20.0,
                               "bbox": [10, 10, 200, 200], "orientation": "south"}],
                    "walls": [], "windows": [], "dimensions": [],
                    "total_area": 60.0, "has_north_arrow": True, "confidence": 0.8,
                }
                return schema.model_validate(payload), LLMResult(
                    text="{}", provider="fake", model_used="fake",
                    prompt_tokens=1, completion_tokens=1, elapsed_ms=1)

            async def complete(self, **kw):  # pragma: no cover
                raise AssertionError("不走纯文本路径")

        monkeypatch.setitem(workflow._AGENTS, "parse_layout",
                            LayoutParserAgent(llm=_OK()))

        graph = workflow.build_graph(with_checkpointer=False, stages="parse")
        out = asyncio.run(graph.ainvoke(
            initial_state(task_id="t", image_ref=_data_uri(_floorplan_png()))
        ))
        assert called["n"] >= 1, "预检通过的图应当继续走到解析"
        assert out["precheck"]["ok"] is True


# ══════════════════════════════════════════════════════════════════
# 诚实性：prechecking 阶段是真的
# ══════════════════════════════════════════════════════════════════


class TestPrecheckingPhaseIsReal:
    def test_prechecking_由真实节点产生而不是写死(self):
        """
        ⚠️ 这条守的是**诚实性**，不是功能。

        之前 `prechecking` 是任务启动时无条件写入的一个阶段，
        而"检查图片质量"这件事根本没发生 —— 界面在撒谎。

        现在它必须对应 `precheck_image` 这个真实节点。
        如果将来有人把节点删了却忘了删阶段映射（或反过来），
        这条会炸。
        """
        from backend.app.api.tasks import _NODE_PHASE

        assert _NODE_PHASE.get("precheck_image") == "prechecking", (
            "prechecking 阶段必须由一个真实节点驱动"
        )
        # 反向：这个节点必须真的在图里注册了
        graph_nodes = workflow.build_graph(
            with_checkpointer=False, stages="parse"
        ).get_graph().nodes
        assert "precheck_image" in graph_nodes, (
            "阶段表里有 precheck_image，但图里没有这个节点 —— 阶段又说谎了"
        )

    def test_阶段表里每个节点都真实存在(self):
        """
        穷举：`_NODE_PHASE` 里登记的每个节点名，都必须在图上能找到。

        防的是 `detecting_rooms` / `extracting_dimensions` 那类问题 ——
        需求文档的阶段表列了 8 行，实现只发出 4 行，另外两行
        **永远不会出现**。声明了却不存在，和撒谎只差一步。
        """
        from backend.app.api.tasks import _NODE_PHASE

        graph_nodes = set(workflow.build_graph(
            with_checkpointer=False, stages="full"
        ).get_graph().nodes)
        missing = [n for n in _NODE_PHASE if n not in graph_nodes]
        assert not missing, (
            f"以下节点在 _NODE_PHASE 里登记了，但图上不存在：{missing}"
            f"（图上的节点：{sorted(graph_nodes)}）"
        )


class TestPrecheckPhaseIsEmitted:
    """
    `prechecking` 阶段必须**真的被发出过一次**。

    上面那条 `test_prechecking_由真实节点产生而不是写死` 只验了"节点存在、
    映射存在"，但没验"事件真的会流出来"。这两件事可以同时为真而阶段仍然不发 ——
    比如节点名字写错一个字母、或者被加进了 Send 分支。

    做法和 `api/tasks.py::_run` 完全一致：监听 `astream_events` 的
    `on_chain_start`，取事件名。那里怎么取，这里就怎么取 ——
    用别的方式验证等于验了个别的东西。
    """

    @staticmethod
    def _chain_starts(image_ref: str) -> list[str]:
        from backend.app.agents.layout_parser import LayoutParserAgent

        # 解析器换成一个永不被调用的桩：这条测试只关心事件流，
        # 且要确认预检失败时根本走不到它。
        class _Never:
            async def complete_json(self, *a, **kw):  # pragma: no cover
                raise AssertionError("预检失败时不该调用模型")

            async def complete(self, *a, **kw):  # pragma: no cover
                raise AssertionError("预检失败时不该调用模型")

        import backend.app.graph.workflow as wf

        saved = wf._AGENTS.get("parse_layout")
        wf._AGENTS["parse_layout"] = LayoutParserAgent(llm=_Never())
        try:
            graph = wf.build_graph(with_checkpointer=False, stages="parse")
            names: list[str] = []

            async def _run():
                try:
                    async for ev in graph.astream_events(
                        initial_state(task_id="t", image_ref=image_ref), version="v2"
                    ):
                        if ev.get("event") == "on_chain_start":
                            names.append(str(ev.get("name")))
                except Exception:  # noqa: BLE001 —— 预检失败是预期内的
                    pass

            asyncio.run(_run())
            return names
        finally:
            if saved is not None:
                wf._AGENTS["parse_layout"] = saved

    def test_预检失败时_precheck_image_事件先于解析发生(self):
        buf = io.BytesIO()
        Image.new("RGB", (200, 150), "white").save(buf, "PNG")
        names = self._chain_starts(_data_uri(buf.getvalue()))

        assert "precheck_image" in names, (
            f"事件流里没有 precheck_image —— 那么 api/tasks.py 就取不到 "
            f"prechecking 阶段，界面又回到'说了在做其实没做'。实际事件：{names}"
        )
        # 预检是**第一跳**：它必须出现在 parse_layout 之前（或后者压根没发生）
        if "parse_layout" in names:
            assert names.index("precheck_image") < names.index("parse_layout"), (
                "预检必须发生在解析之前 —— 否则不合格的图已经进过模型了，AC-27 破"
            )

    def test_合格图片时解析事件也发生(self):
        names = self._chain_starts(_data_uri(_floorplan_png()))
        assert "precheck_image" in names
        assert "parse_layout" in names, (
            "预检通过后应当继续走到解析 —— 否则预检把什么都拦了"
        )
