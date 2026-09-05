# K Agent npm CLI / 终端工作台技术方案

> 文档类型：CLI 产品设计、终端页面设计与实现边界技术方案  
> 方案状态：已按方案实现 CLI 代码，待连接服务联调与 npm 发布  
> 目标发布物：npm 公共包，命令名 `k-agent`  
> 服务端约束：永久不修改 `access_layer/` 和 `backend/`  
> 文档日期：2026-08-29

---

## 1. 方案结论

K Agent CLI 采用 TypeScript / Node.js 实现，并发布为独立 npm 包。现有 Python 服务保持不变：

```text
Web Frontend      React / TypeScript
CLI / TUI         Node.js / TypeScript
Access Layer      Python / FastAPI
Agent Backend     Python / FastAPI
```

CLI 是现有 Access Layer 的另一个客户端，不是新的 Agent Runtime，也不是 Python 服务的进程内包装。

```text
┌────────────────────┐
│ npm CLI / TUI      │
│ cli                │
└─────────┬──────────┘
          │ HTTP + AG-UI SSE
          ▼
┌────────────────────┐       Internal NDJSON       ┌────────────────────┐
│ Access Layer :3001 │ ───────────────────────────▶ │ Agent Backend :3002│
│ Python / FastAPI   │                              │ Python / FastAPI   │
└────────────────────┘                              └────────────────────┘
```

终端页面的布局、颜色、文案、快捷键和响应式规则统一归属一个模块：

```text
cli/src/terminal-page/
```

其中 `design.ts` 是主要设计修改入口。修改终端外观时，不需要进入 HTTP、SSE、会话或运行状态代码。

---

## 2. 永久实施边界

### 2.1 禁止修改的目录

本方案的所有开发阶段和后续 CLI 版本都不得修改：

```text
access_layer/
backend/
```

该约束不是第一阶段的临时限制，而是 CLI 项目的永久架构边界。只有用户另行提出服务端需求并明确批准后，才能在独立任务中讨论 Python 服务改动；服务端改动不得夹带在 CLI 提交中。

### 2.2 CLI 禁止行为

CLI 不得：

- 导入任何 `access_layer` 或 `backend` Python 模块。
- 直接调用 Agent Backend 的 `:3002` 内部接口。
- 直接读取或写入 `~/.k_agent`、项目 `.k_agent/` 或 Session JSON。
- 自己实现第二套会话持久化、审批真相源或并发控制。
- 根据 UI 需要修改、重排或伪造服务端 AG-UI 事件。
- 因为某个功能缺少 API 而擅自补充服务端接口。

### 2.3 API 能力不足时的处理

如果现有公开 API 无法支持某项 CLI 功能：

1. CLI 将该功能标记为“当前服务端 API 不支持”。
2. 已支持功能继续正常发布，不以隐藏兼容逻辑绕过边界。
3. 在单独文档中说明缺失接口、使用场景和风险。
4. 只有用户明确同意后，才能另开服务端任务。

### 2.4 允许修改的范围

```text
cli/                   # npm CLI、TypeScript 协议与终端工作台
docs/                  # CLI 技术与使用文档
README.md              # 可选的安装入口说明
frontend/              # 仅在用户另行同意共享 TS 协议时调整
```

当前工作区的 `access_layer/` 和 `backend/` 已存在其他未提交修改。开始 CLI 实现前必须记录目录状态；完成后验证这两个目录相对实施前基线没有变化，不能以“整个仓库 Git diff 为空”作为验收条件。

---

## 3. 产品目标与非目标

### 3.1 产品目标

- 提供可通过 npm 安装的 `k-agent` 命令。
- 支持一次性命令和交互式终端工作台两种模式。
- 复用现有 Session、AG-UI SSE、审批、用户问题和 Workspace API。
- 保留原始 AG-UI 事件顺序。
- 默认适配本地 `http://127.0.0.1:3001`。
- 支持脚本消费的稳定 `stdout`、`stderr`、JSON 和退出码。
- 在 80 列窄终端、无颜色终端和非交互环境中可预测地退化。

### 3.2 非目标

- npm 包不内置或自动安装 Python 服务。
- 不把 Agent Backend 改写为 Node.js。
- 不在第一版实现浏览器语音和桌面宠物。
- 不要求 CLI 与 Web 视觉完全一致。
- 不在客户端复制 Team、Automation 或 Session 的业务状态机。
- 不把完整工具参数、工具输出或敏感数据默认写入本地日志。

---

## 4. npm 包与运行方式

### 4.1 包名与命令名

推荐包名：

```text
@k-agent/cli
```

如果 npm scope 不可用，再评估未被占用的非 scope 包名。无论包名如何，终端命令保持：

```text
k-agent
```

安装和执行方式：

```bash
npm install -g @k-agent/cli
k-agent doctor
k-agent chat

# 不全局安装时
npx @k-agent/cli doctor
```

源码仓库提供一个不启动 Web 前端的本地开发监督命令：

```bash
npm --prefix cli run dev:local
```

该命令检查并按需启动 `127.0.0.1:3001` 和 `127.0.0.1:3002`，等待 Access Layer 确认 Agent Backend 健康后再打开 TUI。它会复用已有健康服务，并只回收本次启动的子进程。服务日志写入临时目录，避免破坏 Ink 页面。此监督脚本不包含在 npm 发布文件中，不能改变“发布后的 CLI 只是 HTTP 客户端”的边界。

### 4.2 Node.js 版本

最低版本为 Node.js 22.12。当前使用的 Ink 7 与 Commander 15 都要求 Node.js 22 系列能力；CLI 使用原生 `fetch`、`AbortController` 和 ESM，不额外打包 Node.js Runtime。

### 4.3 npm 包职责

npm 包只包含：

- 命令解析。
- HTTP / SSE 客户端。
- TypeScript 协议类型。
- 终端页面与非交互输出渲染。
- 客户端配置和端点选择。
- CLI 单元测试与终端快照测试。

它不包含 Python Runtime、模型凭据、MCP 子进程或用户的本地状态。

---

## 5. 实现目录结构

CLI 与现有 `frontend/`、`access_layer/`、`backend/` 并列，不增加没有必要的 `packages/` 外层：

