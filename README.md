# k_agent

一个基于 OpenAI API 的 React Agent 初始框架，包含：

- React 前端聊天界面
- Python FastAPI 后端 agent 服务
- OpenAI Responses API 接入
- AG-UI 标准前后端通信
- 本地工具注册与执行
- MCP server 发现与调用桥接

## 快速开始

1. 安装前端依赖：`npm install`
2. 安装后端依赖：`pip install -r requirements.txt`
3. 复制 `.env.example` 为 `.env`
4. 填入 `OPENAI_API_KEY`
5. 可选：复制 `mcp.config.example.json` 为 `mcp.config.json`
6. 运行 `npm run dev`

## 项目结构

```text
src/                  React 前端
backend/              Python Agent 服务端
backend/config/       后端集中配置
backend/tools.py      本地工具
backend/mcp/          MCP 客户端与配置加载
backend/agent/        React Agent 编排与生命周期 callbacks
src/config.ts         前端集中配置
```

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
- `TOOL_CALL_START` / `TOOL_CALL_ARGS` / `TOOL_CALL_END` / `TOOL_CALL_RESULT`
- `STATE_SNAPSHOT`
- `CUSTOM`，用于状态提示和执行轨迹

后端协议适配位于 `backend/agui.py`，业务 Agent 保持独立，后续可以直接接入
其他兼容 AG-UI 的 React 客户端。

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

后端配置集中在 `backend/config/config.py`，会自动读取 `.env`。前端配置集中在 `src/config.ts`，支持 `VITE_*` 环境变量。

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
