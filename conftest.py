"""pytest 根配置：确保 `backend` / `mcp_servers` 可作为顶层包导入。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 本机控制台默认 GBK，测试里打印中文/emoji 会 UnicodeEncodeError 崩掉
for _stream in (sys.stdout, sys.stderr):
    _rc = getattr(_stream, "reconfigure", None)
    if callable(_rc):
        try:
            _rc(encoding="utf-8", errors="replace")
        except Exception:
            pass