```text
k_agent/
├── frontend/                           # 现有 React Web 前端
├── cli/                                # 新增：独立 npm CLI
│   ├── package.json
│   ├── package-lock.json
│   ├── tsconfig.json
│   ├── README.md
│   ├── LICENSE
│   ├── scripts/
│   │   ├── dev-local.mjs              # 源码开发的一键服务监督器，不进入 npm 包
│   │   └── mark-bin-executable.mjs     # 构建后固定 npm bin 可执行权限
│   ├── src/
│   │   ├── index.ts                    # k-agent 命令入口
│   │   ├── protocol/                   # 公开 HTTP / AG-UI TypeScript 类型
│   │   │   └── index.ts               # 首版集中契约，后续可按领域无行为拆分
│   │   ├── commands/
│   │   │   ├── chat.tsx
│   │   │   ├── run.ts
│   │   │   ├── sessions.ts
│   │   │   ├── teams.ts
│   │   │   ├── automations.ts
│   │   │   ├── config.ts
│   │   │   ├── doctor.ts
│   │   │   └── shared.ts
│   │   ├── client/
│   │   │   ├── access-layer-client.ts
│   │   │   ├── sse-parser.ts
│   │   │   └── errors.ts
│   │   ├── application/
│   │   │   ├── chat-controller.tsx
│   │   │   ├── event-projector.ts
│   │   │   ├── run-input.ts
│   │   │   ├── run-lifecycle.ts
│   │   │   └── view-model.ts
│   │   ├── terminal-page/              # 唯一的交互式终端页面模块
│   │   │   ├── index.ts                # 模块唯一公共出口
│   │   │   ├── design.ts               # 集中设计配置，主要修改入口
│   │   │   ├── types.ts                # ViewModel、Action、焦点和页面状态
│   │   │   ├── line-editor.ts          # grapheme 级行编辑纯逻辑
│   │   │   ├── slash-catalog.ts        # `/` 命令目录与过滤排序
│   │   │   ├── home-catalog.ts         # 首页模式、最近会话与模式循环
│   │   │   ├── use-ime-cursor.ts       # 真实终端光标与 IME 候选窗定位
│   │   │   ├── TerminalPage.tsx        # 页面根组件与区域编排
│   │   │   ├── screens/                # 完整页面，不对模块外导出
│   │   │   │   ├── HomeScreen.tsx
│   │   │   │   ├── ChatScreen.tsx
│   │   │   │   ├── TeamScreen.tsx
│   │   │   │   ├── AutomationScreen.tsx
│   │   │   │   └── DoctorScreen.tsx
│   │   │   ├── panels/                 # 页面内稳定区域
│   │   │   │   ├── GlobalBar.tsx
│   │   │   │   ├── ScreenFrame.tsx
│   │   │   │   ├── SessionRail.tsx
│   │   │   │   ├── ConversationTimeline.tsx
│   │   │   │   ├── ActivityInspector.tsx
│   │   │   │   ├── WorkspaceBrowser.tsx
│   │   │   │   ├── Composer.tsx
│   │   │   │   ├── CommandList.tsx     # `/` 与 Ctrl+K 共用的候选渲染
│   │   │   │   ├── SlashMenu.tsx
│   │   │   │   └── StatusBar.tsx
│   │   │   ├── overlays/               # 临时覆盖层，按优先级管理
│   │   │   │   ├── Overlay.tsx         # 覆盖层共用外框
│   │   │   │   ├── CommandPalette.tsx
│   │   │   │   ├── SessionSwitcher.tsx
│   │   │   │   ├── ApprovalDialog.tsx
│   │   │   │   ├── UserQuestionDialog.tsx
│   │   │   │   └── HelpOverlay.tsx
│   │   │   └── renderers/              # 内容类型的终端表现
│   │   │       ├── MarkdownBlock.tsx
│   │   │       ├── ThinkingBlock.tsx
│   │   │       ├── ToolBlock.tsx
│   │   │       ├── CodeBlock.tsx
│   │   │       └── ErrorBlock.tsx
│   │   ├── output/
│   │   │   ├── human.ts
│   │   │   ├── json.ts
│   │   │   ├── exit-codes.ts
│   │   │   └── sanitize.ts
│   │   └── config/
│   │       └── index.ts
│   └── tests/
│       ├── dev-local.test.ts
│       ├── event-order.test.ts
│       ├── sse-parser.test.ts
│       ├── terminal-page.test.tsx
│       ├── terminal-responsive.test.ts
│       ├── run-lifecycle.test.ts
│       └── output-contract.test.ts
├── access_layer/                       # 现有 Python 接入层，永久不修改
├── backend/                            # 现有 Python Agent Backend，永久不修改
└── docs/
```

这里的 `screens/`、`panels/`、`overlays/` 和 `renderers/` 都是 `terminal-page` 内部实现，不是四套独立 UI 模块。模块外只能通过 `terminal-page/index.ts` 使用终端页面。

依赖方向固定为：

```text
commands
   │
   ▼
application ─────────▶ client ─────────▶ Access Layer HTTP
   │
   ▼
terminal-page

protocol 被 client、application、terminal-page 只读引用
```

`terminal-page` 不得反向调用 `client`，也不得直接发起 `fetch`。这样修改终端页面不会改变网络、持久化或运行生命周期。

---

## 6. 终端页面产品级设计

### 6.1 设计目标

终端页面不是把 Web 三栏布局缩小到字符网格，而是针对高频键盘操作重新建立信息层级。它需要同时回答五个问题：

1. 我现在在哪个工作区和 Session？
2. Agent 正在做什么，是否仍在运行？
3. 当前输出、Thinking、Tool 和审批之间是什么顺序？
4. 我现在可以执行什么动作？
5. 出错、断线或切换 Session 后，状态是否仍然可信？

整体体验定位为“安静、专业、高信息密度的本地执行台”：默认界面克制，但细节可以逐层展开。高频键盘操作即时响应，不增加装饰性动画；只有持续运行状态使用轻量 spinner，并在 `--plain` 下退化为静态文字。

### 6.2 “一个模块”的定义

所有交互式终端页面统一归属：

```text
cli/src/terminal-page/
```

该模块对外只导出：

```ts
export { TerminalPage } from "./TerminalPage.js";
export type { TerminalPageProps, TerminalPageAction } from "./types.js";
```

模块外不得直接引用 `screens/`、`panels/`、`overlays/`、`renderers/` 或 `design.ts`。本约束保证终端页面可以重构内部布局，而不会把视觉实现扩散到命令和网络层。

### 6.3 页面信息架构

终端工作台包含五个一级页面：

| 页面 | 用途 | 默认入口 |
| --- | --- | --- |
| Home | 新建任务、继续最近 Session、查看服务状态 | `k-agent` 无活动 Session |
| Chat | 单 Agent 对话、运行时间线、Workspace | `k-agent chat` |
| Team | Supervisor、Worker、Task DAG、Artifact | 命令面板或 `k-agent team open` |
| Automation | 定时任务、最近运行、暂停与恢复 | 命令面板或 `k-agent automation` |
| Doctor | 服务、终端、协议和版本诊断 | `k-agent doctor` |

页面切换通过命令面板完成，不在顶部铺开大量常驻 Tab。顶部只显示当前位置和运行状态，减少长期占用终端行数。

### 6.4 页面层级

每个页面由四个稳定层级组成：

```text
Global Bar       产品、连接、当前位置、Agent/Model、权限模式
Primary Surface  当前页面的主要内容
Context Surface  Session、Activity、Workspace 或页面详情
Action Surface   Composer、快捷键提示、审批或用户问题
```

临时覆盖层位于所有页面之上：

