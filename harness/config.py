"""JSON 配置与 profile 管理（不使用 .env）。

profile 目录结构（默认 .harness/profiles/default/）：
    config.json        模型列表 / 默认模型 / 权限
    plugins/           已安装的外部插件
    sessions/          会话历史（每会话一个 JSON）

设计要点（缺陷审计 H-01 / H-03 / H-07 修复）：
- 权限取值统一经 :func:`permission_mode` 归一化，**非法值一律回落到最保守的 ask**，
  绝不 fail-open（此前插件侧只做等值比较、无 else 分支，非规范值全部放行）。
- ``save_config`` 采用「临时文件 + os.replace」原子写，``update_config`` 持锁，
  避免并发读-改-写丢更新。
- ``load_config`` 解析失败时**备份原文件并抛 :class:`ConfigError`**，
  不再静默回落默认值（否则下一次写回会抹掉用户的 models / api_key）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# 权限门合法取值；其余任何取值都归一到 DEFAULT_PERMISSION_MODE
PERMISSION_MODES = ("ask", "allow", "deny")
DEFAULT_PERMISSION_MODE = "ask"

DEFAULT_CONFIG: Dict[str, Any] = {
    "default_model": "",
    "models": [],          # [{name, provider, base_url, api_key, model}]
    "permissions": {"shell": DEFAULT_PERMISSION_MODE, "fs": DEFAULT_PERMISSION_MODE},
}

CONFIG_EXAMPLE = {
    "default_model": "deepseek",
    "models": [
        {"name": "deepseek", "provider": "openai",
         "base_url": "https://api.deepseek.com/v1",
         "api_key": "sk-...", "model": "deepseek-chat"},
    ],
    "permissions": {"shell": "ask", "fs": "ask"},
}


class ConfigError(RuntimeError):
    """config.json 已存在但无法解析；原文件已备份，调用方应提示用户处理。"""


def _warn(message: str) -> None:
    """配置层的告警出口：打印到 stderr，GUI 环境下 stderr 可能为空。"""
    try:
        print(f"[harness.config] 警告: {message}", file=sys.stderr)
    except Exception:
        pass


def normalize_permission(raw: Any) -> str:
    """把任意权限取值归一为 ask / allow / deny。

    只接受字符串形式的规范值（大小写、首尾空白宽容）；其余（None / bool / 数字 /
    "no" 之类的自造值）一律回落到 DEFAULT_PERMISSION_MODE，保证 fail-closed。
    """
    if isinstance(raw, str):
        mode = raw.strip().lower()
        if mode in PERMISSION_MODES:
            return mode
    return DEFAULT_PERMISSION_MODE


def permission_mode(config: Dict[str, Any], key: str, default: str = DEFAULT_PERMISSION_MODE) -> str:
    """读取 ``permissions.<key>`` 并归一化。

    这是权限门的**唯一入口**：调用方只需判断返回值是否为 "allow" 才放行，
    不需要（也不应该）自己做等值比较，否则又会退化成 fail-open。
    """
    perms = config.get("permissions")
    raw = perms.get(key) if isinstance(perms, dict) else None
    if raw is None:
        return default if default in PERMISSION_MODES else DEFAULT_PERMISSION_MODE
    mode = normalize_permission(raw)
    normalized = raw.strip().lower() if isinstance(raw, str) else raw
    if normalized != mode:
        _warn(f"permissions.{key} 取值非法（{raw!r}），已按最保守的 {mode!r} 处理")
    return mode


# 同一配置文件的进程内互斥锁（桌面端每个 js_api 调用都在独立线程）
_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """原子写文本：写同目录临时文件 → fsync → os.replace 覆盖目标。

    避免「写入过程中被读到半截文件」；同目录保证 os.replace 是同分区原子替换。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with open(tmp, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class Profile:
    """一个 harness profile：配置 + 插件 + 会话的隔离目录。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.config_path = self.root / "config.json"
        self.plugins_dir = self.root / "plugins"
        self.sessions_dir = self.root / "sessions"
        self._lock = _lock_for(self.config_path)
        self._ensure()

    def _ensure(self) -> None:
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        if not self.config_path.is_file():
            self.save_config(dict(CONFIG_EXAMPLE))

    # -- 配置 ------------------------------------------------------------
    def load_config(self) -> Dict[str, Any]:
        """读取配置。

        文件不存在 → 返回默认配置；文件存在但无法解析 → 备份后抛 ConfigError。
        """
        if not self.config_path.is_file():
            return _merged({})
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            backup = self._backup_broken_config(exc)
            raise ConfigError(
                f"配置文件无法解析: {self.config_path}（{exc}）。"
                f"原文件已备份为 {backup}，请修复或删除后再启动；"
                f"为避免抹掉已有 models/api_key，本次不会回落默认配置。"
            ) from exc
        return _merged(raw)

    def _backup_broken_config(self, exc: Exception) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self.config_path.with_name(f"{self.config_path.name}.corrupt-{stamp}")
        try:
            shutil.copy2(self.config_path, backup)
        except OSError:
            return self.config_path
        _warn(f"配置无法解析（{exc}），已备份到 {backup}")
        return backup

    def save_config(self, config: Dict[str, Any]) -> None:
        """原子保存配置（临时文件 + os.replace），并保证权限字段被归一化。"""
        with self._lock:
            payload = dict(config)
            payload["permissions"] = _normalize_permissions(config.get("permissions"))
            atomic_write_text(
                self.config_path,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )

    def update_config(self, **changes: Any) -> Dict[str, Any]:
        """读-改-写，全程持锁，避免并发丢更新。"""
        with self._lock:
            config = self.load_config()
            config.update(changes)
            self.save_config(config)
            return config

    def mutate_config(self, mutator: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
        """在同一把锁内完成「读 → 由 mutator 就地修改 → 写」，适合读改写序列。"""
        with self._lock:
            config = self.load_config()
            mutator(config)
            self.save_config(config)
            return config

    # -- 插件安装 --------------------------------------------------------
    def _unique_dest(self, name: str) -> Path:
        dest = self.plugins_dir / name
        counter = 2
        while dest.exists():
            dest = self.plugins_dir / f"{name}-{counter}"
            counter += 1
        return dest

    def install_plugin(self, source: str | Path) -> Path:
        """把插件装进 profile。

        - 本地目录：直接复制（``git`` 地址与 http(s) 地址走浅克隆）
        - git/http(s) 地址：``git clone --depth 1`` 到临时目录后再复制，不会留下 .git

        仓库根不是插件时（常见的 ``repo/plugin/`` 结构），若恰好只有一个子目录
        带 plugin.json / register.py，就取它。
        """
        text = str(source).strip()
        if text.startswith(("http://", "https://", "git@", "ssh://", "file://")):
            return self._install_plugin_from_git(text)
        source_path = Path(source)
        if not source_path.is_dir():
            raise FileNotFoundError(f"插件目录不存在: {source}")
        dest = self._unique_dest(source_path.name)
        shutil.copytree(source_path, dest)
        return dest

    def _install_plugin_from_git(self, url: str) -> Path:
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            try:
                subprocess.run(["git", "clone", "--depth", "1", url, tmp],
                               check=True, capture_output=True, timeout=300)
            except FileNotFoundError as exc:
                raise OSError("未找到 git 命令，无法从地址安装插件") from exc
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
                raise OSError(f"git clone 失败: {detail or exc}") from exc
            try:
                subprocess.run(["git", "-C", tmp, "remote", "remove", "origin"],
                               check=False, capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError):
                pass
            root = Path(tmp)
            if not self._looks_like_plugin(root):
                children = sorted(p for p in root.iterdir()
                                  if p.is_dir() and p.name != ".git" and self._looks_like_plugin(p))
                if len(children) != 1:
                    raise OSError(
                        f"仓库里没有找到插件（需要 plugin.json 或 register.py）：{url}")
                root = children[0]
            name = root.name if root != Path(tmp) else Path(url.rstrip("/")).stem
            if name.endswith(".git"):
                name = name[:-4]
            dest = self._unique_dest(name)
            shutil.copytree(root, dest, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        return dest

    @staticmethod
    def _looks_like_plugin(path: Path) -> bool:
        return (path / "plugin.json").is_file() or (path / "register.py").is_file()

    def remove_plugin(self, name_or_path: str | Path) -> bool:
        """删除 profile 内的插件目录。

        接受目录名（相对 ``plugins/``）或目录路径；只允许删除 ``plugins/`` 之内
        的目标（越界或不存在返回 False），避免误删 profile 之外的内容。
        """
        candidate = Path(name_or_path)
        target = candidate if candidate.is_absolute() else (self.plugins_dir / candidate)
        try:
            target = target.resolve()
            root = self.plugins_dir.resolve()
        except OSError:
            return False
        if target == root or not target.is_relative_to(root) or not target.is_dir():
            return False
        shutil.rmtree(target)
        return True

    # -- 会话 ------------------------------------------------------------
    def save_session(self, session_id: str, history: List[dict]) -> Optional[Path]:
        path = self.sessions_dir / f"{session_id}.json"
        atomic_write_text(path, json.dumps(history, ensure_ascii=False, indent=2))
        return path

    def load_session(self, session_id: str) -> List[dict]:
        path = self.sessions_dir / f"{session_id}.json"
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except ValueError:
            return []

    def session_ids(self) -> List[str]:
        return sorted(p.stem for p in self.sessions_dir.glob("*.json"))


def _normalize_permissions(raw: Any) -> Dict[str, str]:
    """把 permissions 段归一为 {'shell': mode, 'fs': mode}，缺键补默认、非法值收敛。"""
    perms = raw if isinstance(raw, dict) else {}
    return {key: normalize_permission(perms.get(key)) for key in ("shell", "fs")}


def _merged(raw: Any) -> Dict[str, Any]:
    """深合并一层：``permissions`` 需与默认值合并，浅更新会丢键（H-07）。"""
    config = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        config.update(raw)
    config["permissions"] = _normalize_permissions(config.get("permissions"))
    if not isinstance(config.get("models"), list):
        _warn("config.models 不是列表，已重置为 []")
        config["models"] = []
    return config
