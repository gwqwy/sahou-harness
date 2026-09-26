"""插件发现与装载：plugin.json 清单 + register.py 的 register(ctx)。"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any, Dict

_logger = logging.getLogger(__name__)

# load_manifest 解析失败时在返回的清单里挂这个私有键，
# 由 Harness.mount 取出后写进 record.skipped（P2 #3）。
MANIFEST_ERROR_KEY = "_manifest_error"


def load_manifest(path: Path) -> Dict[str, Any]:
    """读取 plugin.json；不存在时返回最小清单（name=目录名）。

    存在但无法解析 / 不是对象时：记一条 warning，并在清单里带 MANIFEST_ERROR_KEY，
    调用方可据此在 record.skipped 标注 bad-manifest（不再静默回落，P2 #3）。
    """
    manifest_path = Path(path) / "plugin.json"
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            reason = f"plugin.json 不是对象（{type(data).__name__}）"
        except (ValueError, OSError) as exc:
            reason = f"plugin.json 无法解析: {exc}"
        _logger.warning("%s 的 %s，已回落到目录名作为插件名", path, reason)
        return {"name": Path(path).name, MANIFEST_ERROR_KEY: reason}
    return {"name": Path(path).name}


def run_register(record: Any, ctx: Any) -> None:
    """执行插件的 register.py（定义 register(ctx) 函数）；也可只提供 TOOLS 列表。"""
    register_py = Path(record.path) / "register.py"
    if not register_py.is_file():
        return

    module_name = f"harness_plugin_{record.name}_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(module_name, register_py)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise

    # 卸载时把模块从 sys.modules 摘掉，否则反复「装卸/热激活」会让模块表单调膨胀（P2 #2）
    def _release_module() -> Any:
        return lambda: sys.modules.pop(module_name, None)

    ctx.effect(_release_module)

    register_fn = getattr(module, "register", None)
    if callable(register_fn):
        register_fn(ctx)
    for item in getattr(module, "TOOLS", []) or []:
        ctx.tools.register(item)
    if register_fn is None and not ctx.tools_list:
        ctx.skipped.append("no-entry: register.py 中既无 register(ctx) 也无 TOOLS")
