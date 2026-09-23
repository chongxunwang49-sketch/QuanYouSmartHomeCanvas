#!/usr/bin/env python
"""
UI 素材采集 —— 为前端准备真实图片资源。

═══════════════════════════════════════════════════════════════════
为什么要有脚本，而不是手工存图
═══════════════════════════════════════════════════════════════════
1. **可复现**：素材是前端的地基。散装下载的图过两个月就说不清哪来的，
   而 MANIFEST.json 里连来源页、原始 URL、sha1 都在，跑一遍就能重建。
2. **可审计**：每张图记了许可状态。官网素材是**全友版权**，unDraw 是
   MIT 类，Iconify 各集合许可不同 —— 用之前得知道自己在用什么。
3. **有礼貌**：对官网 1 req/s 限速、单栏目封顶，不把人家站点当下行链路。

═══════════════════════════════════════════════════════════════════
五个来源
═══════════════════════════════════════════════════════════════════
    官网实拍   quanyou.com.cn      真实案例/产品图，**最高优先**
    插画       undraw.co           MIT 类许可的扁平插画
    图标       iconify.design      多集合聚合
    照片       pexels.com          免费商用实拍
    占位图     picsum.photos       开发期占位

⚠️ 三个外部约束（都是实测出来的，不是猜的）
  · undraw.co 的 `/api/illustrations` 已随改版失效，现走 Next.js 的
    `/_next/data/{buildId}/illustrations/{page}.json`。buildId 会随站点
    重新构建而变，所以脚本每次**先抓页面取 buildId**，不硬编码。
  · Pexels 的 www 全站被 Cloudflare 挡死（403，区域子域与 Googlebot UA
    同样被挡），但 images.pexels.com **直连可用**。所以本脚本不接受
    关键词，只按 ID 列表下载 —— 发现环节缺一个免费 API key，见 README。
  · **本机 Windows 系统代理是坏的，而 requests 默认会去吃它**
    （`trust_env=True`）。症状极隐蔽：一部分请求静默失败、另一部分正常，
    同一时刻 curl 访问同一个 URL 却是 200 —— 第一轮采集据此误判成
    "对端在封我"，实际是本地代理。已用 `_SESSION.trust_env = False` 关掉。

用法：
    python scripts/collect_ui_assets.py                      # 全部
    python scripts/collect_ui_assets.py --only quanyou undraw
    python scripts/collect_ui_assets.py --dry-run            # 只报数不下载
    python scripts/collect_ui_assets.py --proxy http://127.0.0.1:7897
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests

# Windows 控制台默认 GBK，输出中文会炸成乱码或直接 UnicodeEncodeError。
# 这个脚本的输出是给人看的进度报告，乱码了就白写了。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# ── 路径 ──────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
ASSET_ROOT = REPO_ROOT / "ui参考"
MANIFEST_PATH = ASSET_ROOT / "MANIFEST.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: 各来源的请求间隔（秒）。官网最慢 —— 那是真实企业的生产站点。
#: iconify 的公开 API 限流很紧（实测 0.2s 间隔会成片 429），给足余量。
DELAY = {"quanyou": 1.0, "undraw": 0.3, "iconify": 0.6, "pexels": 0.5, "picsum": 0.3}

#: 429/5xx 的重试次数。退避是 2^n 秒 —— iconify 的限流窗口是滑动式的，
#: 固定间隔重试只会继续撞墙。
RETRIES = 4

#: 内容图小于此字节数的一律丢弃 —— 那多半是间隔用的 1px 占位或装饰斜纹，
#: 混进素材库会让人以为"图挂了"。
MIN_CONTENT_BYTES = 15 * 1024

#: 单张上限。超过的通常是未压缩原图，采集来是负担不是资产。
MAX_BYTES = 20 * 1024 * 1024


# ══════════════════════════════════════════════════════════════════════
# 基础设施
# ══════════════════════════════════════════════════════════════════════

_MANIFEST: dict[str, Any] = {"generated_at": "", "assets": [], "skipped": []}
_SEEN_SHA1: set[str] = set()
#: 已下过的源 URL。**续跑靠它省流量** —— 只按内容 sha1 去重的话，
#: 重跑一次要把 200 多张图全下完才发现「都有了」，官网那 1s 限速下
#: 白等好几分钟，对人家服务器也是白挨一遍请求。
_SEEN_URLS: set[str] = set()
_DRY_RUN = False
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})

# ⚠️ **必须关掉 trust_env。**
#
# requests 默认会去读 Windows 的系统代理设置（WinINET/WinHTTP），而本机那个
# 代理已经不通了。后果不是"报个代理错误"这么直白 —— 是**一部分请求静默失败**：
# 第一轮采集里 20 张官网图 + 整个 undraw 全军覆没，而 curl 同一时刻访问
# 同一个 URL 是 200。两边行为不一致，很容易误判成"对端在封我"。
#
# 关掉之后走的才是真实网络路径。需要代理时用 `--proxy` 显式指定，
# 不依赖环境里那份看不见的配置。
_SESSION.trust_env = False


def _slug(text: str, fallback: str = "img") -> str:
    """把任意字符串收拾成安全的文件名片段。"""
    text = re.sub(r"\.(jpg|jpeg|png|webp|svg|gif)$", "", text, flags=re.I)
    text = re.sub(r"[^\w一-鿿-]+", "-", text)
    return (text.strip("-") or fallback)[:60]


def _ext_from(url: str, content_type: str = "") -> str:
    """
    定扩展名。**优先看 Content-Type** —— 官网有 `.jpg` 实为 png 的图，
    按 URL 后缀存会让浏览器按 jpg 解一张 png，部分环境直接不显示。
    """
    ct = (content_type or "").lower()
    for key, ext in (
        ("image/jpeg", ".jpg"), ("image/png", ".png"), ("image/webp", ".webp"),
        ("image/svg", ".svg"), ("image/gif", ".gif"),
    ):
        if key in ct:
            return ext
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".svg", ".gif"} else ".jpg"


def _fetch(url: str, *, source: str, timeout: int = 30) -> requests.Response | None:
    """
    取一个 URL，失败返回 None 而不是抛 —— 单个 404 不该终止整轮采集。

    429 与 5xx 会退避重试。**429 必须重试**：iconify 的公开 API 限流很紧，
    第一版没做退避，一轮下来 23 张图全是 429 被静默跳过 ——
    素材库少了三分之一而控制台只显示"跳过 23 个"，很容易被当成正常损耗。
    """
    if _DRY_RUN:
        return None

    for attempt in range(RETRIES):
        try:
            r = _SESSION.get(url, timeout=timeout)
        except requests.RequestException as e:
            if attempt == RETRIES - 1:
                _MANIFEST["skipped"].append({"url": url, "why": f"{type(e).__name__}"})
                return None
            time.sleep(2 ** attempt)
            continue

        if r.status_code == 200:
            return r
        if r.status_code == 429 or r.status_code >= 500:
            if attempt < RETRIES - 1:
                time.sleep(2 ** (attempt + 1))   # 2s → 4s → 8s
                continue
        _MANIFEST["skipped"].append({"url": url, "why": f"HTTP {r.status_code}"})
        return None
    return None


def _load_existing() -> int:
    """
    载入上一轮的清单，把**仍在磁盘上**的素材当已下载。

    做这个是为了续跑：iconify 被限流那轮之后重跑，不该把已下好的 71 个图标
    再拉一遍。但只认文件还在的条目 —— 手工删过图之后重跑要能补回来，
    否则清单会说"有"而磁盘上没有，前端引用时才发现图片裂了。
    """
    if not MANIFEST_PATH.exists():
        return 0
    try:
        old = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return 0

    kept = 0
    for a in old.get("assets", []):
        if (REPO_ROOT / a["path"]).exists():
            _MANIFEST["assets"].append(a)
            if a.get("sha1"):
                _SEEN_SHA1.add(a["sha1"])
            if a.get("source_url"):
                _SEEN_URLS.add(a["source_url"])
            kept += 1
    return kept


def _have(url: str) -> bool:
    """这个源 URL 已经采过了吗。是的话连请求都不用发。"""
    return url in _SEEN_URLS


def _save(
    rel_dir: str,
    filename: str,
    content: bytes,
    *,
    source: str,
    source_url: str,
    source_page: str = "",
    title: str = "",
    license_note: str = "",
) -> bool:
    """
    落盘 + 记账。返回是否真的写了一张新图。

    **按内容 sha1 去重**：官网同一张图常出现在多个页面（首页 banner 和
    案例页可能共用），按 URL 去重会重复存好几份，按内容去重才是真的去重。
    """
    digest = hashlib.sha1(content).hexdigest()
    if digest in _SEEN_SHA1:
        return False
    _SEEN_SHA1.add(digest)

    target_dir = ASSET_ROOT / rel_dir
    target = target_dir / filename

    _MANIFEST["assets"].append({
        "path": str(target.relative_to(REPO_ROOT)).replace("\\", "/"),
        "source": source,
        "source_url": source_url,
        "source_page": source_page,
        "title": title,
        "bytes": len(content),
        "sha1": digest,
        "license": license_note,
    })

    if _DRY_RUN:
        return True
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    # **每存一张就落一次清单。** 采集要跑十几分钟，中途被打断是常事
    # （实测：Ctrl-C 一次，磁盘上多了 79 张图，清单里一张都没有 ——
    # 下次续跑认不出这些文件，会重新下一遍，而清单也不再可信）。
    _write_manifest()
    return True


# ══════════════════════════════════════════════════════════════════════
# 1. 官网实拍（最高优先）
# ══════════════════════════════════════════════════════════════════════

QUANYOU_BASE = "https://www.quanyou.com.cn/"

#: 栏目 → 页面。挑的是有实拍内容的，`brand.html` 那类宣传页图少且重复。
QUANYOU_SECTIONS: dict[str, list[str]] = {
    "装修案例": ["cases.html", "appreciation.html", "idea2024.html"],
    "产品-热销": ["hot.html"],
    "产品-定制橱柜": ["cupboard.html"],
    "产品-卫浴": ["bathroom.html"],
    "产品-窗帘软装": ["curtain.html"],
    "品牌视觉": ["brand.html", "about2024.html", "culture2024.html", "chain2024.html"],
}

QUANYOU_LICENSE = "全友家居官网素材，版权归全友家居所有；仅作本项目演示与设计参考，不得商用"

#: 命中即丢的路径片段 —— 二维码、遮罩、返回按钮之类不是设计素材。
_JUNK = ("wechat", "qrcode", "erweima", "mask", "back.png", "close", "loading", "placeholder")

_IMG_ATTR = re.compile(
    r'(?:data-original|data-src|data-lazy|data-url|src)\s*=\s*["\']([^"\']+\.(?:jpg|jpeg|png|webp))',
    re.I,
)


def _quanyou_images(html: str, page_url: str) -> list[str]:
    """从一页 HTML 里抠出绝对图片地址，按页面顺序去重。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _IMG_ATTR.findall(html):
        if raw.startswith("data:") or any(j in raw.lower() for j in _JUNK):
            continue
        url = urljoin(page_url, raw.split("?")[0])
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def collect_quanyou(*, max_per_section: int = 18) -> None:
    """采官网实拍。每个栏目封顶，避免把整站镜像下来。"""
    print("\n═══ 1/5  官网实拍  quanyou.com.cn ═══")
    for section, pages in QUANYOU_SECTIONS.items():
        got = 0
        for page in pages:
            if got >= max_per_section:
                break
            url = urljoin(QUANYOU_BASE, page)
            r = _fetch(url, source="quanyou")
            time.sleep(DELAY["quanyou"])
            if r is None:
                continue

            r.encoding = r.apparent_encoding or "utf-8"
            for img_url in _quanyou_images(r.text, url):
                if got >= max_per_section:
                    break
                # 站点图标/Logo 单独归置：它们体积小，会被 MIN_CONTENT_BYTES 误杀
                is_chrome = "/static/" in img_url or "/resources/mobile/assets" in img_url
                if is_chrome or _have(img_url):
                    continue

                im = _fetch(img_url, source="quanyou")
                time.sleep(DELAY["quanyou"])
                if im is None:
                    continue
                if len(im.content) < MIN_CONTENT_BYTES:
                    _MANIFEST["skipped"].append(
                        {"url": img_url, "why": f"too small ({len(im.content)}B)"})
                    continue
                if len(im.content) > MAX_BYTES:
                    _MANIFEST["skipped"].append({"url": img_url, "why": "too large"})
                    continue

                name = f"{_slug(Path(urlparse(img_url).path).stem)}{_ext_from(img_url, im.headers.get('Content-Type',''))}"
                if _save(f"01-官网实拍/{section}", name, im.content,
                         source="quanyou", source_url=img_url, source_page=url,
                         title=f"{section} · {Path(urlparse(img_url).path).stem}",
                         license_note=QUANYOU_LICENSE):
                    got += 1
        print(f"  {section:<14} {got:>3} 张")


