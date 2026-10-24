# Web / 独立 CLI 功能对齐清单

更新日期：2026-10-24。范围是终端启动 `k-agent` 后的独立交互页面，网页底部终端不能替代 CLI。

## 用户确认的范围与例外

Web 与 CLI 原则上同步业务功能、必要信息、可见状态和行为语义，终端布局按键盘操作适配。**用户明确排除 CLI 语音输入、麦克风、听写、连续语音输入、文字朗读，以及 CLI 桌宠**；这些不属于 CLI 验收项。Web 现有语音功能和桌宠保留。CLI 不重新添加桌宠或朗读，也不请求麦克风授权。

本次新增的输入适配桥、浏览器页面和服务已删除，对应协议和测试也已清理。机器学习平台仅保存待确认方案，未启动实现。

## 已交付交互

- `/projects` 是项目首页。先进入一个项目，再在其中新建目录会话、隔离 Worktree，或从已有会话做同目录 / 隔离分支。顶栏会标出共享目录。当前项目下的首次发送会先创建项目会话。
- Ctrl+G / `/workbench` 是完整业务入口：76 项业务和本地操作，其中 67 个 HTTP 路由、方法和必填业务字段与真实 OpenAPI 对齐。
- 关键词搜索、键盘选择、逐字段表单、写操作确认、错误与结果展示。JSON 数组和对象通过结构化编辑器管理成员、配置、环境变量、参数、能力与答案；Enter 进入、a 新增、d 删除、Ctrl+S 保存，未知字段保留。
- 结果是可导航的业务列表与分层详情，展示状态、成员、任务、消息、产物、审批及运行记录。上下文动作继承所选对象 ID，不要求手动复制；文件与 Diff 直接显示正文并可分页。冲突解决自动带入文件版本 Hash。
- 会话顶栏显示服务端 Local / Worktree、分支 / detached 与实际 cwd；会话切换重取快照和能力，不隐式停止其他 Session 的运行。历史与实时均按事件生命周期呈现。
- MCP / Skill 全局启用与本轮选择分开：s 本轮勾选、a 全选、n 全不选、Space 修改全局配置。本轮空数组表示不选。模型菜单使用对应 Agent 的模型目录。
- 团队和定时执行的对话、思考、工具、审批与错误可分层查看；详情每 2.5 秒刷新，事件查询支持 afterSeq、taskId、limit。轮询与 Web 的 SSE 实现不同，但操作与所需状态保留。
- PTY 通过 Ink 的 suspendTerminal 交给宿主终端，原样传输字节、VT、光标、全屏程序和窗口尺寸。Ctrl+] 返回，Alt+T 新标签、Alt+N/P 切换、Alt+Q 关闭；Ctrl+C/D/P 等普通控制键继续传给 shell。隐藏不终止 shell，退出应用关闭自己的连接。
- 主题、阅读缩放和面板显示可配置并持久化。CLI 阅读缩放调整行距，不宣称改变宿主字体。桌宠只在 Web 端显示。
- 图片 / 视频附件最多 4 个、每个 20MB，可只发送附件；正文与历史保留附件名称及类型。支持消息复制。文字朗读只在 Web 端。

## 功能域对照

