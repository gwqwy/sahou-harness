# Changelog

本项目的所有显著变更都记录在此文件里。

格式依据 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，  
版本号依据 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)。  
类型：新增 / 变更 / 修复 / 工程。

## [未发布] —— 2026-09-28 第三十二批：工具参数别名兼容 + 子 agent 空转根治

### 修复

**子 agent「只发出工具调用、拿不到任何结果」（实测 17 秒 35 步空转）**

- 根因一（**真实缺陷，主对话同样受影响**）：参数名不匹配。
  `list_files` 的真实参数是 `pattern`，而模型最自然的叫法是 `path` / `glob` / `dir`
  → 每次调用直接 `TypeError: got an unexpected keyword argument`，
  模型拿不到任何清单（子 agent 自述「没有拿到任何返回结果」正是此症状）
- 根因二：子 agent 无人工确认通道，`shell=ask` 时 `run_command` 必被拒
  （「未获人工确认，已拒绝」），而它反复重试同一个工具
- 修复：
  1. **参数别名兼容层（内核级，所有插件受益）**：注册工具时包一层，
     把 path/file/filename/glob/dir/pattern、content/text、command/cmd 等
     常见别名归一成函数实际参数名；认不出的参数原样传递（保留可读报错）
  2. 子 agent 在 shell 权限不是 allow 时**不再挂载 shell 类工具**（带了也是空转）
  3. 子 agent 指令补充：工具被拒不要反复重试，改用其他工具或直接说明限制
  4. `subagents.jsonl` 记录每次工具调用的**结果摘要**（trace），
     结论为空时兜底说明里直接给出「首个失败：X → 原因」；
     面板逐条展示工具名 + 结果（失败的标红）

### 工程

- 新增 ToolParamAliasTests（list_files 用 path/glob、read_file 用 file、
  write_file 用 filename+text 全通过）；全量测试通过；ruff / mypy 双清零

## [未发布] —— 2026-09-28 第三十一批：输入排队 + 子 agent 修复与面板

### 新增

**输入排队（本轮未答完也能输入）**

- 会话正在回答时输入框照常可用：点发送即**入队**（按钮保持可点，显示「排队 N」徽标）
- 本轮 done 后**自动按序发送**队列消息，无缝续跑下一轮
- 队列按会话隔离：后台会话完成时其排队消息同样自动续跑；切换会话各自独立显示

**右侧「🤖 子agent」面板**

- 新增面板标签：列出每次子 agent 调用（任务、✓/✕、耗时），点击展开
  执行步骤、结论全文、子会话 id 与时间
- 调用实时可见：live 区显示「🤖 子 agent 开始/调用 X/完成（N 秒 · M 步）」步骤
- 面板开着时每轮结束自动刷新

### 修复

**子 agent 常返回「（子 agent 没有给出结论）」**

- 根因：子 agent 跑完连续工具调用后不产出文字（与主对话空回复同源）；
  且子 agent 没有 tracer，内部工具调用对界面完全不可见
- 修复：子 agent 增加 tracer（跨轮累计工具名 + 实时推送事件）；
  结论为空时自动补一轮「只总结、不许调工具」的收尾请求；
  仍为空则返回带工具清单的明确说明（不再是光秃秃的空结论）
- 每次调用写入 `profile/subagents.jsonl`（任务/耗时/步骤/结论），供面板与复盘

### 工程

- 新增 SubagentLogTests；jsdom 验证排队自动发送（第二条在 done 后自动派发）
  与子 agent 面板渲染；ruff / mypy 双清零

## [未发布] —— 2026-09-28 第三十批：工具调用进正文 + 空回复兜底

### 变更

**工具调用显示在正式回复里**

- 流式期间累积本轮工具调用（名称 + 结果首行摘要），收尾时作为
  「🔧 工具调用」区块写入正式回复正文（在思考折叠块之后、正文之前），
  不再只藏在「已深度思考」里面
- 区块内逐行显示：`🔧 write_file · 已写入 index.html (28 行)`，
  虚线分隔、弱化配色，正文主体不受干扰

**空回复兜底**

- 模型多轮工具后没给文字总结时（如达轮数上限），不再显示「（空回复）」，
  而是明确说明：有工具 →「本轮模型未返回文字总结。以下是执行的 N 步操作…」；
  无工具 →「模型没有返回内容…重发一次即可」
- 整段（非流式）路径同样兜底

### 工程

- jsdom 全链路验证：工具块渲染（含摘要）、兜底文案、思考块保留、零 JS 错误；
  ruff / mypy 双清零

## [未发布] —— 2026-09-28 第二十九批：文件预览全面扩展

### 新增

**工作区文件预览支持大部分格式**

- **PDF**：内嵌 iframe 渲染（WebView2 内置查看器，≤12MB）
- **音频 / 视频**：原生播放器（audio ≤16MB / video ≤48MB，data URL 直出）
- **HTML**：沙箱 iframe 渲染 + 一键「查看源码」切换
- **Office（OOXML）**：docx 提正文、xlsx 转 TSV 表格（首表 ≤300 行）、
  pptx 提逐页文本——零第三方依赖（zipfile + 正则解析 XML）
- **压缩包**：zip 列出条目清单（名称/大小，≤500 条）
- **二进制**：十六进制摘要（偏移 / hex / ASCII，前 2KB）
- 各类都有大小上限，超限时明确提示「用系统打开」（保留原兜底路径）
- 原有能力不变：图片（data URL）、Markdown 渲染、CSV/TSV 表格、代码高亮

### 修复

- `open_external` 在 macOS / Linux 分支缺少 `subprocess` 导入（F821，真实缺陷）
- 12 个类级扩展名集合补 `ClassVar` 注解（RUF012）
- 预览元信息行显示解析说明（如「Word 文档正文（无排版）」）

### 工程

- 新增 FilePreviewExtTests（docx/xlsx/pptx/zip/二进制/音视频 data URL/HTML）；
  ruff / mypy 双清零；JS 校验通过

