"""文字识别（OCR）：扫描版 PDF（页面上没有文字层）用 macOS 自带的 Vision 框架识别文字，完全离线。

只在 macOS 上可用，需要 pyobjc-framework-Vision 与 pyobjc-framework-Quartz（setup_mac.sh 会安装）；
其他系统或缺少依赖时 available() 返回 False，扫描版 PDF 照旧记为“没有文字层”。
"""

from __future__ import annotations

import sys
from functools import lru_cache


@lru_cache(maxsize=1)
def available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Quartz  # noqa: F401
        import Vision  # noqa: F401
    except ImportError:
        return False
    return True
