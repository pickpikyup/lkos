"""打开应用窗口：lkos app（或双击 LKOS.app / LKOS.command）。

界面是一份本地 HTML（样式与脚本内联），用 macOS 自带的 WebKit 显示，不开网络端口、不联网。
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from lkos.errors import LkosError


def build_html() -> str:
    static = resources.files("lkos.ui").joinpath("static")
    html = static.joinpath("index.html").read_text("utf-8")
    css = static.joinpath("app.css").read_text("utf-8")
    js = static.joinpath("app.js").read_text("utf-8")
    return html.replace("/*APP_CSS*/", css).replace("/*APP_JS*/", js)


def _enable_folder_drop(window, bridge) -> None:
    """拖入文件夹：pywebview 5 起支持在拖放事件里拿到完整路径（File.pywebviewFullPath）。旧版本静默跳过。"""
    try:
        from webview.dom import DOMEventHandler
    except ImportError:
        return

    def on_drop(_event):  # 只需注册，前端会读 pywebviewFullPath
        return None

    try:
        window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
    except Exception:  # noqa: BLE001
        pass


def run(home: Path | None = None, config_path: Path | None = None, debug: bool = False) -> None:
    try:
        import webview
    except ImportError as e:
        raise LkosError("没有安装界面组件 pywebview：在项目目录运行 .venv/bin/pip install pywebview "
                        "（或双击 LKOS.command，会自动安装）") from e
    from lkos.ui.bridge import Bridge

    bridge = Bridge(home=home, config_path=config_path)
    window = webview.create_window(
        "Local Knowledge OS", html=build_html(), js_api=bridge,
        width=1280, height=820, min_size=(900, 600), background_color="#FFFFFF", text_select=True,
    )
    bridge._attach(window)
    window.events.loaded += lambda: _enable_folder_drop(window, bridge)
    try:
        webview.start(debug=debug)
    finally:
        bridge._close()