def collect_quanyou_chrome() -> None:
    """站点 Logo 与图标。单独一轮 —— 体积小，不适用内容图的下限。"""
    print("\n═══ 官网站内 Logo / 图标 ═══")
    r = _fetch(QUANYOU_BASE, source="quanyou")
    if r is None:
        print("  首页取不到，跳过")
        return
    r.encoding = r.apparent_encoding or "utf-8"
    got = 0
    for raw in _IMG_ATTR.findall(r.text):
        if "/static/" not in raw and "/resources/mobile/assets" not in raw:
            continue
        if any(j in raw.lower() for j in _JUNK):
            continue
        url = urljoin(QUANYOU_BASE, raw.split("?")[0])
        if _have(url):
            continue
        im = _fetch(url, source="quanyou")
        time.sleep(DELAY["quanyou"])
        if im is None:
            continue
        name = f"{_slug(Path(urlparse(url).path).stem)}{_ext_from(url, im.headers.get('Content-Type',''))}"
        if _save("01-官网实拍/站内Logo与图标", name, im.content,
                 source="quanyou", source_url=url, source_page=QUANYOU_BASE,
                 title=f"站内素材 · {Path(urlparse(url).path).stem}",
                 license_note=QUANYOU_LICENSE):
            got += 1
    print(f"  站内Logo与图标    {got:>3} 张")


