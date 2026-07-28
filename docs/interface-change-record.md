# K Agent 接口与重要修改记录

> 维护约定：后续涉及前后端接口、MCP/Skill/Memory 加载链路、系统提示词拼接、权限边界或缓存策略的重要修改，都需要在本文追加记录。

## 2026-07-28 接入层与无状态 Agent Backend

- 请求链路调整为 `frontend -> access_layer -> backend/agent`，其中
  `access_layer/` 与 `backend/` 为并列的顶层目录；前端接口仍为
  `POST /api/agent`，因此前端无需感知内部服务拆分。
- `AgentAccessLayer` 统一负责：
  - AG-UI 输入转换与 SSE 输出适配。
  - 会话读取、并发锁和最终快照持久化。
  - 模型、附件、`skillIds` 校验；未知或已禁用 Skill 返回 HTTP 400。
  - System Prompt、历史消息、Memory、Skill、MCP Prompt/Instructions 和图片
    消息的拼接。
  - 根据请求消息中引用的路径加载嵌套 Memory 与条件 Skill 上下文。
- Agent Backend 的唯一运行输入改为完整的 `AgentRunRequest`。Agent 不再接收
  `session_id`，不再导入 session/memory/prompt/skill 模块，也不维护会话级缓存。
- App 生命周期不再创建共享 Agent 实例；每次请求创建并销毁独立 Agent 与 MCP
  manager，避免连接状态或可变运行状态跨会话泄漏。
- FastAPI 应用入口和服务启动器分别为 `access_layer/main.py` 与
  `access_layer/run_server.py`；`backend/` 不定义前端 API 路由。
- Agent Backend 现在是独立 FastAPI 进程，入口为 `backend/main.py`，默认监听
  `127.0.0.1:3002`。接入层通过内部 NDJSON 流式 HTTP 调用
  `POST /internal/agent/run`，不再在接入层进程内实例化 Agent。
- `npm run dev` 同时启动 Access Layer、Agent Backend 和 Vite；`npm run start`
  同时启动两个 Python 服务。
- `GET /api/health` 新增 `agentBackendOk`，用于确认独立 Agent Backend 是否可达。
- 新增架构边界测试，阻止 Agent Backend 重新依赖接入层或会话上下文模块。

## 2026-07-28 Claude Code-like 工具扩展

- 默认 `coding` preset 的本地工具从 13 个扩展到 19 个。
- 新增可直接使用的 Claude Code-like 工具：
  - `LS`：列出工作区目录内容。
  - `WebFetch`：抓取 HTTP/HTTPS 页面并返回可读文本。
  - `WebSearch`：通过网页搜索返回候选结果。
  - `NotebookEdit`：插入、替换或删除 `.ipynb` 单元格。
  - `ListMcpResourcesTool`：列出已连接 MCP 服务暴露的资源。
  - `ReadMcpResourceTool`：按 `server_id` 与 `uri` 读取 MCP 资源。
- `Skill`、`ListMcpResourcesTool`、`ReadMcpResourceTool` 统一通过请求级 MCP manager 绑定，避免跨请求复用 MCP 连接状态。
- 暂未接入 `Agent/Monitor/LSP/Cron/Worktree/Team` 等需要独立运行时状态机或 IDE 能力的工具，避免暴露半成品能力。

## 2026-07-28 前后端目录隔离与配置迁移

- 边界要求：`frontend/src` 不允许读取、导入或解析任何后端文件；MCP、Skill、模型、会话等数据读取必须全部通过 HTTP API 完成。
- 前端只展示后端接口名与后端返回的业务数据，不展示后端物理文件路径。
- `frontend/scripts/check-boundary.mjs` 会检查 `frontend/src` 内是否出现后端跨目录引用或 Node 文件读写 API。
- 前端工程已整体迁移到 `frontend/`：
  - `frontend/src/`
  - `frontend/index.html`
  - `frontend/vite.config.ts`
  - `frontend/tsconfig.json`
  - `frontend/package.json`
  - `frontend/package-lock.json`
- 后端运行配置已迁移到 `backend/config/runtime/`：
  - `backend/config/runtime/models.config.json`
  - `backend/config/runtime/mcp.config.json`
  - `backend/config/runtime/mcp.config.example.json`
  - `backend/config/runtime/skills.config.json`
- `backend/config/config.py` 的 `MCP_CONFIG_PATH` 默认值改为后端运行配置目录。
- `backend/main.py` 的模型与 Skill 配置常量改为绝对后端路径，避免受启动工作目录影响。
- `frontend/package.json` 中的后端启动脚本会先 `cd ..` 回到项目根目录，再启动 FastAPI。

## 2026-07-28 MCP 与 Skill 配置接口完善

### MCP 配置

