"""插件发现与装载：plugin.json 清单 + register.py 的 register(ctx)。"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# load_manifest 解析失败时在返回的清单里挂这个私有键，
# 由 Harness.mount 取出后写进 record.skipped（P2 #3）。
MANIFEST_ERROR_KEY = "_manifest_error"

# 依赖声明的字段名（对齐 dsh 的 inject；兼容常见别名）
DEP_KEYS = ("inject", "deps", "requires", "dependencies")


def manifest_deps(manifest: dict[str, Any]) -> list:
    """从插件清单里取出依赖的插件名列表。

    支持 ``"inject": ["models"]`` 与 ``"inject": "models, tools_fs"`` 两种写法；
    未声明时返回空列表（保持挂载顺序，向后兼容）。
    """
    for key in DEP_KEYS:
        value = manifest.get(key)
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [item.strip() for item in value.split(",") if item.strip()]
    return []


def load_manifest(path: Path) -> dict[str, Any]:
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
    if spec is None or spec.loader is None:
        # 理论上只在文件被并发替换成非法形态时发生；fail-fast 让 mount_notes 可见
        raise ImportError(f"无法从 {register_py} 创建模块 spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        # 直接 exec 源码而不用 spec.loader.exec_module：后者走 __pycache__ 字节码缓存，
        # 其有效性按「mtime 秒级 + 文件大小」判断——同一秒内改写 register.py（大小不变）
        # 会命中旧缓存，热重载/重装拿到的是旧代码。
        source = register_py.read_text(encoding="utf-8")
        exec(compile(source, str(register_py), "exec"), module.__dict__)  # noqa: S102
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