## [未发布] —— 2026-09-28 第二十八批：多会话并行 + 拖拽排序 + 模型预设

### 新增

**多会话并行回答**

- 后端流式忙标志从全局单开关改为按会话集合（`_busy_sessions`）：
  会话 A 在回答时，会话 B 照常发送（同一会话仍拒绝双开）
- 事件流全部携带会话 id：非当前会话的增量/思考步骤不渲染到当前线程；
  后台会话完成时 toast 提示并刷新侧栏
- 前端发送状态按会话管理（busySessions）：切换会话时发送按钮即时恢复/禁用

**侧栏会话拖拽排序**

- 会话行可拖拽（仅同工作区分组内），松手即保存顺序
  （`sessions_meta.order`，置顶/完成标记的优先级不变）

**右侧标签页：拖拽重排 + 右键菜单**

- 标签页顺序改为动态渲染（`rp_tab_order` 随界面偏好持久化）
- 拖拽标签即可换位；右键标签弹出「◀ 左移 / ▶ 右移 / ✕ 隐藏」菜单

**内置模型 API 预设**

- 模型卡片新增「提供商预设」下拉：DeepSeek（对话/推理）、Moonshot Kimi、
  智谱 GLM、阿里百炼 Qwen、SiliconFlow、OpenAI、Anthropic——
  选中自动填 base_url + 模型 ID（顺带填名称），只需再填 API Key 即可保存

### 修复

**空用户消息气泡**

- 历史回放过滤空内容消息；add() 对空 user 内容显示「（空）」而非空白气泡

**设置页导航样式统一**

- 七个导航项统一等高（34px）、图标定宽对齐（.ic），选中/悬停态完全一致

### 工程

- 全量 180 条测试通过（新增：多会话并行互斥、排序持久化）；
  ruff / mypy 双清零；jsdom 全链路复现验证（发送/渲染/用量条/零错误）

## [未发布] —— 2026-09-27 第二十七批：修复消息渲染崩溃（吞消息/空白聊天区）

### 修复

**消息全部渲染失败：发送被吞、聊天区空白**

- 根因：第二十四批移除编辑按钮时，add() 里残留了一行旧代码
  `div.appendChild(acts);`——而 `acts` 已改为块内作用域变量，
  导致**每条消息渲染都抛 `ReferenceError: acts is not defined`**：
  启动回放历史时第一条消息就崩（聊天区空白、hero 被隐藏），
  发送时用户消息刚上屏就崩（看起来像「被吞」），发送按钮卡死
- 定位手段：jsdom + stub pywebview api 在 Node 里真实执行页面脚本，
  一发即中（`acts is not defined`）；修复后同环境验证全链路：
  历史回放 + 发送 + 流式 done 回推 = 4 条消息全部渲染、
  send 不卡死、用量条显示「输入/输出/缓存命中 0%」、__errs 为空
- 第二十六批的启动链隔离 + 全局 onerror 此时也验证了价值：
  错误被完整捕获并定位，而不是「莫名空白」

### 工程

- harness 全量测试通过；ruff / mypy 双清零；JS 校验通过；
  sha.exe / ShaDesktop.exe 重建

## [未发布] —— 2026-09-27 第二十六批：启动链错误隔离 + 全局错误显形

### 修复

**聊天区偶发「莫名空白」且无从排查**

- 启动链（状态加载 → 偏好恢复 → 会话历史 → 辅助对话）此前是串联 await，
  任何一步抛错都会中断后续所有步骤——聊天区整块空白，且没有任何提示
- 修复：
  - 启动链逐步隔离（`step()` 包装）：一步失败只影响那一步，其余照常执行，
    并在聊天区显示「启动步骤「X」失败: <具体错误>」
  - 新增全局 `window.onerror`：任何未捕获异常都会弹红色 toast 并记录到
    `window.__errs`——界面异常从此可见、可报告

### 工程

- 全量 178 条测试通过；ruff / mypy 双清零；JS 校验通过

## [未发布] —— 2026-09-27 第二十五批：会话 ↔ 工作区联动

### 修复

**继续旧会话时 agent 答成另一个工作区的文件**

- 根因（两个叠加）：
  1. 会话有工作区戳（`sessions_meta.ws`），但 `selectSession` / `chat` /
     `chat_stream` **从不联动切回**——工具的路径监狱动态读 `host.workspace`，
     打开旧会话后工具仍指向当前工作区，与回放进来的历史（属于另一个工程）错位
  2. 模型没现场列目录，直接凭回放历史里的旧清单作答
- 修复：
  - `chat` / `chat_stream` 发送前调用 `_ensure_session_workspace`：会话记录的
    工作区与当前不一致时**自动切回**（目录仍存在才切；工作区条随后刷新）
  - 系统提示词显式声明「当前工作区：<路径>」，并新增准则：
    回答目录/文件类问题必须现场调用 list_files/read_file 实际查看，
    禁止凭会话历史里的旧清单作答

### 工程

- 全量 178 条测试通过（新增跨工作区联动回归测试）；ruff / mypy 双清零

## [未发布] —— 2026-09-27 第二十四批：移除用户消息的编辑按钮

### 变更

- 按用户要求移除用户消息下方的「✎ 编辑」按钮（连同前端 editMessage 函数）——
  消息操作条至此只剩 bot 最后一条的「↻ 重新生成」
- `data-hi` 序号保留（会话搜索的跳转定位仍依赖它）；后端 `edit_message_resend`
  API 保留不动（仅前端不再有入口）

### 工程

- 全量 178 条测试通过；ruff / mypy 双清零；JS 校验通过

## [未发布] —— 2026-09-27 第二十三批：流式 usage 帧漏采修复 + 命中率常显

### 修复

**流式 usage 帧漏采（用量统计全 0 的真正根因）**

- 诊断依据：真实 profile 的 usage.jsonl——修复前所有行要么全 0 要么 estimated
- 根因：nanoagent 流式采集只认「**独立的无 choices 帧**」上的 usage（OpenAI 官方
  形态）；很多网关把 usage 挂在**最后一个带 choices 的帧**上，这种形态被
  `continue` 直接漏掉 → total_usage 恒为 0 → 用量条/统计页全 0
