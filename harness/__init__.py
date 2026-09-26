"""sahou-harness：一个「一切皆插件」的 agent harness（参考 DeepSeek Harness 架构）。

分层：
    kernel    微内核：Harness 宿主 + Context（provide/on/effect）+ 生命周期状态机
    loader    插件发现与装载（plugin.json + register.py）
    config    JSON 配置（不使用 .env）
    builtins  内置插件——对话循环、模型、工具、技能、MCP、REPL 全部是插件
    cli       命令行入口（sha 命令）

执行引擎复用 nanoagent 框架（pip install nanoagent 或本地路径安装）。
"""

__version__ = "0.1.0"