# ══════════════════════════════════════════════════════════════════════
# 2. unDraw 插画
# ══════════════════════════════════════════════════════════════════════

UNDRAW_LICENSE = "unDraw 开放许可（可商用、可修改、无需署名）"

#: 标题关键词 → 落到哪一类。标题是英文的，匹配用小写子串。
UNDRAW_TOPICS: dict[str, tuple[str, ...]] = {
    "家居空间": ("home", "house", "room", "interior", "furniture", "sofa", "bed",
                 "kitchen", "bathroom", "living", "apartment", "building", "door", "window"),
    "设计与户型": ("design", "blueprint", "architect", "plan", "layout", "sketch",
                   "drawing", "palette", "ruler", "measure", "3d", "model"),
    "预算与报价": ("money", "budget", "price", "invoice", "receipt", "payment",
                   "cost", "finance", "wallet", "coin", "bank", "calculate",
                   "tax", "savings", "transfer"),
    "审查与风控": ("check", "audit", "inspect", "review", "search", "document",
                   "contract", "security", "shield", "warning", "alert", "error",
                   "blocked", "verify", "clipboard", "list"),
    "AI与数据": ("ai", "artificial", "robot", "machine", "data", "analytic",
                 "chart", "graph", "statistic", "dashboard", "algorithm",
                 "neural", "generating", "assistant", "chat"),
    "拍摄与上传": ("upload", "download", "image", "photo", "camera", "scan",
                   "file", "cloud", "sync", "transfer"),
    "人与沟通": ("people", "team", "user", "customer", "support", "meeting",
                 "contact", "call", "message", "profile", "collaborat"),
}


