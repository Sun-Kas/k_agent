# K Agent CLI

K Agent 的 TypeScript / Node.js 终端客户端。CLI 只通过 Access Layer 的公开 HTTP 与 AG-UI SSE 接口工作，不直接读取服务端文件，也不连接 Agent Backend 内部端口。

## 本地开发

在仓库根目录一键启动 Access Layer、Agent Backend 和 CLI（不启动 Web 前端）：

```bash
npm --prefix cli run dev:local
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
- `1`–`4`：工作 / 团队 / 自动 / 诊断（输入框为空时）
- `↑` / `↓`：提示符历史（首页空输入时选择功能）
- 终端滚动查看更早的对话，提示符保持在底部
- 运行中仍可输入，回车后排队，当前 Run 结束后再发送
- `Ctrl+K`：命令面板
- `Ctrl+P`：会话切换
- `Ctrl+O`：展开工具调用与思考详情
- `Ctrl+C`：运行中执行 stop；空闲时退出
- `Esc`：关闭普通覆盖层，不代表拒绝审批
- `!`：返回仍待处理的审批或用户问题
- `/model`、`/mcp`、`/skill`、`/permissions`：只读查看；改配置请用 Web

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
