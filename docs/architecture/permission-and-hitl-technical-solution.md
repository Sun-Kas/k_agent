# 权限模式与 HITL 技术方案

> 状态：已实现，与 2026-08-24 当前代码一致
> 适用范围：Work 对话、Agent Team、定时任务；K Agent、Codex、Claude Code
> 实现级逐函数说明：[K Agent HITL 实现说明](hitl-implementation.md)
> 设计演进与恢复状态机：[可持久化 HITL 检查点与延迟恢复技术方案](durable-hitl-checkpoint-technical-solution.md)

## 1. 先说结论

当前 HITL 不是“Backend 保存一个 Future，HTTP 一直等用户点击”。实际模型是：

```text
原 Run
  模型产生工具调用或 AskUserQuestion
  → Agent Backend 在副作用前生成 Interrupt
  → Access Layer 先持久化 checkpoint，再把卡片发给浏览器
  → RUN_FINISHED(outcome=interrupt)
  → 原 HTTP、Runner、MCP 租约与并发槽释放

用户以后提交决定
  → 相同 threadId + 新 runId + resume[]
  → Access Layer 原子认领持久化 Interrupt
  → Agent Backend 收到可信 resumeCheckpoints
  → K Agent 从工具边界继续 Act，再进入下一轮 Reason
```

`backend/approvals.py` 仍叫 `ApprovalBroker`，但它不再保存跨请求
`requestId → Future`。跨刷新、断线和 Backend 重启的权威状态属于 Access Layer。

系统当前有两类 HITL：

| 类型 | category | 用户提交 | `full_access` 是否跳过 |
| --- | --- | --- | --- |
| 权限审批 | `local_tool` / `mcp_tool` / provider 类别 | approve / deny / cancel，once / run | 是，K Agent 权限规则直接 allow |
| 用户提问 | `user_input` | 每题 `selected[] + custom`，或 cancel | 否，问题必须由用户回答 |

这两类请求共用 durable Interrupt/Resume 基础设施，但语义不能混用：回答问题不是给工具授权，
权限批准也不能携带一份任意表单覆盖原工具参数。

---

## 2. 服务职责与信任边界

```mermaid
flowchart LR
    UI[Frontend\n卡片与答案] -->|POST /api/agent + resume[]| AL
    AL[Access Layer\nSession/审批文件/Resume CAS] -->|完整历史 + resumeCheckpoints| BE
    BE[Agent Backend\n无状态执行/权限/Interrupt] --> RUNNER[K Agent / Codex / Claude]
    RUNNER --> PIPE[sealed Tool Pipeline]
    PIPE --> TOOL[Local / MCP tool]
    BE -->|AG-UI NDJSON| AL
    AL -->|SSE, checkpoint 已剥离| UI
```

### 2.1 Frontend

- 只渲染公开的 Activity 与 `openInterrupts` 投影。
- 权限卡提交 `approved/scope`；问题卡提交 `answers`。
- 不保存、不回传、不修改 checkpoint。
- 刷新后从 Session API 的 `openInterrupts` 恢复可操作卡片，不能只根据历史事件猜测。

### 2.2 Access Layer

- 拥有 Session、Team、定时任务及 Interrupt 持久化。
- 在卡片可见前执行 durable-before-visible 写入。
- 校验 Resume 覆盖全部开放 Interrupt，使用 ResumeIntent 一次性认领。
- 将浏览器 `resume[]` 与服务端 `resumeCheckpoints` 分开：后者永不来自客户端。
- 有开放 Interrupt 时拒绝普通新消息，防止对话越过未完成的工具边界。

### 2.3 Agent Backend

- 每个 worker 复用无状态 Runner/Agent，给每次请求创建独立 Runtime。
- 绑定 workspace、网络、权限模式和工具环境的 ContextVar。
- 计算 allow / deny / ask，或产生 `AskUserQuestion` 输入请求。
- 在工具副作用前生成 checkpoint 与 Interrupt。
- Resume 时消费 Access Layer 下发的可信 checkpoint/decision。
- 不读写 `$K_AGENT_HOME/state/sessions`，不持有跨请求 Future。

### 2.4 工具层

