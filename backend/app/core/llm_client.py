"""
统一 LLM 客户端：三级降级 + 结构化输出 + 可观测性。

═══════════════════════════════════════════════════════════════════════
本文件的三个关键设计决策，均来自 2026-09-22 的本机实测（详见 V2.0 文档第零章）
═══════════════════════════════════════════════════════════════════════

【决策 1】不使用 LangChain 的 with_structured_output
  实测结果（DeepSeek /v1）：
    - method="function_calling" -> HTTP 400 "Thinking mode does not support this tool_choice"
    - method="json_schema"      -> HTTP 400 "This response_format type is unavailable now"
    - method="json_mode"        -> 能返回 JSON，但**不遵守 schema**：
                                    要求 rooms[] 却返回 living_room_area_sqm，校验必失败
    - 提示词内嵌 schema + 纯文本 -> ✅ 结构完全正确
  因此本客户端统一采用「schema 注入 + 宽松抽取 + Pydantic 校验 + 失败重试」的策略。
  附带好处：该策略与提供方无关，DeepSeek / Ollama / 未来的 DashScope 走同一套代码。

【决策 2】max_tokens 必须给足
  实测：deepseek-flash 是思考模型，单次调用的 179 个 completion token 中有 169 个是
  reasoning token。当 max_tokens=100 时，推理吃光全部预算，content 返回**空字符串**。
  症状隐蔽（HTTP 200、无异常），排查成本高，故在此显式检测并抛出明确错误。

【决策 3】降级必须是显式的
  每次调用都返回 LLMResult，携带 degraded / degrade_reason / model_used。
  这些字段会一路传到 API 响应、审计日志和前端 UI（对应 AC-17）。
  **静默降级视为缺陷**——用户有权知道这次结果来自本地小模型。
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

import httpx
from loguru import logger
from pydantic import BaseModel, ValidationError

from .config import settings

# ══════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════


@dataclass
class ImagePart:
    """内部统一的图像片段。各提供方适配器负责转成自己的报文格式。"""

    data_b64: str
    media_type: str = "image/png"

    @classmethod
    def from_path(cls, path: str) -> "ImagePart":
        raw = open(path, "rb").read()
        suffix = path.lower().rsplit(".", 1)[-1]
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp"}.get(suffix, "image/png")
        return cls(base64.b64encode(raw).decode(), mime)

    @classmethod
    def from_raw(cls, data: bytes, media_type: str = "image/png") -> "ImagePart":
        return cls(base64.b64encode(data).decode(), media_type)

    @classmethod
    def from_data_uri(cls, uri: str) -> "ImagePart":
        """解析 data:image/png;base64,xxxx 形式的 URI。"""
        m = re.match(r"data:(?P<mime>[^;]+);base64,(?P<data>.+)", uri, re.S)
        if not m:
            raise ValueError("不是合法的 data URI")
        return cls(m.group("data"), m.group("mime"))

    def to_data_uri(self) -> str:
        return f"data:{self.media_type};base64,{self.data_b64}"


@dataclass
class LLMResult:
    """一次 LLM 调用的完整结果，含可观测性字段。"""

    text: str
    provider: str                      # deepseek / ollama
    model_used: str
    degraded: bool = False
    degrade_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    elapsed_ms: int = 0
    attempts: int = 1
    raw_usage: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMError(RuntimeError):
    """LLM 全部提供方均失败。"""


class LLMTruncatedError(LLMError):
    """输出被 max_tokens 截断导致 content 为空（思考模型特有）。"""


# ══════════════════════════════════════════════════════════════════════
# JSON 抽取工具
# ══════════════════════════════════════════════════════════════════════

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)

# 小模型（如本地 minicpm）经常产出语法残缺的 JSON，实测最常见的两种：
#   1. 字符串值不加引号：  "orientation": north      （枚举值裸写）
#   2. 尾随逗号：          {"a": 1,}
_JSON_LITERALS = {"true", "false", "null", "True", "False", "None"}
_UNQUOTED_VALUE_RE = re.compile(
    r'(:\s*)([A-Za-z_一-鿿][A-Za-z0-9_一-鿿]*)(\s*[,}\]])'
)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _repair_json(text: str) -> str:
    """
    尝试修复小模型常见的 JSON 语法错误。

    注意：这是**尽力而为的容错**，不是许可证。修复后的内容仍要过 Pydantic 校验，
    结构错误不会被这里掩盖。数字、true/false/null 等字面量保持原样。
    """

    def _quote(m: re.Match) -> str:
        prefix, value, suffix = m.group(1), m.group(2), m.group(3)
        if value in _JSON_LITERALS:
            return f"{prefix}{value}{suffix}"
        return f'{prefix}"{value}"{suffix}'

    repaired = _UNQUOTED_VALUE_RE.sub(_quote, text)
    repaired = _TRAILING_COMMA_RE.sub(r"\1", repaired)
    return repaired


def extract_json(text: str) -> Any:
    """
    从模型输出中宽松抽取 JSON。

    依次尝试：直接解析 -> 剥 markdown 围栏 -> 取第一个平衡的括号块 -> 语法修复后重试。
    模型时常在 JSON 前后加解释文字，小模型还会写出语法残缺的 JSON，都要兜住。
    """
    if not text or not text.strip():
        raise ValueError("模型返回内容为空")

    candidates: list[str] = [text.strip()]

    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())

    # 扫描第一个平衡的 { ... } 或 [ ... ]
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break

    # 先按原样试，全部失败后再试修复版
    for c in list(candidates):
        if not c:
            continue
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            candidates_fallback = _repair_json(c)
            if candidates_fallback != c:
                candidates.append(candidates_fallback)

    last_err: Exception | None = None
    for c in candidates:
        if not c:
            continue
        try:
            return json.loads(c)
        except json.JSONDecodeError as e:
            last_err = e
    raise ValueError(f"无法从模型输出中抽取 JSON: {last_err}; 原文前 300 字: {text[:300]}")


def build_schema_instruction(schema: type[BaseModel]) -> str:
    """
    把 Pydantic 模型渲染成给模型看的字段说明。

    ⚠️ 这里是本项目结构化输出的**命门**，修复记录见下：

    初版实现只展开了「直接 `$ref`」的字段，**没有展开数组元素里的 `$ref`**
    （如 `rooms: list[Room]`，其 JSON Schema 是
    `{"type":"array","items":{"$ref":"#/$defs/Room"}}`）。
    结果是模型只被告知"rooms 是个数组"，从没见过 Room 有哪些字段、
    type 能取哪些值 —— 于是自由发挥了 `type: "unknown"`，校验必然失败，
    而且重试再多次也没用（模型压根不知道错在哪）。

    因此本函数必须**递归展开** `$ref` / `items` / `anyOf` / `enum`，
    并显式枚举所有可选值。深度上限用于防止自引用模型无限递归。
    """
    js = schema.model_json_schema()
    defs = js.get("$defs", {})
    lines: list[str] = [
        "你**只能**输出一个 JSON 对象，不要输出任何解释、前后缀或 markdown 围栏。",
        "",
        "字段定义（请严格使用列出的字段名与可选值）：",
    ]

    def resolve(spec: dict) -> dict:
        """解引用 $ref，返回真实 schema 片段。"""
        seen = 0
        while "$ref" in spec and seen < 8:
            name = spec["$ref"].split("/")[-1]
            spec = defs.get(name, {})
            seen += 1
        return spec

    def type_str(spec: dict, depth: int = 0) -> str:
        """把类型渲染成一行可读文字，枚举值全部列出。"""
        if depth > 8:
            return "…"
        spec = resolve(spec)

        # Optional[X] 会变成 anyOf: [{...}, {"type":"null"}]
        alts = spec.get("anyOf") or spec.get("oneOf")
        if alts:
            parts: list[str] = []
            for a in alts:
                rendered = type_str(a, depth + 1)
                if rendered and rendered != "null":
                    parts.append(rendered)
            # 去重保序
            return " | ".join(dict.fromkeys(parts))

        if "enum" in spec:
            return " | ".join(str(e) for e in spec["enum"])

        if "const" in spec:
            return str(spec["const"])

        t = spec.get("type")
        if t == "array":
            inner = type_str(spec.get("items", {}), depth + 1)
            # 嵌套数组（如 list[list[int]] 的墙体折线）说人话
            if inner.endswith("组成的数组"):
                return f"二维数组（{inner[:-len('组成的数组')].strip()}）"
            return f"{inner} 组成的数组"
        if t == "object" or "properties" in spec:
            return "对象"
        return t or "any"

    def walk(spec: dict, indent: str, depth: int) -> None:
        """递归输出字段定义。"""
        if depth > 6:
            return
        spec = resolve(spec)

        # 数组 -> 下钻到元素
        if spec.get("type") == "array":
            walk(spec.get("items", {}), indent, depth + 1)
            return

        props = spec.get("properties")
        if not props:
            return

        required = set(spec.get("required", []))
        for name, pspec in props.items():
            resolved = resolve(pspec)
            desc = pspec.get("description") or resolved.get("description", "")
            mark = "必填" if name in required else "可选"
            lines.append(f"{indent}- {name}（{mark}）: {type_str(pspec)}  {desc}".rstrip())

            child_indent = indent + "    "
            # 嵌套对象 / 数组对象都要继续展开，否则模型看不到内层字段与枚举
            if resolved.get("type") == "object" or "properties" in resolved:
                lines.append(f"{child_indent}其结构为：")
                walk(resolved, child_indent + "  ", depth + 1)
            elif resolved.get("type") == "array":
                item = resolve(resolved.get("items", {}))
                if item.get("type") == "object" or "properties" in item:
                    lines.append(f"{child_indent}其每个元素的结构为：")
                    walk(item, child_indent + "  ", depth + 1)

    walk(js, "", 0)
    lines += [
        "",
        "务必注意：",
        "1. 枚举类型字段只能取上面列出的值之一，不要自创取值（如把房间类型写成 unknown 之外的词）。",
        "2. 只输出 JSON 对象本身，不要包裹 markdown 代码围栏。",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# 提供方适配器
# ══════════════════════════════════════════════════════════════════════


def _to_openai_messages(
    system: str | None, messages: Sequence[dict], images: Sequence[ImagePart] | None
) -> list[dict]:
    """转成 OpenAI 兼容格式（DeepSeek /v1 与 Ollama /v1 共用）。"""
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})

    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            out.append({"role": msg["role"], "content": content})
        else:
            out.append({"role": msg["role"], "content": content})

    # 图像统一附加到最后一条 user 消息上
    if images:
        if not out or out[-1]["role"] != "user":
            out.append({"role": "user", "content": ""})
        last = out[-1]
        parts: list[dict] = []
        if isinstance(last["content"], str) and last["content"]:
            parts.append({"type": "text", "text": last["content"]})
        elif isinstance(last["content"], list):
            parts.extend(last["content"])
        for img in images:
            parts.append({"type": "image_url",
                          "image_url": {"url": img.to_data_uri()}})
        last["content"] = parts
    return out


def _parse_openai_usage(data: dict) -> tuple[int, int, int, dict]:
    u = data.get("usage") or {}
    prompt = u.get("prompt_tokens", 0) or 0
    completion = u.get("completion_tokens", 0) or 0
    details = u.get("completion_tokens_details") or {}
    reasoning = details.get("reasoning_tokens", 0) or 0
    return prompt, completion, reasoning, u


class _Provider:
    """提供方基类。"""

    name: str = "base"

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    @property
    def available(self) -> bool:
        return True

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def complete(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
        timeout: float,
    ) -> tuple[str, int, int, int, dict]:
        """返回 (text, prompt_tokens, completion_tokens, reasoning_tokens, usage)。"""
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code >= 400:
                raise LLMError(
                    f"[{self.name}] HTTP {resp.status_code}: {resp.text[:300]}"
                )
            data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        p, c, r, u = _parse_openai_usage(data)

        if not text.strip():
            if r > 0:
                raise LLMTruncatedError(
                    f"[{self.name}] 返回空内容，但消耗了 {r} 个 reasoning token —— "
                    f"max_tokens={max_tokens} 被思考过程耗尽。请调大 LLM_MAX_TOKENS。"
                )
            raise LLMError(f"[{self.name}] 返回空内容，finish_reason={choice.get('finish_reason')}")
        return text, p, c, r, u


class DeepSeekProvider(_Provider):
    """DeepSeek（多模态主模型）。实测 /v1 支持 image_url 内联 base64 图片。"""

    name = "deepseek"

    @property
    def available(self) -> bool:
        return bool(self.api_key)


class OllamaProvider(_Provider):
    """Ollama（本地兜底）。走 OpenAI 兼容端点，无网络依赖。"""

    name = "ollama"

    def __init__(self, base_url: str, model: str):
        super().__init__(base_url=f"{base_url.rstrip('/')}/v1", model=model, api_key="")

    async def complete(self, messages, *, max_tokens, temperature, timeout):
        # 本地推理慢，且 3B 级模型不擅长长输出，适当收敛
        return await super().complete(
            messages,
            max_tokens=min(max_tokens, 2048),
            temperature=temperature,
            timeout=max(timeout, 180.0),
        )


# ══════════════════════════════════════════════════════════════════════
# 客户端
# ══════════════════════════════════════════════════════════════════════


class LLMClient:
    """
    统一 LLM 客户端。所有 Agent 只通过本类调用模型。

    用法：
        client = LLMClient()
        res = await client.complete(system="...", user="...", images=[img])
        parsed = await client.complete_json(system="...", user="...", schema=LayoutSchema)
    """

    def __init__(self) -> None:
        self._deepseek_text = DeepSeekProvider(
            settings.DEEPSEEK_BASE_URL, settings.DEEPSEEK_MODEL, settings.DEEPSEEK_API_KEY
        )
        self._deepseek_vision = DeepSeekProvider(
            settings.DEEPSEEK_BASE_URL, settings.DEEPSEEK_VISION_MODEL, settings.DEEPSEEK_API_KEY
        )
        self._ollama_text = OllamaProvider(settings.OLLAMA_BASE_URL, settings.OLLAMA_TEXT_MODEL)
        self._ollama_vision = OllamaProvider(settings.OLLAMA_BASE_URL, settings.OLLAMA_VISION_MODEL)

    # ── 降级链构造 ────────────────────────────────────────

    def _chain(
        self, *, vision: bool, allow_degrade: bool = True, force_local: bool = False
    ) -> list[_Provider]:
        """
        按优先级返回可用提供方。未配置 key 的提供方自动跳过。
        V2.0 决策：DeepSeek 为主（本机唯一可用 key），Ollama 为兜底。

        force_local=True  —— 只返回本地提供方，**云端绝不参与**。
        allow_degrade=False —— 只返回主提供方，失败即抛错。

        ⚠️ 为什么需要 force_local 这个开关（这是一个真实踩过的坑）：
        「本地模式」最初只改了提示词与 Schema，却没改提供方，结果 prefer_local=true
        时户型图**照样发到了 api.deepseek.com** —— 隐私承诺被静默违反。
        单元测试全绿，因为 Fake 客户端不认识 provider 概念。
        因此本地模式必须在**提供方选择**这一层强制，而不能只在业务层"表示一下"。
        对应验收项 AC-24（抓包确认图像未离开本机）。

        ⚠️ 为什么需要 allow_degrade=False：
        实测本地 minicpm-v4.6 可以读对房间名，但**无法产出合法的结构化户型 JSON**
        （实测会编造越界 bbox、编造不存在的墙体，且 JSON 语法本身就是坏的）。
        因此对于「要求完整结构」的任务（如 A-01 全量解析），必须禁止自动降级到它——
        否则系统会安静地吐出一坨编造数据，比直接失败危险得多。
        这类任务应当自己捕获失败，然后改走**能力匹配的降级路径**（只问房间名）。
        """
        if force_local:
            return [self._ollama_vision if vision else self._ollama_text]

        chain: list[_Provider] = []
        if settings.deepseek_enabled:
            chain.append(self._deepseek_vision if vision else self._deepseek_text)

        if allow_degrade and (settings.ENABLE_DEGRADATION or not chain):
            chain.append(self._ollama_vision if vision else self._ollama_text)

        return chain

    # ── 主入口 ────────────────────────────────────────────

    async def complete(
        self,
        *,
        user: str,
        system: str | None = None,
        images: Sequence[ImagePart] | None = None,
        vision: bool | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        agent: str = "unknown",
        allow_degrade: bool = True,
        force_local: bool = False,
    ) -> LLMResult:
        """
        完成一次对话。按降级链依次尝试，首个成功即返回。

        vision 参数为 None 时根据是否带图自动推断。
        allow_degrade=False 时只用主模型，失败直接抛错（见 _chain 的说明）。
        force_local=True 时只用本地模型，云端绝不参与（隐私模式，见 _chain 的说明）。
        """
        use_vision = bool(images) if vision is None else vision
        messages = _to_openai_messages(system, [{"role": "user", "content": user}], images)
        chain = self._chain(
            vision=use_vision, allow_degrade=allow_degrade, force_local=force_local
        )

        if not chain:
            raise LLMError("没有可用的 LLM 提供方（未配置 DEEPSEEK_API_KEY 且降级被禁用）")

        errors: list[str] = []
        started = time.perf_counter()

        for idx, provider in enumerate(chain):
            try:
                text, p, c, r, usage = await provider.complete(
                    messages,
                    max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
                    temperature=settings.LLM_TEMPERATURE if temperature is None else temperature,
                    timeout=timeout or settings.LLM_TIMEOUT_SECONDS,
                )
                elapsed = int((time.perf_counter() - started) * 1000)
                degraded = idx > 0
                result = LLMResult(
                    text=text,
                    provider=provider.name,
                    model_used=provider.model,
                    degraded=degraded,
                    degrade_reason="; ".join(errors) if degraded else None,
                    prompt_tokens=p,
                    completion_tokens=c,
                    reasoning_tokens=r,
                    elapsed_ms=elapsed,
                    attempts=idx + 1,
                    raw_usage=usage,
                )
                if degraded:
                    logger.warning(
                        f"[{agent}] 主模型失败，已降级到 {provider.name}/{provider.model}；"
                        f"原因: {errors[-1] if errors else '未知'}"
                    )
                else:
                    logger.debug(
                        f"[{agent}] {provider.name}/{provider.model} "
                        f"tokens={result.total_tokens} (reasoning={r}) {elapsed}ms"
                    )
                return result
            except Exception as e:  # noqa: BLE001 —— 降级链必须吞掉所有异常继续尝试
                msg = f"{provider.name}/{provider.model}: {type(e).__name__}: {e}"
                errors.append(msg)
                logger.warning(f"[{agent}] 提供方 {provider.name} 调用失败: {e}")
                continue

        raise LLMError(f"[{agent}] 所有提供方均失败:\n  " + "\n  ".join(errors))

    # ── 结构化输出 ────────────────────────────────────────

    async def complete_json(
        self,
        schema: type[BaseModel],
        *,
        user: str,
        system: str | None = None,
        images: Sequence[ImagePart] | None = None,
        max_tokens: int | None = None,
        agent: str = "unknown",
        allow_degrade: bool = True,
        force_local: bool = False,
    ) -> tuple[BaseModel, LLMResult]:
        """
        完成一次调用并解析为 Pydantic 模型。

        采用「schema 注入 + 宽松抽取 + 校验重试」策略（见模块头「决策 1」）。
        校验失败时把错误回灌给模型重试，最多 settings.LLM_MAX_RETRIES 次。
        """
        schema_hint = build_schema_instruction(schema)
        full_system = f"{system}\n\n{schema_hint}" if system else schema_hint
        prompt = user
        last_error: Exception | None = None
        last_result: LLMResult | None = None

        for attempt in range(settings.LLM_MAX_RETRIES + 1):
            result = await self.complete(
                system=full_system,
                user=prompt,
                images=images,
                max_tokens=max_tokens,
                agent=agent,
                allow_degrade=allow_degrade,
                force_local=force_local,
            )
            last_result = result
            try:
                raw = extract_json(result.text)
                return schema.model_validate(raw), result
            except (ValueError, ValidationError) as e:
                last_error = e
                logger.warning(
                    f"[{agent}] 第 {attempt + 1} 次结构化解析失败: {str(e)[:200]}"
                )
                if attempt >= settings.LLM_MAX_RETRIES:
                    break
                # 把错误回灌，要求模型修正
                prompt = (
                    f"{user}\n\n"
                    f"---\n上一次输出无法通过校验，请修正后重新输出完整 JSON。\n"
                    f"错误信息：{str(e)[:500]}\n"
                )

        raise LLMError(
            f"[{agent}] 结构化输出在 {settings.LLM_MAX_RETRIES + 1} 次尝试后仍失败: {last_error}\n"
            f"最后一次原文前 500 字: {(last_result.text[:500] if last_result else '')}"
        )

    # ── Embedding ─────────────────────────────────────────

    async def embed(self, texts: Sequence[str], *, timeout: float = 60.0) -> list[list[float]]:
        """
        文本向量化。走本地 Ollama 的 bge-large-zh-v1.5（1024 维，已实测可用）。
        """
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=timeout) as client:
            for text in texts:
                resp = await client.post(
                    f"{settings.OLLAMA_BASE_URL}/api/embed",
                    json={"model": settings.OLLAMA_EMBEDDING_MODEL, "input": text},
                )
                resp.raise_for_status()
                data = resp.json()
                embs = data.get("embeddings") or []
                if not embs:
                    raise LLMError(f"Ollama embedding 返回空: {str(data)[:200]}")
                out.append(embs[0])
        return out


# ── 进程级单例 ────────────────────────────────────────────
_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


__all__ = [
    "LLMClient", "LLMResult", "LLMError", "LLMTruncatedError",
    "ImagePart", "extract_json", "build_schema_instruction", "get_llm_client",
]
