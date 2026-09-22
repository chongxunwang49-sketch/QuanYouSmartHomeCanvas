"""
Agent 基类。

设计目标（对应 V2.0 文档第 3.3 节「超时熔断」与 AC-17「降级容错」）：
1. **单个 Agent 失败绝不拖垮主流程** —— execute() 捕获一切异常，返回带错误标记的
   部分状态，由下游节点或 fan-in 决定如何处理。
2. **超时熔断** —— 默认 30s，超时即跳过并标记，不阻塞其他并行分支。
3. **全链路可观测** —— 每次执行都产出 trace 记录（耗时/token/是否降级），
   这些字段最终会进入审计日志（AC-14）与性能基线（AC-23）。
"""

from __future__ import annotations

import abc
import asyncio
import time
from typing import Any, Sequence

from loguru import logger
from pydantic import BaseModel

from ..core.config import settings
from ..core.llm_client import ImagePart, LLMClient, LLMError, LLMResult, get_llm_client
from ..core.logger import log_llm_call, logger
from ..graph.state import HomeDecoState


class AgentTimeoutError(TimeoutError):
    """Agent 执行超时。"""


class BaseAgent(abc.ABC):
    """
    所有 Agent 的基类。

    子类只需实现 run()，并把跨切面关注点（超时、重试、日志、降级标记）
    交给基类处理。
    """

    #: Agent 标识，用于日志与 trace，如 "A-01"
    code: str = "A-XX"
    #: Agent 名称，如 "LayoutParserAgent"
    name: str = "BaseAgent"
    #: 单次执行超时（秒）。可从配置覆盖。
    timeout: float = settings.AGENT_TIMEOUT_SECONDS
    #: 本 Agent 是否依赖多模态能力
    requires_vision: bool = False

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or get_llm_client()
        self.log = logger.bind(agent=self.code, name=self.name)

    # ── 子类实现 ──────────────────────────────────────────

    @abc.abstractmethod
    async def run(self, state: HomeDecoState) -> dict[str, Any]:
        """
        执行 Agent 主体逻辑，返回**部分状态**（将与入参 state 合并）。

        实现约定：
        - 不要捕获异常后返回空结果，让异常抛出，由 execute() 统一处理；
        - 若确需降级（例如主模型失败），在返回值中显式带上 degraded=True 与
          degrade_reason，**不要静默降级**。
        """
        raise NotImplementedError

    # ── 执行入口（LangGraph 节点使用）─────────────────────

    async def execute(self, state: HomeDecoState) -> dict[str, Any]:
        """
        LangGraph 节点函数。包裹 run()，提供超时熔断与异常吞没。

        无论成功与否都返回可合并的部分状态，保证图能继续走。
        """
        started = time.perf_counter()
        self.log.info(f"开始执行，task_id={state.get('task_id', '?')}")

        try:
            result = await asyncio.wait_for(self.run(state), timeout=self.timeout)
            elapsed = self._elapsed_ms(started)
            self.log.info(f"执行完成，耗时 {elapsed}ms")
            return self._finalize(result, elapsed, ok=True)

        except asyncio.TimeoutError:
            elapsed = self._elapsed_ms(started)
            reason = f"执行超时（>{self.timeout}s），已跳过"
            self.log.error(reason)
            return self._finalize({}, elapsed, ok=False, reason=reason)

        except LLMError as e:
            elapsed = self._elapsed_ms(started)
            reason = f"LLM 调用失败: {e}"
            self.log.error(reason)
            return self._finalize({}, elapsed, ok=False, reason=reason)

        except Exception as e:  # noqa: BLE001 —— 熔断器必须兜住一切
            elapsed = self._elapsed_ms(started)
            reason = f"{type(e).__name__}: {e}"
            self.log.exception(f"执行异常，已隔离: {reason}")
            return self._finalize({}, elapsed, ok=False, reason=reason)

    def _finalize(
        self,
        result: dict[str, Any],
        elapsed_ms: int,
        *,
        ok: bool,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """补上 trace / errors / degraded 等跨切面字段。"""
        out = dict(result)
        out.setdefault("degraded", not ok)
        if not ok and reason:
            out.setdefault("degrade_reasons", [])
            out["degrade_reasons"] = list(out["degrade_reasons"]) + [f"[{self.code}] {reason}"]
            out["errors"] = list(out.get("errors", [])) + [{
                "agent": self.code,
                "type": "timeout" if "超时" in reason else "error",
                "message": reason,
            }]

        out["trace"] = list(out.get("trace", [])) + [{
            "agent": self.code,
            "name": self.name,
            "ok": ok,
            "elapsed_ms": elapsed_ms,
            "llm": out.pop("_llm_meta", None),
        }]
        return out

    # ── 受保护的辅助方法 ──────────────────────────────────

    async def _call_llm(
        self,
        *,
        user: str,
        system: str | None = None,
        images: Sequence[ImagePart] | None = None,
        schema: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        allow_degrade: bool = True,
        force_local: bool = False,
    ) -> tuple[Any, LLMResult]:
        """
        调用 LLM。传入 schema 时返回 (Pydantic 模型, LLMResult)，否则返回 (文本, LLMResult)。

        统一在此记录 token 与降级信息。

        allow_degrade=False —— 只走主模型，失败即抛错。用于「要求完整结构」的任务，
                               避免本地小模型拿完整 Schema 硬编。
        force_local=True    —— 只走本地模型，云端绝不参与。用于隐私模式，
                               这是 AC-24 的技术保证点。
        （详见 LLMClient._chain 的说明）
        """
        kw = {"allow_degrade": allow_degrade, "force_local": force_local}
        if schema is not None:
            parsed, result = await self.llm.complete_json(
                schema, user=user, system=system, images=images,
                max_tokens=max_tokens, agent=self.code, **kw,
            )
            self._note_llm(result)
            return parsed, result

        result = await self.llm.complete(
            user=user, system=system, images=images,
            max_tokens=max_tokens, agent=self.code, **kw,
        )
        self._note_llm(result)
        return result.text, result

    def _note_llm(self, result: LLMResult) -> None:
        """
        记录本次 LLM 调用的元信息。

        做两件事：
        1. 暂存到 self._last_llm_meta，供 _finalize 写入 trace（前端可见）；
        2. **输出一条结构化日志**，供审计与性能统计（AC-14 / AC-23）。

        第 2 步不能省：审计日志要求记录 model_used 与 degraded，
        且降级事件要以 WARNING 级别落盘，便于事后 grep 统计降级率。
        """
        self._last_llm_meta: dict[str, Any] = {
            "provider": result.provider,
            "model": result.model_used,
            "degraded": result.degraded,
            "degrade_reason": result.degrade_reason,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "reasoning_tokens": result.reasoning_tokens,
            "elapsed_ms": result.elapsed_ms,
        }
        log_llm_call(agent=self.code, **self._last_llm_meta)

    async def _call_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """
        调用 MCP 工具。延迟导入以避免 mcp 未安装时影响纯 LLM Agent。
        """
        from ..core.mcp_client import get_mcp_client

        return await get_mcp_client().call_tool(tool_name, arguments)

    async def _retry(
        self,
        fn,
        *args: Any,
        attempts: int | None = None,
        delay: float = 1.0,
        backoff: float = 2.0,
        **kwargs: Any,
    ) -> Any:
        """带指数退避的重试。仅用于非 LLM 的易失败操作（网络、IO）。"""
        attempts = attempts or settings.LLM_MAX_RETRIES + 1
        last: Exception | None = None
        for i in range(attempts):
            try:
                return await fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                last = e
                if i < attempts - 1:
                    wait = delay * (backoff ** i)
                    self.log.warning(f"第 {i + 1}/{attempts} 次失败（{e}），{wait:.1f}s 后重试")
                    await asyncio.sleep(wait)
        raise last  # type: ignore[misc]

    def _log(self, message: str, **fields: Any) -> None:
        """结构化日志便捷方法。"""
        self.log.bind(**fields).info(message)

    def _elapsed_ms(self, started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    @staticmethod
    def _image_from_state(state: HomeDecoState) -> ImagePart | None:
        """从状态中还原图像。约定状态里存 data URI 或 base64 原文。"""
        uri = state.get("image_ref")
        if not uri:
            return None
        if uri.startswith("data:"):
            return ImagePart.from_data_uri(uri)
        return ImagePart(data_b64=uri, media_type=state.get("image_media_type", "image/png"))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.name} code={self.code} timeout={self.timeout}s>"


__all__ = ["BaseAgent", "AgentTimeoutError"]