def _undraw_index() -> list[dict[str, str]]:
    """
    拉全量插画目录。

    buildId 每次现抓 —— 它是 Next.js 的构建产物哈希，站点一重新部署就变，
    硬编码会在下一次部署当天失效，且报错形式是 404 而不是"buildId 过期"，
    排查起来很绕。

    复用 `_SESSION`（`trust_env=False` 已在模块级设好）——
    另起一个 Session 会重新吃进系统代理，等于把这个坑再踩一遍。
    """
    # 页面本身要重试：undraw 对本机限速，实测出现过 Read timed out。
    # 这一步是整源的单点 —— 它失败则一张插画都拿不到，不能只试一次。
    r = None
    for attempt in range(4):
        r = _fetch("https://undraw.co/illustrations", source="undraw")
        if r is not None:
            break
        time.sleep(2 ** attempt)
    if r is None:
        raise RuntimeError("undraw 页面连不上（已重试 4 次）")

    m = re.search(r'"buildId":"([^"]+)"', r.text)
    if not m:
        raise RuntimeError("undraw 页面里找不到 buildId —— 站点结构可能又改了")
    build_id = m.group(1)

    def page_url(page: int | None) -> str:
        tail = f"/{page}" if page else ""
        return f"https://undraw.co/_next/data/{build_id}/illustrations{tail}.json"

    def get_json(url: str) -> dict | None:
        for attempt in range(4):
            resp = _fetch(url, source="undraw")
            if resp is not None:
                try:
                    return resp.json()
                except ValueError:
                    pass
            time.sleep(DELAY["undraw"] * (2 ** attempt))
        return None

    first = get_json(page_url(None))
    if first is None:
        raise RuntimeError("undraw 目录首页取不到")

    total_pages = first["pageProps"]["totalPages"]
    items = list(first["pageProps"]["illustrations"])

    missed = 0
    for page in range(2, total_pages + 1):
        d = get_json(page_url(page))
        time.sleep(DELAY["undraw"])
        if d is None:
            missed += 1
            continue          # 单页失败不该毁掉整轮 —— 少 40 张，不是少 1800 张
        items.extend(d["pageProps"]["illustrations"])

    print(f"  目录：{len(items)} 张插画（{total_pages} 页"
          + (f"，{missed} 页取失败）" if missed else "）"))
    return items


