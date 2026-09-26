"""UI 预览服务：注入 mock pywebview.api，供浏览器截图检查桌面端视觉（仅开发用）。"""
import http.server
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness.desktop import UI_HTML

MOCK = """
const NOW = Date.now() / 1000;
// 有状态 mock：切换主题/工作区后 status 返回新值，行为与真实应用一致
const state = {
  theme: 'light',
  wsCurrent: 'ws-1', wsName: '我的项目', wsPath: 'E:\\\\demo\\\\agent',
  workspaces: [
    { id:'ws-1', name:'我的项目', path:'E:\\\\demo\\\\agent' },
    { id:'ws-2', name:'shanhe-os', path:'E:\\\\demo\\\\os' },
  ],
  shell: 'ask', fs: 'allow', thinking: 'medium', model: 'deepseek',
};
window.pywebview = { api: {
  status: async () => ({ ok:true, model:state.model, models:['deepseek','gpt-4o','claude-sonnet'],
    profile:'C:\\\\Users\\\\find\\\\.sahou-harness\\\\profiles\\\\default',
    workspace:state.wsPath, workspace_name:state.wsName,
    session:'default', session_name:'当前会话',
    shell_permission:state.shell, fs_permission:state.fs,
    thinking_level:state.thinking, theme:state.theme, context_window:128000,
    usage:{prompt_tokens:45231, completion_tokens:6789} }),
  set_permission: async (k,m) => ({ ok:true, kind:k, mode:m }),
  workspaces: async () => ({ ok:true, current:state.wsCurrent, workspaces:state.workspaces }),
  rename_workspace: async (n) => ({ ok:true, name:n }),
  sessions: async () => ({ ok:true, current:'default', sessions:[
    { id:'default', name:'agent v0.0.1 重构', ws:'ws-1', updated:NOW-420 },
    { id:'s2', name:'修 README', ws:'ws-1', updated:NOW-86400 },
    { id:'s3', name:'内核调试', ws:'ws-2', updated:NOW-86400*3 },
  ]}),
  rename_session: async (id,n) => ({ ok:true, session:id, name:n }),
  new_session: async () => ({ ok:true, session:'s-new' }),
  session_history: async () => ({ ok:true, session:'default', history:[
    { role:'user', content:'帮我看一下这个仓库的结构，然后写个 README' },
    { role:'assistant', content:'已查看仓库结构：`src/` 下有 3 个模块，`tests/` 有 12 个用例。\\n\\n**README.md 已写好**，包含安装、快速开始与架构说明。' },
  ]}),
  get_models: async () => ({ ok:true, default_model:'deepseek', models:[
    { name:'deepseek', provider:'openai', base_url:'https://api.deepseek.com/v1',
      model:'deepseek-chat', api_key:'sk-xxx' },
    { name:'claude-sonnet', provider:'anthropic', base_url:'', model:'claude-sonnet-4',
      api_key:'' },
  ]}),
  save_models: async () => ({ ok:true, rebuild_error:'' }),
  plugins: async () => ({ ok:true, plugins:[
    { name:'chat_loop', state:'ACTIVE', provided:{services:['agent_factory','ask']} },
    { name:'models', state:'ACTIVE', provided:{tools:['list_models','switch_model']} },
    { name:'tools_shell', state:'ACTIVE', provided:{tools:['run_command']} },
    { name:'mcp_client', state:'FAILED', provided:{}, error:'connect timeout' },
  ]}),
  remove_plugin: async () => ({ ok:true }),
  install_plugin: async () => ({ ok:true, name:'demo', note:'已热激活' }),
  chat: async () => ({ ok:true, model:'deepseek', tool_calls:[{name:'list_files'}],
    reasoning:'先分析目录结构：src 下有三个模块……\\n再检查 tests 覆盖情况，决定 README 章节安排。\\n最后核对安装说明中的 Python 版本要求。',
    reply:'**README.md 已写好**。仓库结构：`src/` 下有 3 个模块，`tests/` 有 12 个用例。',
    usage:{prompt_tokens:1234, completion_tokens:567}, session:'default' }),
  usage: async () => ({ ok:true, usage:{prompt_tokens:45231, completion_tokens:6789} }),
  context_usage: async () => ({ ok:true, used:143360, window:1000000, percent:14.4,
    breakdown:[
      { name:'消息', tokens:127000, percent:88.6 },
      { name:'系统工具', tokens:13500, percent:9.4 },
      { name:'技能', tokens:1000, percent:0.7 },
      { name:'系统提示词', tokens:1860, percent:1.3 },
      { name:'其他', tokens:0, percent:0 }]}),
  set_thinking: async (l) => ({ ok:true, thinking_level: state.thinking = l }),
  switch_model: async (n) => ({ ok:true, message:'已切换 '+n, model: state.model = n }),
  choose_workspace: async () => {
    const w = state.workspaces[state.wsCurrent === 'ws-1' ? 1 : 0];  // 模拟用户选了另一个目录
    state.wsCurrent = w.id; state.wsName = w.name; state.wsPath = w.path;
    return { ok:true, workspace:w.path, workspace_name:w.name };
  },
  set_workspace: async (p) => {
    const w = state.workspaces.find(x => x.path === p);
    if (w) { state.wsCurrent = w.id; state.wsName = w.name; state.wsPath = w.path; }
    return { ok:true, workspace:state.wsPath, workspace_name:state.wsName };
  },
  set_theme: async (m) => ({ ok:true, theme: state.theme = m }),
  open_url: async () => ({ ok:true }),
  market_list: async () => ({ ok:true, count:5, categories:['🎨 UI 增强','🧠 记忆','🛠 工具与能力'],
    items:[
      { name:'findark/dsh-skin', url:'https://github.com/findark/dsh-skin', category:'🎨 UI 增强',
        desc:'Set of skins for DeepSeek Harness UI，含暗色/浅色主题与紧凑布局。' },
      { name:'00080000/dsh-project-memory', url:'https://github.com/00080000/dsh-project-memory', category:'🧠 记忆',
        desc:'Cross-project memory for DeepSeek Harness，跨项目持久记忆与召回。' },
      { name:'melonychou/dsh-notifier', url:'https://github.com/melonychou/dsh-notifier', category:'🛠 工具与能力',
        desc:'桌面通知插件：任务完成、命令失败时弹出系统通知。' },
    ]}),
  market_install: async (u) => ({ ok:true, name:u.split('/').pop(), note:'已热激活' }),
  ws_tree: async (p) => {
    // 按层级返回不同内容，才能真正预览「逐层展开」链路（原先无论 p 是什么都回同样的 4 项）
    const tree = {
      '': [
        { name:'harness', dir:true }, { name:'tests', dir:true },
        { name:'desktop.py', dir:false, size:48213 }, { name:'README.md', dir:false, size:2048 },
      ],
      'harness': [
        { name:'builtins', dir:true }, { name:'desktop.py', dir:false, size:48213 },
        { name:'kernel.py', dir:false, size:9100 }, { name:'cli.py', dir:false, size:6200 },
        { name:'config.py', dir:false, size:8100 }, { name:'loader.py', dir:false, size:2100 },
      ],
      'harness/builtins': [
        { name:'chat_loop', dir:true }, { name:'models', dir:true },
        { name:'tools_fs', dir:true }, { name:'tools_shell', dir:true },
        { name:'skills', dir:true }, { name:'mcp_client', dir:true }, { name:'repl', dir:true },
      ],
      'harness/builtins/chat_loop': [ { name:'register.py', dir:false, size:6900 } ],
      'tests': [
        { name:'test_harness.py', dir:false, size:12000 },
        { name:'test_desktop.py', dir:false, size:9000 },
      ],
    };
    const rel = p || '';
    const entries = (tree[rel] || []).map(e => ({
      name:e.name, dir:!!e.dir, size:e.dir ? 0 : (e.size || 0), mtime:NOW }));
    return { ok:true, rel, entries };
  },
  run_terminal: async (c) => ({ ok:true, output:'[exit 0] ' + c + ' 的模拟输出\\nmain.py  README.md  src' }),
  review: async () => ({ ok:true, text:'git status\\n M harness/desktop.py\\n\\ngit diff\\ndiff --git a/harness/desktop.py b/harness/desktop.py\\n- 旧的一行\\n+ 新的一行' }),
  skills: async () => ({ ok:true, skills:[
    { name:'code-review', desc:'审查代码改动并给出风险清单', source:'profile', path:'skills/code-review' },
    { name:'weekly-report', desc:'汇总会话生成周报', source:'插件', path:'' },
  ]}),
  install_skill: async (p) => ({ ok:true, name:p.split('\\\\').pop().split('/').pop() }),
  remove_skill: async (n) => ({ ok:true, name:n }),
}};
setTimeout(() => window.dispatchEvent(new Event('pywebviewready')), 50);
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = UI_HTML.replace(
            "<script>", "<script>" + MOCK + "</script>\n<script>", 1
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 8642), Handler)
print("serving http://127.0.0.1:8642/")
server.serve_forever()
