"""
Markdown → PDF（走浏览器打印，**mermaid 图会真的渲染出来**）。

    python scripts/md_to_pdf.py docs/需求/需求文档.md
    python scripts/md_to_pdf.py <in.md> -o <out.pdf>

═══════════════════════════════════════════════════════════════════
为什么不用 pandoc
═══════════════════════════════════════════════════════════════════
本机 **没有任何 LaTeX 引擎**（xelatex / pdflatex / lualatex / weasyprint 全都没有），
而 pandoc 出 PDF 就是靠它们。更关键的是：这份文档里有 **16 张 mermaid 图**
（架构图、时序图、状态机…），LaTeX 那套链子渲染不了它们，会退化成代码块。

所以走**浏览器打印**：Python 把 md 转成自包含的 HTML（mermaid.js 内联进去），
再用 Edge 的 `Page.printToPDF` 出 PDF。好处是：

  · mermaid 真渲染（本机 Edge + 内联 mermaid.js，不依赖渲染时的网络）；
  · 中文字体不会有坑（LaTeX 那条路最容易在这里翻车，要配 xeCJK）；
  · 表格、代码块、emoji 都能按 CSS 控制分页。

═══════════════════════════════════════════════════════════════════
几个刻意为之的细节
═══════════════════════════════════════════════════════════════════
  · **`h1` 分页**：这份文档的 h1 是「标题 / 卷一 / 卷二 / 卷三」，让每卷新起一页；
  · **表格行不跨页**（`tr { page-break-inside: avoid }`）—— 否则一行被劈成两半，
    读起来像数据错位；
  · **每张表都单独一行标题**：文档里表头不带编号，所以打印时靠 `h3/h4` 的
    `page-break-after: avoid` 保证"标题不孤零零留在页脚"；
  · **页脚带页码**（`displayHeaderFooter`）—— 100 多页的东西没有页码没法引用。

⚠️ 依赖：本机 Edge 开着 `--remote-debugging-port=9222`（与其它探针同一套）。
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import re
import sys
import time
import urllib.request

MERMAID_CDN = ("https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js",
               "https://unpkg.com/mermaid@10.9.1/dist/mermaid.min.js")


def _project_root() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "backend" / "app").is_dir():
            return parent
    return here.parent


ROOT = _project_root()
CACHE = ROOT / "logs" / "_mermaid.min.js"      # logs/ 是 gitignored 的
CDP = "http://127.0.0.1:9222"

for _s in (sys.stdout, sys.stderr):
    _rc = getattr(_s, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass

CSS = """
/* ⚠️ 这里**不要写 `margin: 0`**：那会把打印边距抹平，于是页眉/页脚
   （CDP 的 headerTemplate / footerTemplate）没有自己的位置，
   直接压在正文第一行上（实测第 5 页表头被压）。边距交给 CDP 的
   marginTop/Bottom/Left/Right 给，这里只声明纸张。 */