def collect_undraw(*, max_per_topic: int = 8) -> None:
    print("\n═══ 2/5  插画  undraw.co ═══")
    try:
        items = _undraw_index()
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 目录拉取失败：{type(e).__name__}: {e}")
        return

    # 目录本身也是一份资产：前端后续想找"某个语义的插画"时可以直接检索，
    # 不必再去爬一次。它跟已下载的图是两回事，所以单独存。
    if not _DRY_RUN:
        out = ASSET_ROOT / "02-插画-unDraw" / "_目录索引.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                [{"title": i["title"], "slug": i["newSlug"], "url": i["media"]} for i in items],
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    counts: dict[str, int] = {}
    for topic, keys in UNDRAW_TOPICS.items():
        n = 0
        for it in items:
            if n >= max_per_topic:
                break
            if not any(k in it["title"].lower() for k in keys):
                continue
            if _have(it["media"]):
                continue
            im = _fetch(it["media"], source="undraw")
            time.sleep(DELAY["undraw"])
            if im is None:
                continue
            name = f"{_slug(it['newSlug'] or it['title'])}.svg"
            if _save(f"02-插画-unDraw/{topic}", name, im.content,
                     source="undraw", source_url=it["media"],
                     source_page="https://undraw.co/illustrations",
                     title=it["title"], license_note=UNDRAW_LICENSE):
                n += 1
        counts[topic] = n
    for topic, n in counts.items():
        print(f"  {topic:<14} {n:>3} 张")


# ══════════════════════════════════════════════════════════════════════
# 3. Iconify 图标
# ══════════════════════════════════════════════════════════════════════

#: 首选 Phosphor —— 描边风格统一、网格一致，放进企业后台不违和。
#: 备选 Tabler。两套都下，前端按需挑，别在写页面时才纠结图标风格。
ICON_PREFIX = "ph"
ICON_PREFIX_ALT = "tabler"
ICON_API = "https://api.iconify.design"

ICON_LICENSE = "各图标集合许可不同（Phosphor: MIT / Tabler: MIT）；使用前见 iconify.design 对应集合"

#: 语义分组 → 检索关键词。用搜索接口而不是手写图标名 ——
#: 手写的名字有一半会 404，而且撞不到最贴切的那个。
ICON_TOPICS: dict[str, tuple[str, ...]] = {
    "户型与空间": ("house", "floor-plan", "door", "window", "ruler", "compass",
                   "blueprint", "layout", "cube"),
    "家具与品类": ("sofa", "bed", "bathtub", "toilet", "cabinet", "lamp",
                   "curtain", "plant", "armchair"),
    "预算与报价": ("currency-cny", "coins", "receipt", "calculator", "chart-line",
                   "wallet", "invoice", "percent"),
    "审查与风控": ("shield-check", "warning", "magnifying-glass", "clipboard-check",
                   "file-text", "scales", "bug", "first-aid"),
    "AI与智能": ("robot", "sparkle", "cpu", "brain", "magic-wand", "lightning"),
    "操作与状态": ("upload-simple", "download-simple", "arrow-right", "check-circle",
                   "x-circle", "plus", "trash", "share-network", "eye", "spinner"),
}


#: 批量取图标的单次上限。URL 里塞太多名字会顶到长度限制，32 是安全值。
ICON_BATCH = 32


