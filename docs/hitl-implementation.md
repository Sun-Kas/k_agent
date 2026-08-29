# K Agent HITL 实现说明

> 读完本文应能独立画出：一次工具调用如何变成权限审批或用户问题、原 Run 如何结束、Resume 后如何只处理用户实际看过的那次请求。
> 描述的是**当前代码**，不是迁移计划。对照方案稿见 [权限模式与 HITL](permission-and-hitl-technical-solution.md)、[可持久化检查点](durable-hitl-checkpoint-technical-solution.md)。

---

## 0. 先建立心智模型

HITL 在这里不是「弹窗阻塞当前 Python 协程直到用户点按钮」。

正确模型是两次 HTTP Run：

```text
Run A（原任务）
  模型已经产出普通 tool_call 或 AskUserQuestion
  工具还没执行
  权限判定为 ask，或模型明确要求用户回答
  → 发出权限卡片或问题表单
  → 以 AG-UI RUN_FINISHED(outcome=interrupt) 正常结束
  → 释放 SSE、会话锁、MCP、并发槽

（用户可以关页、刷新、第二天再来）

Run B（Resume，新 runId，同一 threadId）
  Access Layer 认领那个 Interrupt
  Backend 带上 checkpoint + 用户决定
  批准：执行当时那一次工具（参数 hash 必须一致）
  拒绝：把「未执行」写成 tool 结果给模型
  回答：校验 selected/custom，把答案写成 AskUserQuestion 的 tool 结果
  然后 K Agent 从下一轮 Reason 继续
```

Backend **不保存** `requestId → Future`。HITL 权威状态在 Access Layer 的 Session 目录里。所以 Backend 多 worker 时，提交决定不必打回「当时那个进程」。

三种 Agent（K Agent / Claude Code / Codex）共用这套「Interrupt + Resume」产品面；**谁算出 ask、Resume 时能不能精确续上**，三种不一样。本文以 K Agent 为主路径，第 11 节补另外两个。

---

## 1. 职责切分（必须记住）

| 层 | 干什么 | 不干什么 |
| --- | --- | --- |
| 前端 | 渲染权限卡或问题表单；提交带 `resume[]` 的新 `POST /api/agent` | 不存 checkpoint；不能只凭历史卡片推断可执行状态 |
| Access Layer | 会话锁、落盘 `approvals/{id}.json`、CAS 认领 Resume、拦截「有开放 Interrupt 时发新消息」、剥掉 `_checkpoint` 再给浏览器 | 不执行工具；不判权限规则 |
| Agent Backend | 判 allow/deny/ask；校验用户问题；组 Interrupt payload；Resume 时执行、拒绝或注入答案 | 不写 `$K_AGENT_HOME/state/sessions`；不等跨请求 Future |
| 工具实现 | 只在已经过门之后跑；Bash 根据已批准的越权字段决定是否跳过 srt | 不自己弹 HITL；不把 `require_escalated` 当成已授权 |

公开 API 仍是 AG-UI 的 `RunAgentInput`。没有另一套「离线执行工具」接口。审批主路径是 Resume Run。

---

## 2. 权限模式：什么时候根本不会问

用户可见只有两个值：`default` | `full_access`。来自 `forwardedProps.agentOptions.permissionMode`，Access Layer 校验后写入本次 run，Backend 再用 `ContextVar` 绑到工具实现（并发会话互不污染）。

- **`default`**：走规则 + 沙箱；越权申请会 `ask`。
- **`full_access`**：K Agent 决策函数直接 `allow`（含规则 deny）；Bash 可跳过 srt。这是人在入口选的，模型改不了。

未传字段当 `default`。定时任务 / Team 把该字段存在各自表里，每次触发带进 run。

普通工具的权限 HITL 只处理 **`ask`**。`deny` 变成模型可见的工具错误 JSON，不弹卡，`allow` 直接执行。`AskUserQuestion` 是另一种语义：即使是 `full_access` 也必须询问，因为它是在收集用户意图，不是在申请工具权限。

---

## 3. K Agent：一次工具调用怎么走到 ask

主循环在 `backend/agent/react_agent.py` 的 `run_stream_react`。

### 3.1 ReAct 里 HITL 卡在哪