- 修复：`_consume_stream`（同步+异步）对每个事件都尝试读 usage（有
  prompt/completion 即采纳），两种形态通吃；`_usage_dict` 继续归一缓存字段

### 变更

**缓存命中率常显**

- 输入框下方用量条：当用量来自**真实 usage 帧**（非估算）时，缓存命中段
  始终显示——服务商没报缓存字段时明确显示「缓存命中 0%」，而不是藏起来；
  估算来源（部分端点完全不回 usage）则维持仅有数据才显示

### 工程

- nanoagent 328 条测试全绿；harness 178 条全绿；ruff / mypy 双清零

## [未发布] —— 2026-09-27 第二十二批：缓存命中率打通 + 生成速度 + 紧凑排版

### 新增

**输入框下方用量条升级**

- `输入 X · 输出 Y tokens · 缓存命中 Z% · N tok/s`——缓存命中率与生成速度
  并排展示；无对应数据时自动隐藏该段
- 生成速度：本轮 completion tokens ÷ 实际耗时（流式/整段双路径，0.3s 以下不计）
- 用量页移除两个「缓存命中率」卡（信息密度低），保留折线图里的缓存命中虚线

**nanoagent：缓存字段采集（方案②，跨仓库改动）**

- 新增 `_usage_dict()`：SDK usage 对象 → 统一用量字典，兼容 DeepSeek
  （`prompt_cache_hit_tokens`）与 OpenAI（`prompt_tokens_details.cached_tokens`）
  两套约定，未知字段经 model_extra 也能读到
- `_accumulate` 键扩为三项、两处 `total_usage` 初值补 `cached_tokens`、
  LLM/AsyncLLM 四个采集点全部改走 `_usage_dict`——`llm.total_usage`
  从此可直接算总命中率；harness 侧 `_extract_cached_tokens` 同步兼容
  `prompt_cache_hit_tokens`

### 修复

**模型回答上下间距过长（真正的根因）**

- `.msg.bot` 的 `white-space:pre-wrap` 会把 md() 渲染出的 HTML 源码换行也
  显示成空行，与段落 margin 叠加成大空隙；Markdown 正文容器改为
  `.md-body { white-space:normal }`（代码块 .fence 自带 pre 不受影响），
  并收紧行高 1.65、段距 6px、列表/标题/围栏间距

### 工程

- nanoagent 328 条测试全绿（`_usage_dict` 沿用既有风格，未动无关样式）；
  harness 178 条全绿；ruff / mypy 双清零；JS 校验通过

## [未发布] —— 2026-09-27 第二十一批：思考块恢复显示 + 用量估算兜底

### 变更

**思考过程恢复显示（撤销上一批的门控）**

- 「思考关」语义收敛为**请求不思考**（后端 enable_thinking=false 不变）；
  服务商忽略开关仍返回 reasoning 时，内容照常折叠展示——毕竟 token 已经花了，
  折叠卡片不碍眼且可回看。上一批「关=隐藏」的做法导致完成后完全看不到思考过程，
  与预期相反，已撤销

**用量统计估算兜底（修复 6 轮对话 0 tokens）**

- 根因：部分 OpenAI 兼容端点的流式响应**不回 usage 帧**（尽管已下发
  `stream_options.include_usage`），导致 usage.jsonl 全是 0、total_usage 恒为 0
- 修复：`_record_usage` 在真实用量缺失时改用 nanoagent `estimate_tokens`
  按上下文+回复**估算**并标记 `estimated: true`——统计不缺席，精度降级；
  ask 与 ask_stream 两条路径都接入
- `usage()` js_api：llm.total_usage 为零时兜底汇总 usage.jsonl **今日**记录，
  输入框下方的用量条（含缓存命中率，有真实缓存数据才显示）恢复有数

### 工程

- 全量 178 条测试通过；ruff / mypy 双清零

## [未发布] —— 2026-09-27 第二十批：移除消息上的复制按钮

### 变更

- 按用户要求移除每条消息下方的「复制」按钮（连同 copyText/fallbackCopy 辅助函数）——
  需要复制时直接选中文本即可；消息操作条仅保留用户消息「✎ 编辑」与
  bot 最后一条「↻ 重新生成」

### 工程

- 全量 178 条测试通过；ruff / mypy 双清零；JS 校验通过

## [未发布] —— 2026-09-27 第十九批：会话净化 + 缓存命中率 + 输入行美化

### 新增

**缓存命中率**

- `usage.jsonl` 与 llm.total_usage 防御式提取缓存命中 tokens
  （`cached_tokens` / `prompt_tokens_details.cached_tokens`，随服务商字段位置自适应）
- 输入框下方用量条升级为胶囊样式：`输入 X · 输出 Y tokens · 缓存命中 Z%`
  （无缓存数据时自动隐藏该段）
- 用量统计卡新增「近 30 天 缓存命中率」「本进程 缓存命中率」；
  折线图叠加缓存命中虚线（有数据才显示）

### 变更

**会话净化：操作提示不再写进消息会话**

- 新增顶部浮层 toast（2.6s 自动消失，错误红色标识）：
  计划模式开关、模型/思考级别切换、无状态重置、Bootstrap、导出失败、
  搜索无结果、剪贴板/拖拽提示、删除失败等 20 处操作反馈全部改走 toast，
  消息会话只保留真正的对话内容与对话级错误

**思考级别「关」时不再显示深度思考块**

- 部分服务商忽略 enable_thinking 仍返回 reasoning——前端按当前思考级别门控：
  「关」时消息不再渲染「🧠 已深度思考」卡片（辅助对话同样处理）

**输入行控件美化**

- 权限徽标 / 📋 计划 / 📎 / ⚡ / 侧栏 / … 等全部统一为等高胶囊（elev 底、
  悬停上浮）、下拉框自定义箭头 + 聚焦环；输入框下方用量条改为居中胶囊

