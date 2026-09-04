# K Agent 接口与重要修改记录

> 维护约定：后续涉及前后端接口、MCP/Skill/Memory 加载链路、系统提示词拼接、权限边界或缓存策略的重要修改，都需要在本文追加记录。

## 2026-09-03 删除旧 Prompt 拼接器

- 删除仅由兼容导出和旧测试保活的 `backend/prompts/prompting.py`，以及只服务于该旧链路的
  `mcp_prompt.py`、`sections.py`。这些模块不在 K Agent 生产调用链中，删除不改变 Provider 请求。
- `backend.prompts` 不再导出 `build_prompt_bundle`、`build_effective_system_prompt` 等旧函数；
  唯一 Prompt 编译入口继续是 `compose_prompt(PromptInputs) -> PromptBundle`。
- 原有 lazy nested-memory 测试改为覆盖生产实际使用的
  `load_fresh_nested_memory()` 与 `render_nested_reminder()`，不再通过旧拼接器间接验证。

## 2026-08-24 AskUserQuestion 持久化用户输入 HITL

- K Agent 的 `coding` 工具预设新增 `AskUserQuestion`。一次调用可包含 1–4 个
  问题，每题提供 2–4 个预设选项并声明单选或多选；前端始终同时提供自由文本。
  预设选择与自由文本是两个独立字段，因此用户可以只选、只输入，或选择后继续补充。
- 该工具不是权限申请，不受 `permissionMode=full_access` 跳过审批的行为影响。
  Agent Backend 在 sealed preflight 中先校验问题定义，再发出
  `category=user_input` 的 terminal Interrupt；用户回答前不会执行工具。
- Resume payload 使用 `answers[questionId] = { selected: string[], custom: string }`。
  Access Layer 必须用服务端 checkpoint 中的问题和选项重新校验答案，浏览器不能替换
  问题定义或 request hash。恢复后 K Agent 把结构化答案写成原调用的
  `TOOL_CALL_RESULT`，然后从下一轮 Reason 继续。
- Work、Agent Team 与定时任务共用相同答案结构和问题卡。回答后的 Activity 状态为
  `answered`；取消则产生明确的 cancelled 工具结果。问题 Interrupt 与权限审批仍复用
  durable-before-visible、开放 Interrupt 阻断新消息和一次性 Resume 认领机制。

## 2026-08-11 会话、Team 与定时任务统一权限模式

> 设计依据、完整审批时序、Runner 映射与失败恢复见
> [权限模式与 HITL 技术方案](../architecture/permission-and-hitl-technical-solution.md)。

- 三类运行入口统一新增 `permissionMode: "default" | "full_access"`。省略时为
  `default`，旧会话、旧 Team 与旧定时任务通过数据库/JSON 迁移继续沿用原沙箱行为。
- `default` 保持运行时沙箱和现有权限规则；K Agent 本地工具以
  `sandbox_permissions=require_escalated` 请求越过工作区时，统一经现有
  `ApprovalBroker` 发出 HITL。Codex app-server 与 Claude approval bridge 继续使用
  同一 Access Layer 审批接口。
- `full_access` 是 run 级高风险授权：K Agent Bash 跳过 srt 且文件工具允许工作区外
  路径；Codex 使用 `danger-full-access + never`；Claude Code 使用
  `bypassPermissions` 并关闭 sandbox。该模式也跳过 K Agent 权限规则和 Skill 工具
  白名单，但不会凭空提供操作系统权限、第三方凭据或外部服务 OAuth scope。
- 会话把选择写入 session capabilities；Team 写入 `teams.permission_mode` 并传给主管
  与所有成员；定时任务写入 `scheduled_tasks.permission_mode`，每次计划/手动触发都
  复用该授权。定时任务页面必须持续提示：完全权限会在无人值守执行时直接生效，
  不等待审批；默认模式的审批则在执行记录原页面完成并轮询恢复状态。

## 2026-08-04 流式消息字体稳定性

