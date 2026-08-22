# K Agent Hook 机制（现状说明）

> 适用范围：Agent Backend 里 **K Agent** 的进程内扩展点。  
> 配套设计稿：[`agent-hooks-and-middleware-technical-solution.md`](./agent-hooks-and-middleware-technical-solution.md)。  
> 本文描述 **当前代码实际怎么跑**，而不是迁移计划。

这里的 Hook **不是** Git Hook，也 **不是** Skill frontmatter 里的 `hooks` 字段。Skill 的 `hooks` 只作为不可信文本交给模型，**从不在 Python 里执行**。

---

## 1. 要解决什么问题

ReAct 循环里，模型调用、工具调用、失败、收尾，都会反复碰到同一类横切需求：

- 记 trace / 结构化日志 / Langfuse
- 改请求（例如清掉历史消息里的脏参数）
- 将来：重试、fallback、缓存

如果把这些 `await` 散落在 `react_agent.py` 的每个分支里，漏收尾、Local/MCP 两套观测、观测异常打断业务都会反复出现。

现在的做法是：**业务循环只面对一条 Pipeline**。观测和可改执行的扩展分成两类，编译一次、每个 HTTP run 绑定一份隔离状态。

---

## 2. 两层对象：Definition 与 Runtime

```text
进程启动
  └─ AgentPipelineDefinition.compile(middleware=...)
        不可变；没有 run_id、没有 messages

每次 K Agent run
  └─ definition.bind_runtime(context, observers)
        → AgentPipelineRuntime
        有 AgentRunContext、ObserverDispatcher、state
```

- **`AgentPipelineDefinition`**：进程级蓝图。只保存排好序的 middleware 函数引用。`KAgentRunner` 在构造时调用 `build_k_agent_pipeline_definition()`，之后多轮对话复用同一份 definition。
- **`AgentPipelineRuntime`**：一次 run 一份。`OpenAIAgent.create_runtime()` 里 `bind_runtime`。并发两个会话不会共用 `context` / `state` / observer 实例。

Observer **不**编进 definition。日志和 Langfuse 是请求级对象（带着 `request_id` / Langfuse span），必须在 bind 时注入。

---

## 3. Observer 与 Middleware

| | Observer | Middleware |
| --- | --- | --- |
| 干什么 | 看，不改 | 可以改请求、短路、重试 |
| 失败 | **fail-open**：记 warning，run 继续 | **fail-closed**：异常向上抛 |
| 怎么写 | `async def handle(event)`，或 `@observe` 函数 | `@before_*` / `@after_*` / `@wrap_*` |
| 怎么挂上 | `bind_runtime(observers=[...])` | `compile(middleware=(...))` 显式列表 |
| 默认顺序键 | `order`，默认 100，同 order 按注册下标 | 同左；`after_*` **逆序** |

Observer 拿到的是 **frozen 事件快照**（`AgentStartedEvent` 等）。不能靠返回值改 Agent。单个 observer 超时（若声明了 `timeout_seconds`）或抛错，dispatcher 吞掉业务异常，只打 `observer=… event=… error=TypeName`，**不把 prompt/工具正文写进日志**。

`CancelledError` 仍向上传，取消 run 不会被观测层拦住。

Middleware 异常视为执行失败：preflight deny、wrap 里抛错、工具 execute 失败，都会变成 `OperationFailedEvent`，然后继续往上抛，由 `_run_tool` 收成模型可见的 Observation（权限 Interrupt 除外）。

**权限、Skill 白名单、参数 schema 校验不是 Middleware。** 它们焊在工具管线的 sealed terminal 里，列表里的 wrap **拆不掉、也绕不过**。见第 6 节。

---

## 4. 和 ReAct 怎么接在一起

K Agent 的循环仍在 `OpenAIAgent.run_stream_react()`。Pipeline 只包住三块执行：

```mermaid
sequenceDiagram
    participant Loop as run_stream_react
    participant P as AgentPipelineRuntime
    participant Obs as Observers
    participant MW as Middleware
    participant Core as 模型 / 工具实现

    Loop->>P: agent_run(...) 进入 scope
    P->>MW: before_agent
    P->>Obs: AgentStarted
    Loop->>P: emit_context_built
    P->>Obs: ContextBuilt
    loop 每一轮 Reason
        Loop->>P: stream_model(request, terminal)
        P->>MW: before_model
        Note over P,MW: wrap_model 从外到内
        P->>Obs: ModelStarted
        P->>Core: 真正打 provider
        Core-->>Loop: reasoning / text delta（原样 yield）
        P->>Obs: ModelCompleted
        P->>MW: after_model
    end
    loop 每一个工具
        Loop->>P: run_tool(request, preflight, execute)
        Note over P,MW: wrap_tool 从外到内
        P->>MW: preflight（权限 / Skill 白名单）
        P->>Obs: ToolStarted
        P->>Core: execute
        P->>Obs: ToolCompleted
    end
    Loop->>P: complete(final) 后退出 scope
    P->>Obs: AgentCompleted
    P->>MW: after_agent
```