### 工程

- 全量 178 条测试通过；ruff / mypy 双清零；JS 校验通过

## [未发布] —— 2026-09-27 修复：启动/工具调用时黑色控制台窗口闪现

### 修复

**每次进入软件闪一个 cmd 黑色弹窗**

- 根因：ShaDesktop 是 `--noconsole` 的 GUI 进程（无控制台），Windows 上此类进程
  用默认参数 spawn 控制台程序（git 等）时，系统会临时分配一个 conhost 窗口
  再销毁——表现为「黑窗弹一下就消失」
- 触发点：启动时 `status()` 构建 agent → `_recovery_context` 跑 `git log`（**每次启动必现**）；
  此外 agent 的 git/shell 工具、审查面板、远程推送、bootstrap、插件市场克隆等
  9 处 subprocess 调用点均有同样问题
- 修复：新增 `harness/procutil.py`（`CREATE_NO_WINDOW`，非 Windows 平台为 0），
  全部 9 处 `subprocess.run` / `Popen` 统一传入 `creationflags=CREATE_NO_WINDOW`——
  子进程照常运行、输出照常被管道捕获，只是不再显示窗口

### 工程

- 全量 178 条测试通过；ruff / mypy 双清零（30 个源文件）

## [未发布] —— 2026-09-27 计划模式 + 远程仓库入口可达性

### 新增

**计划模式（📋 按钮 / REPL `/plan`）**

- 输入行新增「📋 计划」开关（开启呈强调色高亮，随 `config.plan_mode` 持久化）：
  开启后 agent 收到实现类任务时**必须先输出实现计划**——目标与验收标准、
  编号步骤清单、涉及文件、风险与待拍板项——**等用户确认后才动手**；
  确认后先 `todo_write` 写入 TODO.md 再逐个原子任务实现
- 切换立即重建 agent 生效（记忆按会话回放，上下文不丢）
- REPL 同步支持 `/plan`（切换）、`/plan on`、`/plan off`

**远程仓库入口可达性（用户反馈 exe 里找不到）**

- 设置 → 通用 → 数据新增「🐙 Git 远程仓库」行：点「配置」自动关闭设置、
  找回（若被隐藏）并跳转到「🔍 审查」标签页、聚焦远程地址输入框
- 审查面板即远程地址与推送的所在地：输入 origin（https/ssh/git@）→ 保存 → ⇅ 推送

### 工程

- 全量 178 条测试通过（新增 2：计划模式注入与开关、js_api/status 往返）；
  ruff / mypy 双清零

## [未发布] —— 2026-09-27 长程任务修订：git 提交归用户 + 远程仓库配置

### 变更

**agent 提交默认关闭（F4 修订：提交/推送由用户手动完成）**

- `git_add` / `git_commit` 默认对 agent 拒绝：返回「提交由用户手动完成，
  agent 不代劳，请把建议的提交信息告诉用户」——即使 `permissions.git=allow`；
  `permissions.git=deny` 优先级仍最高
- 开放方式：`config.git.allow_agent_commit = true`（开放后依旧要过权限门 + 测试门）
- 长程准则同步修订：测试全绿后**报告建议的提交信息**，不自行提交
- 只读工具（status/diff/log）保留给 agent；bootstrap 基线提交、无状态重置等
  用户手动触发的功能不受影响

**远程仓库地址配置（审查面板）**

- 「🔍 审查」面板新增远程地址行：输入 origin URL（https:// / ssh:// / git@）→ 保存
  （自动判断添加或更新）；加载审查时回显当前 origin
- 「⇅ 推送」按钮：确认后 `git push -u origin HEAD`（当前分支推到 origin，
  输出回显在面板；**用户手动点击，agent 无 push 能力**）
- js_api：`git_remote_get` / `git_remote_set` / `git_push`

### 工程

- 全量 176 条测试通过（新增：默认拒绝提交/allow 下仍拒绝、远程 set/get/无 origin 推送拒绝）；
  ruff / mypy 双清零

## [未发布] —— 2026-09-27 长程任务第三批（收尾）

### 变更

**「显示已完成会话」开关（F8 强化）**

- 设置 → 通用 → 界面开关新增一项：默认隐藏已完成的会话（当前会话除外），
  开启后侧栏显示全部；随 `ui_prefs.show_done` 持久化

**打包冒烟验证**

- 临时 profile 实测 `sha status`：`tools_git [ACTIVE]`（5 工具 + /git 命令）随
  onefile 产物正常激活，内置插件 17→18 个
- `docs/长程任务架构-功能规划.md` 已标注 F1~F8 全部实施完成

### 工程

- 全量 174 条测试通过；ruff / mypy 双清零

## [未发布] —— 2026-09-27 长程任务架构第二批（P1：F4/F5 + P2：F7/F8）

### 新增

**F4：受限 Git 工具（`tools_git` 插件，Git 即时提交固化）**

- 只暴露五个白名单操作，**没有 push / reset / checkout**：
  - 只读：`git_status` / `git_diff` / `git_log`（`permissions.git=deny` 时连读也拒绝）
  - 写操作：`git_add` / `git_commit`——走新权限门 `permissions.git = ask / allow / deny`
    （ask 经 host.confirm，无确认通道 fail-closed；权限菜单新增「GIT 提交」分区）
- **提交纪律（Agentic TDD 工具化）**：`config.git.test_command` 非空时 commit 前必须
  跑绿（失败回显输出尾部并拒绝提交）；先查暂存区再询问，暂存为空不打扰；
  commit 只提交已暂存内容，不偷跑 `-a`
- CLI：`/git` 命令查看权限与测试门配置

**F5：`/bootstrap` 工程脚手架（Bootstrap 工作流前 4 步）**

- 新模块 `harness/bootstrap.py`（CLI 与桌面端共用）：`src/tests/docs` 目录规范、
  `.gitignore`、README（有目标时）、git init + 基线提交——**只增量创建，绝不覆盖**