- 用户与 Agent 消息正文改用本机稳定的中文字体栈，不再依赖远程
  `Noto Sans SC` 的 `display=swap`。避免中文内容流式追加时先按 fallback 字体绘制、
  远程字体就绪后再因字形度量变化而出现字体先大后小的跳动。
- 消息容器固定 `text-size-adjust: 100%`，防止动态内容或窄视口触发浏览器文本自动放大；
  Markdown 标题、代码和正文原有层级规则保持不变。

## 2026-08-04 Claude Code 与 Codex Skill 路径适配

- Claude Code 和 Codex 的 Skill 提示词现在同时包含 `SKILL.md` 绝对路径与
  Skill package root；Skill 正文中的 `scripts/`、`references/`、`assets/`、
  `templates/` 等相对路径必须以 package root 为基准解析，不能以会话工作区为基准。
- 两套路径提示分别由 Claude Code 和 Codex runner 构造，共享 CLI 子进程层不感知
  Skill 路径，K Agent 的原生 Skill 工具加载流程不变。

## 2026-08-03 Codex 与 K Agent 人工审批通道

- Codex runner 从单向 `codex exec --json` 切换为双向 `codex app-server --stdio`。
  `commandExecution`、`fileChange`、`tool/requestUserInput`、MCP elicitation 和
  permissions 请求会暂停当前 run，等待前端明确答复，不再被 CLI 自动取消。
- Agent Backend 新增 run-scoped `ApprovalBroker`。它把待审批请求输出为
  `CUSTOM(approval_request)`，完成后输出 `CUSTOM(approval_resolved)`；请求 ID 必须
  同时匹配 thread ID 和 run ID，审批不设固定超时，流关闭时未决请求会被取消，
  避免跨会话误批。
- 新增 `POST /internal/approvals/{requestId}` 和 Access Layer 对外代理
  `POST /api/approvals/{requestId}`。请求体包含 `threadId/runId/action/scope`，
  并可为 Codex 表单请求携带 `answers/content`；过期或归属不符统一返回 404。
- K Agent 的 `ask` 权限规则接入同一审批通道。选择“本轮始终允许”只缓存当前
  Agent 实例内的精确工具目标，不写入持久权限配置。
- 前端活动时间线新增审批卡片，展示 Agent、请求类型、命令或工具参数，并提供
  “拒绝”“允许一次”“本轮始终允许”；提交中、已批准、已拒绝、已取消和失败
  都有独立状态，切换会话后仍由已保存的原始 AG-UI 事件恢复。

## 2026-08-03 Claude Code 流去重与 MCP 凭据传递

- Claude Code 的 `stream_event` 增量与随后到达的完整 `assistant` 快照按内容前缀
  对齐：已发送的文本/思考不再重复，若增量流缺失尾部则只补发快照后缀。
- Claude runner 增加 `--strict-mcp-config`，只加载本轮在工作台选中的 MCP；提示词
  明确禁止沿用 Codex/K Agent 的工具名，必须使用 Claude 初始化时实际暴露的名称。
- 生成的 `.mcp.json` 会把 `bearerTokenEnv`、`envHeaders` 和 `envPassthrough`
  转换为 `${ENV_NAME}` 引用。带动态认证头的 HTTP MCP 通过本地 stdio bridge
  接入 Claude，再由 bridge 使用 K Agent 的 HTTP MCP 客户端访问远端，规避 Claude
  把有效 Bearer 请求误判为 OAuth 的兼容问题。密钥仍只存在于进程环境，不会写入
  会话工作区。
- Claude 初始化事件中的 MCP 状态随 AG-UI `CUSTOM(status)` 发送；若仍存在
  `needs-auth`，前端状态会明确列出服务器名称，而不是等业务工具报不存在。
- Provider 专属逻辑不进入共享 CLI 层：Claude 的增量去重状态、`.mcp.json`
  生成、密钥注入和 HTTP-to-stdio bridge 分别位于 `claude_code.py`、
  `claude_mcp.py` 和 `claude_mcp_stdio_proxy.py`。`cli_process.py` 只负责通用
  JSONL 子进程与消息生命周期；Codex app-server 和 K Agent MCP manager 不经过
  Claude 的兼容处理。

