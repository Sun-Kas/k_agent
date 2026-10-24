# K Agent CLI

K Agent 的 TypeScript / Node.js 终端客户端。CLI 只通过 Access Layer 的公开 HTTP 与 AG-UI SSE 接口工作，不直接读取服务端文件，也不连接 Agent Backend 内部端口。

## 本地开发

在仓库根目录一键启动 Access Layer、Agent Backend 和 CLI（不启动 Web 前端）：

```bash
npm --prefix cli run dev:local
```

如果已将 `scripts/dev-local.mjs` 链接到 `PATH` 中的 `k-agent`，则无需切换目录，在任意位置直接运行：

```bash
k-agent
k-agent doctor
```

启动器会复用已经健康的本地服务，并只在退出时关闭本次由它启动的进程。Python 日志写入系统临时目录，不会污染 TUI。目标端口被其他服务占用时会直接报错。

`dev:local` 只属于源码仓库开发流程，不随 npm 发布包提供。分别执行 CLI 开发命令时：

```bash
npm install
npm run check
npm test
npm run build
npm run dev -- doctor
```

默认连接 `http://127.0.0.1:3001`，可通过 `--endpoint` 或 `K_AGENT_ENDPOINT` 修改。

## 基本命令

```bash
k-agent
k-agent chat
k-agent run "总结当前项目"
k-agent sessions list
k-agent team list
k-agent automation list
k-agent doctor
```

一次性任务支持 `--json`、`--jsonl`、`--quiet`；非交互模式遇到审批或用户问题时不会自动批准，返回退出码 `2`。`stdout` 只输出助手正文或显式请求的结构化数据，运行状态写入 `stderr`。

交互页面快捷键：

- `?`：快捷键说明
- `1`–`4`：项目 / 技能 / 连接器 / 定时任务（输入框为空时，与网页侧栏一致）
- `↑` / `↓`：提示符历史（首页空输入时选择功能）
- 终端滚动查看更早的对话，提示符保持在底部
- 运行中仍可输入，回车后排队，当前 Run 结束后再发送
- `/projects`：进入项目。在项目里新建对话、同目录分支或隔离 Worktree，并选择目标分支
- `/fork`：从当前会话分支；`/fork worktree` 使用隔离目录，`/fork here` 从最后一条回复
- `/handoff`：在 Local 与 Worktree 之间切换
- `/compact`：压缩当前会话上下文
- `Ctrl+K`：命令面板
- `Ctrl+P`：会话切换
- `Ctrl+O`：展开工具调用与思考详情
- `Ctrl+C`：运行中执行 stop；空闲时退出
- `Esc`：关闭普通覆盖层，不代表拒绝审批
- `!`：返回仍待处理的审批或用户问题
- `/mcp`：管理 MCP（Space 开关、Enter 查看工具、`r` 重载）；也支持 `/mcp enable [name|all]`、`/mcp disable`、`/mcp reload`
- `/model`、`/agent`、`/permissions`：切换本次会话的模型、Agent 与权限，下一轮发送生效
- `/skill`：开关 Skill（写入 Access Layer，与 Web 配置中心同一份目录）

```bash
k-agent mcp list
k-agent mcp enable calendar
k-agent mcp disable all
k-agent mcp reload
k-agent model list
k-agent agent list
k-agent skill list
k-agent skill enable writer
k-agent skill disable all
```

## 中文输入法与光标

CLI 使用真实终端光标作为输入法候选窗的定位锚点，不再在页面里绘制假的光标字符。该行为同时覆盖：

- 页面底部的主输入框
- 用户问题弹窗中的“补充说明”输入行

光标位置按终端显示列宽计算，已处理中文、Emoji、组合字符、换行和自动折行。因此在 macOS 中文输入法中，候选窗应跟随当前插入点，而不是停留在终端底部；使用方向键移动插入点后，候选窗也会跟随新的位置。

输入法需要终端提供真实的 TTY 和 ANSI 光标定位能力。请直接在 Terminal、iTerm2、WezTerm 等交互式终端中运行，不要把 CLI 输出通过管道重定向到文件或非交互式输出面板。如果候选窗仍未对齐，请先在仓库根目录更新依赖并重新启动：

```bash
npm --prefix cli install
npm --prefix cli run dev:local
```

终端页面的颜色、宽度阈值、文案和密度集中在 `src/terminal-page/design.ts`。页面组件不直接访问 HTTP；所有网络请求仍由 application/client 层完成。

需要 Node.js 22.12 或更高版本。


## 完整功能工作台

启动 `k-agent` 后按 **Ctrl+G** 或输入 `/workbench`。关键词搜索项目、会话、Git、配置、广场、团队、定时或偏好，↑↓ 选择、Enter 打开。Tab / ↑↓ 切换字段，Enter 编辑，Ctrl+S 提交，写操作显示确认；Esc 返回。

成员、模型、MCP、Skill、字典和数组通过逐项编辑器管理，a 新增、d 删除、Ctrl+S 保存；配置先读当前值，定时任务编辑填写 ID 后 Ctrl+L 读取。详情按业务对象分层浏览，Enter 展开或打开关联记录，上下文快捷操作继承 ID。文件与 Diff 直接预览，↑↓ / PgUp / PgDn 翻阅。

MCP / Skill 菜单：s 本轮勾选，a 全选，n 全不选；Space 开关全局配置。空 ID 数组表示本轮不选能力。工作台还提供网络与恢复参数、图片视频附件、复制、主题、阅读缩放和面板。CLI 不提供桌宠，也不提供文字朗读。

会话 PTY 原样交给宿主终端，支持 VT 和全屏应用：Ctrl+] 返回、Alt+T 新标签、Alt+N/P 切换、Alt+Q 关闭。CLI 按用户明确要求不提供语音输入、听写、连续语音输入、麦克风访问或文字朗读；Web 现有语音功能保留。

完整对照和验证边界见 [Web / CLI 对齐清单](../docs/features/web-cli-parity.md)。
