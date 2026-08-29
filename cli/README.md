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

- `1`–`4`：工作 / 团队 / 自动 / 诊断
- `5`–`7`：首页快捷提问
- `Ctrl+K`：命令面板
- `Ctrl+P`：会话切换
- `Ctrl+O`：Activity / Workspace Inspector
- `Ctrl+C`：运行中执行 stop；空闲时退出
- `Esc`：关闭普通覆盖层，不代表拒绝审批
- `!`：返回仍待处理的审批或用户问题

终端页面的颜色、宽度阈值、文案和密度集中在 `src/terminal-page/design.ts`。页面组件不直接访问 HTTP；所有网络请求仍由 application/client 层完成。

需要 Node.js 22.12 或更高版本。