- 普通工具只实现 schema 与 execute，不直接访问前端。
- `sandbox_permissions=require_escalated` 是申请，不是授权凭证。
- `AskUserQuestion` 的 executor 故意不可达；答案只由 Resume 路径生成 tool result。

---

## 3. 权限模式与持久化

统一字段：

```json
"permissionMode": "default" | "full_access"
```

### 3.1 default

- 保持 K Agent 权限规则、Skill allowlist 和 Bash 沙箱。
- 明确越权请求可进入权限审批 Interrupt。
- deny 是可恢复工具错误，不弹审批卡。

### 3.2 full_access

- K Agent 本地/MCP 权限决策直接 allow，文件工具可越过 workspace，Bash 跳过 srt。
- Codex 使用 `danger-full-access + approvalPolicy=never`。
- Claude Code 使用 `bypassPermissions` 并关闭其 sandbox。
- 不会提升为 root，也不会提供不存在的密钥、OAuth scope 或操作系统能力。
- **不会跳过 `AskUserQuestion`**；业务信息缺失与宿主机权限是两件事。

### 3.3 所有权

| 场景 | 字段位置 | 持久化位置 |
| --- | --- | --- |
| Work | `forwardedProps.agentOptions.permissionMode` | Session capabilities |
| Team | `TeamCreateInput.permissionMode` | `teams.permission_mode` |
| 定时任务 | `ScheduledTaskInput.permissionMode` | `scheduled_tasks.permission_mode` |

旧数据或缺省字段一律规范化为 `default`。新会话不会继承上一会话的完全权限。

---

## 4. Agent Backend 请求入口

入口是 `backend/main.py::run_agent()`：

1. `AgentBackendRunInput` 接收完整历史、Runtime catalog 快照、workspace、
   `resume[]` 与私有 `resumeCheckpoints[]`。
2. 构造 `RunnerContext`，把浏览器决定和服务端 checkpoint 分开存放。
3. 设置请求级 ContextVar：
   - `set_tool_workspace()`
   - `set_tool_network_access()`
   - `set_tool_permission_mode()`
   - `set_tool_env_overrides()`
4. 从 worker-local `runner_registry` 取惰性缓存的 Runner。
5. 使用 `app.state.approvals.stream(runner.run_stream(...))` 合流普通 Runner 事件与
   Interrupt。
6. `translate_agent_events()` 把内部事件转成 AG-UI，再编码为 NDJSON。
7. `finally` 恢复全部 ContextVar；K Agent Runner 自己关闭本请求的 MCP manager 租约。

这里的“不保存 Session”很重要：Backend 可以多 worker，因为 Resume 不需要命中原来的
worker。多 worker 限制当前只存在于文件式 Access Layer ResumeIntent CAS，而不是
Agent Backend。

---

## 5. K Agent 的 ReAct 工具边界

主循环位于 `backend/agent/react_agent.py::run_stream_react()`：

```text
Reason
  model stream
  assistant.tool_calls 写入 provider messages

Act（按 tool_calls 顺序串行）
  yield tool_start
  写 runtime["_react_tool_boundary"]
  _run_tool
    → _execute_tool
      → pipeline.run_tool

Observe
  yield tool_result
  append role=tool + tool_call_id
  下一轮 Reason
```

`tool_start` 早于权限判断，所以 UI 能看到模型打算调用什么；真正副作用仍在 sealed
preflight 之后。工具必须串行执行，否则 `pendingIndex` 与 Observation 顺序无法稳定恢复。

### 5.1 react_tool_boundary

每次调用 `_run_tool` 前保存：

```json
{
  "version": 1,
  "kind": "react_tool_boundary",
  "iteration": 2,
  "pendingIndex": 0,
  "pendingCalls": [
    {"id": "call-1", "name": "Bash", "arguments": "{...}"}
  ],
  "modelMessages": [
    {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {"name": "Bash"}}]}
  ]
}
```

它表示“这一轮 Reason 已完成，正准备执行第几个 Act”。Resume 不能重新请求同一轮模型，
否则会重放已展示文本并可能生成不同参数。

---

## 6. sealed Tool Pipeline

`backend/agent/hooks/pipeline.py::AgentPipelineRuntime.run_tool()` 的顺序是：

