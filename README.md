# k_agent

一个基于 OpenAI API 的 React Agent 初始框架，包含：

- React 前端聊天界面
- Python FastAPI 接入层与无状态 Agent Backend
- OpenAI Chat Completions 流式接入
- AG-UI 标准前后端通信
- 本地工具注册与执行
- MCP server 发现与调用桥接

协议说明见 [docs/ag-ui-protocol.md](docs/ag-ui-protocol.md)，工具说明见
[docs/tools.md](docs/tools.md)。

## 快速开始

1. 安装前端依赖：`cd frontend && npm install`
2. 安装后端依赖：`pip install -r requirements.txt`
3. 复制 `.env.example` 为 `.env`
4. 填入 `OPENAI_API_KEY`
5. 可选：复制 `backend/config/runtime/mcp.config.example.json` 为 `backend/config/runtime/mcp.config.json`
6. 运行 `cd frontend && npm run dev`

该命令会同时启动三个独立进程：

- Access Layer：`http://localhost:3001`
- Stateless Agent Backend：`http://127.0.0.1:3002`
- Vite Frontend：`http://localhost:5173`

## 项目结构

```text
frontend/             React 前端工程
access_layer/         接入层：公开 API、完整会话持久化与 AG-UI 透传
backend/              无状态 Agent 服务端
backend/home.py       $K_AGENT_HOME 路径与旧 data/ 迁移
backend/config/runtime/  仓库内 MCP/models 示例模板（运行时写入 $K_AGENT_HOME）

$K_AGENT_HOME/        默认 ~/.k_agent，可用环境变量改到项目内 .k_agent
  config/             mcp.json、models.json、permissions.json、catalog/
  state/sessions/     会话历史
  content/memory/     长期记忆
  content/skills/     Skill 包
```

## 服务边界

请求链路为 `frontend -> access layer (:3001) -> agent backend (:3002)`：

- 接入层负责会话读写、并发串行化、MCP/Skill 摘要目录和选择校验；运行时把完整历史以及选中 MCP/Skill 的自包含定义转发给 Agent Backend。
- Agent Backend 不读取 MCP/Skill 摘要列表，也不扫描 Skill 目录；它只消费本次内部请求携带的定义，负责系统提示词拼接、上下文预算、模型消息组装、推理与工具调用，并直接生成标准 AG-UI Event。
- 接入层通过内部 NDJSON 流式 HTTP 调用 Agent Backend；两个服务是独立进程，
  并非进程内函数调用。
- `access_layer/` 与 `backend/` 是并列的顶层目录，依赖方向为接入层调用后端。
- `backend/agent/` 不依赖 access layer、session、memory、prompt 或 skill 模块，也不保存任何
  按会话标识索引的缓存；每次运行所需状态都由请求显式传入并在运行结束后释放。
- Access Layer 与 Agent Backend 默认分别只监听 `127.0.0.1:3001` 和
  `127.0.0.1:3002`；内部接口会携带模型凭据，两个服务都不应暴露到公网或局域网。

### Agent Team Runtime

Access Layer 同时提供持久化 Agent Team Control Plane。打开侧栏中的
“Agent Team”即可创建由 `k_agent`、`codex`、`claude_code` 任意组合的团队，
并为每个成员独立选择模型、MCP 和 Skill。Team 的任务、Mailbox、Artifact
和事件序列保存在 `$K_AGENT_HOME/state/teams/team_runtime.db`；单个 Worker
Run 仍由无状态 Agent Backend 执行。

Team Scheduler 使用独立并发池，不获取公开对话的 same-session lock。编码
Agent 首次运行时会优先创建独立 detached Git worktree；如果当前目录不是 Git
仓库，则退化为独立目录。普通工具失败只结束对应工具调用或 Task Attempt，
不会清空 Team 的其它任务和 Artifact。

```env
TEAM_RUNTIME_ENABLED=true
TEAM_MAX_ACTIVE_RUNS=8
TEAM_TASK_LEASE_SECONDS=120
```

主要 API：`/api/teams`、`/api/teams/{teamId}`、
`/api/teams/{teamId}/stream`、`/api/teams/{teamId}/commands`。

## 当前能力

- 聊天消息发送与回显
- AG-UI over SSE 流式响应
- 标准运行、文本消息、工具调用和状态同步事件
- 服务端统一维护模型调用
- 会话记忆与历史列表
- Claude Code 风格的分层指令、自动记忆、上下文预算与持久化压缩
- 旧工具结果优先裁剪，以及 `/api/debug/prompt-context` 上下文查看接口
- 多轮 tool_calls / tool 结果持久化，跨轮保留工具执行历史
- MCP 会话池按连接配置复用，避免每轮 stdio 冷启动
- 本地 function tools
- Bash 经 `srt` OS 沙箱执行（macOS/Linux；默认 `auto`，可配 `required`），子进程环境变量白名单
- 工具参数基础校验
- 将 MCP tools 暴露给模型
- Agent 生命周期 callbacks：`before_model`、`after_model`、`before_tool`、`after_tool`、`on_error`
- 展示执行轨迹与任务拆解

