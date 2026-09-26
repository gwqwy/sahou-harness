# 架构

本文回答三个问题：**核心抽象是什么**、**一个插件的一生怎么过**、**目录里各管什么**。
读完应该能不猜地写出第一个自己的插件。

参考原型是 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的
Cordis 微内核（"Everything is a Plugin"）；执行引擎（LLM 调用 / 工具执行 / 记忆）
不在本仓库，复用 [nanoagent](https://github.com/gwqwy/nanoagent)。

## 1. 微内核：ctx 上的三件事

`harness/kernel.py` 的 `Harness` 只做三件事，全部通过激活时注入的 `ctx` 暴露给插件：

| API | 作用 | 约束 |
|---|---|---|
| `ctx.provide(name, value)` | 注册一个**服务**（别的插件按名取用） | 同名服务冲突会被记录（见 §4），不静默覆盖 |
| `ctx.on(event, handler)` | 订阅内核事件 | 卸载时自动退订 |
| `ctx.effect(disposer)` | 声明**清理动作** | **必须**返回 disposer（无参可调用），内核保证调用 |

插件 = 一个 `register(ctx)` 函数 + 一份 `plugin.json` 清单。没有类继承，没有基类，
没有「插件类型」——会话服务、一组工具、一个 UI，都只是往 `ctx` 上挂东西的不同方式。

`effect` 是整个设计的支点：**激活路径上做过的每件事都要能在卸载时反着做掉**。
内核按 LIFO 调用 disposer（后激活的先卸载，依赖者的清理先于被依赖者），且幂等——
disposer 被调两次不能炸。忘了调 `ctx.effect` 的资源会在插件停用时泄漏，
这是插件作者要负的第一责任。

## 2. 插件生命周期

```
PENDING ──activate()──► ACTIVE ──deactivate()──► DISPOSED
   │                      │
   │ activate 抛错         │ reload(name) = deactivate → 重新 mount → activate
   ▼                      ▼
 FAILED ◄──── 修复后再次 activate，record.error 被清空
```

- **mount**：`loader.py` 从目录读 `plugin.json`，`exec` 其中的 `register.py`。
  内置插件直接挂源码目录（改动即时生效）；外部插件先复制进
  `<profile>/plugins/<名>/` 再挂载（所以改源码目录不影响已装副本——这是预期行为）。
- **activate**：建 `ctx` → 调 `register(ctx)` → 逐条执行 `ctx.effect` 注册的 disposer。
  中途抛错则立即 LIFO 回滚已注册的 effect，插件记 FAILED，`record.error` 存原因。
  **成功路径会清空 `record.error`**——修好后的重启激活不该背着上次的历史错误。
- **deactivate**：LIFO 调 disposer → DISPOSED。内核自身不缓存插件函数引用，
  `sys.modules` 里的旧副本会在重新 mount 时被换掉（热重载拿到的是新代码）。

## 3. 依赖拓扑

`plugin.json` 的 `inject` 字段（兼容 `deps` / `requires` / `dependencies` 别名）声明
「我依赖哪些插件」：

```json
{ "name": "repl", "inject": ["chat_loop"] }
```

`activation_order()` 做拓扑排序（DFS 三色标记）：

- **依赖先于依赖者激活**——repl 激活时 chat_loop 的服务已经就位；
- 缺失依赖记 `missing-dep`，已存在但没激活记 `dep-not-active`，都进 skipped 原因，
  **不阻塞**其他插件；
- 成环记 warning 不炸（列出环路径），按发现顺序激活。

设计取向：依赖是**便利**不是**契约**——插件应该在拿不到服务时优雅降级
（repl 在没有 chat_loop 时提示装一个），而不是把「依赖未就绪」当成致命错误。

## 4. 服务与冲突

服务就是命名值。`ctx.provide("shell_permissions", obj)` 之后，任何插件
`host.get_service("shell_permissions")` 都能拿到。

两个插件 provide 同名服务时，`_rebuild_services()` 会：

1. 两个都保留在 `owners` 里（后来者居前）；
2. 记入 `service_conflicts`（服务名 → 提供者列表），`sha status` 可见；
3. 打 warning——**不**抛错，因为「替换实现」本身就是合法用法（比如换掉内置 repl）。

## 5. 权限与安全

- **路径监狱**（`workspace.py`）：所有文件工具过 `safe_path`（解析后必须仍在工作区内，
  glob 模式过 `guard_pattern` 防止 `..` 展开），`iter_files` 统一跳过 build / dist /
  `__pycache__` 等产物目录并限量。**越界判定只在这一处实现**，插件不自查。
- **shell 权限门**：`shell=deny` 最高；`=allow` 直接放行；`=ask` 走三级判定
  （白名单 → 会话记忆 → 询问），无确认通道时 fail-closed。
  每次放行/拒绝追加审计 JSONL 到 `<profile>/audit/shell.jsonl`。
- **fail-closed 原则**：权限读取归一化在 `config.permission_mode`（唯一入口），
  非法取值收敛到最保守档。

## 6. 目录职责

```
harness/
  kernel.py     微内核：ctx、生命周期状态机、拓扑激活、服务表、reload
  loader.py     插件装载：读 plugin.json、exec register.py、manifest_deps
  config.py     profile 配置：模型池 / 权限 / 会话存取 / 插件安装（含 git）
  workspace.py  路径监狱（safe_path / guard_pattern / iter_files / read_text_file / safe_image）
  schedule_store.py 定时任务存储（tasks.json 读写/校验，scheduler 插件与 sha schedule 共用）
  cli.py        sha 命令行（组装运行时，无业务逻辑）
  desktop.py    pywebview 桌面端：DesktopApp 纯 API 层 + 单页 HTML 渲染层
  builtins/     16 个内置插件（每个一个目录：plugin.json + register.py）
  py.typed      PEP 561 标记

内置插件一览：
  chat_loop   对话循环（nanoagent Agent：流式 / 护栏 / 追踪 / 图片输入）
  models      模型池 + switch_model 工具（逐条容错，坏条目不连坐）
  tools_fs    文件读写 / 局部编辑 / 分页读（路径监狱）
  tools_search 全工作区文本搜索
  tools_shell Shell 执行（三级权限门 + 审计 + cwd/env/流式输出）
  tools_todo  待办读写
  skills      SKILL.md 渐进式披露
  mcp_client  MCP 服务器接入（stdio/SSE/HTTP）
  guardrails  输入/输出护栏（config 的 guardrails 段构建）
  tracing     按天 JSONL 追踪
  subagent   spawn_subagent（剪掉递归工具）
  knowledge   RAG 知识库（索引 / 检索）
  browser     fetch_url：抓网页转纯文本（可选域名白名单）
  notifications notify 工具：POST JSON 到 webhook（缺 url 则插件缺席）
  scheduler   定时任务：worker 线程到点用独立 agent 执行，结果落 scheduled/<名>.log
  repl        终端界面（本身也是插件，可整个换掉）
```

## 7. 写一个新插件

```bash
sha plugin new my-thing     # 生成骨架：plugin.json + register.py
```

```python
def register(ctx):
    # 1) 提供服务（可选）
    state = {"count": 0}
    ctx.provide("my_thing_state", state)

    # 2) 注册工具（可选，host.get_service("agent") 拿 chat_loop 的 agent）
    # 3) 声明清理（凡是有副作用的，都要有）
    def dispose():
        state.clear()
    ctx.effect(dispose)
```

三条纪律：有副作用必有 `ctx.effect`；不 own 路径检查（用 `workspace.safe_path`）；
拿不到服务就降级，别崩。