```text
Reason（模型）
  → 若有 tool_calls：把 assistant（含 tool_calls）写入 messages
  → 按顺序 Act，不并行
       对每个工具：
         1. yield tool_start（前端已经有卡片）
         2. 拍 _react_tool_boundary（检查点）
         3. _tool_excute_serially → _run_tool → pipeline.run_tool
         4. 若执行成功：yield tool_result，append Observation
  → 下一轮 Reason
```

并行会打乱 Observation 顺序，也会让检查点里的 `pendingIndex`（「做到第几个工具」）失去意义。

`tool_start` **早于** 权限判定。用户能先看到「模型想调什么」。真正副作用仍在审批之后。Interrupt 时通常还没有 `tool_result`，这是合法边界。

### 3.2 检查点必须在进工具之前拍

每次即将 `_run_tool` 前写入：

```text
runtime["_react_tool_boundary"] = {
  version, kind: "react_tool_boundary",
  iteration,          # 第几轮 Reason 之后
  pendingIndex,       # 本批 tool_calls 的下标
  pendingCalls,       # 这一轮模型点的全部工具
  modelMessages,      # 已含当前 assistant.tool_calls，不含本工具及之后的 Observation
}
```

原 Run 结束时模型文本已经流过。Resume **禁止重放 Reason**，只从「第几个工具」续 Act，然后 `iteration + 1` 再 Reason。

### 3.3 密封管线：权限焊在 execute 之前

`pipeline.run_tool`（`backend/agent/hooks/pipeline.py`）：

```text
wrap_tool（可改参数，改完必须重新进门）
  └─ sealed
        1. preflight     ← Skill 白名单 + 权限 + HITL
        2. ToolStarted   ← 观测；此时人若还没批，走不到这里
        3. execute       ← schema 校验 + 真正执行
        4. ToolCompleted
```

Middleware 拿不到 raw `execute`。HITL 发生在 preflight，**还没有** ToolStarted，通常也没有 live stdout。

`_run_tool` 会把普通 `Exception` 收成 Observation，让模型改方案。`CancelledError` 和 `ApprovalInterrupt`（`BaseException`）不会被收成工具错误，以免把「等人」伪装成「工具失败」。

### 3.4 决策只出结论，不挂起

`_local_permission_decision`（同文件）按层覆盖：

1. `check_permissions(工具名, subjects)`
   规则文件 `permissions.json`：`tool` + `pattern` + `allow|deny|ask`。
   Bash 的 subject 是整句命令 **加上** 按 `&& || ; |` 换行拆开的每一段，取最严。路径类工具用 `file_path` / `path`。
2. Read / Glob / Grep / LS：规则 `ask` **降成 allow**；`deny` 仍在。
3. `full_access`：一律 allow。
4. Write / Edit / NotebookEdit / Bash 若带 `sandbox_permissions=require_escalated`：在 default 下 **强制 ask**。
   这是申请不是凭证。Bash 还必须有合法 `escalation_scope` + 非空 `escalation_resource`。白名单内域名再申请越权 → **deny**（不弹卡）。

MCP：`full_access` 则 allow，否则 `check_permission("mcp", "{server}:{tool}")`。

Skill 白名单：非 full_access 时，已激活 Skill 的 `allowedTools` 在 preflight 里硬拦，不进 HITL，直接失败。

### 3.5 `_enforce_permission`：从结论到 Interrupt

```text
若 Resume 一次性授权匹配 callId + requestHash → 消费掉，放行（不再问）
若 allow 或本 continuation 的 approved_targets 已有该 target → 放行
若 ask → await approval_handler(...)
     原 Run 上 handler 会进 Broker.request()，随后原 Run 被拆掉
     Resume Run 上若授权未匹配，仍会再问
若 deny → RuntimeError → Observation
```

`scope=run` 的「本轮始终允许」写入 `runtime["approved_targets"]`，只活在**当前逻辑 continuation** 的内存里，不落库、不带到用户新开的普通消息 Run。

### 3.6 `AskUserQuestion`：问题门，不是权限门

`backend/tools/user_question.py` 注册模型可见工具 `AskUserQuestion`。它允许一次提交 1–4 个问题；每题有 2–4 个选项，并声明 `multiSelect`。注册的 executor 故意不可达，因为该工具不能产生普通副作用，也不能直接返回伪答案。

`_local_tool_preflight` 识别到该工具后走专用分支：