```text
wrap_tool middleware
  └─ sealed(current)
       1. preflight(current)
       2. emit ToolStarted
       3. execute(current)
       4. emit ToolCompleted
```

安全性质：

1. middleware 只能拿到 `call_next`，拿不到 raw executor。
2. middleware 修改参数或重试时会重新进入 sealed preflight。
3. 权限判断、Skill allowlist 与用户提问都在 preflight。
4. Observer 是 fail-open 的观测面，不是权限决定面。
5. `ApprovalInterrupt`/任务取消使用 `BaseException` 级控制流，不会被普通工具错误恢复层
   伪装成模型可见的执行失败。

### 6.1 本地/MCP 权限审批

普通本地工具 preflight：

1. 非 `full_access` 时校验 Skill allowlist。
2. `_local_permission_decision()` 根据规则、只读工具特殊语义、权限模式和越权字段得到
   `PermissionDecision`。
3. `_enforce_permission()`：
   - allow：继续；
   - deny：抛出可恢复错误；
   - ask：调用 `approval_handler`，生成权限 Interrupt。

MCP 工具使用相同 sealed 门，只是 target 与规则 subject 为 `{serverId}:{toolName}`。

### 6.2 AskUserQuestion 特殊门

`AskUserQuestion` 仍进入同一个 sealed preflight；非 `full_access` 时先经过 Skill allowlist，
但它不经过 `_local_permission_decision()`：

1. `validate_tool_arguments()` 校验顶层 schema。
2. `normalize_user_questions()` 做业务校验并生成稳定 ID：`question-1..4`。
3. 调用相同 `approval_handler`，但 `source=user_input`、`category=user_input`。
4. handler 进入 Broker 后当前 Run terminal interrupt。
5. `_unreachable_execute()` 不应执行；若 Broker 非预期同步返回，preflight fail-closed。

该路径在 `full_access` 中仍会触发，因为它请求信息，不请求权限。

---

## 7. 权限决策规则

`_local_permission_decision()` 的当前覆盖顺序：

1. `check_permissions(tool, subjects)` 读取权限规则；Bash 会匹配整句与按
   `&& || ; |` 拆出的片段并取最严结果。
2. Read / Glob / Grep / LS 的规则 ask 降为 allow，deny 保留。
3. `permissionMode=full_access` 直接 allow。
4. Write / Edit / NotebookEdit / Bash 带
   `sandbox_permissions=require_escalated` 时在 default 强制 ask。

Bash 越权还必须给出：

```json
{
  "sandbox_permissions": "require_escalated",
  "escalation_scope": "outside_workspace_write | host_resource | network_destination",
  "escalation_resource": "具体路径、资源或 hostname"
}
```

白名单内域名再申请网络越权会 deny；普通网络错误不会自动升级成 HITL。

---

## 8. ApprovalBroker 的当前职责

`backend/approvals.py::ApprovalBroker` 只持有当前 HTTP run 的瞬时合流状态：

```text
(threadId, runId) → interrupt_queue(maxsize=1)
(threadId, runId) → run_closed Event
```

### 8.1 request()

1. 确认请求属于已注册的 active run。
2. 计算 canonical `requestHash`：target、source、serverId、arguments。
3. 生成公开卡片字段与私有 `_checkpoint`。
4. `put_nowait()` 到容量为 1 的 Interrupt queue。
5. 等待 `run_closed`，保证调用工具的协程停在副作用前。

它不会等待用户决定。返回的 cancel 只是给少数 provider bridge 的同步调用契约兜底；
正常 K Agent Runner 会先被 `stream()` 取消。

### 8.2 stream()

`stream()` 同时等待 Runner 的下一事件与 Interrupt queue：

```text
普通事件到达 → 原样 yield

Interrupt 到达
  → cancel pump_task
  → set run_closed
  → yield approval_request
  → yield interrupt
  → return，原 Run 结束
```

原 Run 在 interrupt 后结束；用户决定通过 Access Layer 认领 Resume，再开新 Run。

---

## 9. AG-UI 映射

`backend/agui.py::translate_agent_events()` 生成：

