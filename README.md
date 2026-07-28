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
frontend/src/         React 前端源码
access_layer/         接入层：协议、校验、会话、消息与 Prompt 拼接
backend/              无状态 Agent 服务端
backend/agent/        无状态 Agent Backend：模型循环与工具执行
backend/config/       后端集中配置
backend/config/runtime/ 后端运行配置文件
backend/tools/        本地工具
backend/mcp_tool/     MCP 客户端与配置加载
frontend/src/config.ts 前端集中配置
```

## 服务边界

请求链路为 `frontend -> access layer (:3001) -> agent backend (:3002)`：

- 接入层负责 AG-UI 协议适配、会话读写、并发串行化、模型/附件/Skill
  校验、Memory/Skill/MCP 上下文组装以及最终状态持久化。
- Agent Backend 只接收完整的 `AgentRunRequest`，负责模型推理循环、工具调用
  和运行事件输出。
- 接入层通过内部 NDJSON 流式 HTTP 调用 Agent Backend；两个服务是独立进程，
  并非进程内函数调用。
- `access_layer/` 与 `backend/` 是并列的顶层目录，依赖方向为接入层调用后端。
- `backend/agent/` 不依赖 access layer、session、memory、prompt 或 skill 模块，也不保存任何
  按会话标识索引的缓存；每次运行所需状态都由请求显式传入并在运行结束后释放。
- Agent Backend 默认只监听 `127.0.0.1:3002`，内部接口会携带模型凭据，不应暴露到公网。

## 当前能力

- 聊天消息发送与回显
- AG-UI over SSE 流式响应
- 标准运行、文本消息、工具调用和状态同步事件
- 服务端统一维护模型调用
- 会话记忆与历史列表
- 本地 function tools
- 工具参数基础校验
- 将 MCP tools 暴露给模型
- Agent 生命周期 callbacks：`before_model`、`after_model`、`before_tool`、`after_tool`、`on_error`
- 展示执行轨迹与任务拆解

## AG-UI 接口

前端通过 `POST /api/agent` 发送标准 `RunAgentInput`，后端返回
`text/event-stream`。主要事件包括：

- `RUN_STARTED` / `RUN_FINISHED` / `RUN_ERROR`
- `TEXT_MESSAGE_START` / `TEXT_MESSAGE_CONTENT` / `TEXT_MESSAGE_END`
- `THINKING_START` / `THINKING_TEXT_MESSAGE_START` /
  `THINKING_TEXT_MESSAGE_CONTENT` / `THINKING_TEXT_MESSAGE_END` / `THINKING_END`
- `TOOL_CALL_START` / `TOOL_CALL_ARGS` / `TOOL_CALL_END` / `TOOL_CALL_RESULT`
- `STATE_SNAPSHOT`
- `CUSTOM`，用于状态提示和执行轨迹

协议适配位于 `access_layer/agui.py`，业务 Agent 保持独立，后续可以直接接入
其他兼容 AG-UI 的 React 客户端。完整事件顺序、前端状态机和持久化约定见
[K Agent AG-UI 协议约定](docs/ag-ui-protocol.md)。

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

常用配置项：

- `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`
- `HOST` / `PORT` / `APP_TITLE`
- `CORS_ALLOW_ORIGINS` / `CORS_ALLOW_METHODS` / `CORS_ALLOW_HEADERS`
- `MCP_CONFIG_PATH`
- `MAX_MODEL_ITERATIONS` / `STREAM_CHUNK_SIZE`
- `DEFAULT_SESSION_TITLE` / `SESSION_TITLE_MAX_LENGTH`
- `VITE_API_BASE_URL` / `VITE_SESSION_STORAGE_KEY` / `VITE_CLIENT_PORT`

## 后续建议

- 将会话记忆持久化到数据库
- 为工具参数加入完整 JSON Schema 校验
- 增加鉴权、日志和任务状态管理
