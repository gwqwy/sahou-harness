# 卅 harness（sahou-harness）

[![CI](https://github.com/gwqwy/sahou-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/gwqwy/sahou-harness/actions/workflows/ci.yml)

**一个「一切皆插件」的 agent harness**——架构参考 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
（Cordis 微内核 / Everything is a Plugin），执行引擎复用 [nanoagent](https://github.com/gwqwy/nanoagent) 框架。
配置全部走 JSON 文件，**不使用 .env**。

## 架构

```
sha CLI（cli.py）
   │  组装
   ▼
Harness 微内核（kernel.py）──────── 一切皆插件（16 个内置）：
   │  ctx.provide / on / effect      ├─ chat_loop    对话循环（流式 / 护栏 / 追踪 / 图片输入）
   │  effect 必返回 disposer，        ├─ models       模型池 + 自主切换（switch_model）
   │  卸载时 LIFO 回滚、幂等          ├─ tools_fs     文件读写 / 局部编辑（路径监狱）
   │  状态机 PENDING→ACTIVE→         ├─ tools_search 全工作区文本搜索
   │  FAILED/DISPOSED                ├─ tools_shell  Shell 执行（三级权限门 + 审计）
   │  插件依赖拓扑激活 / 热重载        ├─ tools_todo   TODO.md 任务索引（工作区持久）
   │                                 ├─ tools_git    受限 git（status/diff/log/add/commit，无 push；测试门）
   │                                 ├─ skills       SKILL.md 技能（渐进式披露）
   │                                 ├─ mcp_client   MCP 服务器接入（stdio/SSE/HTTP）
   │                                 ├─ guardrails   输入/输出护栏
   │                                 ├─ tracing      按天 JSONL 追踪
   │                                 ├─ subagent     spawn_subagent 子 agent
   │                                 ├─ knowledge    RAG 知识库（索引 / 检索）
   │                                 ├─ browser      fetch_url 抓网页转纯文本
   │                                 ├─ notifications notify webhook 通知
   │                                 ├─ scheduler    定时任务（到点独立 agent 执行）
   │                                 └─ repl         终端界面（UI 也是插件，可换掉）
   ▼
profile 隔离目录（.harness/profiles/<名>/）
    config.json   模型列表 / 默认模型 / 权限（不使用 .env）
    plugins/      已安装的外部插件
    sessions/     会话历史
    audit/        shell 审批审计日志（JSONL）
```

架构细节（插件生命周期 / 依赖拓扑 / 服务冲突 / 权限模型）见 [docs/architecture.md](docs/architecture.md)，
各版本变更见 [CHANGELOG.md](CHANGELOG.md)。

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
- **新会话向导**：先选工作区（含「选择其他文件夹…」）、再命名；取消/留空不创建；
  默认名自动生成（不再出现裸 id 会话名）
- **图片输入**：📎 按钮选图或直接粘贴图片路径，随下一条消息发给视觉模型（路径受工作区监狱约束）
- **模型可视化设置**：弹窗内增删改模型卡片（名称/提供商/Base URL/模型 ID/API Key/默认），
  保存即校验（名称、模型、Key 必填，provider 仅 openai/anthropic，名称唯一）并**热重建模型池**，无需重启
- **当前模型下拉切换**：等价 `/model`，持久化到 config.json
- **插件管理**：侧栏查看插件状态、输入本地目录**可视化安装**（热激活）、点击移除
- **会话管理**：重命名 / 🗑 删除（确认对话框；删除当前会话自动切到最近的）；
  ★ 置顶（排在分组最前）；🔍 按关键词搜索全部历史并跳转高亮
- **消息操作**：bot 消息「↻ 重新生成」（仅最后一条）、用户消息「✎ 编辑后重发」、一键复制
- **知识库（RAG）**：右侧「📚 知识库」面板看库状态、选文件夹索引文本文件、按来源管理（单独删除某文件的片段）、一键清空；
  agent 对话自动用 `search_knowledge` 检索
- **任务索引（TODO.md）**：agent 的 todo_write/todo_read 读写工作区根 `TODO.md`
  （Markdown 勾选格式，Git 可跟踪）；右侧「✅ 任务」面板可视化渲染
- **长程任务支持**：系统提示词内置长程执行准则（原子粒度 / 串行 / TDD 默认失败，
  `config.longrun.guidelines=false` 可关）；新会话自动恢复上下文（TODO.md 未完成项 +
  最近 git log，`config.longrun.recovery=false` 可关）；上下文卡片 ≥80% 水位警示，
  一键「⟳ 无状态重置」（进展写回 TODO.md → 自动新会话接续）
- **用量图表**：右侧「📈 用量」标签页，usage.jsonl 按天聚合近 30 天 tokens 折线图
- **完成提醒**：回复完成提示音 / 最小化时系统通知（上下文卡片内开关，可持久化）
- **Shell 权限门**：`permissions.shell=ask` 时弹**原生确认对话框**（无窗口时 fail-closed 拒绝）
- **可调布局**：右侧面板与左侧栏都可拖拽调宽（记忆）；工作区选择条可隐藏/从侧栏找回；
  右侧标签页可 ✕ 隐藏、从「＋」找回；偏好持久化到 `config.ui_prefs`
- **导出**：📤 导出当前会话为 Markdown；🗂 导出全部会话打包 zip（等价 `sha export --all`）
- **个性化**：🎨 自定义强调色（预设/任意 #RRGGBB）与界面字体，全套主题变量联动
- **更多**：浏览器多标签页（各自独立 iframe）、⚡ 快捷指令库（常用提示词一键插入，
  REPL `/snip` 同步可用）、拖拽文件入窗（图片附加 / 文本插入输入框）

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

## 许可证

本项目采用 MIT 协议，详见 [LICENSE](LICENSE)。