循环自己仍然 `yield` AG-UI 内部事件（`tool_start`、`delta`、`_run_status`、`_run_trace` 等）。**Hook 事件 ≠ 前端事件。** TraceObserver 往 `runtime["trace"]` 里追加字符串，循环再用 `_run_trace` 推给右侧「执行轨迹」；那是观测的副作用，不是 Observer 直接打 SSE。

Scope 必须成对退出：正常 Finish、迭代上限、异常、取消，都要 `__aexit__`。漏了会导致 Langfuse / 日志没有 `agent_completed` 或失败事件。

---

## 5. Observer 事件一览

定义在 `backend/agent/hooks/types.py`，名字稳定，给日志和 Langfuse 当路由键。

| 事件 | 何时 | 典型消费者 |
| --- | --- | --- |
| `agent_started` | `before_agent` 成功之后 | Trace、日志、Langfuse 根 span |
| `context_built` | 开场 `emit_context_built` | 日志（预算数字，不含正文） |
| `context_pruned` | 某轮 Reason 前裁掉旧 tool 输出 | 日志 |
| `model_started` / `model_completed` | 每一次物理模型调用（含 wrap 重试后的新 `operation_id`） | Trace、日志、Langfuse generation |
| `tool_started` / `tool_completed` | 每一次物理工具尝试（同一 `call_id` 重试会增加 `attempt`） | Trace、日志、Langfuse tool span |
| `operation_failed` | preflight / execute / middleware / agent_run 等失败 | Trace `error:stage:ExcName`、日志、Langfuse |
| `agent_completed` | scope 正常结束且调用了 `complete(result)` | Trace `agent:end`、日志、Langfuse |

没有「token 增量」事件。模型流式 token 走 `stream_model` 的 `yield`，不经过 Observer，以免观测拖垮背压。

线上 Observer（均实现 `handle`，**没有**用 `@observe` 装饰器）：

1. **`TraceObserver(trace)`** — 永远第一个。只写短字符串，不写请求体。
2. **`AgentBackendLoggingObserver`** — `backend/main.py` 按 request 构造；只打关联 ID、计数、工具名、耗时。
3. **`LangfuseAgentObserver`** — 有 Langfuse 配置时，挂在该 run 的 agent span 下。

`bind_runtime` 时顺序是 `[TraceObserver, *传入的 observers]`。Dispatcher 再按 `order` 排；类 observer 无 decorator 时 `order=100`，因此 **Trace 因注册下标更靠前而先跑**。

`@observe(event_type=...)` 可以给函数挂元数据，并让 dispatcher 只把该类型事件送给它。当前内置观测没用这条路径，测试里有。

---

## 6. 工具管线：wrap + 密封门

`pipeline.run_tool(request, preflight=..., execute=...)` 是工具的唯一入口。Local 和 MCP 都走它，只是 `preflight` / `execute` 闭包不同。

洋葱从外到内：

```text
wrap_tool（order 小的在外）
  └─ wrap_tool（order 大的在内）
        └─ sealed terminal（写死，不能注册、不能排序）
              1. await preflight(current)     # Skill 白名单 + 权限 / HITL
              2. emit ToolStarted
              3. await execute(current)       # schema 校验 + 真正执行
              4. emit ToolCompleted
```

要点：

- Middleware 只能拿到 `call_next`，**拿不到 raw `execute`**。改参数必须 `request.override(...)` 再 `await call_next(new_request)`。新请求会 **重新进 sealed**，所以清字段、换名字之后权限和 schema 会再跑一遍。
- HITL `ask` 发生在 preflight 里的 `_enforce_permission`。此时还没有 `ToolStarted`，Interrupt 时通常也还没有 live stdout。
- `call_id` 在多次 retry 间保持；每次进入 sealed 会发新的 `operation_id`，`attempt` +1。
- 当前唯一内置 wrap：`strip_legacy_read_escalation_fields`。只读工具（Read/Glob/Grep/LS）若历史重放带了 `sandbox_permissions` 等提权字段，先剥掉再 `call_next`，避免 schema 失败或看起来像一次提权。

`OpenAIAgent._execute_tool` 负责解析「本地 / Skill 别名 / MCP / 未知」，组装 `ToolCallRequest` 和两个闭包，然后只调用 `run_tool`。

---

## 7. 模型管线：流式 + 可见 delta 后禁止重试

`pipeline.stream_model(request, terminal)`：

1. `before_model`（可把 `runtime.state["model_request"]` 换成新的 `ModelCallPayload`）
2. `wrap_model` 洋葱
3. sealed：`ModelStarted` → `terminal(...)` 逐个 `yield` → 必须出现 `ModelCallCompleted` → `ModelCompleted`
4. `after_model`

`terminal` 是 `OpenAIAgent` 里真正打 OpenAI 兼容流的那段。循环把 `ModelReasoningDelta` / `ModelTextDelta` 转成内部 `reasoning_*` / `delta`，再由 `agui.py` 变成 AG-UI。