```text
approval_request
  → ACTIVITY_SNAPSHOT(activityType="approval", replace=true)

interrupt
  → STATE_SNAPSHOT(openInterrupts)
  → MESSAGES_SNAPSHOT(checkpoint.messages)
  → RUN_FINISHED(outcome.type="interrupt")
```

权限请求：

```json
{
  "reason": "tool_call",
  "responseSchema": {
    "required": ["approved"],
    "properties": {
      "approved": {"type": "boolean"},
      "scope": {"enum": ["once", "run"]}
    }
  }
}
```

用户问题：

```json
{
  "reason": "input_required",
  "responseSchema": {
    "required": ["answers"],
    "properties": {"answers": {"type": "object"}}
  }
}
```

Activity 是富 UI 投影；真正可执行的 checkpoint 只在 Backend → Access Layer 私有事件里。

---

## 10. AskUserQuestion 数据契约

### 10.1 模型调用

```json
{
  "questions": [
    {
      "header": "实现方式",
      "question": "你希望怎样继续？",
      "options": [
        {"label": "方案 A", "description": "保持当前边界"},
        {"label": "方案 B", "description": "扩大实现范围"},
        {"label": "方案 C", "description": "先做最小版本"}
      ],
      "multiSelect": true
    }
  ]
}
```

限制：1–4 个问题；每题 2–4 个 label 唯一的选项；header 最长 24；问题最长 500；
自定义答案最长 4000。

### 10.2 用户回答

```json
{
  "answers": {
    "question-1": {
      "selected": ["方案 A", "方案 C"],
      "custom": "同时保留旧接口，并补一份迁移说明"
    }
  }
}
```

`selected` 与 `custom` 独立：

- 只选选项：允许；
- 只写自定义内容：允许；
- 选择后补充自由文本：允许；
- 两者都空：拒绝；
- 单选题多个 preset：拒绝；
- label 不属于服务端问题定义：拒绝。

Access Layer 使用持久化 `detail.questions` 再校验；Frontend 不能新增选项或问题 ID。

### 10.3 回给模型的 Observation

Resume 后 K Agent 生成：

```json
{
  "ok": true,
  "answers": [
    {
      "id": "question-1",
      "question": "你希望怎样继续？",
      "selected": ["方案 A", "方案 C"],
      "custom": "同时保留旧接口"
    }
  ]
}
```

取消则为 `{"ok": false, "cancelled": true}`。两者都会成为原
`AskUserQuestion` callId 对应的 `TOOL_CALL_RESULT`。

---

## 11. Access Layer durable-before-visible

`access_layer/gateway.py` 在读取 Backend NDJSON 时：

1. 识别带 `_checkpoint` 的 approval Activity。
2. 把本轮 model/MCP/Skill/reasoning/agentOptions 写入 `checkpoint.resumeContext`。
3. 调用 `SessionStore.persist_interrupt()` 原子写：
   `sessions/{sessionId}/approvals/{interruptId}.json`。
4. 更新 Session 主 JSON 的 `openInterruptIds`。
5. 从公开 Activity 剥离 `_checkpoint`。
6. 最后才 yield SSE，并追加 flush comment。

因此“卡片已经可点击”意味着 checkpoint 已经落盘。若持久化失败，卡片不能先暴露给用户。

---

## 12. Resume 校验与状态机

Frontend 使用同一个公开 `POST /api/agent`：

```json
{
  "threadId": "same-thread",
  "runId": "new-run",
  "messages": [],
  "resume": [{
    "interruptId": "interrupt-id",
    "status": "resolved",
    "payload": {"approved": true, "scope": "once"}
  }]
}
```

问题回答把 payload 换成 `answers`。Resume 不得同时携带新用户消息。

`SessionStore.prepare_resume()`：

1. `resume[]` 必须精确覆盖当前线程所有开放 Interrupt。
2. Interrupt 状态必须是 `pending/unknown_outcome/resume_failed`。
3. 当前 runtime selection 必须与 checkpoint 的 `resumeContext` 相同。
4. 权限请求要求 `approved:boolean`；用户问题要求完整、合法 answers。
5. 未知结果或恢复失败必须显式 `reconfirm:true`。
6. 先写 ResumeIntent，再把记录改成 `resuming`。
7. 返回带 checkpoint、decision、requestHash 的私有 records 给 Backend。

