# 卅 harness（sahou-harness）

**一个「一切皆插件」的 agent harness**——架构参考 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
（Cordis 微内核 / Everything is a Plugin），执行引擎复用 [nanoagent](https://github.com/gwqwy/nanoagent) 框架。
配置全部走 JSON 文件，**不使用 .env**。

## 架构

```
sha CLI（cli.py）
   │  组装
   ▼
Harness 微内核（kernel.py）──────── 一切皆插件：
   │  ctx.provide / on / effect      ├─ chat_loop   对话循环（nanoagent Agent 引擎）
   │  effect 必返回 disposer，        ├─ models      模型池 + 自主切换（switch_model 工具）
   │  卸载时 LIFO 回滚、幂等          ├─ tools_fs    文件读写（工作区路径监狱）
   │  状态机 PENDING→ACTIVE→         ├─ tools_shell Shell 执行（权限门 ask/allow/deny）
   │  FAILED/DISPOSED                ├─ skills      SKILL.md 技能（渐进式披露）
   │                                 ├─ mcp_client  MCP 服务器接入（stdio/SSE/HTTP）
   │                                 └─ repl        终端界面（UI 本身也是插件，可整个换掉）
   ▼
profile 隔离目录（.harness/profiles/<名>/）
    config.json   模型列表 / 默认模型 / 权限（不使用 .env）
    plugins/      已安装的外部插件
    sessions/     会话历史
```

## 桌面端（desktop）

形态对齐 [dsh-desktop](https://github.com/dataelement/dsh-desktop)（桌面壳 + profile 隔离），
但不引入 Electron：**pywebview 原生窗口**（Windows 上是 WebView2），HTML 只是渲染层，
页面通过 `window.pywebview.api` 直接调用进程内 `DesktopApp`——**没有 HTTP 端口、没有浏览器**。

```cmd
pip install sahou-harness[desktop]   :: 或 pip install pywebview
sha desktop                          :: 打开桌面窗口
```

桌面端能力（全部基于同一套插件运行时，与 CLI 共享 profile）：

- **对话**：会话侧栏（历史会话点击回放 / 新会话），工具调用与模型信息随回复展示
- **模型可视化设置**：弹窗内增删改模型卡片（名称/提供商/Base URL/模型 ID/API Key/默认），
  保存即校验（名称、模型、Key 必填，provider 仅 openai/anthropic，名称唯一）并**热重建模型池**，无需重启
- **当前模型下拉切换**：等价 `/model`，持久化到 config.json
- **插件管理**：侧栏查看插件状态、输入本地目录**可视化安装**（热激活）、点击移除
- **Shell 权限门**：`permissions.shell=ask` 时弹**原生确认对话框**（无窗口时 fail-closed 拒绝）

架构：`harness/desktop.py` 的 `DesktopApp` 是纯 Python API 层（可独立测试，见 `tests/test_desktop.py`），
`run()` 负责创建窗口并注入 `js_api`；渲染层是单页 HTML 常量，不含任何业务逻辑。


## 快速开始

```cmd
:: 1. 安装（需要 Python 3.10+）：先装执行引擎 nanoagent，再装 harness
pip install "git+https://github.com/gwqwy/nanoagent.git"
pip install "git+https://github.com/gwqwy/sahou-harness.git"

:: 2. 初始化 profile 并编辑 config.json 填入模型
sha init
::   编辑 %USERPROFILE%\.sahou-harness\profiles\default\config.json：
::   {"default_model": "deepseek",
::    "models": [{"name": "deepseek", "provider": "openai",
::                "base_url": "https://api.deepseek.com/v1",
::                "api_key": "sk-...", "model": "deepseek-chat"}],
::    "mcpServers": {"remote": {"url": "https://example.com/mcp", "type": "http"}}}

:: 3. 对话
sha                     :: 交互式 REPL
sha chat -m "写一个 你好.saho 并运行"   :: 单次执行
sha desktop             :: 桌面端窗口（需 pip install pywebview）
sha status              :: 插件与能力总览
```

## 插件

**写一个插件**：任意目录放 `plugin.json`（声明 name）+ `register.py`：

```python
# register.py —— ctx 是 mini-Cordis 上下文
def register(ctx):
    def translate(text: str) -> str:
        """把文本翻译成中文。"""
        return "译文: " + text

    ctx.tools.register(translate)          # 注册 agent 工具
    ctx.provide("my.service", object())    # 提供服务（可被其它插件消费）
    ctx.effect(lambda: open_resource_or_none)  # 可回滚资源：disposer 由内核 LIFO 管理
    ctx.commands.register("hello", lambda args: "你好", "打招呼命令")
```

**安装**：`sha plugin add <目录>`（复制进 profile，重启 harness 生效）；
**移除**：`sha plugin remove <名>`；**总览**：`sha status`。

**自主切换模型**：models 插件给 agent 注册了 `switch_model`/`list_models` 工具——
模型可以按任务需要自己换模型（强模型写代码、快模型跑杂活）；`sha model use <名>` 或
REPL 里 `/model <名>` 手动切换，选择持久化到 config.json。

## 与 DeepSeek Harness / nanoagent 的关系

- 内核的 ctx/effect/disposer/状态机模型对齐 dsh 的 Cordis 微内核
- 插件声明（plugin.json + register.py）对齐 dsh 的 package.json 声明式插件
- profile 隔离与 `plugin add` 对齐 dsh 的 profile 机制
- 执行引擎（agent loop / 工具 schema / 记忆 / MCP / 技能）来自 nanoagent 框架

## 测试

```cmd
python -m unittest discover tests
```

全部测试使用假模型客户端离线运行，不需要 API Key。