- CLI：`sha bootstrap [--goal "工程目标"]`（带目标时再让 agent 拆解原子任务写入 TODO.md）
- 桌面端：设置 → 通用 → 数据 → 「🚀 初始化工程」，目标弹窗一键完成

**F7：子代理只读模式（串行优于并发）**

- `config.subagent.readonly = true` 时，`spawn_subagent` 的工具集剥离
  write_file / edit_file / run_command / git_add / git_commit——子代理只读不旁路写码

**F8：会话完成标记（验收后销毁会话的纪律配套）**

- 会话行新增 ✓ 按钮：标记完成（划线、排分组最后）；`mark_session_done` js_api

### 修复

- `_normalize_permissions` 白名单漏掉新键 `git` 会在每次保存时**静默丢弃**该权限
  （H-07 同类问题，条目 docstring 已警示过）——已把 `git` 纳入归一列表

### 工程

- 全量 174 条测试通过（新增 6：git 只读/提交流/测试门/deny、bootstrap 增量幂等、
  子代理只读过滤）；ruff / mypy 双清零（29 个源文件）

## [未发布] —— 2026-09-27 长程任务架构第一批（P0：F1/F2/F3/F6）

依据 `docs/长程任务架构-功能规划.md`（F1~F8 规划）落地第一批。

### 新增

**F1：TODO.md 工作区任务索引（环境即状态的地基）**

- `tools_todo` 从**纯内存态**重写为读写 `<工作区>/TODO.md`（Markdown 勾选格式
  `- [ ] / - [~] / - [x] / - [-]`）——人可读、Git 可跟踪、跨会话/跨进程不丢；
  渲染格式与既有 `[ ] 1. 标题` 兼容；容忍手写文件（容错解析）
- 桌面端右侧新增「✅ 任务」标签页：按状态渲染 TODO.md（待办/进行中/已完成样式），
  提示如何让 agent 生成清单

**F2：长程执行准则注入系统提示词**

- chat_loop 系统提示词追加三原则：原子任务最小粒度、串行推进（先读 TODO/git 再动手）、
  TDD 默认失败、测试绿才提交、进度写 TODO.md
- `config.longrun.guidelines = false` 可关闭

**F3：新会话自动恢复上下文**

- 新会话构建 agent 时自动注入工程状态：TODO.md 未完成项 + 最近 5 条 git log
  （UTF-8/GBK 双解码兜底；TODO 缺失 / 非 git 仓库静默跳过；两者皆空不注入）
- `config.longrun.recovery = false` 可关闭

**F6：一键无状态重置（水位闭环）**

- 「…」上下文卡片：水位 ≥80% 红色警示；卡片底部新增「⟳ 重置」——
  请 agent 把当前进展总结写回 TODO.md（todo_write）→ 自动开全新会话（F3 自动恢复）；
  旧会话保留可回看
- js_api `stateless_reset`

### 工程

- 全量 168 条测试通过（新增 5：todo 落盘/手写解析、准则+恢复注入/可关闭、
  todo_content/stateless_reset）；ruff / mypy 双清零；JS 校验通过

## [未发布] —— 2026-09-27 桌面端第十二批：修复裸 `sha` 启动崩溃

### 修复

**裸敲 `sha` 直接报 AttributeError: 'Namespace' object has no attribute 'message'**

- 根因：`command = args.command or "chat"` 让裸命令默认走 chat 分支，
  但 `message` / `session` / `new` 三个字段只定义在 chat **子命令**解析器上——
  裸 `sha` 的顶层 Namespace 没有这些属性，`args.message` 直接抛 AttributeError，
  用户只看到一行裸异常，进不了 REPL
- 修复：chat 调用点改用 `getattr(args, "message", None)` 等兜默认值；
  新增回归测试（mock build_runtime：裸 `sha` 必须以 (None, None, False) 进入 REPL，
  `sha chat -m "..."` 参数透传不变）

### 工程

- 全量 163 条测试通过；ruff / mypy 双清零；sha.exe / ShaDesktop.exe 已重建

## [未发布] —— 2026-09-27 桌面端第十一批：窄宽保护 + 过滤 / 快捷键 / 图片预览

### 修复

**拖窄侧栏时输入行被挤成竖排文字**

- 根因：composer 工具行允许 flex 收缩，权限徽标「⌘ 命令允许 · 写入允许」等长文本
  被压到一字宽后逐字换行，出现竖排
- 修复：`.composer-row` 禁止换行（nowrap），所有按钮 `flex:none` 且不折行；
  仅权限徽标与模型/思考下拉允许收缩（收缩时省略号），最窄保留 70px

### 新增

**侧栏会话过滤框**
- 「项目」列表上方新增过滤输入：按会话名 / id 即时过滤；
  无匹配的分组整组隐藏，全部无匹配给出提示；清空即恢复

**全局快捷键**（通用页新增「快捷键」说明卡）
- `Ctrl+N` 新会话 · `Ctrl+F` 搜索会话 · `Ctrl+,` 打开设置 ·
  `Ctrl+B` 显隐左侧栏 · `Ctrl+J` 显隐右侧面板

**图片点击预览**
- 附加图片 chips 可点击：lightbox 全屏放大（点击任意处关闭）；
  data URL（剪贴板粘贴）直接显示，本地路径经新增 js_api `image_preview`
  读取（走工作区路径监狱，≤15MB）

### 工程

- 48 条桌面端测试全绿（新增 image_preview：data URL 往返 / 越狱拒绝 / 不存在拒绝）；
  ruff / mypy 双清零

## [未发布] —— 2026-09-27 桌面端第十批：设置页两栏化

### 变更

**设置页改为「左侧导航 + 右侧内容」两栏布局**（对齐 WorkBuddy 设置页风格）

- 左侧导航分两组：**能力**（🧩 模型 / 🔌 插件 / 📚 技能 / 🛒 市场）、
  **界面**（🎨 外观 / ⚙ 通用 / 📈 用量）；当前项高亮（accent-soft 底）