## 2026-07-30 统一 `$K_AGENT_HOME` 数据布局

- 新增 `backend/home.py`：默认根目录 `~/.k_agent`（`K_AGENT_HOME` 可覆盖；相对路径相对仓库根）。
- 布局：`config/`（mcp、models、permissions、catalog）、`state/sessions/`、
  `content/memory/`、`content/skills/`。
- 首次启动在目标为空时，从旧 `data/` 与 `backend/config/runtime/` 复制迁移，不删除原文件。
- Settings 默认 `STORAGE_BASE_DIR` / `MCP_CONFIG_PATH` 指向新布局；Skill/记忆/目录不再硬编码仓库 `data/`。

## 2026-07-30 Bash 接入 Anthropic sandbox-runtime

- 新增 `backend/sandbox/`：探测 `srt`、生成内容寻址的 srt settings、规划 argv；
  `required` 模式在后端不可用时抛错，禁止静默降级。
- `cc_bash` 改为经 `plan_bash_invocation` 启动；工具结果增加 `sandboxed` /
  `sandboxReason`。无论沙箱是否可用，子进程环境都走白名单，不再继承
  `OPENAI_API_KEY` 等凭据。
- 沙箱不可用时：本轮首次发 `CUSTOM(status)` 提示；工具结果附带安装引导；
  新增本地工具 `InstallSandbox`（必须 `confirmed=true`，仅在用户对话确认后调用）；
  `GET /internal/health` 与 `GET /api/health` 增加 `bashSandbox`。
- 配置项：`BASH_SANDBOX_MODE`（默认 `auto`）、`BASH_SANDBOX_COMMAND`、
  `BASH_SANDBOX_ALLOWED_DOMAINS`、`BASH_SANDBOX_WRITE_PATHS`、
  `BASH_SANDBOX_DENY_READ`。原生 Windows 明确不支持，需 WSL2。
- `BASH_SANDBOX_WRITE_PATHS` 默认包含 `~/.agently-cli` 和
  `~/Library/Application Support/agently-cli`，这样 Agently Mail 的本地 token/state
  可以写入，而不是把整个 home 目录放开。

## 2026-07-30 架构评估后的可靠性与权限收口

- **多轮工具历史**：`ChatMessage` 新增 `toolCalls`，`ChatMeta` 新增
  `toolCallId`。Access Layer 在 `TOOL_CALL_RESULT` 到达时把缓冲的工具调用投影成
  一对 assistant/tool 消息写入会话历史；没有结果的调用整对丢弃。后端上下文管理
  新增 `pair_tool_messages`，压缩时不会把 assistant/tool 配对拆开。
- **模型调用超时**：新增 `model_request_timeout_seconds` 与
  `model_stream_idle_timeout_seconds`，前者限制请求建立，后者是流式看门狗，防止
  服务端长时间挂在一个静默的连接上。
- **MCP 连接复用**：新增 `backend/mcp_tool/pool.py`。会话池按连接配置指纹复用
  stdio/HTTP 连接，配置变化即新建，空闲超过 `mcp_session_idle_ttl_seconds` 回收，
  进程退出时强制关闭。`GET /internal/health` 增加 `mcpPool` 占用统计。
- **权限模型**：规则按文件签名缓存；默认策略可由 `K_AGENT_PERMISSION_DEFAULT`
  设为 `deny`；`ask` 在没有确认通道前按拒绝处理；`Bash` 按分隔符拆分多子命令取
  最严结果；Skill 的 `allowedTools` 在激活期间强制生效。
- **WebFetch SSRF 防护**：解析目标 IP 并拒绝回环/私网/链路本地/组播地址，重定向
  逐跳校验，并限制下载字节数。
- **并发与写入**：`save_run_start` 移到会话锁之后，避免并发请求写出孤立用户消息；
  模型配置与自动记忆改用 `write_json_atomic` / `write_text_atomic` 原子写；单个
  损坏的会话文件只记录告警并跳过，不再拖垮整个会话索引。
- **接口重命名**：`POST /internal/skills/reload` 改为 `POST /internal/prompt/reset`，
  因为它只清 prompt/memory 缓存，不重载 Skill。对外的
  `POST /api/debug/prompt-cache/reset` 不变。