```text
Command Palette
Session Switcher
Approval Dialog
User Question Dialog
Help Overlay
```

覆盖层优先级固定为：

```text
Approval / User Question
        > 连接和协议致命错误
        > Command Palette / Session Switcher
        > Help
```

审批或用户问题出现时必须立即可见，但不能自动选中“允许”。普通命令面板不能遮住待处理 HITL。

### 6.5 集中设计入口 `design.ts`

布局、主题、文案、符号、密度和快捷键全部从 `design.ts` 读取，不在组件中散落设计常量：

```ts
/**
 * 终端页面的唯一设计配置入口。
 *
 * 此处只描述视觉和交互表现，不保存 Session、run 或审批状态。
 * 修改页面设计时优先调整该对象，避免视觉常量散落后难以整体校准。
 */
export const TERMINAL_DESIGN = {
  layout: {
    /** 144 列以上显示 Session、对话和 Inspector 三个稳定区域。 */
    wideMinColumns: 144,
    /** 110–143 列保留 Session + 对话，Inspector 改为覆盖层。 */
    standardMinColumns: 110,
    /** 80–109 列只显示主页面，其余区域由快捷键切换。 */
    compactMinColumns: 80,
    /** 低于 80 列启用最小兼容布局，保证审批和正文仍可操作。 */
    minimumColumns: 56,
    sessionRailColumns: 26,
    inspectorColumns: 42,
    composerMinRows: 3,
    composerMaxRows: 8,
    contentHorizontalPadding: 2,
  },
  density: {
    /** 对话项之间保留一行，让长时间线仍能快速区分事件边界。 */
    timelineGapRows: 1,
    toolPreviewRows: 3,
    thinkingPreviewRows: 2,
    sessionVisibleRows: 12,
  },
  theme: {
    /** 默认沿用终端背景，避免强制背景色破坏用户现有配色。 */
    background: "terminal",
    accent: "cyan",
    focus: "blueBright",
    text: "default",
    muted: "gray",
    success: "green",
    warning: "yellow",
    danger: "red",
    selection: "inverse",
  },
  borders: {
    unicode: "round",
    ascii: "classic",
    active: "double",
  },
  symbols: {
    running: "●",
    waiting: "○",
    success: "✓",
    error: "×",
    stopped: "■",
    thinking: "◇",
    tool: "◆",
    approval: "!",
    question: "?",
    unread: "•",
  },
  copy: {
    composerPlaceholder: "描述目标、约束或下一步…",
    connected: "已连接",
    reconnecting: "正在重连",
    disconnected: "连接已断开",
    approvalRequired: "需要你的确认",
    questionRequired: "Agent 正在等待你的回答",
    scrolledOutput: "有新的运行活动",
  },
  keys: {
    commandPalette: "ctrl+k",
    sessionSwitcher: "ctrl+p",
    toggleInspector: "ctrl+o",
    focusNext: "tab",
    focusPrevious: "shift+tab",
    stopRun: "ctrl+c",
    closeOverlay: "escape",
  },
} as const;
```

设计修改位置表：

| 想修改的内容 | 首选文件 |
| --- | --- |
| 颜色、边框、符号、阈值 | `terminal-page/design.ts` |
| 页面整体区域关系 | `terminal-page/TerminalPage.tsx` |
| Home / Chat / Team / Automation 页面 | `terminal-page/screens/` |
| Session、Composer、Inspector 等区域 | `terminal-page/panels/` |
| 命令面板、审批、用户问题 | `terminal-page/overlays/` |
| Markdown、Thinking、Tool、Code | `terminal-page/renderers/` |

### 6.6 页面输入、动作与焦点模型

终端页面只消费不可变 ViewModel，并把用户动作发送给 application 层：

```ts
export interface TerminalPageProps {
  /** 页面只读取已投影状态，不直接持有服务端 Session 的写权限。 */
  model: TerminalPageViewModel;
  /** 页面只报告用户意图，HTTP、SSE 和持久化由 application 层处理。 */
  onAction: (action: TerminalPageAction) => void;
}

export type TerminalPageAction =
  | { type: "submit_prompt"; text: string }
  | { type: "select_session"; sessionId: string }
  | { type: "open_surface"; surface: "chat" | "team" | "automation" | "doctor" }
  | { type: "select_activity"; activityId: string }
  | { type: "open_workspace"; path?: string }
  | { type: "stop_run" }
  | { type: "cancel_run" }
  | { type: "answer_interrupt"; interruptId: string; payload: unknown }
  | { type: "retry_connection" }
  | { type: "quit" };
```

焦点区域包括：

```text
session-rail
timeline
inspector
composer
overlay
```

焦点规则：

- 打开页面后默认聚焦 Composer；有待处理 Approval 或 User Question 时聚焦覆盖层。
- `Tab` 和 `Shift+Tab` 在当前可见区域间移动，不进入隐藏区域。
- `Esc` 只关闭普通覆盖层或退出当前选择态，不代表拒绝审批。
- 鼠标点击属于增强能力，任何核心功能必须只用键盘完成。
- 页面切换和命令面板等高频键盘动作即时完成，不增加开关动画。

### 6.7 Home 页面

Home 不是空白欢迎页，而是“下一步做什么”的启动面板。页面不预置示例问题，避免用户把模板
文案当成真实建议直接发给模型；引导只说明可执行的操作路径：

```text
 ✻ K Agent  ● 已连接                                        k_agent · model · default
  1 工作   2 团队   3 自动   4 诊断                                 Shift+Tab 切换模式
 ╭──────────────────────────────────────────────────────────────────────────────────╮
 │ ✻ 欢迎使用 K Agent  本地优先的 Agent 工作台                                      │
 │ http://127.0.0.1:3001 · k_agent · model · default                                │
 ╰──────────────────────────────────────────────────────────────────────────────────╯

 开始
   • 输入目标后回车，直接开始一次新的运行
   • 输入 / 展开命令选择栏，Tab 补全命令
   • Shift+Tab 在 工作 / 团队 / 自动 / 诊断 之间切换

 模式
 ❯ 1  工作                                                        对话、工具与审批
   2  团队                                                       Agent Team 与任务
   3  自动                                                      定时任务与执行记录
   4  诊断                                                    连接、模型与工具检查

 最近会话                                                              Ctrl+P 全部
   ○  修复审批恢复                                                      12 条 · 8m
```

细节：

- 健康状态必须区分 Access Layer 和 Agent Backend，不能只显示一个模糊的 Connected。
- 最近 Session 显示消息数量和相对更新时间，便于判断该恢复哪一个。
- 引导条目只描述键位与行为，不代替用户输入任何 Prompt。
- Runtime 摘要始终可见，但详细配置通过命令选择栏打开。

### 6.8 Chat 页面：宽屏工作台

144 列以上使用三栏，但每一栏承担明确产品语义：