- 右侧内容区独立滚动，每页带标题；条目统一为「名称 + 描述 + 控件」的卡片行
- 原「通用」拆分：**外观**（主题分段按钮 / 强调色色板+自定义 / 字体）与
  **通用**（5 个界面开关行 + 数据管理行：搜索会话 / 导出当前 / 导出全部）——
  数据动作从三个按钮升级为带说明的设置行
- 弹窗加宽至 min(880px, 94vw)，关闭按钮固定右上角；顶部标签行移除
- `switchTab` 改按 `.set-nav-item` 与 pane id 表驱动切换；渲染函数拆分
  `renderAppearance()` / `renderGeneral()`

### 修复

**切换设置页时弹窗大小跳变**

- 根因：弹窗高度由当前页内容决定，通用（内容少）与插件（内容多）两页尺寸不同，
  每次切换都会跳变
- 修复：`#modal.settings` 固定为 `height:min(720px, 88vh)`，所有设置页统一尺寸，
  内容区自行滚动（对齐参考的 WorkBuddy 固定尺寸设置窗口）

### 工程

- 47 条桌面端测试全绿，ruff / mypy 双清零，JS 经 node --check 与引用一致性校验

## [未发布] —— 2026-09-27 桌面端第九批：设置页面整合

### 变更

**设置页面整合（左下角功能全部移入）**
- 设置弹窗新增「通用」标签页，原侧栏左下角的入口全部收拢：
  - **外观**：深色 / 浅色 / 跟随系统（分段按钮，原「🌙 外观」按钮）
  - **强调色**：6 个预设色板 + 自定义 #RRGGBB 输入（原「🎨 主题色」对话框）
  - **界面字体**：输入即应用，留空恢复默认
  - **界面开关**：工作区选择条 / 右侧面板 / 左侧栏 / 完成提示音 / 窗口通知
    （原 📁 工作区、◧ 面板按钮与「…」卡片内的提醒开关）
  - **数据**：🔍 搜索会话 / 📤 导出当前会话 / 🗂 导出全部 zip
    （执行前自动关闭弹窗，不遮挡跳转与提示）
- 侧栏左下角只保留「⚙ 设置」与状态提示，界面更干净
- 删除 `cycleTheme` / `accentFlow`（职责并入设置页）；清理 `themeBtn` / `wsBarToggle` /
  `panelToggle` 的失效引用

**设置页新增 token 用量统计**
- 「用量」标签页：四张统计卡（今日 tokens·轮次 / 近 30 天 tokens / 近 30 天 输入·输出 /
  本进程累计）+ 复用右侧「📈 用量」同源的 30 天 SVG 折线
  （图表渲染重构为 `renderUsageChart(pane, pts)`，两处共用）

### 修复

**设置页按钮的失效引用**：侧栏按钮移除后 `dismissWsBar` / `togglePanel` /
`showHeroIfEmpty` 对已删元素的直接访问会抛 TypeError——全部清理或加守卫。

### 工程

- 47 条桌面端测试全绿，ruff / mypy 双清零，JS 经 node --check 与引用一致性校验

## [未发布] —— 2026-09-27 桌面端第八批：粘贴图片与排版修复

### 修复

**输入框粘贴出现「@image#1:Clipboard_Screenshot.png」占位文本**

- 根因：外部聊天工具（WorkBuddy）把剪贴板截图表示为 `@image#N:文件名` 的内部引用文本，
  旧粘贴处理只认「以 .png 结尾的路径」，引用串校验失败后原样留在输入框
- 修复：粘贴处理重写为三段——① 剪贴板里是**图片本体**（截图/复制图片）→ 读成 data URL
  直接附加为多模态图片（`safe_image` 对 data: URL 原样放行，无需临时文件，上限 8MB）；
  ② 文本是 `@image#N:` 引用 → 给出可读提示（请直接 Ctrl+V 图片或用 📎）；
  ③ 文本是真实图片路径 → 走既有校验附加。附件 chips 与用户气泡的 meta
  对 data URL 显示「剪贴板图片」标签，不再泄漏整串 base64

**回复排版松散（段落间大空隙）**

- 根因：`md()` 输出的 `<p>` 带浏览器默认 1em 上下边距，叠加 1.75 行高后
  段落间空隙接近两行，视觉松散
- 修复：`.msg.bot p` 收为单向 `margin-bottom:10px`（末段不留尾空隙），
  列表 4/10px、列表项 3px，与流式区（第七批已修）排版一致

### 工程

- 纯前端改动（desktop.py 的 UI_HTML 常量）；47 条桌面端测试全绿，ruff / mypy 双清零

## [未发布] —— 2026-09-27 桌面端第七批：流式输出优化与思考样式美化

### 变更

**流式输出优化**
- **rAF 批量渲染**：逐 token 的 delta 事件只做字符串拼接，每个动画帧最多整体重绘一次，
  不再每 token 触发一次 layout（长回复的 DOM 压力从 O(n²) 降到 O(n)）
- **流式期间实时 Markdown 渲染**：生成中即显示标题/列表/粗体/代码块
  （`md()` 本就容忍未闭合围栏），与最终渲染视觉一致，不再「先纯文本后跳变成 Markdown」
- **智能吸底滚动**：流式增量与工具步骤只在用户停在底部附近时自动滚动，
  向上翻阅历史不再被强行拉回；新消息仍始终滚到底
- **生成中光标**：流式内容末尾显示主题色闪烁光标（`▍`），生成结束随 live 块消失

**模型思考样式美化**
- 「🧠 已深度思考」重构为可交互卡片：▶ 旋转指示展开态、悬停高亮（accent-soft 底）、
  多轮思考显示轮数（如「已深度思考（3 轮）」）、展开体淡入动画 + 双色分区（elev 头 / panel 体）
- 思考中三点动画从原地闪烁改为波浪跳动（错峰位移 + 透明度），更接近「正在工作」的隐喻
- 进行中的工具步骤标题着强调色，与已完成步骤形成状态对比

### 工程