1. 非 `full_access` 时仍先检查 Skill `allowedTools`，防止 Skill 调用未声明工具。
2. 按工具 schema 校验外层参数。
3. `normalize_user_questions` 再校验问题 id、标题、文案、选项 label/description、数量及重复值。
4. 调 `approval_handler(..., source="user_input")`，把规范化问题放入 `arguments.questions`。
5. handler 正常返回也会 fail-closed；原 Run 必须通过 Broker 的 terminal Interrupt 结束，绝不能继续调用不可达 executor。

这里**不调用** `_local_permission_decision`，也不读取 `approved_targets`。因此 `full_access`、权限规则 allow、一次性工具授权都不能替用户回答问题。

---

## 4. ApprovalBroker：把「还要执行」变成「结束 Run」

文件：`backend/approvals.py`。名字还叫 Broker，但已经**没有** pending Future 表。

Backend `POST /internal/agent/run` 把 Runner 事件包进 `approvals.stream(events, thread_id, run_id)`。`stream()` 为这次 run 登记：

- `interrupt_queue`（容量 1：同一 run 不能同时两个 Interrupt）
- `run_closed` Event

两路并发等：Runner 的下一个事件，或队列里的 Interrupt。

### 4.1 `request()` 做什么

K Agent 的 `approval_handler`（`backend/runners/k_agent.py`）调用：

```text
broker.request(
  thread_id, run_id, agent_kind="k_agent",
  category=local_tool|mcp_tool|user_input,
  title/message,
  detail={ toolName, callId, arguments, source, ... },
  checkpoint=_react_tool_boundary + messages,
)
```

`request()`：

1. 算 `requestHash` = canonical JSON SHA256（toolName / source / serverId / arguments）。问题文本、选项或 multiSelect 改动都会改变哈希。
   以后批准只对这个哈希有效。
2. 组 payload（`id` 即 interruptId，`toolCallId`，`_checkpoint`）。
3. `interrupt_queue.put_nowait`。队列满说明本 run 已有未决 Interrupt。
4. `await run_closed.wait()`。

它**不返回用户决定**。用户决定发生在另一次 HTTP 上。原 Run 被拆掉时，等在这里的协程被 `run_closed.set()` 或取消唤醒；返回的 `{"action":"cancel"}` 只是占位，给 Claude 桥这类「必须给调用方一个 JSON」的路径用。K Agent 原 Run 随后被取消，不会拿这个 cancel 去执行工具。

### 4.2 `stream()` 看到队列之后

```text
取消 pump（停止消费 Runner）
set run_closed          # 松开 request() / 子进程桥
yield approval_request  # → 前端审批卡
yield interrupt         # → RUN_FINISHED interrupt
return                  # 原 HTTP 流正常结束
```

`ApprovalInterrupt`（`BaseException`）仍保留：若有 Runner 直接抛它，空队列时也会塞进去。当前 K Agent 主路径走队列，不靠这个异常传决定。

---

## 5. 内部事件如何变成 AG-UI

`backend/agui.py`：

| 内部事件 | AG-UI |
| --- | --- |
| `tool_start` | `TOOL_CALL_START` + `ARGS` + `END` |
| `approval_request` | `ACTIVITY_SNAPSHOT(activityType=approval)`，`messageId=interruptId` |
| `interrupt` | `STATE_SNAPSHOT`（开放 Interrupt 引用）→ `MESSAGES_SNAPSHOT` → `RUN_FINISHED.outcome.type=interrupt` |

权限 Interrupt 的 `reason` 为 `tool_call`，带 `toolCallId` 和 `responseSchema`（`approved` + 可选 `scope: once|run`）。`category=user_input` 时，`reason=input_required`，响应 schema 改为每题的 `selected[]` 与 `custom`。`_checkpoint` 留在 payload 里给 Access Layer，**不应**作为浏览器可执行状态。

`agui` 遇到 `interrupt` 会 `return`，不再发普通 `RUN_FINISHED` 成功收尾。

---

## 6. Access Layer：先落盘，再让卡片可点

`access_layer/gateway.py` 读 Backend SSE 时，若帧是带 `_checkpoint` 的审批 Activity：

1. 把当前模型 / MCP / Skill / `permissionMode` 等写进 checkpoint 的 `resumeContext`（恢复时不得改用用户后来改的下拉框）。
2. **`persist_interrupt` 成功之后**才把**剥掉 checkpoint** 的公共事件 yield 给浏览器。
3. 审批帧后再发 SSE comment 并 `asyncio.sleep(0)`，减少中间层缓冲导致「卡要刷新才出现」。