```text
┌─ K Agent ────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ ● Ready  /work  修复审批恢复                     k_agent · model-name · reasoning high          default sandbox      │
├─ SESSIONS ───────────────┬─ CONVERSATION ────────────────────────────────────────────────┬─ INSPECTOR ────────────────┤
│ ⌕ 搜索                   │                                                               │ ACTIVITY  WORKSPACE  RUN   │
│                          │  10:42  你                                                    │ ─────────────────────────── │
│ ● 修复审批恢复       8m  │  检查为什么审批卡片刷新后才出现。                             │ ◇ Thinking        complete │
│   等待工具               │                                                               │ ◆ Read             24 ms  │
│                          │  10:42  K Agent                                               │ ◆ Bash            running │
│ ○ CLI 技术方案       2h  │  ┃ 分析事件链路                                               │                           │
│ ○ MCP 配置排查     昨天  │  ┃ 已确认事件持久化正常，继续检查实时投影。                    │ Selected: Bash            │
│                          │  ┃                                                            │ npm test                  │
│ ──────────────────────── │  ◆ Read  frontend/src/App.tsx                    ✓ 24 ms       │                           │
│ RUNNING                  │  ◆ Bash  npm test                                ● running     │ Output                    │
│ ● Team 发布检查          │                                                               │ PASS event-order.test     │
│                          │  Agent 正在执行测试…                                           │ … 18 more lines           │
│                          │                                                               │                           │
├──────────────────────────┴───────────────────────────────────────────────────┴───────────────────────────┤
│ MCP 3  Skills 2  Permission default  Context 42%                    ↑ 新活动 3  Ctrl+O 查看              │
├─────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ > 描述目标、约束或下一步…                                                                              │
│   Enter 发送   Alt+Enter 换行   Ctrl+C 停止   Ctrl+K 命令                                            │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

关键细节：

- Session 列表在标题之外显示状态副标题，例如“等待工具”“需要输入”“已停止”。
- Timeline 使用连续左侧轨道表达一个 run 内的 Thinking、Tool、Text 顺序。
- Tool 行先给结论，再展开参数和输出；完成状态不抢夺正文注意力。
- Inspector 有 `ACTIVITY / WORKSPACE / RUN` 三个页签，但只展示一个详情面板。
- 用户向上滚动后停止自动跟随，底部显示“新活动 N”；回到底部后恢复跟随。
- Composer 上方的状态条展示当前 MCP、Skill、权限和上下文摘要。

### 6.9 Chat 页面：标准与紧凑布局

110–143 列显示 Session + Conversation，Inspector 通过 `Ctrl+O` 覆盖右侧；80–109 列只显示 Conversation，Session 与 Inspector 都使用覆盖层。

```text
┌─ K Agent ────────────────────────────────────────────────────────────────┐
│ ● Ready  修复审批恢复                k_agent · model-name · default      │
├─────────────────────────────────────────────────────────────────────────┤
│ 10:42  你                                                               │
│ 检查为什么审批卡片刷新后才出现。                                        │
│                                                                         │
│ 10:42  K Agent                                                          │
│ ┃ ◇ 分析事件链路                                             complete   │
│ ┃ ◆ Read frontend/src/App.tsx                                 24 ms     │
│ ┃ ◆ Bash npm test                                           running     │
│ ┃                                                                     │
│ ┃ Agent 正在执行测试…                                                   │
│                                                                         │
│                                              有新的运行活动 3  [End]     │
├─────────────────────────────────────────────────────────────────────────┤
│ MCP 3 · Skills 2 · default             Ctrl+P 会话 · Ctrl+O Inspector   │
│ > 描述目标、约束或下一步…                                               │
└─────────────────────────────────────────────────────────────────────────┘
```

低于 80 列进入最小兼容布局：

- 隐藏时间戳、次要元数据和装饰边框。
- Session、Activity、Workspace 都变成全屏切换页。
- Tool 参数和 Thinking 默认折叠为一行。
- Approval 和 User Question 替换当前主区域，不能被折叠。
- 表格转换为键值列表，代码块允许局部横向滚动。
- 低于 56 列仍可运行，但顶部显示“终端过窄，已启用兼容布局”。

### 6.10 Timeline 与运行活动

Timeline 不是把所有事件都画成卡片，而是按信息价值分层：

| 类型 | 默认表现 | 何时自动展开 |
| --- | --- | --- |
| User Message | 完整正文 | 始终展开 |
| Assistant Text | Markdown 正文 | 始终展开 |
| Thinking | 标题、状态、两行摘要 | 当前 active 或用户选择 |
| Tool preparing/running | 名称、目标、spinner、耗时 | 当前 running 或失败 |
| Tool complete | 单行结果摘要 | 用户选择 |
| Tool error | 红色状态词和错误摘要 | 自动展开 |
| Approval | 覆盖层 + Timeline 占位 | 始终聚焦 |
| User Question | 覆盖层 + Timeline 占位 | 始终聚焦 |
| Run Error | 明确错误块和可恢复动作 | 自动展开 |

视觉规则：

- 不能只用颜色表达状态；必须同时显示符号和状态文本。
- Running spinner 只更新自己的字符，不重新渲染整条 Timeline。
- 工具完成后停止 spinner，并固定最终耗时，避免历史页面持续变化。
- 高频增量不使用逐字符动画；服务端 delta 到达后直接显示。
- 键盘触发的展开、切换和命令面板不做延时动画。

AG-UI 原始顺序仍然是唯一顺序来源。页面不得把 Thinking 汇总到顶部、把 Tool 全部移到侧栏或把最终 Text 提前显示。

### 6.11 Activity Inspector

Inspector 提供三个页签：

```text
ACTIVITY   WORKSPACE   RUN
```

`ACTIVITY` 展示当前选中 Thinking、Tool、Approval 或 Error 的完整信息：

```text
◆ Bash                                      running
──────────────────────────────────────────────────
Command
npm test

Live output
PASS event-order.test
PASS run-lifecycle.test
… 18 more lines

Started 10:42:18   Elapsed 00:07
```

长输出处理：

- 默认只显示尾部有限行数，并保留“还有 N 行”的提示。
- 支持打开内置 pager，不一次渲染数万行。
- 原始输出中的 ANSI 控制码必须转义或剥离，防止终端注入和界面破坏。
- Tool 参数可能包含敏感内容，默认展示摘要，显式展开后才显示完整值。

### 6.12 Workspace 浏览器

Workspace 在宽屏 Inspector 内展示，在标准和紧凑布局中成为全屏页面：

```text
WORKSPACE / sessions/abc/workspace
──────────────────────────────────────────────────
▾ src/
  ├─ app.ts
  ├─ api.ts
  └─ styles.css
▸ tests/
  README.md