`finish_resume()`：

| 情况 | 最终状态 |
| --- | --- |
| 权限批准 | `approved` |
| 用户回答 | `answered`，保存规范化 answers |
| 拒绝 | `denied` |
| 取消 | `cancelled` |
| 已知恢复失败 | `resume_failed` |
| 外部副作用结果不明 | `unknown_outcome` |

成功后从 `openInterruptIds` 移除；失败状态保留为可复核卡片。

---

## 13. K Agent Resume 的两条分支

`backend/runners/k_agent.py` 要求恰好一个 checkpoint，并写入 Runtime：

- `resume_checkpoint`
- `resume_decision`
- `resume_request_hash`

`run_stream_react()` 用 checkpoint 的 `modelMessages/pendingCalls/pendingIndex` 恢复 Act。

### 13.1 权限审批

1. 拒绝/取消：不执行，写“用户拒绝” Observation。
2. 批准：设置 `_resume_authorization={callId, requestHash}`。
3. 再进入 `_run_tool()`；完整 preflight 仍会运行。
4. `_enforce_permission()` 只有在 callId 与重新计算的 hash 都一致时消费一次授权。
5. 同批后续工具没有该授权，需要时再次 Interrupt。

### 13.2 AskUserQuestion

1. 重新计算 `AskUserQuestion + source=user_input + arguments` 的 request hash。
2. 与 `resume_request_hash` 不同则拒绝恢复。
3. cancelled 生成取消结果；resolved 重新校验每题答案。
4. 直接生成结构化 tool result，不调用 `_unreachable_execute()`。
5. append `role=tool` Observation，再从 `checkpoint.iteration + 1` Reason。

问题回答不会写入 `approved_targets`，也不会授权任何后续工具。

---

## 14. Codex 与 Claude Code

### 14.1 Codex

`backend/runners/codex_app_server.py` 把 provider 请求映射为类别：

- command execution
- file change
- `item/tool/requestUserInput` → `user_input`
- MCP elicitation
- permissions

CLI/app-server 原执行现场不会跨 terminal Interrupt 保存。Resume 使用
`consume_resume_authorization()` 按 requestHash 消费一次 provider 重放请求。
`requestUserInput` 的 `{selected, custom}` 会合并为 Codex 所需的 answers 数组。

`_codex_request_detail()` 将 method 和实际语义参数纳入哈希，但剔除重启时会变化的
`threadId/turnId/itemId`。因此新 provider Turn 可以消费同一决定，命令、补丁、权限范围或
问题内容一旦变化则不能消费。Access Layer 对 Codex 不开旁路：仍然先持久化
`restart_from_context` checkpoint，公开 Activity 才能向浏览器发送。

### 14.2 Claude Code

Claude CLI 自己执行权限算法。`claude_approval_bridge.py` 把
`--permission-prompt-tool` 的请求经 loopback bridge 接入同一个 Broker。原 CLI
进程在 Interrupt 后结束；Resume 属于 `restart_from_context` + 一次性 hash 授权，
不是恢复旧 Promise 或旧子进程。

Claude 原生 `AskUserQuestion` 由 bridge 识别为 `category=user_input`。bridge 将 Claude
可选的 preview 字段投影为共享问题契约，Access Layer 仍用服务端问题定义校验答案；Resume
再把 `selected + custom` 合并到 Claude `updatedInput.answers`，并把自定义内容保留在
`annotations.notes`。即使 `permissionMode=full_access`，私有 prompt bridge 也会保留，
因为 Claude 的 `requiresUserInteraction` 不能由 bypass 自动回答。

---

## 15. Team 与定时任务

- 定时任务在执行记录对应 Session 中保存 Interrupt；详情页使用
  `ScheduledApprovalResumeInput` 提交 approve/deny/cancel/answer。
- Team 的 checkpoint 保存于 SQLite 事件日志，浏览器只提交 approvalId 与决定；
  `TeamRuntime._approval_resume_decision()` 再按服务端问题定义校验答案。