def _icon_search(kw: str, prefix: str, limit: int) -> list[str]:
    """按关键词搜图标名。搜索接口没有批量形式，只能一个一个来。"""
    try:
        r = _SESSION.get(f"{ICON_API}/search",
                         params={"query": kw, "prefix": prefix, "limit": limit},
                         timeout=20)
    except requests.RequestException:
        return []
    finally:
        time.sleep(DELAY["iconify"])
    if r.status_code != 200:
        return []
    try:
        return r.json().get("icons", [])[:limit]
    except ValueError:
        return []


def _icon_batch(prefix: str, names: list[str]) -> dict[str, str]:
    """
    一次取一批图标的 SVG。

    **这是被限流逼出来的改法。** 第一版一个个取（114 次请求），撞 iconify
    的限流成片 429；加了退避之后每张最多睡 14 秒，整轮要跑近半小时。
    批量接口把 114 次压到 6 次，问题从根上没了。
    返回 {完整图标名: SVG 文本}。
    """
    out: dict[str, str] = {}
    for i in range(0, len(names), ICON_BATCH):
        chunk = names[i : i + ICON_BATCH]
        url = f"{ICON_API}/{prefix}.json"
        try:
            r = _SESSION.get(url, params={"icons": ",".join(chunk)}, timeout=30)
            time.sleep(DELAY["iconify"])
        except requests.RequestException:
            continue
        if r.status_code != 200:
            _MANIFEST["skipped"].append(
                {"url": f"{url}?icons={len(chunk)}个", "why": f"HTTP {r.status_code}"})
            continue
        try:
            d = r.json()
        except ValueError:
            continue

        w, h = d.get("width", 24), d.get("height", 24)
        for name, body in (d.get("icons") or {}).items():
            inner = body.get("body", "")
            if not inner:
                continue
            # 套回完整 SVG。不额外加 fill —— body 自带 currentColor，
            # 前端继承文字色即可，写死颜色到后面改主题时是灾难。
            out[f"{prefix}:{name}"] = (
                f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'viewBox="0 0 {body.get("width", w)} {body.get("height", h)}" '
                f'width="1em" height="1em">{inner}</svg>'
            )
        for miss in d.get("not_found") or []:
            _MANIFEST["skipped"].append(
                {"url": f"{ICON_API}/{prefix}/{miss}.svg", "why": "not_found"})
    return out


def collect_iconify(*, per_keyword: int = 2) -> None:
    print("\n═══ 3/5  图标  iconify.design ═══")
    index: dict[str, dict[str, list[str]]] = {}
    wanted: set[str] = set()

    # ── 阶段一：搜索，只收集名字 ──
    for topic, keywords in ICON_TOPICS.items():
        index[topic] = {}
        for kw in keywords:
            found: list[str] = []
            for prefix in (ICON_PREFIX, ICON_PREFIX_ALT):
                found = _icon_search(kw, prefix, per_keyword)
                if found:
                    break
            index[topic][kw] = found
            wanted.update(found)

    # ── 阶段二：按图标集分组，批量取 ──
    by_prefix: dict[str, list[str]] = {}
    for full in sorted(wanted):
        prefix, _, name = full.partition(":")
        by_prefix.setdefault(prefix, []).append(name)

    svgs: dict[str, str] = {}
    for prefix, names in by_prefix.items():
        svgs.update(_icon_batch(prefix, names))
        print(f"  {prefix:<10} 取回 {len(names)} 个")

    # ── 阶段三：按主题归位落盘 ──
    for topic, kws in index.items():
        n = 0
        for kw, found in kws.items():
            for full in found:
                svg = svgs.get(full)
                if not svg:
                    continue
                set_name, _, icon_name = full.partition(":")
                if _save(f"03-图标-Iconify/{topic}", f"{set_name}-{icon_name}.svg",
                         svg.encode("utf-8"), source="iconify",
                         source_url=f"{ICON_API}/{set_name}/{icon_name}.svg",
                         source_page="https://iconify.design/",
                         title=f"{topic} · {full}", license_note=ICON_LICENSE):
                    n += 1
        print(f"  {topic:<14} {n:>3} 个")

    if not _DRY_RUN:
        out = ASSET_ROOT / "03-图标-Iconify" / "_检索索引.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        # 这份索引的用处：前端要"找个户型的图标"时先查它，命中就直接引用，
        # 没命中再跑一次本脚本 —— 比在 20 万图标里翻快得多。
        out.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════