- **删除的死代码**：`access_layer/session_memory.py`、
  `access_layer/mcp_instructions.py`、`backend/prompts/memory_loader.py`、
  `backend/memory/trace.py`，以及 `prepend_user_context`、`PromptSection.cacheable`、
  `stream_chunk_size`、`AgentBackendClient.put_json` 等未被引用的成员。

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
- Access Layer 发起运行时只从 `config/catalog/skills.json` 选择并发送 Skill
  元数据，不读取 `SKILL.md`，也不发送正文或包路径。Agent Backend 的 Skill
  工具先按本轮请求快照完成授权，确认调用后才从 `content/skills/<id>/SKILL.md`
  读取正文；文件 frontmatter 不参与运行时元数据。
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

## 2026-08-12 对话图片 / 视频输入

- 模型配置新增 `inputModalities: (text|image|video)[]`；旧的 `multimodal: true`
  继续解释为 `text + image`，`multimodal` 字段保留用于向后兼容。
- `forwardedProps.attachments` 接受最多 4 个内联图片或视频，每个不超过 20 MB。
  Access Layer 校验 MIME 与 Data URL 后，将附件写入所属 user message 的
  `attachments` 字段，因此刷新会话及下一轮上下文仍能保留媒体归属。
- Agent Backend 将图片转换为 OpenAI 兼容的 `image_url` 内容块，将视频转换为
  `video_url` 内容块，并依据所选模型的 `inputModalities` 再次拒绝不支持的媒体。

## 2026-08-19 Durable HITL / AG-UI Resume

- `POST /api/agent` 现在接受标准 `RunAgentInput.resume[]`：Resume 必须使用原
  `threadId`、新的 `runId`、空 `messages`，并完整覆盖该线程全部开放 Interrupt。
- 原 run 通过 `RUN_FINISHED.outcome.type="interrupt"` 正常终止；此前依次发送
  `STATE_SNAPSHOT` 与 `MESSAGES_SNAPSHOT`。审批卡仍使用
  `ACTIVITY_SNAPSHOT(activityType="approval")`，但只承担展示。
- `GET /api/sessions/{sessionId}` 新增 `openInterrupts`，只返回审批展示/状态字段，
  不返回 `_checkpoint`。有开放 Interrupt 时，缺少 Resume 的普通输入返回 409。
- Session 包新增 `approvals/{interruptId}.json`、
  `resume-intents/{resumeIntentId}.json`；主 JSON 新增 `openInterruptIds`。
- Agent Backend 私有运行载荷新增 `resume` 与 `resumeCheckpoints`。后者只能由
  Access Layer 从可信存储装配，浏览器提交的 checkpoint 不会被接受。
- 新增 `POST /api/scheduled-tasks/{taskId}/runs/{runId}/resume`，请求体为
  `interruptId/action/scope`。
- 新增 `POST /api/teams/{teamId}/approvals/{approvalId}/resume`，Team checkpoint
  保存在 SQLite 事件日志中，但 Team 查询与 SSE 接口会剥离 `_checkpoint`。
- 旧 `/api/approvals/{id}` 暂留迁移壳，但新前端和新 run 均不再依赖其 Backend
  内存 pending 状态。
- Claude Code 与 Codex 使用同一 durable-before-visible 接入层：两者的 Interrupt 都先
  保存 `restart_from_context` checkpoint，再公开 Activity；Access Layer 重启后仍可列出、
  原子认领并向 Backend 发送私有 `resumeCheckpoints`。
- Codex provider 请求哈希绑定 method 与实际语义 params，排除重放时变化的
  `threadId/turnId/itemId`；命令、补丁、权限或问题内容变化时不能消费旧授权。
- Claude 原生 `AskUserQuestion` 经私有 permission-prompt bridge 映射为 `user_input`；
  Resume 将共享的 `selected/custom` 回填到 Claude `updatedInput.answers/annotations`。
  即使 `full_access/bypassPermissions`，该交互 bridge 仍保留。