`SessionStore.persist_interrupt`（`access_layer/sessions/store.py`）：

- 原子写 `$K_AGENT_HOME/state/sessions/{id}/approvals/{interruptId}.json`
- 主 Session JSON 的 `openInterruptIds` 追加该 id
- 同一 interruptId 若 `requestHash` 变了 → 冲突

`list_open_interrupts` 给前端刷新后重建卡片，**永远不返回 checkpoint**。

有开放 Interrupt 时，`ensure_accepts_new_input` 拒绝普通新用户消息。Resume 请求不得夹带新 chat 消息。

文件式 CAS **只安全于单 Access Layer worker**。多 worker 必须换共享事务存储，代码里写死了这条约束。

---

## 7. 用户点按钮：Resume Run

前端 `submitApproval`（`frontend/src/App.tsx`）构造：

```text
POST /api/agent
threadId: 原会话
runId: 新的客户端 id
messages: []
resume: [{
  interruptId,
  status: "resolved" | "cancelled",
  payload?: { approved, scope, reconfirm? }
}]
forwardedProps: 尽量带上原 agentKind / permissionMode
```

问题表单走同一个 Resume Run，但 payload 是答案而不是批准：

```text
resume: [{
  interruptId,
  status: "resolved",
  payload: {
    answers: {
      "question-id": { selected: ["选项 A"], custom: "补充约束" }
    }
  }
}]
```

`selected` 与 `custom` 相互独立：可以只选选项、只填自定义内容，也可以两者同时提交。每题至少有一项非空；单选题最多一个 selected；selected 必须精确匹配原问题的 option label。取消仍用 `status: cancelled`。

`unknown_outcome` / `resume_failed` 必须带 `reconfirm: true` 才能再认领。

Gateway：

1. `prepare_resume`：开放 Interrupt 必须被 **全部覆盖**（现在通常一次只有一个）；校验 payload；checkpoint 里的 `resumeContext` 必须和本次 forwarded 一致；写 ResumeIntent；状态改为 `resuming`。
2. 把 `resume` 和完整 `resumeCheckpoints`（含 checkpoint、decision、requestHash）转给 Backend。浏览器看不到 checkpoint。
3. Resume 流结束后 `finish_resume`：成功则从 `openInterruptIds` 去掉；失败标 `resume_failed`；工具已开始但结果不明标 `unknown_outcome`。

相同 resume 输入哈希会返回原 `resumeRunId`（幂等）；不同 payload 冲突。

---

## 8. K Agent Resume：精确续 Act

`KAgentRunner` 要求 `resume_checkpoints` **恰好一条**。写入 agent runtime：

- `resume_checkpoint`（`react_tool_boundary`）
- `resume_decision`
- `resume_request_hash`

`run_stream_react` 开头若有 checkpoint：

1. 用 `modelMessages` 覆盖刚拼的 messages（必须对上 `assistant.tool_calls`）。
2. 从 `pendingIndex` 走到本批末尾。
3. 每个工具先再发一遍同 id 的 `tool_start`（旧 run 的未完成 buffer 在 terminal 边界已被清掉，前端按 id 原位更新）。
4. **当前下标且用户未批准**：不执行，写入固定 Observation（「user denied/cancelled」），否则下一 Reason 会以为这个 call 没结果。
5. **当前下标且批准**：设置 `_resume_authorization = { callId, requestHash }`，再 `_run_tool`。
   preflight 仍会跑完整决策；`_enforce_permission` 只在 **callId + 现算 hash 都匹配** 时放行。参数被改过 → 授权无效 → 再 ask。
6. 同批 **后面的** 工具不带这次授权；若仍要 HITL，会再 Interrupt。一次批准不是整批放行。
7. `start_iteration = checkpoint.iteration + 1`，进入正常 Reason 循环。

批准路径上 schema 仍在 execute 里校验。门和执行之间仍是「先问后人跑」；Resume 时会再跑一遍 Skill 白名单和规则（除被一次性授权跳过的那次 ask）。

如果当前调用是 `AskUserQuestion`，第 4–5 步换成专用恢复分支：

1. 用 checkpoint 中的 `toolName/source/serverId/arguments` 重算 requestHash，必须等于 Access Layer 转发的 `resume_request_hash`。
2. `cancelled` 生成 `{"ok": false, "cancelled": true}` 的 tool result。
3. `resolved` 调 `normalize_user_question_answers`；它以 checkpoint 中的原问题为准验证答案，不能信任浏览器另传的问题定义。
4. `render_user_question_result` 把规范化答案写成模型可读的 tool result，然后继续本批 Act 与下一轮 Reason。