| Web 功能域 | CLI 对应能力 | 信息及行为 |
| --- | --- | --- |
| 对话、Markdown、代码、思考、工具、审批与问题 | 原聊天页面、Ctrl+O、完整时间线、分层对话 | 到达顺序、参数、输出、结果、审批状态、上下文告警 |
| 会话历史、分叉、删除、压缩 | Ctrl+P 会话列表、`/fork`、`/compact` | 当前项目内的会话；同目录或隔离分支；从最后一条回复分支；压缩 / 重置 |
| 项目管理与置顶 | `/projects` 项目首页，以及工作台中的目录浏览、编辑和删除 | 先进入项目再管理会话；名称、路径、Git 身份、目标分支、置顶、共享目录 |
| Local / Worktree、起点、绑定、Handoff | 项目首页的开工方式、`/handoff`、会话列表中的移入 | 项目目录 / 独立 Worktree / 带当前改动；同目录或隔离分支；服务端错误 |
| 文件、变更与 Diff | 可选择的文件与变更列表、直接正文预览 | 路径、状态、大小、二进制和截断标记、多行全文 |
| Git 提交、合入和冲突 | 整合状态、提交、开始 / 继续 / 中止、冲突编辑 | 目标分支、冲突、Hash 校验、结果 OID、失败原因 |
| 模型 / MCP / Skill 配置与导入 | 先读取当前配置的逐项编辑器、ZIP 导入 | 新增 / 修改 / 删除、来源、警告、凭据掩码与工具详情 |
| 模型、Agent、权限、思考、网络、恢复与本轮能力 | 运行菜单与设置表单 | Agent 专属模型目录；启用与选择分离；下一轮生效 |
| 图片、视频与复制 | 附件与复制 | 文字朗读、语音输入和麦克风只在 Web 端 |
| Skill / MCP 广场 | 搜索、分页、预览、安装、配置字段 | 来源、安装状态、冲突、限制、警告和所需配置 |
| 团队 | 创建、逐项成员编辑、任务 / 成员 / 产物 / 消息详情、追踪与审批 | 主管状态、角色、依赖、尝试次数、错误与到达顺序 |
| 定时任务 | 创建 / 编辑 / 删除、暂停 / 恢复 / 立即执行、记录与审批 | 日期、星期、时间、时区、能力、下一次执行与结果 |
| 健康与沙箱 | `/doctor` 与完整能力详情 | 服务状态、工具数量、Agent 版本、沙箱及安装建议。首页 1–4 对应网页侧栏：项目、技能、连接器、定时任务 |
| 多标签会话终端 | 原生宿主 PTY、多标签、返回与恢复 | 同一服务端 cwd；保留完整 VT 字节和普通 shell 控制键 |
| 主题、阅读和面板 | CLI 本地偏好 | 按终端能力适配；不以浏览器像素布局作为终端验收。桌宠只在 Web 端 |
| Web 语音输入、连续语音对话与文字朗读 | 用户明确排除 CLI 此项 | Web 保留；CLI 不请求麦克风，也不调用本机朗读 |

## 验证证据与实际边界

- CLI：`npm --prefix cli run check`、`npm --prefix cli test`（116 项通过）、`npm --prefix cli run build`。
- API 契约：`PYTHONPYCACHEPREFIX=/private/tmp/k_agent_cli_parity_pycache .venv/bin/python -m cli.scripts.check-workbench-contract`，67 个路由、HTTP 方法与必填请求字段通过。
- 真实集成：构建后运行 `PYTHONPYCACHEPREFIX=/private/tmp/k_agent_cli_parity_pycache .venv/bin/python -m cli.scripts.run-access-layer-e2e`。一次性目录内经真实 HTTP / WebSocket 验证项目、Local / Worktree cwd、文件 / 变更 / Diff、提交 / 整合、分叉 / Handoff、定时任务管理、团队消息及状态、真实 PTY VT 与终端恢复；自动关停并清理。未调用模型、外网或麦克风。
- 后端相关回归共 80 项通过：项目 Git、整合、定时任务、会话文件、终端消息、能力目录、广场、Skill 导入、团队文件。未宣称后端全量套件通过。
- Web：`npm --prefix frontend run check` 与 `npm --prefix frontend run build:client` 通过；保留已有大 bundle 提示。文字朗读留在 Web 端。
- 交互测试包含成员逐字段新增 / 编辑、对象选择继承 ID、冲突 Hash 自动带入、原始 PTY 字节与标签生命周期、媒体限制、能力计数及 Agent 模型目录。CLI 不再包含朗读队列。
- `git diff --check` 通过。未提交或推送。

真实模型调用、多媒体理解效果、外部广场服务安装和跨操作系统音频不是此次隔离测试证明的结果。客户端行为与信息已实现，不把 mock、契约检查或本机平台结果宣称为所有外部服务和硬件成功。新增 API 导出由功能映射测试检查；App 内本地交互及导出函数内部语义，仍必须按 AGENTS.md 逐项检查另一端。

## 可执行功能目录

下表与 `cli/src/application/workbench-catalog.ts` 对应。API 函数级映射见 [web-cli-api-map.json](web-cli-api-map.json)。