# 4. Pexels 实拍
# ══════════════════════════════════════════════════════════════════════

PEXELS_LICENSE = "Pexels 许可（免费商用、无需署名）；署名作者信息见 MANIFEST 的 title"

#: 房间分类 → {photo_id: 描述}。ID 全部**逐个验活过**（HTTP 200），
#: 失效的已剔除 —— 留着会在每轮采集里安静地失败一次，白等 4 次退避。
#: 发现渠道见模块说明：ID 靠 WebSearch 挖，不是搜索接口来的。
PEXELS_IDS: dict[str, dict[str, str]] = {
    "客厅起居": {
        "1571460": "客厅 · 木地板沙发区",
        "1643383": "客厅 · 现代简约",
        "2724749": "客厅 · 北欧风",
        "1457842": "客厅 · 灰调布艺沙发",
        "1918291": "客厅 · 采光通透",
        "37676360": "客厅 · 空间全景",
        "34377944": "客厅 · 灯光氛围",
        "20547058": "客厅 · 极简起居",
    },
    "卧室": {
        "1454806": "卧室 · 原木床架",
        "164595": "卧室 · 简约白",
        "35527007": "卧室 · 木质家具",
        "36816994": "卧室 · 自然采光",
    },
    "厨房餐厅": {
        "2724748": "厨房 · 白色橱柜",
        "1080721": "厨房 · 木色台面",
        "36511371": "厨房 · 白柜木地板",
        "36906951": "厨房 · 中岛与吊灯",
        "36777864": "厨房 · 灯光设计",
        "36777500": "厨房 · 不锈钢电器",
        "34794673": "厨房 · 大理石台面",
    },
    "书房工作区": {
        "36098979": "书房 · 阳光办公区",
        "28461041": "书房 · 中性色调",
        "30539351": "书房 · 笔记本与咖啡",
        "36123568": "书房 · 极简桌面",
        "30196536": "书房 · 深色墙面",
        "31938716": "书房 · 书籍与收纳",
        "32048649": "书房 · 创意桌面",
        "32553379": "书房 · 木质书桌",
        "34688835": "书房 · 家居工作位",
    },
    "卫浴": {
        "1454804": "卫浴 · 干湿分离",
        "1909796": "卫浴 · 台盆区",
    },
    "空间与细节": {
        "1571459": "空间 · 阳台与采光",
        "2062426": "细节 · 软装陈设",
        "276583": "细节 · 墙面与挂画",
        "1866149": "细节 · 户型过道",
        "3935350": "细节 · 收纳柜体",
    },
}


def collect_pexels(
    ids: dict[str, dict[str, str]] | None = None, *, width: int = 1600
) -> None:
    print("\n═══ 4/5  照片  pexels.com ═══")
    ids = ids or PEXELS_IDS
    for room, group in ids.items():
        got = 0
        for pid, desc in group.items():
            # jpeg / png 两种命名都试 —— Pexels 早期是 png，现在是 jpeg，
            # 老 ID 偶尔两种都有。命中一个就走。
            variants = [
                (f"https://images.pexels.com/photos/{pid}/pexels-photo-{pid}.{ext}"
                 f"?auto=compress&cs=tinysrgb&w={width}")
                for ext in ("jpeg", "png")
            ]
            if any(_have(u) for u in variants):
                continue

            hit = False
            for url in variants:
                r = _fetch(url, source="pexels")
                time.sleep(DELAY["pexels"])
                if r is None or len(r.content) < MIN_CONTENT_BYTES:
                    continue
                hit = True
                if _save(f"04-照片-Pexels/{room}", f"{pid}-{_slug(desc)}.jpg",
                         r.content, source="pexels", source_url=url,
                         source_page=f"https://www.pexels.com/photo/{pid}/",
                         title=f"{room} · {desc}", license_note=PEXELS_LICENSE):
                    got += 1
                break
            if not hit:
                _MANIFEST["skipped"].append(
                    {"url": f"https://www.pexels.com/photo/{pid}/", "why": "两种格式均不可取"})
        print(f"  {room:<14} {got:>3} 张")


# ══════════════════════════════════════════════════════════════════════
# 5. Picsum 占位图
# ══════════════════════════════════════════════════════════════════════