Ask 分支不会设置 `_resume_authorization`，也不会再次进入普通工具 executor；用户答案本身就是该调用的 Observation。

---

## 9. 用一条 Bash 越权把时间走一遍

假设 `permissionMode=default`，模型调用：

```json
{
  "command": "echo hi > /tmp/out",
  "sandbox_permissions": "require_escalated",
  "escalation_scope": "outside_workspace_write",
  "escalation_resource": "/tmp/out"
}
```

1. Reason 结束，yield `tool_start`（命令已经在卡片上）。
2. 写入 `_react_tool_boundary`。
3. wrap 若剥过非法字段，会重新 preflight。
4. 规则可能 allow；第 4 层因 `require_escalated` 变成 ask。
5. `approval_handler` → `request()` → 队列。
6. Broker yield 卡片 + interrupt；agui 发 Activity、Snapshot、`RUN_FINISHED`。
7. Gateway 先写 `approvals/{id}.json` 再给浏览器。
8. 工具未执行，srt 未启动，HTTP 结束。
9. 用户 `approve` + `once` → 新 Run。`prepare_resume` CAS。
10. K Agent 从 pendingIndex 执行；hash 一致则跳过第二次询问；execute 里校验参数；Bash 因 payload 仍带越权字段且 ContextVar 仍是 default，按「已批准的单次越权」跳过 srt。
11. Observation 进 messages，下一轮 Reason。
12. 再一次无越权字段的 Bash：仍走沙箱，不会因为上次 once 而永久免问。若当时选了 `scope=run`，同一 continuation 里相同 target 可免问，新用户消息 Run 不会继承。

拒绝则第 10 步写「未执行」Observation，模型可以换不越权的写法。

---

## 10. 三种用户决定

| 操作 | Resume payload | 当前这次调用 | 之后 |
| --- | --- | --- | --- |
| 批准一次 | `approved: true, scope: once` | 执行（hash 命中） | 再 ask 再问 |
| 本轮始终允许 | `approved: true, scope: run` | 执行 | 写入 `approved_targets`，仅本 continuation |
| 拒绝 | `approved: false` | 不执行，Observation 告诉模型 | 下次仍问 |
| 取消 | `status: cancelled` | 同拒绝 | 同拒绝 |

没有「写入用户 permissions.json 永远允许」。

### 10.1 用户问题的三种回答组合

假设单题提供 `方案 A / 方案 B / 方案 C`，以下都合法：

| 用户操作 | `selected` | `custom` |
| --- | --- | --- |
| 只选择已有选项 | `["方案 A"]` | `""` 或省略 |
| 只输入自己的答案 | `[]` 或省略 | `"请改用方案 D"` |
| 既选择又补充 | `["方案 A"]` | `"但只处理最近 7 天"` |

多选题可选择多个 label；单选题最多一个。前端 `UserQuestionForm` 不会因勾选选项而禁用文本框，也不会因输入文本而清空选择。Backend 才是最终校验者，因此 Team、定时任务或其他客户端也必须遵守同一契约。

---

## 11. Claude Code 与 Codex（同一张卡，不同恢复精度）

二者需要人时也调同一个 `ApprovalBroker.request()`，前端仍是 Interrupt + Resume。

**Claude Code**（`claude_code.py` + `claude_approval_bridge.py`）：子进程 `claude -p --permission-prompt-tool ...`。Claude 自己的权限引擎决定 ask 后，stdio MCP `request_approval` 打 loopback 到本 run 的桥。桥再 `broker.request()`。失败必须 deny（fail-closed）。`--allowedTools` 会预放行一串内置工具，未列出的 MCP 更容易进卡。

Resume：CLI 进程已死，走 `restart_from_context`。`consume_resume_authorization` 按 hash 消费**一次**；Claude 若重放完全相同 tool/input 则免问，参数变了再问。不能声称原地恢复旧进程。

Claude 调用原生 `AskUserQuestion` 时，bridge 将其改为 `category=user_input`，把问题投影为
共享表单。Access Layer 校验、持久化和 CAS 与 K Agent 相同；Resume 时 bridge 把每题
`selected/custom` 转为 Claude 需要的 `updatedInput.answers`，自定义文本同时写入
`annotations.notes`。`full_access` 仍安装私有 prompt bridge，只让 Claude 的普通权限进入
`bypassPermissions`，不能让交互工具失去回答通道。

