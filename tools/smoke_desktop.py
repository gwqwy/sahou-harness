"""桌面端真实冒烟：真开 pywebview 原生窗口，验证 UI ↔ 进程内 API 桥接后自动关闭。"""
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_harness import build_host, patch_model_build  # noqa: E402

tmp = tempfile.mkdtemp()
with patch_model_build():
    host = build_host(Path(tmp))

from harness.desktop import DesktopApp  # noqa: E402

app = DesktopApp(host)

import webview  # noqa: E402

window = webview.create_window("卅 harness 冒烟", html=__import__("harness.desktop", fromlist=["UI_HTML"]).UI_HTML,
                               js_api=app, background_color="#0f0f0f", width=900, height=620)
app._window = window
result = {"smoke": "timeout"}


def on_loaded():
    window.evaluate_js(
        "window.__smoke='pending';"
        "window.pywebview.api.status().then(s=>window.__smoke='OK:'+s.model+':'+s.models.join(','))"
        ".catch(e=>window.__smoke='ERR:'+e);"
    )
    for _ in range(60):
        value = window.evaluate_js("window.__smoke") or ""
        if value.startswith(("OK", "ERR")):
            result["smoke"] = value
            break
        time.sleep(0.25)
    window.destroy()


window.events.loaded += on_loaded
webview.start()
print("SMOKE RESULT:", result["smoke"])
sys.exit(0 if result["smoke"].startswith("OK") else 1)