## AG-UI 接口

前端通过 `POST /api/agent` 发送标准 `RunAgentInput`，后端返回
`text/event-stream`。主要事件包括：

- `RUN_STARTED` / `RUN_FINISHED` / `RUN_ERROR`
- `TEXT_MESSAGE_START` / `TEXT_MESSAGE_CONTENT` / `TEXT_MESSAGE_END`
- `REASONING_START` / `REASONING_MESSAGE_START` /
  `REASONING_MESSAGE_CONTENT` / `REASONING_MESSAGE_END` / `REASONING_END`
- `TOOL_CALL_START` / `TOOL_CALL_ARGS` / `TOOL_CALL_END` / `TOOL_CALL_RESULT`
- `CUSTOM`，用于状态提示和执行轨迹

协议适配位于 `backend/agui.py`，业务 Agent 保持独立，后续可以直接接入
其他兼容 AG-UI 的 React 客户端。完整事件顺序、前端状态机和持久化约定见
[K Agent AG-UI 协议约定](docs/ag-ui-protocol.md)。

上下文来源、预算、路径规则、自动压缩与持久化字段见
[K Agent 上下文管理系统](docs/context-management.md)。

## Agent callbacks

可以通过实现 callback 类来监听或扩展 agent 生命周期：

```python
from backend.agent.callbacks import AgentRunContext, ModelCallPayload, ToolCallPayload


class AuditCallback:
    async def before_model(self, context: AgentRunContext, payload: ModelCallPayload) -> None:
        print("before model", context.run_id, payload.model)

    async def before_tool(self, context: AgentRunContext, payload: ToolCallPayload) -> None:
        print("before tool", payload.name, payload.arguments)
```

然后在创建 agent 时传入：

```python
agent = OpenAIAgent(LOCAL_TOOLS, mcp_manager, callbacks=[AuditCallback()])
```

## 配置

后端代码配置集中在 `backend/config/config.py`，运行配置文件集中在 `backend/config/runtime/`，会自动读取 `.env`。前端配置集中在 `frontend/src/config.ts`，支持 `VITE_*` 环境变量。

MCP 与 Skill 的前端选择列表由 Access Layer 直接读取 `data/mcp.json` 和
`$K_AGENT_HOME/config/catalog/`。MCP 连接参数与模型配置在
`$K_AGENT_HOME/config/`，Skill 正文在 `$K_AGENT_HOME/content/skills/`。
首次启动若新目录为空，会从旧的 `data/` 与 `backend/config/runtime/` 复制一次。

常用配置项：

- `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`
- `HOST` / `PORT` / `APP_TITLE`
- `AGENT_BACKEND_HOST` / `AGENT_BACKEND_PORT` / `AGENT_BACKEND_LOG_LEVEL`
- `CORS_ALLOW_ORIGINS` / `CORS_ALLOW_METHODS` / `CORS_ALLOW_HEADERS`
- `MCP_CONFIG_PATH`
- `MAX_MODEL_ITERATIONS` / `STREAM_CHUNK_SIZE`
- `DEFAULT_SESSION_TITLE` / `SESSION_TITLE_MAX_LENGTH`
- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL`
- `LANGFUSE_ENABLED` / `LANGFUSE_TRACING_ENVIRONMENT` / `LANGFUSE_SAMPLE_RATE`
- `VITE_API_BASE_URL` / `VITE_SESSION_STORAGE_KEY` / `VITE_CLIENT_PORT`

## 本地部署

需要使用构建后的前端时，先构建前端，再以非 reload 模式启动两个本地服务：

```bash
cd frontend
npm run deploy:local
```

Access Layer 默认监听 `127.0.0.1:3001`，并在同一端口托管
`frontend/dist`；Agent Backend 监听 `127.0.0.1:3002`。两个端口都只允许
当前机器访问，不再向局域网开放。启动后使用 `http://127.0.0.1:3001` 或
`http://localhost:3001` 访问；构建后的前端默认通过同源 `/api` 调用接入层。

Agent Backend 默认向终端输出 `INFO` 级别的单行文本日志，格式为
`时间 [级别] 模块 [sess/run/trace] [组件] 事件 | key=value`，包括服务生命周期、
请求、Prompt 拼接、MCP 配置与连接、上下文预算与压缩、模型调用、工具调用、完成、
取消和异常。日志只记录关联 ID、计数、长度、预算、工具名和耗时，不记录消息正文、
Prompt 正文、工具参数值、工具输出或凭据。可以通过
`AGENT_BACKEND_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR` 调整级别。

## 后续建议

- 为上下文预算接入各模型提供方的精确 tokenizer
- 为工具参数加入完整 JSON Schema 校验
- 增加鉴权和任务状态管理
