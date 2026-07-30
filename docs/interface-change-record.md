# K Agent 接口与重要修改记录

> 维护约定：后续涉及前后端接口、MCP/Skill/Memory 加载链路、系统提示词拼接、权限边界或缓存策略的重要修改，都需要在本文追加记录。

## 2026-07-29 Agent Backend Langfuse 可观测性

- Agent Backend 启动时从进程环境读取 `LANGFUSE_PUBLIC_KEY`、
  `LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL`，执行鉴权检查，并在
  `GET /internal/health` 的 `langfuse` 字段返回
  `configured/enabled/authenticated/lastError`，不返回凭据。
- 每次 `/internal/agent/run` 创建一个 `agent` 根观测，以前端 session ID
  作为 Langfuse session；每轮模型调用创建 `generation`，每次本地或 MCP
  工具调用创建 `tool` 子观测。
- Trace 元数据包含请求 run ID、模型、MCP/Skill ID、推理强度、Memory 数量和
  request ID；模型输入输出、工具参数结果和耗时记录在对应观测中。
- 常见 token、password、secret、Authorization 等字段统一脱敏；图片 Data URL
  只记录媒体类型。SDK 初始化、更新、结束、刷新失败均采用 fail-open 策略，
  不影响 Agent 正常执行。
- 进程关闭时执行 `flush()` 和 `shutdown()`；可通过
  `LANGFUSE_ENABLED`、`LANGFUSE_TRACING_ENVIRONMENT`、
  `LANGFUSE_SAMPLE_RATE`、`LANGFUSE_TIMEOUT_SECONDS` 调整运行行为。

## 2026-07-29 会话级 MCP/Skill 选择

- 每次 `POST /api/agent` 开始运行时，Access Layer 把本轮
  `mcpServerIds`、`skillIds` 写入对应的 `data/sessions/{sessionId}.json`。
- `GET /api/sessions/{sessionId}` 新增可空的 `capabilities`：
  `{"mcpServerIds": string[], "skillIds": string[]}`。缺少该字段表示旧会话尚未保存
  选择；字段存在且数组为空表示用户明确取消全部能力。
- 前端打开或切换会话时恢复该会话的选择，目录刷新只移除已禁用或已删除的条目；
  同一会话后续消息默认沿用，直到用户修改。新会话仍使用 MCP 默认全选、Skill
  默认不选，并且不会继承其他会话的偏好。

## 2026-07-29 MCP/Skill 摘要目录归属 Access Layer

- 新增 `data/mcp.json` 与 `data/skill.json`，仅保存前端选择列表需要的
  `id/name/description/enabled`；`GET /api/catalog` 直接读取这两个文件，不连接
  Agent Backend，也不扫描 `SKILL.md`。
- `GET/PUT /api/config/mcp` 与 `GET/PUT /api/config/skills` 改由 Access Layer
  管理。MCP 保存后仅通知 Agent Backend 重载连接；Skill 保存或导入不再要求
  Agent Backend 重载列表。
- `POST /internal/agent/run` 不再接收 `mcpServerIds`、`skillIds`，改为接收由
  Access Layer 校验并解析好的 `mcpServers`、`skills` 对象数组。
- Access Layer 仅在发起运行时读取被选中 Skill 的 `SKILL.md`，并把摘要、指令及
  执行元数据一并发送。Agent Backend 的 Skill 工具只使用请求内定义，不访问
  Skill 目录。
- 每次运行的 MCP manager 只使用请求内携带的选中 MCP 连接定义；MCP/Skill
  摘要同时进入本轮上下文，Agent Backend 不再提供内部配置列表接口。
- MCP 配置保存或重新载入后，Access Layer 会把 Agent Backend 的真实连接状态
  合并回每个服务，前端展示 `已连接/连接失败/已禁用/连接中`，并在失败时显示
  原始错误摘要。`bearerTokenEnv` 只接受大写环境变量名，避免把令牌内容误填为
  变量名。
- 公共配置初始化时通过 `load_dotenv(..., override=False)` 一次性加载项目根目录
  `.env`；进程已有变量保持最高优先级，后续 MCP、模型和其他模块统一通过
  `os.getenv()` / `os.environ` 读取，不再各自解析 `.env`。

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
  - `data/skill/<skill-id>/SKILL.md`
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
    - `path`：统一 Skill 根目录 `data/skill`。
    - `skillDir`：统一 Skill 根目录，不再区分托管、用户、项目或配置型 Skill。
    - `skills`：旧 JSON 配置项，仍可编辑保存。
    - `loadedSkills`：后端从 `data/skill` 真实载入的 Skill 列表。
  - `loadedSkills` 元数据：`source/loadedFrom/filePath/baseDir/paths/whenToUse/userInvocable/editable`。

- `PUT /api/config/skills`
  - 用途：保存旧 JSON Skill 配置并刷新系统提示词缓存。
  - 返回：`ok/enabledCount/skills`。

- `POST /api/skills`
  - 用途：通过 zip 压缩包导入项目目录型 Skill。
  - 请求体：`multipart/form-data`，字段 `file` 为 `.zip` 文件。
  - 写入位置：`data/skill/{normalized-name}/`，其中 `normalized-name` 来自 `SKILL.md` frontmatter 的 `name`。
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