PICSUM_LICENSE = "Picsum Photos（取自 Unsplash，占位用；**上线前必须替换为真实素材**）"

#: 尺寸 → 用途。占位图是按位置准备的，不是随便下几张正方形。
PICSUM_SIZES: dict[str, tuple[int, int, str]] = {
    "卡片缩略图": (640, 480, "card"),
    "宽幅Banner": (1920, 800, "banner"),
    "头像": (128, 128, "avatar"),
    "方案对比图": (960, 720, "compare"),
    "户型图占位": (1200, 900, "floorplan"),
}

PICSUM_SEEDS = (
    "quanyou-living", "quanyou-bedroom", "quanyou-kitchen", "quanyou-bath",
    "quanyou-balcony", "quanyou-study", "quanyou-dining", "quanyou-kids",
)


def collect_picsum() -> None:
    print("\n═══ 5/5  占位图  picsum.photos ═══")
    for label, (w, h, slug) in PICSUM_SIZES.items():
        n = 0
        for seed in PICSUM_SEEDS:
            url = f"https://picsum.photos/seed/{seed}-{slug}/{w}/{h}"
            if _have(url):
                continue
            r = _fetch(url, source="picsum")
            time.sleep(DELAY["picsum"])
            if r is None:
                continue
            if _save(f"05-占位图-Picsum/{label}", f"{seed}-{slug}-{w}x{h}.jpg",
                     r.content, source="picsum", source_url=url,
                     source_page="https://picsum.photos/",
                     title=f"{label}占位 · {seed}",
                     license_note=PICSUM_LICENSE):
                n += 1
        print(f"  {label:<14} {n:>3} 张")


# ══════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════


def _write_manifest() -> None:
    if _DRY_RUN:
        return
    _MANIFEST["generated_at"] = datetime.now().isoformat(timespec="seconds")
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)

    by_source: dict[str, int] = {}
    total_bytes = 0
    for a in _MANIFEST["assets"]:
        by_source[a["source"]] = by_source.get(a["source"], 0) + 1
        total_bytes += a["bytes"]

    _MANIFEST["summary"] = {
        "total": len(_MANIFEST["assets"]),
        "by_source": by_source,
        "total_mb": round(total_bytes / 1024 / 1024, 1),
        "skipped": len(_MANIFEST["skipped"]),
    }
    MANIFEST_PATH.write_text(
        json.dumps(_MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    global _DRY_RUN
    ap = argparse.ArgumentParser(description="采集前端 UI 素材")
    ap.add_argument("--only", nargs="*", default=None,
                    choices=["quanyou", "undraw", "iconify", "pexels", "picsum"],
                    help="只跑指定来源")
    ap.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    ap.add_argument("--proxy", default="", help="如 http://127.0.0.1:7897")
    ap.add_argument("--max-per-section", type=int, default=18, help="官网单栏目上限")
    args = ap.parse_args()

    _DRY_RUN = args.dry_run
    if not _DRY_RUN:
        kept = _load_existing()
        if kept:
            print(f"（续跑：沿用上一轮已在磁盘上的 {kept} 个素材）")

    proxy = {"http": args.proxy, "https": args.proxy} if args.proxy else None
    if proxy:
        _SESSION.proxies.update(proxy)
        print(f"（走代理 {args.proxy}）")

    only = set(args.only) if args.only else {"quanyou", "undraw", "iconify", "pexels", "picsum"}

    print(f"素材根目录：{ASSET_ROOT}")
    if _DRY_RUN:
        print("⚠ DRY-RUN：不会写任何文件")

    if "quanyou" in only:
        collect_quanyou(max_per_section=args.max_per_section)
        collect_quanyou_chrome()
    if "undraw" in only:
        collect_undraw()
    if "iconify" in only:
        collect_iconify()
    if "pexels" in only:
        collect_pexels()
    if "picsum" in only:
        collect_picsum()

    _write_manifest()

    s = _MANIFEST.get("summary", {})
    print("\n" + "═" * 52)
    print(f"合计 {s.get('total', 0)} 个素材，{s.get('total_mb', 0)} MB")
    for k, v in (s.get("by_source") or {}).items():
        print(f"  {k:<10} {v:>4}")
    if s.get("skipped"):
        print(f"  跳过 {s['skipped']} 个（体积不符 / HTTP 错误，明细见 MANIFEST）")
    if not _DRY_RUN:
        print(f"清单：{MANIFEST_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