**Codex**：app-server 的 `approvalPolicy=on-request` 映射过来，恢复同样是 hash 一次性授权 + 必要时重启上下文。hash 包含 method 和去掉 `threadId/turnId/itemId` 后的实际 params：provider 重启产生的新路由 ID不影响消费，但命令、补丁或问题内容变化一定会重新 Interrupt。`requestUserInput` 的选择与自定义文本都会进入 Codex answers 数组。

三种 Runner 的公共保证在 Access Layer，而不是 provider 进程：Activity 公开前 checkpoint
必须落盘；刷新/Access Layer 重启后仍能列出；ResumeIntent 只认领一次；浏览器永远拿不到
checkpoint。Claude/Codex 的差别只在 Backend 如何重放并消费该 checkpoint。

K Agent 是唯一实现了精确 `react_tool_boundary` 续 Act 的 Runner。

---

## 12. 失败与边界（实现层）

| 情况 | 行为 |
| --- | --- |
| 规则 deny | 不弹卡，工具错误 JSON |
| 越权字段不完整 | Bash deny |
| 白名单域名还申请网络越权 | deny |
| 原 Run 被取消 / 进程退出 | 若已 persist，卡片仍在；未 persist 则不能点 |
| 刷新页面 | `openInterrupts` 重建卡，checkpoint 仍在服务端 |
| 有开放 Interrupt 发新消息 | Access Layer 400 |
| Resume 漏掉某个开放 Interrupt | 冲突 |
| Resume 时模型/MCP 与 checkpoint 不一致 | 冲突 |
| 工具已开始、结果丢失 | `unknown_outcome`，禁止自动重试 |
| wrap 改了参数 | 重新 preflight；Resume hash 对的是 Interrupt 当时的 arguments |
| 同 run 两个 ask | 队列容量 1，第二次 `request` 失败 |

---

## 13. 代码地图（按阅读顺序）

1. `backend/main.py` — Run 输入、ContextVar 与 Runner/Broker/AG-UI 装配
2. `backend/agent/react_agent.py` — 循环、boundary、权限/问题门、Resume Act
3. `backend/agent/hooks/pipeline.py` — `run_tool` 密封门
4. `backend/permissions/rules.py` — 权限规则匹配
5. `backend/tools/user_question.py` — `AskUserQuestion` 工具 schema
6. `backend/user_questions.py` — 问题与答案的规范化、校验、结果渲染
7. `backend/runners/k_agent.py` — handler / checkpoint / decision 接到 Broker
8. `backend/approvals.py` — 当前 Run 的 Interrupt 控制流与 requestHash
9. `backend/agui.py` — Activity + terminal interrupt + response schema
10. `access_layer/gateway.py` — durable-before-visible、转发 resumeCheckpoints
11. `access_layer/sessions/store.py` — 落盘、答案校验、CAS 认领、门禁
12. `frontend/src/components/UserQuestionForm.tsx` — 选项与自定义输入
13. `frontend/src/App.tsx` — Work 场景提交 Resume Run
14. `backend/runners/claude_approval_bridge.py` — Claude 权限提问来源

测试入口：`backend/tests/test_durable_hitl.py`、`backend/tests/test_approvals.py`、`backend/tests/test_user_questions.py`、`frontend/scripts/test-user-question.ts`。

---

## 14. 读完后应能回答的问题

1. 用户点批准时，原 Python 协程还在不在？（不在。那是新 Run。）
2. 浏览器有没有可执行 checkpoint？（没有。只有卡片投影。）
3. 为什么要 `requestHash`？（防止 Resume 执行与卡片不一致的参数。）
4. 为什么 `tool_start` 在询问之前？（展示意图；副作用仍在门后。）
5. 为什么同批第二个工具不一起批准？（授权绑定单次 callId。）
6. `full_access` 还会不会进 Broker？（普通工具权限不会；`AskUserQuestion` 仍会。）
7. 用户能否同时选择选项并补充文本？（可以；`selected` 与 `custom` 独立。）
8. Claude Runner 的权限 ask 是谁算的？（Claude CLI；本系统承接提问并做 Interrupt。）

若这八条都能用自己的话讲清，实现逻辑已经闭环。细节再对着第 13 节的文件往下看即可。