Preview: src/api.ts                       12–38 / 92
──────────────────────────────────────────────────
12 export async function streamAgentRun(...) {
13   ...
```

交互要求：

- 文件树与预览使用独立焦点，`Tab` 切换。
- 二进制文件显示元数据，不把原始字节写进终端。
- 超大文件只加载和显示可视窗口，不阻塞主 Timeline。
- Markdown 使用终端排版；代码保留行号；表格在窄屏下降级。
- 页面只调用现有 Workspace API，不解析服务端物理路径。

### 6.13 命令选择栏与命令面板

主入口是输入框里的 `/`：草稿以 `/` 开头且尚未输入参数时，输入框下方立即展开候选列表，
这样导航和运行时查询都在同一处完成，不必先记住浮层快捷键。

```text
╭──────────────────────────────────────────────────────────────────────────────╮
│ ❯ /se                                                                        │
╰──────────────────────────────────────────────────────────────────────────────╯
╭──────────────────────────────────────────────────────────────────────────────╮
│ ❯ /sessions                                          打开 Session 选择器 · 3 │
│   /new                                        新建 Session（首次发送时创建） │
│   /workspace …                                       浏览当前 Session 工作区 │
│ 匹配 “/se” · ↑↓ 选择   Tab 补全   Enter 执行   Esc 收起                      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

细节：

- 展开和收起立即完成，不做动画。
- 排序确定：完全匹配 > 前缀匹配 > 名称包含 > 描述包含，同分按命令名。
- 选择栏打开时导航键归它所有，输入框只继续处理正文编辑，避免同一次按键产生两种效果。
- 带 `…` 的命令需要参数，`Enter` 只补全命令名，不提交缺参数的动作。
- 选择栏只声明 application 层已实现的命令，不出现永远返回“未知命令”的入口。
- 目录里只保留可执行动作；`/help` 由页面直接打开快捷键帮助，不下发无副作用的命令。

`Ctrl+K` 是同一份目录的浮层呈现，唯一存在理由是**不依赖输入框**：Run 正在进行时输入框被禁用、
草稿里已有正文时 `/` 不会展开，此时仍然需要能执行 stop / cancel / quit。

```text
╔══════════════════════════════════════════════════════════════════╗
║  命令                                                            ║
║  搜索  /                                                         ║
║                                                                  ║
║  ❯ /new                                         当前 Run 未结束  ║
║    /sessions                            打开 Session 选择器 · 3  ║
║    /workspace …                                尚未打开 Session  ║
║    /stop                           停止当前 Run，保留已产生内容  ║
║  输入以过滤   ↑↓ 选择   Enter 执行   Esc 关闭                    ║
╚══════════════════════════════════════════════════════════════════╝
```

两处入口共享的约束：

- 命令目录、过滤排序、可用性判断和候选行渲染只有一份实现，不允许两边各自维护命令表。
- 可用性只依据已投影的运行状态判断，不询问服务端；判断结果只影响能否执行，不影响是否显示。
- 运行中不可用的操作仍然显示，并在右侧用警示色注明原因（如“没有运行中的任务”“尚未打开 Session”），
  不能悄悄消失，也不能在按下 Enter 后静默失败。
- 两者执行命令都走页面根组件同一个提交出口，不各自派发 action。

### 6.14 Session Switcher

`Ctrl+P` 打开的 Session Switcher 除标题外还显示运行状态和更新时间：

```text
┌─ Sessions ──────────────────────────────────────────────────┐
│ > approval                                                  │
│                                                             │
│ ● 修复审批恢复          running             8m              │
│ ! 定时任务恢复          needs input         32m             │
│ ○ 审批技术方案          complete            昨天            │
└─────────────────────────────────────────────────────────────┘
```

切换 Session 只改变当前页面绑定，不自动停止后台 run。切回运行中的 Session 时，根据现有服务端快照和已接收事件恢复视图。

### 6.15 Approval 覆盖层

Approval 使用高优先级覆盖层，但保留请求来源和作用域：

```text
┌─ 需要你的确认 ──────────────────────────────────────────────┐
│ ◆ Bash · k_agent · run 8f31                                 │
│                                                             │
│ Agent 请求在工作区执行：                                    │
│ npm test                                                    │
│                                                             │
│ Scope                                                       │
│ 此次允许只对当前请求生效；“本轮允许”只对当前 run 生效。      │
│                                                             │
│ [d] 拒绝      [a] 允许一次      [r] 本轮允许                 │
│                                                             │
│ Esc 返回查看上下文，但不会自动拒绝。                         │
└─────────────────────────────────────────────────────────────┘
```

审批细节：

- 默认焦点位于“拒绝”或无选择状态，不能落在允许按钮上。
- “允许一次”和“本轮允许”必须使用不同文案和视觉层级。
- 提交后显示 submitting，防止重复发送。
- `unknown_outcome` 必须解释不确定性，不能简单恢复成 pending。
- 用户可以临时返回 Timeline 查看上下文，但待审批状态持续可见。

### 6.16 User Question 覆盖层

```text
┌─ Agent 正在等待你的回答 ────────────────────────────────────┐
│ 运行方式                                                    │
│                                                             │
│ (●) 只生成方案                                              │
│ ( ) 方案确认后实现                                          │
│ ( ) 直接实现                                                │
│                                                             │
│ 补充说明                                                    │
│ > 保持 Access Layer 和 Backend 不变                          │
│                                                             │
│ [Tab] 下一项   [Space] 选择   [Enter] 提交   [Esc] 暂不回答 │
└─────────────────────────────────────────────────────────────┘
```

问题、选项和 request hash 以服务端数据为准。终端页面只能编辑答案，不得修改问题定义。多选、单选和自由输入必须有不同且清晰的操作提示。

### 6.17 Team 页面

Team 页面强调 DAG、Worker 和 Artifact，而不是复用 Chat 布局：

```text
┌─ TEAM / Release Review ─────────────────────────────────────────────────────┐
│ ● running   4 workers   6/9 tasks   2 artifacts   1 waiting approval        │
├─ TASKS ─────────────────────────────┬─ DETAIL ───────────────────────────────┤
│ ✓ T1 Inspect API                    │ T4 Frontend verification               │
│ ├─✓ T2 Protocol tests               │ Worker: codex                          │
│ ├─● T4 Frontend verification        │ Depends on: T2                         │
│ └─○ T5 Release notes                │                                        │
│   └─○ T6 Package validation         │ Latest                                 │
│                                    │ ◆ npm test · running                   │
│ WORKERS                            │ ◇ Checking terminal snapshots          │
│ ● supervisor                       │                                        │
│ ● codex-1                          │ Artifacts                              │
│ ○ claude-1                         │ cli-report.md                          │
├────────────────────────────────────┴────────────────────────────────────────┤
│ [m] Message  [a] Artifacts  [p] Pause  [c] Cancel  Ctrl+K Commands          │
└─────────────────────────────────────────────────────────────────────────────┘
```

Task 状态以服务端返回为准。CLI 不在本地推导依赖是否可以执行，也不实现第二套 Team scheduler。

### 6.18 Automation 页面

```text
┌─ AUTOMATIONS ────────────────────────────────────────────────────────────────┐
│ Name                    Schedule             Next run       Last result      │
│ ● Daily code review     Every day 09:00      in 14h         ✓ 32s            │
│ ○ Weekly report         Mon 10:00 Asia/SH    in 3d          ✓ 1m12s          │
│ ■ Dependency audit      Paused               —              × needs input    │
├─────────────────────────────────────────────────────────────────────────────┤
│ Selected: Daily code review                                                   │
│ Prompt: Review repository changes and create a report                         │
│ Recent runs: ✓ 08-29 09:00   ✓ 08-28 09:00   ! 08-27 09:00                  │
├─────────────────────────────────────────────────────────────────────────────┤
│ [Enter] Open  [r] Run now  [p] Pause/Resume  [h] History                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

无人值守任务使用 `full_access` 时必须持续显示风险标记。需要审批或用户回答的运行显示 `needs input`，不能归类为普通失败。

### 6.19 错误、断线与恢复反馈

错误按照可恢复性分层：

| 错误 | 页面表现 | 用户动作 |
| --- | --- | --- |
| Access Layer 未启动 | Home 顶部错误条 + doctor 建议 | Retry / Doctor |
| SSE 短暂断开 | 当前 run 标记连接中断，不伪造失败 | Retry / 查看 Session |
| 协议解析失败 | ErrorBlock 展示事件类型和 request ID | Export diagnostics |
| Session busy | Composer 上方阻断提示 | 切回运行中 Session |
| Approval unknown outcome | 高优先级风险提示 | 根据服务端允许的恢复动作处理 |
| Tool 普通失败 | Timeline Tool error | Agent 可继续下一轮 |

错误条不自动消失。成功恢复后保留一条简短恢复记录，让用户知道状态为何发生变化。

### 6.20 内容排版和终端能力退化

内容渲染规则：

- 正文中文、英文和代码按终端实际列宽换行，不能按 JavaScript 字符长度直接截断。
- 使用 East Asian Width 计算中文全角字符，避免边框和光标错位。
- Emoji 宽度在不同终端不稳定，核心状态必须提供 ASCII fallback。
- Markdown 标题最多保留三级视觉层级，避免大量空行。
- 代码块显示语言、行号和复制提示；窄屏不强制软换行代码。
- 表格宽度不足时转换为逐项键值布局。
- 模型和工具输出中的 ANSI escape、OSC 链接和控制字符必须清理。

终端能力等级：

```text
True Color → 256 Color → 16 Color → NO_COLOR / plain
Unicode   → ASCII border and symbol fallback
TTY       → Interactive TUI
Non-TTY   → Human stream / JSON / JSONL，不启动全屏页面
```

### 6.21 反馈与动态效果

TUI 不追求浏览器动画。反馈策略是：

- 键盘操作立即切换，不添加开关动画或人为延迟。
- 运行中只使用低成本 spinner、耗时和状态词。
- 新事件到达时更新对应活动，不整页闪烁或清屏。
- 用户正在阅读历史时不强制滚到底部，显示“新活动 N”。
- 提交按钮状态依次为 ready、submitting、running、waiting input、finished。
- 完成提示只短暂更新状态栏，不弹出阻断式庆祝效果。

高频交互的“精致感”来自焦点稳定、无跳动、无重复渲染和正确状态，而不是动画数量。

### 6.22 快捷键总表

| 快捷键 | 行为 | 约束 |
| --- | --- | --- |
| `Enter` | 提交或确认当前选择 | 中文输入法组合态不得误提交 |
| `Alt+Enter` | Composer 插入换行 | 不支持时提供 `/editor` |
| `/` | 展开命令选择栏 | 出现空白参数后自动收起 |
| `Tab` | 选择栏打开时补全命令，否则在可见区域间移动焦点 | 不进入隐藏区域 |
| `Shift+Tab` | 切换 工作 / 团队 / 自动 / 诊断 | 只改页面位置，不影响后台 run |
| `1`–`4` | 直接跳到对应模式 | 仅在输入框为空时生效 |
| `Ctrl+K` | 以浮层打开同一命令目录 | 输入框禁用或草稿非空时的入口 |
| `Ctrl+P` | 打开 Session Switcher | 不停止后台 run |
| `Ctrl+O` | 打开或关闭 Inspector | 只改变页面显示 |
| `Ctrl+C` | 停止当前 run | 保留本轮已产生内容 |
| `/cancel` | 取消并请求服务端回滚本轮 | 永久区别于 stop |
| `Esc` | 收起选择栏或关闭普通覆盖层 | 不等于拒绝审批 |
| `/help` | 打开当前快捷键帮助 | 纯页面能力，不发送命令 |

输入框行编辑保持终端习惯，光标位置以 grapheme 计算，中文和 Emoji 不会被切成半个字符：

| 快捷键 | 行为 |
| --- | --- |
| `←` / `→` | 按字符移动光标 |
| `Alt+←` / `Alt+→` | 按词移动光标 |
| `Ctrl+A` / `Ctrl+E` | 移到行首 / 行尾 |
| `Ctrl+B` / `Ctrl+F` | 左移 / 右移一个字符 |
| `Ctrl+W` | 删除光标左侧一个完整词 |
| `Ctrl+U` | 删除到行首 |
| `Ctrl+D` | 删除光标位置字符 |

光标同时用反显块表现，并把真实终端光标移动到同一列，否则输入法候选窗会漂到输出末尾。
输入框内部不放任何预置占位文案，键位提示放在框外单行。

### 6.23 中文注释要求

页面模块必须保留必要的中文注释，尤其是终端特有的不明显约束：

```ts
/**
 * 页面只消费 application 层提供的只读 ViewModel。
 * 这里禁止直接请求 API，避免用户修改布局时意外改变 run 生命周期或审批语义。
 */
export function TerminalPage({ model, onAction }: TerminalPageProps) {
  // 页面区域编排实现。
}

/**
 * 用户向上滚动后暂停自动跟随，防止流式输出持续抢走阅读位置。
 * 新事件只累计 unread 数量，直到用户主动回到底部。
 */
export function updateScrollPin(state: ScrollState, event: ScrollEvent): ScrollState {
  return state;
}

/**
 * 清理模型和工具输出中的终端控制序列，防止内容移动光标、改写标题或伪造页面区域。
 * 这是终端输出安全边界，不能仅依赖颜色库的默认转义行为。
 */
export function sanitizeTerminalContent(value: string): string {
  return value;
}
```

不需要给简单 JSX、颜色赋值和数组映射逐行写注释。注释必须解释布局、生命周期、安全或兼容原因，不复述代码。

---

## 7. 命令设计

### 7.1 主命令

```bash
k-agent                         # 打开交互式终端工作台
k-agent chat                    # 同上，显式进入 Chat
k-agent run "总结当前项目"      # 一次性执行
k-agent doctor                  # 检查 Access Layer 与终端环境
```

### 7.2 Session

```bash
k-agent sessions list
k-agent sessions show <session-id>
k-agent sessions resume <session-id>
k-agent sessions fork <session-id>
k-agent sessions delete <session-id>
```

具有删除或覆盖风险的操作必须二次确认；非交互环境必须显式提供确认参数，否则拒绝执行。

### 7.3 Team 与 Automation

仅映射现有公开 API：

```bash
k-agent team list
k-agent team open <team-id>
k-agent team pause <team-id>
k-agent team resume <team-id>
k-agent team cancel <team-id>

k-agent automation list
k-agent automation show <task-id>
k-agent automation run-now <task-id>
k-agent automation pause <task-id>
k-agent automation resume <task-id>
k-agent automation history <task-id>
```

具体命令以现有 API 实际支持能力为准；CLI 不为追求命令表完整而补改服务端。

### 7.4 交互式 Slash Command

```text
/new
/sessions
/agent
/model
/mcp
/skill
/permissions
/trace
/workspace
/stop
/cancel
/export
/help
/quit
```

Slash Command 只属于 CLI 输入解析层，不得作为普通用户消息发给模型。

---

## 8. HTTP 与协议设计

### 8.1 唯一服务端入口

默认地址：

```text
http://127.0.0.1:3001
```

优先级：

```text
--endpoint 参数
K_AGENT_ENDPOINT 环境变量
用户 CLI 配置
默认 loopback 地址
```

CLI 不读取服务端 `.env`。端点改变只影响 HTTP 客户端，不影响 Agent、模型和权限配置。

### 8.2 SSE 客户端

`POST /api/agent` 返回 `text/event-stream`。CLI 必须：

- 按空行边界解析完整 SSE frame。
- 保留不完整 frame 到下一网络块。
- 每个事件按收到顺序立即进入事件投影器。
- 不把不同 `runId` 或 `messageId` 的增量合并。
- 连接中断时把未封口活动标记为 interrupted，不伪造正常结束事件。

### 8.3 TypeScript 协议来源

`cli/src/protocol` 只描述公开 HTTP 和 AG-UI 契约。它不得包含 Python 实现细节或物理存储路径。

首版可以依据现有 Web 类型和 Access Layer OpenAPI 生成/整理 TypeScript 类型，并增加契约样例测试。类型生成是只读消费 OpenAPI，不要求修改 Python schema。

---

## 9. AG-UI 事件投影

事件投影由 `application/event-projector.ts` 负责，终端页面只消费投影结果。

必须保留的典型顺序：

```text
REASONING_START
REASONING_MESSAGE_CONTENT
REASONING_END
TOOL_CALL_START
TOOL_CALL_ARGS
TOOL_CALL_END
TOOL_CALL_RESULT
REASONING_START
REASONING_MESSAGE_CONTENT
REASONING_END
TEXT_MESSAGE_START
TEXT_MESSAGE_CONTENT
TEXT_MESSAGE_END
RUN_FINISHED
```

页面时间线的回归序列至少覆盖：

```text
thinking → text → tool → thinking → text
```

实现中应保留以下中文注释：

```ts
/**
 * 原始 AG-UI 事件按到达顺序投影，不能按类型重新分组。
 * Tool 会关闭当前可追加的 Thinking 展示块；后续 Reasoning 必须创建新块，
 * 否则历史回放会把工具前后的思考错误合并。
 */
export function projectEvent(state: TimelineState, event: AgUiEvent): TimelineState {
  // 具体实现由事件类型分支完成。
  return state;
}
```

禁止使用最终聚合文本重新伪造流式事件，也禁止为了终端动画把一个服务端 delta 拆成多个虚拟 delta。

---

## 10. Run 生命周期

### 10.1 Stop

运行中第一次 `Ctrl+C` 执行 stop：

- 请求现有 Session stop API。
- 保留已经产生的用户消息和助手部分内容。
- 页面显示明确的 stopped 终态。
- CLI 退出码根据命令模式决定，不能把 stop 显示为成功完成。

### 10.2 Cancel

`/cancel` 或显式 cancel 命令：

- 请求现有 Session cancel API。
- 由 Access Layer 决定本轮回滚范围。
- CLI 不在本地删除或重写 Session 历史。
- 页面等待服务端快照/响应确认后再展示最终结果。

### 10.3 进程退出

终端退出不等于自动 cancel。CLI 必须区分：

- 用户停止 run。
- 用户取消 run。
- 客户端网络断开。
- CLI 进程被关闭。

是否继续在服务端运行由现有服务端语义决定，CLI 不自行发明生命周期规则。

---

## 11. Approval 与 User Question

### 11.1 交互模式

审批必须自动进入当前可见区域：

```text
┌─ 需要你的确认 ─────────────────────────────┐
│ Agent 请求执行命令                         │
│ npm test                                   │
│                                             │
│ [a] 允许一次  [r] 本轮始终允许  [d] 拒绝   │
└─────────────────────────────────────────────┘
```

User Question 使用终端表单显示服务端提供的问题、选项和自由文本输入。CLI 只能提交答案，不能替换服务端问题定义或 request hash。

### 11.2 非交互模式

一次性命令遇到审批或用户问题时默认 fail-closed：

- 不自动允许。
- 不无限等待 TTY。
- 输出可识别的错误类型。
- 返回退出码 `2`。
- `stderr` 说明如何在交互模式中恢复。

`--yes` 不得隐式开启 `full_access`，也不得批准未展示目标的权限请求。

---

## 12. 非交互输出契约

默认规则：

- `stdout`：最终助手正文或明确要求的结构化数据。
- `stderr`：连接状态、Thinking 摘要、Tool 状态、警告和诊断信息。
- `--json`：单个最终 JSON 对象。
- `--jsonl`：按顺序输出标准化事件记录。
- `--quiet`：隐藏非错误的 `stderr` 状态。
- `NO_COLOR=1` 或 `--plain`：不输出 ANSI 颜色和动态光标控制。

建议退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 正常完成 |
| `1` | Agent 运行失败 |
| `2` | 需要审批或用户输入 |
| `3` | Access Layer 不可连接或协议错误 |
| `4` | CLI 参数或本地配置错误 |
| `130` | 用户中断 |

退出码属于公开 CLI 契约，发布后不能随意改变含义。

---

## 13. 安全与配置

- 默认只连接 loopback Access Layer。
- 自定义远程端点必须由用户显式配置。
- CLI 日志不得默认记录 Prompt 正文、完整工具参数、工具结果或凭据。
- 本地配置只保存端点、主题和显示偏好；API Key 仍由服务端管理。
- `full_access` 必须通过显式参数或交互选择开启，并显示风险说明。
- Approval 的“允许一次”和“本轮允许”必须保持不同作用域。
- JSON/JSONL 输出可能包含用户内容，文档必须提醒脚本使用者自行保护输出文件。

---

## 14. 中文注释规范

CLI 新增代码必须对以下内容写必要的中文注释：

- CLI 与 Access Layer 的 HTTP-only 架构边界。
- AG-UI 原始事件顺序和投影不变量。
- stop、cancel、disconnect、quit 的生命周期差异。
- Approval 和 User Question 的 fail-closed 安全行为。
- 终端宽度阈值和无颜色退化原因。
- stdout、stderr、JSON 和退出码的兼容契约。
- AbortController、并发 run 和 Session 切换的所有权。
- 不直读服务端文件或持久化状态的原因。

不需要注释：

- 明显的变量赋值。
- 简单数组映射。
- 一眼可理解的 JSX 标签。
- 只是把函数名翻译成中文的无信息注释。

示例：

```ts
// 错误：只复述代码。
// 设置加载状态为 true。
setLoading(true);

// 正确：说明生命周期所有权。
// Session 切换只分离当前页面，不中止服务端 run；返回该 Session 时再用事件恢复视图。
detachVisibleSession();
```

---

## 15. 开发阶段

所有阶段都遵守“不修改 `access_layer/` 和 `backend/`”的永久边界。

### 阶段 A：npm 骨架与只读诊断

- 建立顶层 `cli/` npm 包，不引入额外 `packages/` 外层。
- 实现 `k-agent doctor`。
- 实现 endpoint 配置。
- 实现 health、catalog、agents 的只读客户端。
- 建立 `terminal-page` 模块、Home 页面和设计 token 快照。

### 阶段 B：一次性 Run

- 实现 `k-agent run`。
- 实现 POST SSE 解析。
- 实现 human、JSON、JSONL 输出。
- 实现稳定退出码。

### 阶段 C：交互式 Chat

- 实现 Session 列表和切换。
- 实现宽屏、标准、紧凑和最小兼容四种布局。
- 实现 Composer、Thinking、Tool 和正文时间线。
- 实现 Command Palette、Session Switcher、Inspector 和滚动锁定。
- 实现 stop、cancel 与连接中断展示。

### 阶段 D：HITL 与 Workspace

- 实现 Approval。
- 实现 User Question。
- 实现 Workspace 浏览。
- 实现历史回放与开放 Interrupt 恢复。

### 阶段 E：Team 与 Automation

- 只映射现有公开 API。
- 不复制服务端调度状态机。
- 缺少 API 的命令保持不可用并给出明确说明。

### 阶段 F：npm 发布

- 验证包内容。
- 在干净目录安装 tarball。
- 验证 macOS、Linux、Windows 常用终端。
- 发布预览版本，再发布稳定版本。

---

## 16. 测试与验收

### 16.1 架构边界

- CLI 源码不存在 `access_layer`、`backend` 文件导入。
- CLI 不读取 `~/.k_agent` 或项目状态目录。
- 网络客户端只连接配置的 Access Layer 地址。
- 实施前后 `access_layer/` 和 `backend/` 相对基线完全一致。

### 16.2 协议与状态

- SSE 跨网络块时仍能正确拼接 frame。
- `messageId`、`toolCallId`、`runId` 相互隔离。
- `thinking → text → tool → thinking → text` 顺序保持不变。
- 工具错误显示为 error，但不会被客户端误判成协议断开。
- 未封口流在断线时显示 interrupted，不显示 complete。

### 16.3 终端页面

- 144 列以上稳定显示 Session、Conversation 和 Inspector 三栏。
- 110–143 列显示 Session + Conversation，Inspector 使用覆盖层。
- 80–109 列只显示主页面，Session 和 Inspector 使用全屏覆盖层。
- 小于 80 列启用最小兼容布局；小于 56 列给出明确提示但保持可操作。
- Home、Chat、Team、Automation、Doctor 页面具有独立快照测试。
- `/` 选择栏可全键盘操作：过滤确定、Tab 补全、Enter 执行、Esc 收起且不触发任何动作。
- `Ctrl+K` 面板与 `/` 选择栏共用同一命令目录，且在 Run 运行中仍可执行 stop / cancel。
- 不可用命令在两处入口都显示并注明原因，按 Enter 不产生任何动作。
- 需要参数的命令只补全草稿，不提交缺参数动作。
- 输入框支持在光标处插入与删除，中文和 Emoji 不会被切成半个字符。
- Shift+Tab 在一级模式之间循环，且不影响后台正在运行的 run。
- 首页不出现预置示例问题或占位文案。
- Command Palette 和 Session Switcher 可全键盘操作且即时打开。
- 用户向上滚动时不抢夺阅读位置，新事件以 unread 计数提示。
- 中文、Emoji、代码块和表格不会破坏边框对齐。
- Approval 在所有尺寸下都可见且可操作。
- `NO_COLOR=1` 和 `--plain` 不输出 ANSI 控制序列。
- 模型和工具输出中的 ANSI、OSC 和控制字符被安全清理。
- 中文输入法组合过程中按 Enter 不误提交。
- 所有设计常量可从 `terminal-page/design.ts` 集中修改。

### 16.4 生命周期与安全

- stop 保留本轮已产生内容。
- cancel 由服务端执行回滚，CLI 不改本地历史。
- 非交互命令遇到 HITL 返回退出码 `2`，不会卡死或自动批准。
- `--yes` 不等于 `full_access`。
- Session 切换不会把一个 run 的事件写到另一个 Session 页面。

### 16.5 npm 发布

```bash
npm run build
npm test
npm pack --dry-run
npm publish --dry-run
```

在干净临时目录安装生成的 tarball，并验证：

```bash
npm install -g ./k-agent-cli-<version>.tgz
k-agent --version
k-agent doctor
k-agent run "输出一行测试文本" --plain
```

发布包不得包含 `.env`、`.k_agent/`、Session 数据、API Key、测试快照中的真实用户内容或仓库无关文件。

---

## 17. 与现有文档的关系

- AG-UI 事件生命周期和顺序以 [`ag-ui-protocol.md`](../architecture/ag-ui-protocol.md) 为准。
- 服务边界和接口历史以 [`interface-change-record.md`](../reference/interface-change-record.md) 为准。
- Web 工作台视觉背景可参考 [`frontend-design-review-and-improvement-plan.md`](frontend-design-review-and-improvement-plan.md)，但 CLI 不复制浏览器布局实现。
- Team 和 Automation 的业务语义仍由现有服务端技术方案定义，CLI 只做公共 API 客户端。

---

## 18. 最终决策摘要

1. Python Access Layer 与 Agent Backend 保持原样。
2. CLI 使用 TypeScript / Node.js，并作为独立 npm 包发布。
3. CLI 永久只通过 Access Layer HTTP / SSE 工作。
4. 终端页面统一归属 `cli/src/terminal-page/` 模块。
5. `terminal-page/design.ts` 集中管理布局、主题、文案、符号和快捷键。
6. 页面模块只消费 ViewModel，不直接请求 API 或修改运行状态。
7. 非显然的架构、安全、顺序和生命周期代码必须写准确的中文注释。
8. 现有 API 不支持的能力保持不可用，不擅自修改 Python 服务补齐。