- 纯前端改动（desktop.py 的 UI_HTML 常量）；47 条桌面端测试全绿，ruff / mypy 双清零，
  JS 经 node --check 与 id/onclick 引用一致性校验

## [未发布] —— 2026-09-27 桌面端第六批：十一项功能

### 新增

**会话搜索（当前会话 / 全部历史）**
- 侧栏「🔍 搜索会话」：关键词搜索全部历史，选范围 → 命中列表 → 跳转滚动并高亮；
  后端 `search_sessions(query, scope)`，命中项含过滤后序号（与界面 data-hi 对应）

**重新生成 / 编辑消息**
- bot 消息悬停出现「↻ 重新生成」（仅最后一条可见）：丢弃最后一轮重跑
- 用户消息悬停出现「✎ 编辑」：改写后从该处截断重发；
  后端 `regenerate_last` / `edit_message_resend`，截断后强制按磁盘历史重建 agent，
  避免文件改了、内存上下文还是旧的

**会话置顶/收藏**
- 会话行悬停出现 ★：置顶后排在本工作区分组最前（`sessions_meta.pinned`，持久化）

**拖拽文件入窗**
- 图片 → 走既有校验链附加为多模态附件；文本/代码（200KB 内）→ 内容插入输入框；
  其他类型给出跳过提示；全屏拖拽遮罩提示

**用量图表**
- 右侧新「📈 用量」标签页：usage.jsonl 按天聚合近 30 天 tokens，SVG 折线 + 面积，
  悬停圆点看当天入/出/轮次明细；空档天自动补齐保证横轴连续

**完成提示音 / 窗口通知**
- 「…」上下文卡片底部新增开关：回复完成提示音（WebAudio 蜂鸣，无需音频文件）、
  最小化时的系统通知（Notification API，按需申请权限）；状态随 ui_prefs 持久化

**知识库细粒度管理**
- 「📚 知识库」面板按来源文件列出片段数，可单独删除某来源（其余保留、索引即时落盘）；
  knowledge 插件新增 `sources` / `remove_source` 服务

**导出全部会话 zip**
- 侧栏「🗂 导出全部」：每会话一个 Markdown + index.md 目录打包为
  `sessions-<时间戳>.zip`（`<工作区>/exports`）；CLI 新增 `sha export --all`

**浏览器标签多开**
- 「🌐 浏览器」支持多标签：＋ 新开、✕ 关闭、各自独立 iframe，地址栏跟随激活标签

**自定义强调色 / 字体**
- 侧栏「🎨 主题色」：预设色板（天蓝/翠绿/暖橙/玫红/紫）+ 自定义 #RRGGBB + 设置/恢复字体；
  派生 --accent-2/grad/soft/ring/glow 全套变量，随 ui_prefs + localStorage 双通道持久化

**快捷指令库 /snip**
- 输入行新增「⚡」：常用提示词片段一键插入输入框、把当前输入存为指令、删除管理
  （profile config 的 `snippets` 键，新模块 `harness/snippets.py`）
- REPL 同步支持：`/snip` 列出、`/snip add <名> <内容>`、`/snip del <名>`、
  `/snip <名>` 直接发送片段；有 readline 时可 Tab 补全

### 工程

- 测试 161 全绿（新增 8 条：置顶排序 / 搜索 / 重新生成 / 编辑重发 / 导出全部+CLI /
  用量聚合 / 快捷指令往返 / 知识库来源管理）；ruff / mypy 保持双清零
- exporter 重构：抽出 `session_markdown`（纯函数，不落盘）供单会话导出与 zip 打包共用

## [未发布] —— 2026-09-27 桌面端第五批反馈

### 新增

**左侧会话栏可隐藏**
- 输入框工具行新增「◧ 侧栏」按钮：一键隐藏/恢复左侧栏（隐藏时拖拽手柄一并收起），
  状态记忆到 `ui_prefs.side_hidden`，重启后保持

## [未发布] —— 2026-09-27 桌面端第四批反馈修复

### 修复

**暗色模式下右侧「🌐 浏览器」一大块白色**

- 根因：内嵌 iframe 硬编码 `background:#fff` 且加载前常驻显示（about:blank 也是白）  
- 修复：未加载网页时隐藏 iframe，显示主题色占位提示；iframe 背景改随主题变量  
  （`var(--bg)`）；全文件扫描确认无其它硬编码浅色残留

### 工程

- 测试 153 全绿；ruff / mypy 保持双清零

## [未发布] —— 2026-09-27 桌面端第三批反馈修复

### 修复

**「找回标签页」弹层铺满屏幕**

- 改为在**标签栏内弹出下拉菜单**（锚定「＋」按钮下方），点选即恢复；  
  点面板其它位置自动收起；没有隐藏标签时「＋」呈半透明禁用态

**侧栏出现两个聚焦**

- 工作区头部的强调色竖条与当前会话行的强调样式同时出现，视觉上像两个焦点
- 现在焦点唯一：只有当前会话行带强调色，工作区头部仅加粗区分

### 变更

- 样式打磨：对话框与各弹出菜单加出现动画（淡入+上移），侧栏按钮/标签页/小按钮  
  统一 hover 过渡

### 工程

- 测试 153 全绿；ruff / mypy 保持双清零

## [未发布] —— 2026-09-27 桌面端第二批反馈修复（五项）

### 修复

**思考模式关了仍在深度思考**

- 根因：`off` 之前只是**不下发** `reasoning_effort`，把决留给服务端默认——  
  不少 OpenAI 兼容端点（Qwen/DashScope/vLLM 等）默认开思考
- 修复：`off` 现在显式下发 `enable_thinking=false`（nanoagent LLM 同步/异步两路 +  
  models 插件启动 + 桌面端 set_thinking + 热重建全链路）；  
  注意 DeepSeek 官方 reasoner 模型（R1）本身恒开思考，与本设置无关

**切换工作区后 composer 显示与右侧文件树不联动**

- 侧栏工作区组**点击名称即切换**（原先点击只是折叠会话，折叠移到 ⌄ 按钮）；  
  当前工作区有高亮竖条标识