| 分组 | CLI 操作 ID | 功能 | 请求 |
| --- | --- | --- | --- |
| 项目 | `projects` | 项目列表与归属、分支、路径 | `GET /api/projects` |
| 项目 | `project-create` | 注册项目目录 | `POST /api/projects` |
| 项目 | `directory` | 浏览目录 | `GET /api/fs/browse` |
| 项目 | `mkdir` | 创建子目录 | `POST /api/fs/mkdir` |
| 项目 | `project-edit` | 修改名称与目标分支 | `PATCH /api/projects/:projectId` |
| 项目 | `project-clear-target` | 清除目标分支 | `PATCH /api/projects/:projectId` |
| 项目 | `project-delete` | 删除项目注册 | `DELETE /api/projects/:projectId` |
| 项目 | `repository` | 查看仓库分支 | `GET /api/projects/:projectId/repository` |
| 项目 | `project-session` | 创建项目会话 / Local 或 Worktree | `POST /api/projects/:projectId/sessions` |
| 项目 | `bind` | 将会话迁入项目 | `POST /api/sessions/:sessionId/project` |
| 项目 | `pin` | 置顶 / 取消置顶项目（CLI 本地） | CLI 本地 |
| 会话 | `sessions` | 全部会话、归属、环境与更新时间 | `GET /api/sessions` |
| 会话 | `session` | 完整会话 / 能力 / 原始事件 / 审批 | `GET /api/sessions/:sessionId` |
| 会话 | `session-open` | 打开会话 | `GET /api/sessions/:sessionId` |
| 会话 | `session-delete` | 删除会话 | `DELETE /api/sessions/:sessionId` |
| 会话 | `fork` | 从消息或轮次分叉 / 同目录或独立 Worktree | `POST /api/sessions/:sessionId/fork` |
| 会话 | `context` | 上下文容量、压缩指标与告警 | `GET /api/sessions/:sessionId/context` |
| 会话 | `compact` | 压缩上下文 / 重置自动压缩 | `POST /api/sessions/:sessionId/compact` |
| 工作区 | `environment` | 会话实际执行环境 | `GET /api/sessions/:sessionId/environment` |
| 工作区 | `handoff` | 切换 Local / Worktree | `POST /api/sessions/:sessionId/handoff` |
| 工作区 | `changes` | 基于会话起点的变更列表 | `GET /api/sessions/:sessionId/changes` |
| 工作区 | `diff` | 查看文件 Diff | `GET /api/sessions/:sessionId/diff` |
| 工作区 | `files` | 工作区文件列表 | `GET /api/sessions/:sessionId/workspace` |
| 工作区 | `file` | 文件全文 / 二进制及截断状态 | `GET /api/sessions/:sessionId/workspace/file` |
| 整合 | `commit` | 提交会话工作区 | `POST /api/sessions/:sessionId/git/commit` |
| 整合 | `integration` | 合入状态 / 冲突 / 目标分支 | `GET /api/sessions/:sessionId/integration` |
| 整合 | `integration-start` | 合入目标分支 | `POST /api/sessions/:sessionId/integration` |
| 整合 | `integration-continue` | 继续合入 | `POST /api/sessions/:sessionId/integration/continue` |
| 整合 | `integration-abort` | 中止合入 | `POST /api/sessions/:sessionId/integration/abort` |
| 整合 | `conflict` | 读取冲突文件和版本 Hash | `GET /api/sessions/:sessionId/integration/file` |
| 整合 | `resolve` | 解决冲突（校验 expectedHash） | `PUT /api/sessions/:sessionId/integration/file` |
| 配置 | `config-models` | 模型 完整配置与来源 | `GET /api/config/models` |
| 配置 | `save-models` | 编辑 模型（新增 / 修改 / 删除） | `PUT /api/config/models` |
| 配置 | `config-mcp` | MCP 完整配置与来源 | `GET /api/config/mcp` |
| 配置 | `save-mcp` | 编辑 MCP（新增 / 修改 / 删除） | `PUT /api/config/mcp` |
| 配置 | `config-skills` | Skill 完整配置与来源 | `GET /api/config/skills` |
| 配置 | `save-skills` | 编辑 Skill（新增 / 修改 / 删除） | `PUT /api/config/skills` |
| 配置 | `import-skill` | 从本地 ZIP 导入 Skill | CLI 本地 |
| 配置 | `mcp-tools` | MCP 工具与资源能力 | `GET /api/mcp/capabilities` |
| 配置 | `mcp-reload` | 重新连接 MCP | `POST /api/mcp/reload` |
| 配置 | `agents` | Agent 可用性 / 版本 / 模型 / 恢复能力 | `GET /api/agents` |
| 运行 | `runtime` | 设置本轮能力、网络、思考与恢复选项 | CLI 本地 |
| 运行 | `attach` | 添加图片或视频（最多 4 个，每个 20MB） | CLI 本地 |
| 运行 | `attachments-clear` | 清除待发送附件 | CLI 本地 |
| 运行 | `timeline` | 完整时间线：消息 ID / 思考 / 工具参数与结果 | CLI 本地 |
| 运行 | `copy` | 复制消息 / 工具结果到剪贴板 | CLI 本地 |
| 工作区 | `shell` | 进入会话 PTY（Ctrl+] 返回，可多标签 ID） | CLI 本地 |
| 广场 | `market-mcp` | 搜索 MCP（分页） | `GET /api/marketplace/mcp` |
| 广场 | `preview-mcp` | 预览 MCP 安装、冲突与所需配置 | `POST /api/marketplace/mcp/item/preview` |
| 广场 | `install-mcp` | 安装 MCP | `POST /api/marketplace/mcp/item/install` |
| 广场 | `market-skills` | 搜索 Skill（分页） | `GET /api/marketplace/skills` |
| 广场 | `preview-skills` | 预览 Skill 安装、冲突与所需配置 | `POST /api/marketplace/skills/:slug/preview` |
| 广场 | `install-skills` | 安装 Skill | `POST /api/marketplace/skills/:slug/install` |
| 团队 | `teams` | 团队列表 | `GET /api/teams` |
| 团队 | `team-create` | 创建团队与成员 | `POST /api/teams` |
| 团队 | `team-detail` | 完整团队看板 / 任务 / 成员 / 消息 / 产物 / 审批 | `GET /api/teams/:teamId` |
| 团队 | `team-events` | 团队增量事件与任务追踪 | `GET /api/teams/:teamId/events` |
| 团队 | `team-command` | 暂停 / 恢复 / 取消团队 | `POST /api/teams/:teamId/commands` |
| 团队 | `team-message` | 给成员发送消息 | `POST /api/teams/:teamId/messages` |
| 团队 | `team-approval` | 处理团队审批 / 用户问题 | `POST /api/teams/:teamId/approvals/:approvalId/resume` |
| 团队 | `team-files` | 团队产物文件列表 | `GET /api/teams/:teamId/workspace` |
| 团队 | `team-file` | 读取团队产物文件 | `GET /api/teams/:teamId/workspace/file` |
| 定时 | `automations` | 任务列表 / 下次执行 / 最近结果 | `GET /api/scheduled-tasks` |
| 定时 | `auto-create` | 创建定时任务 | `POST /api/scheduled-tasks` |
| 定时 | `auto-detail` | 定时任务完整配置 | `GET /api/scheduled-tasks/:taskId` |
| 定时 | `auto-edit` | 编辑定时任务 | `PUT /api/scheduled-tasks/:taskId` |
| 定时 | `auto-delete` | 删除定时任务 | `DELETE /api/scheduled-tasks/:taskId` |
| 定时 | `auto-pause` | 暂停定时任务 | `POST /api/scheduled-tasks/:taskId/pause` |
| 定时 | `auto-resume` | 恢复定时任务 | `POST /api/scheduled-tasks/:taskId/resume` |
| 定时 | `auto-run-now` | 立即执行定时任务 | `POST /api/scheduled-tasks/:taskId/run-now` |
| 定时 | `auto-runs` | 执行历史 / 错误 / 时间 | `GET /api/scheduled-tasks/:taskId/runs` |
| 定时 | `auto-session` | 执行对话与完整事件 | `GET /api/scheduled-tasks/:taskId/runs/:runId/session` |
| 定时 | `auto-approval` | 处理定时执行审批 / 用户问题 | `POST /api/scheduled-tasks/:taskId/runs/:runId/resume` |
| 偏好 | `preferences` | 界面主题、阅读缩放与面板显示 | CLI 本地 |
| 诊断 | `health` | 服务 / 沙箱可用性与安装建议 | `GET /api/health` |
| 诊断 | `catalog` | 能力目录与来源 | `GET /api/catalog` |