- `GET /api/config/mcp`
  - 用途：读取当前可编辑 MCP 配置，并合并后端实际连接状态。
  - 配置来源：优先读取 `settings.mcp_config_path`，不存在时可回退到 `mcp.config.example.json` 作为展示模板。
  - 支持格式：
    - 推荐：`{"mcpServers": {"server-id": {...}}}`
    - 兼容：`{"servers": [{"id": "server-id", ...}]}`
  - 返回字段：
    - `path`：保存目标路径。
    - `source`：本次读取来源。
    - `format`：源文件格式，通常为 `mcpServers`。
    - `isTemplate`：是否来自示例配置。
    - `servers`：可编辑服务列表，包含 `id/type/command/args/env/url/headers/enabled/status/scope/toolCount/resourceCount/error`。
    - `warnings/blocked/suppressed`：加载器产生的警告、策略屏蔽和去重结果。
  - 安全边界：`env` 与 `headers` 只返回键名，值统一显示为 `***`。

- `PUT /api/config/mcp`
  - 用途：保存 MCP 配置，并热重载后端预览连接。
  - 请求体：`{"servers": McpServerInput[]}`。
  - `McpServerInput` 字段：`id/type/command/args/env/url/headers/enabled`。
  - 校验规则：
    - `stdio` 必须提供 `command`。
    - `http/sse/ws` 必须提供 `url`。
    - `***` 表示保留旧的 `env` 或 `headers` 值。
  - 写入格式：统一写为 Claude 兼容的 `mcpServers` 对象格式。
  - 返回：`ok/restartRequired/servers`，其中 `restartRequired=false` 表示配置中心状态已热重载。

- `GET /api/mcp/status`
  - 用途：读取后端 MCP manager 的连接快照。
  - 返回：`servers` 和 `loadResult`。

- `GET /api/mcp/capabilities`
  - 用途：读取当前已连接 MCP 服务暴露的工具、资源和 prompts。
  - 返回：`tools/resources/prompts`。
  - 前端用途：配置中心统计能力数量，并为后续 MCP Prompt 选择、资源读取 UI 留出契约。

- `POST /api/mcp/reload`
  - 用途：手动热重载 MCP/Skill 运行时预览状态。
  - 行为：关闭旧 app-level MCP manager，重新加载配置、连接服务、重建 Agent 预览实例，并清理提示词缓存。

### Skill 配置与载入

- `GET /api/config/skills`
  - 用途：同时返回旧 JSON 可编辑 Skill 与后端实际载入 Skill。
  - 返回字段：
    - `path`：`skills.config.json` 路径。
    - `projectSkillDir`：项目目录型 Skill 位置，默认为 `.k_agent/skills`。
    - `skills`：旧 JSON 配置项，仍可编辑保存。
    - `loadedSkills`：后端真实载入列表，包含项目、用户、配置与 MCP Prompt 转换出的 Skill。
  - `loadedSkills` 元数据：`source/loadedFrom/filePath/baseDir/paths/whenToUse/userInvocable/editable`。

- `PUT /api/config/skills`
  - 用途：保存旧 JSON Skill 配置并刷新系统提示词缓存。
  - 返回：`ok/enabledCount/skills`。

- `POST /api/skills`
  - 用途：通过 zip 压缩包导入项目目录型 Skill。
  - 请求体：`multipart/form-data`，字段 `file` 为 `.zip` 文件。
  - 写入位置：`.k_agent/skills/{normalized-name}/`，其中 `normalized-name` 来自 `SKILL.md` frontmatter 的 `name`。
  - 校验规则：
    - 必须是有效 `.zip`，压缩包不超过 20MB，解压后不超过 50MB，文件数不超过 500。
    - 压缩包必须包含且只能包含一个 Skill 入口 `SKILL.md`。
    - `SKILL.md` frontmatter 必须包含 `name` 和 `description`。
    - 拒绝绝对路径、`..` 路径穿越、符号链接和特殊文件。
  - 行为：校验通过后解压到项目 Skill 目录，清理 Skill/Prompt 缓存，并热重载运行时预览状态。

### 前端配置中心

- MCP 页新增：
  - transport 类型选择：`stdio/http/sse/ws`。
  - URL 型 MCP 的 headers 编辑。
  - 连接状态、scope、工具数量、资源数量和错误信息展示。
  - 能力统计条：工具、资源、Prompts 数量与手动重新载入按钮。

- Skills 页新增：
  - 显示后端实际载入的 Skill 列表和来源。
  - 通过拖拽或点击上传 `.zip` 导入项目目录型 Skill，并展示后端校验失败原因。

### 缓存与边界

- MCP 保存和手动 reload 会调用运行时热重载逻辑，同时重置 prompt cache。
- 请求执行仍使用 per-request MCP manager，避免 MCP SDK stream 绑定事件循环导致串流请求异常。
- app-level MCP manager 只用于健康检查、配置中心预览和能力读取。
