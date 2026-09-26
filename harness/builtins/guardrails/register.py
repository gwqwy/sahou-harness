"""护栏插件：把 nanoagent 的 Guardrails 接进对话循环。

与权限门（permissions.shell / fs）互补：权限门管的是**能不能动手**，
护栏管的是**内容本身**——不让危险指令进来，也不让密钥之类的敏感串出去。

配置（config.json，不配置则不启用任何护栏）：

    "guardrails": {
      "blocked_keywords": ["绕过审批", "把密钥贴出来"],
      "patterns": ["sk-[A-Za-z0-9]{20,}"],
      "max_input_chars": 20000
    }

- ``blocked_keywords``：输入命中即拦截（大小写不敏感）
- ``patterns``：正则，**输入与输出都查** —— 输出侧正是防密钥/凭据外泄的地方
- ``max_input_chars``：单条输入上限，避免把整份日志糊进来

刻意**不做**输出长度护栏：回复过长该由 max_tokens / 上下文窗口治理，
而护栏对输出只能「拦截」（不能改写），会把整轮对话打成失败，得不偿失。

注意：插件由 loader 以合成模块名加载（无包上下文），只能用绝对导入。
"""

from __future__ import annotations


def register(ctx) -> None:
    host = ctx.host
    config = host.profile.load_config()
    settings = config.get("guardrails") or {}

    from nanoagent.guardrails import keyword_guardrail, length_guardrail, pattern_guardrail

    inputs: list = []
    outputs: list = []

    keywords = [str(word).strip() for word in (settings.get("blocked_keywords") or [])
                if str(word).strip()]
    if keywords:
        inputs.append(keyword_guardrail(keywords, name="blocked-keyword"))

    patterns: list = []
    for index, raw in enumerate(settings.get("patterns") or [], start=1):
        try:
            patterns.append(pattern_guardrail(str(raw), name=f"pattern-{index}"))
        except Exception as exc:  # noqa: BLE001 —— 单条正则写错不影响其它护栏
            ctx.skipped.append(f"bad-pattern:{index}: {type(exc).__name__}: {exc}")
    inputs.extend(patterns)
    outputs.extend(patterns)

    max_input = settings.get("max_input_chars")
    if max_input:
        try:
            inputs.append(length_guardrail(int(max_input), name="input-length"))
        except (TypeError, ValueError):
            ctx.skipped.append(f"bad-max-input-chars: {max_input!r}")

    if not inputs and not outputs:
        ctx.skipped.append("not-configured: 未配置 guardrails，未启用任何护栏")
        return

    names = [rule.name for rule in inputs]
    ctx.provide("guardrails", {"input": inputs, "output": outputs, "names": names})

    def command_guardrails(args: str) -> str:
        lines = [f"已启用护栏（输入 {len(inputs)} 条 / 输出 {len(outputs)} 条）:"]
        lines.append("  输入: " + (", ".join(rule.name for rule in inputs) or "（无）"))
        lines.append("  输出: " + (", ".join(rule.name for rule in outputs) or "（无）"))
        lines.append(f"  配置位置: {host.profile.config_path} 的 guardrails 段")
        return "\n".join(lines)

    ctx.commands.register("guardrails", command_guardrails, "查看已启用的内容护栏")