- 新增统一入口 `switchWorkspace()`：切换后同步刷新 composer 上方的工作区名、  
  侧栏分组高亮、右侧文件树（开着才重载）；composer 下拉 / 原生对话框 /  
  新会话向导三条路径全部收敛到它

**会话消息不支持 Markdown**

- `md()` 渲染器升级：标题（#/##/###）、无序/有序列表、引用块、分隔线、  
  围栏代码块、行内代码、加粗/斜体、http(s) 链接（`javascript:` 等一律按纯文本展示）
- Node 提取单测验证渲染与转义安全

### 新增

**左侧栏拖拽手柄**：侧栏与主区之间可拖拽调宽（180~460px），记忆到 `ui_prefs.side_width`

**右侧标签页可隐藏/找回**

- 标签悬停出现 ✕ 可隐藏该标签页；标签栏末尾新增「＋」，点开列出所有隐藏标签页供找回
- 显隐状态记忆到 `ui_prefs.rp_hidden`

### 工程

- 测试 153 全绿（thinking off 显式下发断言更新）；ruff / mypy 保持双清零

## [未发布] —— 2026-09-27 修复：新建会话的工作区选择与裸名会话

### 修复

**新建会话出现 s-20260927-… 裸名会话**

- 根因：`dialogPrompt` 点「取消」返回 `false`，而 `newSessionFlow` 判断的是  
  `name === null` —— 判断失效，**每次取消都会先创建会话再抛异常中断重命名**，  
  留下一个裸 id 会话；「确定」但名字留空同理（"" 直接创建、跳过重命名）
- 修复：取消现在正确返回 `null`；名称取消或留空都**不创建**会话；  
  对话框语义与所有调用方（重命名工作区/会话）一并对齐

**新建会话时无法选择工作区**

- 原流程没有任何工作区选择步骤，会话被静默归入当前工作区
- 新流程：`＋ 新会话` 先弹**工作区选择对话框**（已注册工作区列表 + 当前标记 ✓ +  
  「📂 选择其他文件夹…」原生目录对话框），再命名；会话按选定的工作区归组

### 变更

- 新会话默认名兜底为可读的「会话 月-日 时:分」，不再是裸 id；  
  **历史遗留**的裸 id 会话在侧栏显示为「会话 …」/「未命名会话」（可重命名或 🗑 删除）
- 新增通用 `dialogChoose` 列表选择对话框（含条目样式；与输入/确认对话框互斥防叠层）

### 工程

- 测试 152 → 153（新会话默认名 + 历史裸名显示兜底）
- ruff / mypy 保持双清零

## [未发布] —— 2026-09-27 桌面端体验迭代

### 新增

**会话删除 + 新会话向导（修复裸名会话 / 无法选工作区）**

- 侧栏会话行悬停出现 🗑 按钮：确认对话框（不可恢复提示）后删除会话文件与命名元数据；  
  删除当前会话时自动切换到最近的剩余会话（没有则新建）
- `Profile.delete_session`：文件定位走与 load/save 相同的 id 白名单（H-04），拒绝路径穿越
- **只有名字、还没有消息的会话也能删**——「新会话」正是这种状态，此前完全删不掉
- `dialogPrompt` 取消返回 null（原先返回 false 挡不住 `=== null` 判断，  
  **每次点取消都会先创建出一个 s-... 裸名会话再中断重命名**）
- 新会话向导：先弹 `dialogChoose` 选工作区（列表 + ✓ 当前 + 「📂 选择其他文件夹…」）再命名；  
  默认名兜底为「会话 月-日 时:分」，历史裸 id 显示为「会话 …/未命名会话」（`_display_name`）
- 新增 `delete_session` js_api

**桌面端知识库面板（📚 知识库）**

- 右侧面板新标签页：片段数 / 向量后端 / Embedding 模型 / 索引目录一目了然
- 「📂 索引文件夹…」：原生对话框选目录，把其中文本文件向量化入库  
  （md/txt/py/json/yaml/rst/csv/sahou 等，上限 200 个文件）；  
  所选目录是用户亲自指定的，允许在工作区外（agent 工具仍受路径监狱约束，不放宽）
- 「🗑 清空」：删除持久化索引文件（确认对话框）
- knowledge 插件新增 `host.service("knowledge")` 宿主服务  
  （status / index / index_absolute / clear），桌面端通过它消费，逻辑不重复；  
  状态展示**不强制构建 KB**（没配模型也能看库状态）

**右侧面板可调宽 + 界面偏好持久化**

- 右侧面板与主区之间新增拖拽手柄（260~760px，随窗口自适应），宽度记忆到  
  `config.ui_prefs.rp_width`（localStorage 作快路径，后端 config 兜底——pywebview  
  的 `html=` 模式下 localStorage 可能因 opaque origin 不可用，故双通道）
- 输入框上方的「工作区选择条」可点 ✕ 隐藏；左侧栏新增「📁 工作区」按钮随时找回，  
  状态存 `ui_prefs.wsbar_hidden`；隐藏后新会话不再自动弹出
- 新增 `get_ui_prefs` / `set_ui_prefs` js_api

**桌面端会话导出（📤 导出会话）**

- 左侧栏一键把当前会话导出为 Markdown → `<工作区>/exports/<会话>.md`（含 token 用量附录）
- 导出实现抽成 `harness/exporter.py`，与 CLI `sha export` 共用（消除重复实现）

**定时任务暂停/恢复**

- `sha schedule pause <名>` / `resume <名>`：只翻 `enabled` 标志、不动 `last_run`，  
  对运行中的 harness 立即生效（worker 每轮重读盘）

### 变更

- 右侧面板标签页文字防溢出：横向滚动替代裁切（窄面板下六个标签不再被截断）

### 工程

- 测试 142 → 152（ui_prefs 2 / 桌面导出 1 / exporter 1 / pause-resume 2 /  
  会话删除 1 / 知识库服务 1 及配套）
- ruff / mypy 保持双清零

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
