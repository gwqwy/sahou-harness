# Changelog

本项目的所有显著变更都记录在此文件里。

格式依据 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号依据 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)。
类型：新增 / 变更 / 修复 / 工程。

## [未发布] —— 2026-09-27 功能扩展（第二日）

### 新增

**三个新内置插件（13 → 16 个）**
- `notifications`：`notify(message, title)` 工具向 `config.notifications.url` POST JSON；
  `on_error=true` 时订阅宿主 `error` 事件推送告警；未配置 url 时插件**缺席**（status 可见原因）
- `scheduler`：定时任务——任务存 `<profile>/scheduled/tasks.json`，后台 daemon 线程
  每秒检查、**独立 agent**（全新 Memory）串行执行，结果追加写 `scheduled/<名>.log`；
  worker 每轮重新读盘，`sha schedule add/list/remove` 对运行中的 harness 立即生效；
  任务名走白名单（对齐会话 id 的 H-04 教训），间隔 1 秒 ~ 30 天
- `browser`：`fetch_url(url, max_chars)` —— httpx GET + HTML 去标签转纯文本
  （script/style 剔除、实体解码、相邻重复行折叠），仅 http/https，
  可选 `config.browser.allowed_domains` 域名白名单，超时/非 2xx/超限返回可读中文错误

**多模态图片输入（功能7）**
- `chat_loop.ask / ask_stream` 新增 `images` 参数；本地路径经工作区路径监狱校验
  （`workspace.safe_image`：须在区内、文件存在、png/jpg/jpeg/gif/webp），URL 原样放行
- REPL 新增 `/image <路径|URL>`（附加到下一轮，`/image clear` 清空）、Tab 补全与 /help 同步
- 桌面端：📎 按钮原生文件对话框选图（可多选）、**粘贴图片路径自动附加**成 chip、
  发送时随消息发出；`chat` js_api 透传 images

### 工程

- 测试 127 → 142（notifications 3 / schedule 存储+端到端 4 / browser 4 / 多模态 4），
  全部离线（本地 http.server 当 webhook 与被抓页面；FakeLLM 验证多模态消息构造）
- ruff / mypy 保持双清零

## [未发布] —— 2026-09-26 大版本升级（A–F 六批次）

### 新增

**工具集（模型可用的工具 4 → 11 个）**
- `edit_file`：局部编辑（old→new 精确替换，默认要求恰好 1 处命中，避免改错位置）
- `search_text`：全工作区文本搜索（glob 过滤 / 大小写 / 正则，默认跳过 build、dist 等产物目录）
- `todo_write` / `todo_read`：多步任务的计划与进度
- `read_file` 支持 `offset`/`limit` 分页，`list_files` 有输出上限（默认 200 条）
- `run_command`（shell）支持 `cwd` / `env` / `max_output`，输出按行流式经 `emit_agent_event` 推给界面

**shell 权限门三级判定**（`permissions.shell=ask` 时）
1. 命令白名单 `permissions.shell_allow`（glob，如 `"git status"` / `"git log*"`）自动放行
2. 会话记忆：本进程内批准过的**同一条**命令不再重复询问（精确匹配，不做前缀推断）
3. 询问用户；无确认通道时 fail-closed 拒绝

外加审批审计日志（`<profile>/audit/shell.jsonl`，记录谁、何时、依据什么放行/拒绝了哪条命令）、
`/approvals` 命令与 `shell_permissions` 服务。`shell=deny` 优先级最高，白名单与记忆都绕不过。

**接入 nanoagent 现成能力（此前框架有、harness 没接）**
- 流式对话：`can_stream` / `ask_stream`（同步 `run_stream` 与异步 `arun_stream` 用后台线程 + queue
  统一成一个同步生成器）；REPL 逐字输出，工具调用插在中间
- `guardrails` 插件：从 config.json 的 `guardrails` 段构建输入/输出护栏（关键词 / 正则 / 长度）
- `tracing` 插件：按天写 JSONL 追踪，CLI 新增 `sha trace` 看落盘摘要
- `subagent` 插件：`spawn_subagent` 工具，子 agent 复用主 agent 的模型与工具，
  但剪掉 `spawn_subagent` / `switch_model`（防递归、防子任务改全局模型）
- `knowledge` 插件：`search_knowledge` / `index_knowledge`，基于 nanoagent.rag
- 插件安装支持 git/http(s)/ssh/file 地址（浅克隆 + 取唯一子目录 + 不带 `.git`）

**插件系统（架构层）**
- 插件依赖声明（plugin.json 的 `inject` 字段）与拓扑排序激活：依赖先于依赖者激活
- 服务冲突可见化：两个插件提供同名服务时记录 `service_conflicts` 并打 warning，不再静默覆盖
- 单插件热重载：`sha plugin reload <名>`（deactivate + mount + activate）
- 插件脚手架：`sha plugin new <名>` 生成可运行的最小插件骨架

**CLI**
- `sha model add|remove`、`sha sessions`、`sha trace`、`sha plugin new|reload`、`sha --version`
- `sha chat -s/--new` 强制新会话；`sha plugin add` 安装后立即校验激活

### 修复

- **REPL 的 `/new` 完全没生效**：只清了 agent 没换会话 id，历史被回放。现在真正换 id + 清 agent
- **`activate()` 成功后不清 `record.error`**：插件修好后状态里仍显示上次的错误
- **`_EventTracer.log` 首参名与 nanoagent 的 `kind=` 数据键撞车**：护栏拦截时抛
  `got multiple values for argument 'kind'`
- **`ask_stream` 从不落盘会话**：`run_stream` 只记内存，流式跑完关界面这轮就丢
- **`desktop.py` 用了 `sys.stderr` 却没 import sys**：config.json 损坏时本该打印的可读错误
  被 `NameError` 盖掉（ruff F821 暴露，已加回归测试复现）
- **`iter_files` 把 ValueError 直接抛给模型**：非法 glob 现在返回可读中文错误
- **`process.returncode` 为 None**：管道读到 EOF 不回填，现在显式 `wait()`
- **模型池逐条容错**：一个模型条目写错不再连累 models 插件整体 FAILED

### 工程

- **打包缺陷**：`harness/builtins/*/` 是无 `__init__.py` 的纯目录，wheel 里有 13 个
  `register.py` 却 **0 个** `plugin.json`——装到别处后内置插件全部丢清单。
  已加 package-data + MANIFEST.in，并给 CI 加「构建 wheel 并断言 13 份清单在包内」的回归
- ruff 全量清零（规则集钉死在 pyproject，含中文全角标点豁免）；`harness/py.typed`（PEP 561）
- CI 新增 lint（ruff，阻塞）与 mypy（advisory，continue-on-error）job
- 开发脚本收进 `tools/`（build_exe / run_tests / smoke_desktop / serve_ui_preview 等 7 个）
- 测试 52 → 112 项；路径监狱收敛到 `harness/workspace.py` 单一实现

## [0.1.0] —— 初始版本

「一切皆插件」的 agent harness：微内核（ctx.provide/on/effect + disposer）、
7 个内置插件（chat_loop / models / tools_fs / tools_shell / skills / mcp_client / repl）、
profile 隔离、pywebview 桌面端、`sha` CLI。