约束：**一旦已经向下游 yield 过 reasoning/text delta，wrap_model 就不能再对 inner 调第二次**（不能在用户已经看到半句话之后换模型重来）。需要重试必须在任何可见 delta 之前。

---

## 8. 声明、编译、排序

装饰器在 `backend/agent/hooks/decorators.py`，只往函数上挂 `HookSpec`，**不注册到全局表**。没写进 `compile(...)` 或 `observers=[...]` 的函数不会跑。这是刻意的：避免 Skill、动态 import、环境变量把不信任代码挂进执行链。

| 装饰器 | 签名（概念） | 失败策略 |
| --- | --- | --- |
| `@observe` | `(event) -> None` | open |
| `@before_agent` / `@after_agent` | `(state, runtime) -> dict \| None` | closed |
| `@before_model` / `@after_model` | 同上 | closed |
| `@wrap_model_call` | `(request, call_next) -> async iter` | closed |
| `@wrap_tool_call` | `(request, call_next) -> ToolCallResult` | closed |

`before_*` / `after_*` 叫 **node hook**：按序 `await`，返回的 dict 会 `state.update`。`after_*` 编译时 **逆序**，方便对称收尾（先装的后拆）。

`wrap_*` 编译后用 `reversed` 套洋葱：**更小的 `order` 包在更外层**。同 `order` 时，列表里靠前的 index 更小，相当于更靠外（对 wrap）或更先执行（对 before）。

新增可信 Middleware 的固定步骤：

1. 在 `builtin_middleware.py` 实现并写清边界。
2. 在 `builtins.py` 的 `compile(middleware=(...))` **显式加上**。
3. 不要把权限门改成可排序的 wrap。

---

## 9. 请求级状态放在哪

| 位置 | 内容 |
| --- | --- |
| `AgentRunContext` | `run_id` / `thread_id` / `permission_mode`（在 `metadata`）/ Skill 白名单 |
| `AgentPipelineRuntime.state` | 本轮逻辑模型请求、`model_result` 等，给 node hook 看 |
| `runtime` 字典（`create_runtime` 返回） | 消息列表、工具表、`approval_handler`、HITL checkpoint 等循环状态 |

不要把 run 数据写进 `OpenAIAgent` 实例或 `AgentPipelineDefinition`。进程里 Agent 是无状态单例，两轮 run 会交错。

Skill `allowedTools` 闩在 `context.skill_allowlist`，由 `_execute_tool` 的 preflight 强制，不是 Observer。

---

## 10. 代码地图

| 文件 | 职责 |
| --- | --- |
| `backend/agent/hooks/types.py` | 事件、Payload、`AgentRunContext`、`ToolCallRequest` |
| `backend/agent/hooks/decorators.py` | `@observe` / `@wrap_*` 等 |
| `backend/agent/hooks/middleware.py` | wrap / node 的类型别名 |
| `backend/agent/hooks/pipeline.py` | compile、bind、`stream_model`、`run_tool`、agent scope |
| `backend/agent/hooks/observers.py` | fail-open dispatcher、`TraceObserver` |
| `backend/agent/hooks/builtin_middleware.py` | 内置 wrap 实现 |
| `backend/agent/hooks/builtins.py` | K Agent 显式注册表 |
| `backend/agent/react_agent.py` | ReAct 循环；调用 pipeline，自己 yield 前端事件 |
| `backend/runners/k_agent.py` | 构造 definition；注入 logging/Langfuse observers 与审批闭包 |
| `backend/observability/logging.py` | 本地生命周期日志 |
| `backend/observability/langfuse.py` | Langfuse observer |
| `backend/tests/test_agent_hooks.py` | 顺序、fail-open、sealed 重入、可见 delta 禁止重试 |

---

## 11. 和前端、权限文档的边界

- **AG-UI**：Hook 不发 `TOOL_CALL_*`。工具卡片仍由循环的 `tool_start` / `tool_result` 翻译。见 [`ag-ui-protocol.md`](./ag-ui-protocol.md)。
- **权限 / HITL**：决策在 `_local_permission_decision` + `_enforce_permission`，挂在 **preflight**。见 [`permission-and-hitl-technical-solution.md`](./permission-and-hitl-technical-solution.md)。
- **Durable HITL**：checkpoint 在循环拍 `_react_tool_boundary`，发生在 `run_tool` 之前。Pipeline 不管跨 HTTP 恢复。见 [`durable-hitl-checkpoint-technical-solution.md`](./durable-hitl-checkpoint-technical-solution.md)。

---

## 12. 读代码时的最短路径

1. `builtins.py` — 这台 Agent 启用了哪些 wrap。
2. `pipeline.py` 的 `run_tool` / `stream_model` — 密封顺序。
3. `react_agent.py` 的 `_execute_tool` — preflight/execute 里具体有什么。
4. `observers.py` 的 `emit` — 为什么观测挂了 run 还能继续。