- Team worker 与 supervisor 都用新 runId Resume，原 task/attempt 保持不变。
- terminal Interrupt 释放当前执行槽，不能把等待人工输入误记为失败或空 Artifact。

---

## 16. 安全与并发不变量

1. 工具副作用只能发生在 sealed preflight 之后。
2. `_checkpoint` 永远不进入公开 SSE、Session API 或 Team API。
3. 浏览器答案不能改变 toolName、arguments、问题定义或 requestHash。
4. 权限批准绑定 callId + requestHash；AskUserQuestion 回答也重新校验 hash。
5. 一次 Resume 必须覆盖线程全部开放 Interrupt。
6. 同一 Backend run 的 Interrupt queue 容量为 1。
7. ContextVar 与 Runtime 均请求隔离，不能放到共享 Agent 实例字段。
8. Backend 不保存跨请求审批状态，因此可以多 worker。
9. 文件式 SessionStore 的 ResumeIntent CAS 当前只支持单 Access Layer worker；多 worker
   必须迁移到 SQLite/共享事务存储。
10. `unknown_outcome` 不能自动重试可能已产生副作用的工具。

---

## 17. 失败语义

| 场景 | 行为 |
| --- | --- |
| 规则 deny | 模型可见工具错误，不弹卡 |
| 问题 schema 非法 | 模型可见工具错误，不生成不可回答卡片 |
| 页面刷新 | 从 `openInterrupts` 重建，checkpoint 仍在服务端 |
| Backend 重启 | 原 Run 已结束；Access Layer checkpoint 仍可 Resume |
| Resume runtime selection 变化 | 409 冲突 |
| 重复/不同 Resume 决定 | ResumeIntent CAS 冲突 |
| 参数或 requestHash 变化 | 授权/答案不消费，拒绝或再次询问 |
| 问题没有选择也没有文本 | 409，仍保持可回答 |
| 工具结果不确定 | `unknown_outcome`，要求人工复核 |

---

## 18. 代码地图

| 文件 | 职责 |
| --- | --- |
| `backend/main.py` | Backend run 入口、ContextVar、Runner/Broker/AG-UI 装配 |
| `backend/agent/react_agent.py` | ReAct、工具边界、权限门、问题门、Resume Act |
| `backend/agent/hooks/pipeline.py` | sealed preflight → observe → execute |
| `backend/permissions/rules.py` | allow/deny/ask 规则 |
| `backend/runners/k_agent.py` | K Agent Runtime 与 Broker/checkpoint 适配 |
| `backend/approvals.py` | requestHash、当前 run Interrupt 合流 |
| `backend/agui.py` | Activity、Snapshot、terminal outcome |
| `backend/tools/user_question.py` | 模型可见 AskUserQuestion schema |
| `backend/user_questions.py` | 问题/答案校验与模型结果渲染 |
| `backend/runners/codex_app_server.py` | Codex approval/input 映射 |
| `backend/runners/claude_approval_bridge.py` | Claude permission prompt bridge |
| `access_layer/gateway.py` | durable-before-visible、Resume 转发 |
| `access_layer/sessions/store.py` | approval 文件、open index、ResumeIntent CAS |
| `access_layer/teams/runtime.py` | Team Interrupt/Resume |
| `access_layer/scheduled_tasks/runtime.py` | 定时任务 Interrupt/Resume |
| `frontend/src/components/UserQuestionForm.tsx` | 选项 + 自定义输入问题表单 |
| `frontend/src/App.tsx` | Work 卡片与标准 Resume Run |

---

## 19. 验证

主要自动化入口：

```bash
.venv/bin/python -m pytest backend/tests -q
npm --prefix frontend run test:user-question
npm --prefix frontend run test:approval
npm --prefix frontend run test:transcript
npm --prefix frontend run check
npm --prefix frontend run build:client
git diff --check
```

当前实现已验证 `255 passed, 18 subtests passed`，其中包含 Claude/Codex 接入层重启恢复、
语义哈希和用户问题回填测试。前端类型检查、用户问题状态测试、审批卡回归、静态时间线
回归与生产构建通过。真实浏览器实例在本次环境中不可用，因此截图、
窄屏和主题视觉验收仍应在可连接浏览器的环境补做。