@page { size: A4; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  font-family: "Microsoft YaHei", "微软雅黑", "PingFang SC", "Noto Sans CJK SC",
               "Segoe UI", sans-serif;
  font-size: 10.5pt; line-height: 1.65; color: #2C2418; margin: 0;
}
h1, h2, h3, h4 { font-family: "Songti SC", "SimSun", "Microsoft YaHei", serif;
                 color: #2C2418; line-height: 1.3; }
h1 { font-size: 20pt; margin: 0 0 14pt; padding-bottom: 6pt;
     border-bottom: 2px solid #4A7C59; page-break-before: always; }
h1:first-of-type { page-break-before: avoid; }
h2 { font-size: 15pt; margin: 18pt 0 8pt; page-break-after: avoid;
     border-left: 4px solid #4A7C59; padding-left: 8pt; }
h3 { font-size: 12.5pt; margin: 14pt 0 6pt; page-break-after: avoid; }
h4 { font-size: 11pt; margin: 12pt 0 5pt; page-break-after: avoid; }
p { margin: 5pt 0; }
ul, ol { margin: 5pt 0 5pt 18pt; padding: 0; }
li { margin: 2pt 0; }
table { width: 100%; border-collapse: collapse; margin: 8pt 0; font-size: 8.8pt;
        page-break-inside: auto; }
th, td { border: 0.5pt solid #C9C1B4; padding: 3.5pt 5pt; vertical-align: top;
         text-align: left; }
th { background: #F1EDE4; font-weight: 600; }
tr { page-break-inside: avoid; }
thead { display: table-header-group; }   /* 表格跨页时重复表头 */
code { font-family: Consolas, "Courier New", monospace; font-size: 9pt;
       background: #F5F2EC; padding: 0 2pt; border-radius: 2pt; }
pre { background: #F7F5F0; border: 0.5pt solid #E0DACD; border-radius: 3pt;
      padding: 6pt 8pt; font-size: 8.6pt; white-space: pre-wrap;
      word-break: break-all; page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 8.6pt; }
blockquote { margin: 6pt 0; padding: 4pt 10pt; border-left: 3pt solid #C9A961;
             background: #FBF8F1; color: #5A5040; }
hr { border: none; border-top: 0.5pt solid #D8D2C6; margin: 12pt 0; }
.mermaid { margin: 10pt 0; text-align: center; page-break-inside: avoid; }
.mermaid svg { max-width: 100%; height: auto; }
.toc { background: #FAF8F3; border: 0.5pt solid #E0DACD; border-radius: 3pt;
       padding: 10pt 14pt; margin: 10pt 0 16pt; font-size: 9.5pt; }
.toc ul { margin: 0; padding-left: 14pt; }
.toc > ul { padding-left: 0; }
.toc a { color: #2C2418; text-decoration: none; }
"""

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>%%TITLE%%</title>
<style>%%CSS%%</style>
<script>%%MERMAID%%</script>
</head><body>
%%TOC%%
%%BODY%%
<script>
  // mermaid 渲染完之前不许打印 —— 否则 PDF 里是 16 个源码块。
  window.__mermaidDone = false;
  window.__err = null;
  window.addEventListener('error', (e) => {
    window.__err = String((e && e.message) || e);
  });
  mermaid.initialize({
    startOnLoad: false, theme: 'neutral',
    flowchart: { htmlLabels: false, useMaxWidth: true },
    sequence: { useMaxWidth: true },
  });
  (async () => {
    const nodes = [...document.querySelectorAll('.mermaid')];
    for (const n of nodes) {
      try { await mermaid.run({ nodes: [n] }); }
      catch (e) {
        n.innerHTML = '<pre>mermaid 渲染失败：' + String((e && e.message) || e) + '</pre>';
      }
    }
    window.__mermaidDone = true;
    window.__mermaidCount = document.querySelectorAll('.mermaid svg').length;
  })();
</script>
</body></html>
"""

#: ⚠️ 用 `%%X%%` 占位 + `str.replace`，**不用 `str.format`** ——
#:    页面里那段 JS 满是`{` `}`（箭头函数、对象字面量），
#:    `format` 会把它们当格式字段，写的时候得把每个括号都写成 `{{` `}}`，
#:    改一行漏一处就报 `KeyError: ' 一个格式字段名）。


def fetch_mermaid() -> str:
    """mermaid.js 的正文。缓存到 logs/（gitignored），避免每次重下 3.3 MB。"""
    if CACHE.is_file() and CACHE.stat().st_size > 1_000_000:
        print(f"  mermaid.js 用缓存（{CACHE.stat().st_size // 1024} KB）")
        return CACHE.read_text(encoding="utf-8")
    last = ""
    for url in MERMAID_CDN:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "md_to_pdf"})
            # ⚠️ 本机访问 CDN 必须走代理（见 memory: git-needs-explicit-proxy）
            proxy = urllib.request.ProxyHandler(
                {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
            with urllib.request.build_opener(proxy).open(req, timeout=60) as r:
                text = r.read().decode("utf-8")
            if len(text) > 1_000_000:
                CACHE.parent.mkdir(parents=True, exist_ok=True)
                CACHE.write_text(text, encoding="utf-8")
                print(f"  mermaid.js 已下载（{len(text) // 1024} KB）并缓存到 {CACHE.name}")
                return text
            last = f"{url} 只回了 {len(text)} 字节"
        except Exception as e:  # noqa: BLE001
            last = f"{url} → {type(e).__name__}: {e}"
    raise RuntimeError(f"拿不到 mermaid.js：{last}")


def md_to_html(md_path: pathlib.Path) -> tuple[str, str, int]:
    """md → (正文 HTML, 目录 HTML, mermaid 图数量)。"""
    import markdown

    text = md_path.read_text(encoding="utf-8")

    # ① 先把 mermaid 围栏抠出来（Python-Markdown 会把它渲染成代码块）
    blocks: list[str] = []

    def stash(m: re.Match[str]) -> str:
        blocks.append(m.group(1))
        return f"\n\n@@MERMAID{len(blocks) - 1}@@\n\n"

    text = re.sub(r"```mermaid\n(.*?)```", stash, text, flags=re.S)
    title = next((ln.lstrip("# ").strip() for ln in text.splitlines()
                  if ln.startswith("# ")), md_path.stem)

    html = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "toc", "attr_list"],
        extension_configs={"toc": {"title": "目录", "toc_depth": "1-2"}},
    )
    for i, code in enumerate(blocks):
        esc = (code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        html = html.replace(f"<p>@@MERMAID{i}@@</p>",
                            f'<div class="mermaid">{esc}</div>')
        html = html.replace(f"@@MERMAID{i}@@", f'<div class="mermaid">{esc}</div>')

    # ② 目录插在第一个 h2 之前（文档自带「阅读约定」，目录放它后面更好读）
    toc = ""
    m = re.search(r'<div class="toc">.*?</div>', html, re.S)
    if m:
        toc = m.group(0)
        html = html.replace(m.group(0), "")
    return html, toc, len(blocks)


def _render(html: str, out: pathlib.Path, timeout_s: int = 240) -> dict:
    """把 HTML 交给 Edge 打开、等 mermaid 画完、打印成 PDF。"""
    import websocket  # type: ignore  # websocket-client

    with urllib.request.urlopen(f"{CDP}/json/list", timeout=10) as r:
        targets = json.load(r)
    page = next(t for t in targets if t["type"] == "page")

    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=timeout_s)
    counter = {"i": 0}
    pending: dict[int, dict] = {}

    def send(method: str, params: dict | None = None) -> dict:
        counter["i"] += 1
        mid = counter["i"]
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} → {msg['error']}")
                return msg.get("result", {})
            if msg.get("id") and msg["id"] in pending:      # 其它在飞的响应
                pending[msg["id"]] = msg

    def ev(expr: str):
        r = send("Runtime.evaluate", {"expression": expr, "awaitPromise": True,
                                      "returnByValue": True})
        return r.get("result", {}).get("value")

    send("Page.enable")
    send("Runtime.enable")
    # ⚠️ **先给一个宽视口再渲染**：mermaid 是按容器宽度决定布局的 ——
    #    窄容器会把 4 个 subgraph 叠成一根竖条，占掉好几页还留一片空白。
    #    A4 正文宽约 720px（8.27in − 左右边距），给 1400 只是为了让它按"宽"排布，
    #    真打印时 SVG 会缩到页宽（`max-width:100%`）。
    send("Emulation.setDeviceMetricsOverride",
         {"width": 1400, "height": 1000, "deviceScaleFactor": 1, "mobile": False})

    # ⚠️ 落到 logs/ 下一个临时 HTML 再用 file:// 打开，而不是用 data: URL ——
    #    data: 是不透明源，mermaid 在里面不跑（实测等了 240s 都没 __mermaidDone）。
    #    logs/ 是 gitignored 的，不会进仓库；留着也方便人工打开对照。
    tmp = ROOT / "logs" / "_md2pdf.html"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(html, encoding="utf-8")
    send("Page.navigate", {"url": tmp.resolve().as_uri()})

    # 等 mermaid 画完（最多等 timeout_s 秒；rAF 在后台页不跑，但这里是同步脚本 ✓）
    for _ in range(timeout_s):
        if ev("window.__mermaidDone === true"):
            break
        time.sleep(1.0)
    else:
        diag = {
            "url": ev("location.href"),
            "title": ev("document.title"),
            "body_len": ev("document.body ? document.body.innerHTML.length : -1"),
            "mermaid_type": ev("typeof mermaid"),
            "blocks": ev("document.querySelectorAll('.mermaid').length"),
            "svgs": ev("document.querySelectorAll('.mermaid svg').length"),
            "err": ev("window.__err || '(没抓到)'"),
            "done": ev("window.__mermaidDone"),
        }
        raise TimeoutError(f"等 mermaid 渲染超时：{diag}")

    n_svg = ev("window.__mermaidCount") or 0
    n_src = ev("document.querySelectorAll('.mermaid').length") or 0

    r = send("Page.printToPDF", {
        "printBackground": True,
        "paperWidth": 8.27, "paperHeight": 11.69,        # A4（英寸）
        "marginTop": 0.62, "marginBottom": 0.55,
        "marginLeft": 0.45, "marginRight": 0.45,
        "displayHeaderFooter": True,
        "headerTemplate": '<div style="font-size:7pt;color:#8A8172;width:100%;'
                          'padding:0 12mm;font-family:sans-serif">'
                          '全友·智绘家 QuanYou Smart HomeCanvas</div>',
        "footerTemplate": '<div style="font-size:7pt;color:#8A8172;width:100%;'
                          'padding:0 12mm;text-align:right;font-family:sans-serif">'
                          '<span class="pageNumber"></span> / '
                          '<span class="totalPages"></span></div>',
    })
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(r["data"]))
    ws.close()
    # ⚠️ 页数**别估**：打印用的视口宽度与渲染时不同，按 scrollHeight 估出来的
    #    数实测差了 30%（估 74 页 / 实际 57 页）。直接问 PDF 本身。
    try:
        import fitz
        pages = fitz.open(out).page_count
    except Exception:  # noqa: BLE001
        pages = 0
    return {"svg": n_svg, "src": n_src, "pages": pages, "bytes": out.stat().st_size}


def main() -> int:
    ap = argparse.ArgumentParser(description="Markdown → PDF（mermaid 会渲染）")
    ap.add_argument("md", type=pathlib.Path)
    ap.add_argument("-o", "--out", type=pathlib.Path, default=None)
    args = ap.parse_args()

    md_path = args.md if args.md.is_absolute() else (ROOT / args.md)
    out = args.out or md_path.with_suffix(".pdf")
    if not md_path.is_file():
        print(f"找不到 {md_path}")
        return 2

    print(f"读 {md_path.relative_to(ROOT)}（{md_path.stat().st_size // 1024} KB）")
    body, toc, n_blocks = md_to_html(md_path)
    print(f"  转成 HTML：{len(body) // 1024} KB；mermaid 图 {n_blocks} 张")
    mermaid_js = fetch_mermaid()
    html = (PAGE.replace("%%TITLE%%", md_path.stem)
                .replace("%%CSS%%", CSS)
                .replace("%%MERMAID%%", mermaid_js)
                .replace("%%TOC%%", toc)
                .replace("%%BODY%%", body))

    r = _render(html, out)
    print(f"\n渲染结果：")
    print(f"  mermaid 图   {r['svg']}/{r['src']} 张画出来了"
          + ("" if r["svg"] == r["src"] and r["src"] else "   ⚠️ 有没画出来的"))
    print(f"  页数         {r['pages']} 页")
    print(f"  输出         {out}（{r['bytes'] / 1048576:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
