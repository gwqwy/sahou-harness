"""模型提供者插件：一切皆插件——模型也是插件。"""

from __future__ import annotations

import os
import re

# API Key 可用环境变量间接引用，避免明文落盘（第三批 #15）：
#   "${OPENAI_API_KEY}" / "$OPENAI_API_KEY" / "env:OPENAI_API_KEY"
_ENV_REF = re.compile(
    r"^\s*(?:\$\{(?P<a>[A-Za-z_][A-Za-z0-9_]*)\}"
    r"|\$(?P<b>[A-Za-z_][A-Za-z0-9_]*)"
    r"|env:(?P<c>[A-Za-z_][A-Za-z0-9_]*))\s*$"
)


def _resolve_api_key(raw, provider: str, name: str) -> str:
    """把 api_key 解析成真实值：支持环境变量引用与 provider 约定变量的兜底。"""
    raw = (raw or "").strip()
    match = _ENV_REF.match(raw) if raw else None
    if match:
        var = match.group("a") or match.group("b") or match.group("c")
        value = os.environ.get(var, "").strip()
        if not value:
            raise ValueError(
                f"模型 '{name}' 的 api_key 引用环境变量 {var}，但该变量未设置或为空"
            )
        return value
    if not raw:
        default_var = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
        value = os.environ.get(default_var, "").strip()
        if value:
            return value
    return raw


def _build_llm(entry: dict):
    provider = (entry.get("provider") or "openai").lower()
    model = entry.get("model")
    api_key = _resolve_api_key(entry.get("api_key"), provider, str(entry.get("name") or ""))
    if not model or not api_key:
        raise ValueError(f"模型 '{entry.get('name')}' 缺少 model 或 api_key 配置")
    if provider == "anthropic":
        from nanoagent import AnthropicLLM

        return AnthropicLLM(model=model, api_key=api_key,
                            base_url=entry.get("base_url") or "https://api.anthropic.com")
    from nanoagent import LLM

    kwargs = {"model": model, "api_key": api_key}
    if entry.get("base_url"):
        kwargs["base_url"] = entry["base_url"]
    return LLM(**kwargs)


def register(ctx) -> None:
    host = ctx.host
    config = host.profile.load_config()
    pool: dict = {}
    order: list = []
    errors: dict = {}
    for entry in config.get("models") or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        # 逐个模型容错：某一条写错（如 ${ENV} 指向的变量没设）只跳过它自己。
        # 旧行为是整段 register 抛异常 → models 插件直接 FAILED → 所有模型一起消失，
        # 而 `sha model list` 只会显示「可用模型: 」，看不出是谁坏了。
        try:
            pool[name] = _build_llm(entry)
        except Exception as exc:  # noqa: BLE001
            reason = f"{type(exc).__name__}: {exc}"
            errors[name] = reason
            ctx.skipped.append(f"model:{name}: {reason}")
            continue
        order.append(name)

    def set_current(name: str) -> str:
        if name not in pool:
            return f"错误：没有模型 '{name}'，可用: {', '.join(order)}"
        runtime["current"] = name
        host.profile.update_config(default_model=name)  # 持久化，重启后仍生效
        # 同步既有 agent 实例（尚未构建时工厂会以新模型懒构建）
        factory = host.service("agent_factory")
        if callable(factory):
            try:
                agent = factory()
                agent.llm = pool[name]
            except Exception:  # noqa: BLE001 —— agent 未就绪时仅切换默认
                pass
        return f"已切换到模型 {name}"

    runtime = {"pool": pool, "order": order, "errors": errors,
               "current": config.get("default_model") or (order[0] if order else ""),
               "set": set_current}
    if runtime["current"] not in pool and order:
        runtime["current"] = order[0]

    # 思考级别（off/low/medium/high）：映射到 OpenAI 兼容的 reasoning_effort
    level = (config.get("thinking_level") or "").strip()
    if level and level != "off":
        for llm in pool.values():
            llm.reasoning_effort = level

    # 单一可变服务：切换后其它插件读到的是最新状态
    ctx.provide("models_runtime", runtime)

    def list_models() -> str:
        """列出配置里的全部模型与当前使用的模型。"""
        text = ("可用模型: " + (", ".join(order) or "（无）")
                + f"（当前: {runtime['current'] or '（未设置）'}）")
        if errors:
            text += "\n不可用: " + "; ".join(f"{k}（{v}）" for k, v in sorted(errors.items()))
        return text

    def switch_model(name: str) -> str:
        """切换当前使用的模型（重启对话后仍生效）。

        Args:
            name: 模型名称，见 list_models
        """
        return set_current(name)

    ctx.tools.register(list_models)
    ctx.tools.register(switch_model)  # 自主切换：模型可按任务需要自己换模型

    def command_model(args: str) -> str:
        if not args.strip():
            return list_models()
        return set_current(args.strip())

    ctx.commands.register("model", command_model, "查看/切换模型（/model [名称]）")
