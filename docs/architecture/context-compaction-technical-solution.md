# K Agent 上下文压缩最终技术方案

> 完整对话历史的落盘格式、AG-UI 事件白名单、`status/trace` 清理和前端投影实施步骤，统一见
> [上下文压缩、完整对话存储与 AG-UI 重构计划](context-compaction-conversation-storage-refactor-plan.md)。

> 状态：已实施（状态协议、工具预算、LLM full compact、自动 continuation、重启恢复及产品入口均已接通）
>
> 更新日期：2026-09-04
>
> 适用范围：`agentKind=k_agent`。Claude Code / Codex CLI Runner 使用各自的会话机制，不接入本方案。
>
> Claude Code 对照基于 `~/Desktop/claude-code-main/` 的本地源码快照。该目录没有版本清单，
> 本文只把它当作设计参考，不把其中的实验开关视为稳定公开协议。

本文替代此前“每次 HTTP run 临时抽取摘要”的方案。最终目标不是逐行复制 Claude Code，而是在保留
K Agent 双进程边界的前提下，实现相同的关键语义：**压缩结果是可持久化的会话状态，压缩后能在
同一个用户回合继续执行，并且下一轮不会重新从完整历史做一次临时摘要。**

---

## 0. 最终结论

K Agent 采用四层上下文控制：

```text
单条工具结果预算
  → microcompact 清理旧工具正文
  → 持久化 full compact（LLM 结构化摘要 + compact boundary）
  → Provider 413 时 reactive compact 兜底
```

其中：

- Access Layer 继续是追加式完整对话历史、AG-UI Event 和 compact state 的唯一真相源；Provider messages 只是调用前投影。
- Agent Backend 继续无状态，只负责预算判断、生成压缩候选和执行模型/工具循环。
- 完整历史永不因压缩删除；UI 仍展示原始对话。
- 模型活动上下文改为“持久摘要 + boundary 之后的原始消息 + 当前工作集”。
- Full compact 可以在任意一次 Reason 之前触发，而不再只发生于 `create_runtime`。
- 压缩提交后由 Access Layer 自动续跑，用户看到的仍是同一个 run，不需要再发送“继续”。
- 当前 `_merge_summary` 的逐条截断 bullet 只作为迁移期兜底，最终删除。

一句话概括最终职责：

> Access Layer 保存压缩后的会话状态；Backend 计算压缩内容；Provider 只接收当前有效投影。

---

## 1. 为什么当前实现必须替换

当前生产链路是：

```text
Access Layer 每轮发送完整历史
  → Backend 在 create_runtime 调一次 build_context_plan
  → 较早消息被机械抽取成 bullet
  → 摘要只用于本次 Provider 请求
  → run 结束后摘要丢失
  → 下一轮再次读取完整历史并重新计算
```

这有六个结构性问题：

1. **摘要没有会话身份**：不存在 boundary、generation 或覆盖范围，无法可靠恢复。
2. **每轮重复压缩**：同一段旧历史反复抽取，浪费 CPU 和请求传输，也无法形成稳定上下文。
3. **摘要质量不足**：逐条截断不能稳定保存用户纠正、技术决策、当前工作和下一步。
4. **触发位置太早**：只在 `create_runtime` 检查一次；本轮多次工具调用仍可能把上下文撑爆。
5. **HITL 状态分叉**：恢复使用 `checkpoint.modelMessages`，新算出的 `ContextPlan` 只是旁路观测。
6. **没有产品闭环**：无手动 compact、自定义指令、失败熔断、临界提示和持久化观测。

这些问题不能通过“把 `_merge_summary` 换成一次模型调用”解决。没有持久状态协议，模型摘要在下一轮
仍会丢失；没有 continuation 协议，中途压缩也无法安全地跨 Backend HTTP run 继续。

---

## 2. 从 Claude Code 采用什么，不采用什么

### 2.1 采用的主干能力

| Claude Code 机制 | K Agent 最终对应 |
|---|---|
| 每次主模型调用前检查预算 | 每次 Reason 前由 `ContextController` 检查 |
| Tool-result budget | 工具完成时按工具策略限制单条 Observation |
| Microcompact | 保留 tool call/result 配对，只替换可重取的旧正文 |
| Full compact | 禁止工具的专用模型调用，输出固定结构摘要 |
| Compact boundary | Access Layer 持久化 boundary、摘要和覆盖前缀哈希 |
| Post-compact attachments | 重建项目指令，并补回近期文件、Skill 和 plan 工作集 |
| 同 turn 继续 | Access Layer 自动 continuation，前端仍看到一个 run |
| 手动 `/compact` | 会话 compact API，前端命令只是该 API 的入口 |
| Reactive compact | 只对明确的 context-length / 413 错误强制重试一次 |
| 失败熔断 | 会话级连续失败计数，三次后停止自动 compact |

### 2.2 第一版明确不采用

- **Session-memory compact**：K Agent 的个人 `MEMORY.md` 不是会话摘要，不能拿来替换 transcript。
- **Context Collapse**：分段上下文 Agent、commit log 和读取投影复杂度高，等主链稳定后再评估。
- **Cached microcompact / provider cache edit**：不同 Provider 能力不一致，第一版使用客户端消息替换。
- **常规 History Snip**：不把无摘要删除旧轮次作为正常策略；只在 compact 请求本身超长时用于逃生。
- **删除原始消息**：压缩永远不改 UI 历史和审计记录。

这意味着第一版复制的是 Claude Code 的稳定主干，而不是把所有实验策略同时打开。

---

## 3. 不可破坏的架构边界

### 3.1 Access Layer 拥有状态

Access Layer 负责：

- 保存 `history.jsonl` 中的用户输入和对话相关 AG-UI 事件，调用 Provider 前临时投影 `messages`；
- 保存 K Agent 专属 compact state；
- 校验 compact boundary 是否仍对应当前历史前缀；
- 在同会话锁内原子提交压缩状态；
- 处理分支、取消、编辑、删除与 HITL 对 compact state 的影响；
- 自动发起 Backend continuation；
- 只向前端公开不含摘要正文的压缩状态和指标。

### 3.2 Agent Backend 只负责计算

Agent Backend 负责：

- 根据真实 Prompt、工具 schema、消息和模型配置计算预算；
- 执行单条工具预算和 microcompact；
- 调用 compact 模型生成结构化摘要；
- 生成带预期 generation 的 `CompactionProposal`；
- 生成恢复当前 ReAct 循环所需的 continuation checkpoint；
- 在每次 Reason 前重新判断上下文，而不是只在创建 Runtime 时判断。

Backend 不读写 session 文件，也不把内存中的 Agent/Runtime 当作恢复真相源。

### 3.3 浏览器不参与可信提交

浏览器可以请求手动 compact，但不能提交以下内容：

- summary 正文；
- boundary 或覆盖消息 ID；
- context generation；
- continuation checkpoint；
- tool-result replacement。

这些字段只通过 Access Layer 与 Agent Backend 的内部协议传递。

---

## 4. 持久化数据模型

### 4.1 存储位置

每个会话增加独立文件：

```text
$K_AGENT_HOME/state/sessions/<session-id>/context/k_agent.json
```

不把 compact summary 混进公开 `SessionState`，也不把它伪装成用户消息。会话详情 API 返回
`history.jsonl` 的可重放事件投影；Provider 输入由服务端根据 history 和该文件构造。

### 4.2 `KAgentContextState`

```json
{
  "schemaVersion": 1,
  "sessionId": "session-id",
  "generation": 3,
  "revision": 12,
  "boundary": {
    "id": "compact-id",
    "coveredThroughMessageId": "message-id",
    "coveredPrefixDigest": "sha256:...",
    "trigger": "auto",
    "sourceRunId": "run-id",
    "createdAt": "2026-09-03T12:00:00Z"
  },
  "summary": {
    "formatVersion": 1,
    "text": "...",
    "modelId": "compact-model-id",
    "inputTokens": 82000,
    "outputTokens": 6200
  },
  "toolReplacements": [
    {
      "messageId": "tool-message-id",
      "toolCallId": "call-id",
      "sourceDigest": "sha256:...",
      "replacement": "[Older Read output cleared; run Read again if exact content is needed.]",
      "originalChars": 42000,
      "reason": "microcompact"
    }
  ],
  "workingSet": {
    "recentFiles": [
      {"path": "backend/context/manager.py", "observedDigest": "sha256:..."}
    ],
    "invokedSkillIds": ["skill-id"],
    "plan": null
  },
  "failureState": {
    "consecutiveAutoFailures": 0,
    "autoDisabled": false,
    "lastFailureCode": null
  },
  "pendingContinuation": null
}
```

`pendingContinuation` 只在“压缩已提交、Backend 尚未续跑完成”时保存一次性 checkpoint。它与新的
boundary/summary 位于同一个 JSON 中，因此一次原子替换就能保证二者同时成功或同时失败；不依赖跨文件事务。

示例展示的是已经成功 compact 的状态。首次 compact 前文件可以不存在；只记录失败熔断而尚无摘要时，
`boundary`、`summary` 和 `pendingContinuation` 均为 `null`。

### 4.3 为什么不用 `compactedMessageIds[]`

长会话保存所有已压缩 ID 会无限增长。最终协议只保存：

- `coveredThroughMessageId`：boundary 在完整消息序列中的锚点；
- `coveredPrefixDigest`：从第一条消息到锚点的规范化内容 SHA-256；
- `generation`：只在 full compact 成功前移 boundary 时递增，供 continuation/HITL 判断活动摘要代次；
- `revision`：compact state 任意变更都递增，供存储 CAS 使用。

新消息追加在 boundary 后面不会让旧摘要失效。若锚点之前的消息被编辑、删除、重排或取消，前缀哈希
不再匹配，Access Layer 必须废弃该 compact state，从完整历史重新建立，不能继续相信旧摘要。

规范化哈希覆盖消息的 `id/role/content/meta/toolCalls` 和附件内容摘要，不包含会话标题、更新时间等旁路字段。
附件只参与内容摘要计算，不把 data URL 原文复制进 context state。

### 4.4 摘要和原始历史的关系

摘要是完整历史的**派生索引**，不是事实源：

```text
history.jsonl：可审计、可重新压缩、用于 UI 与 Provider messages 投影
compact state：Provider 活动上下文的持久化投影
```

删除 compact state 最坏只会导致下一轮重新压缩，不会丢失用户历史。

---

## 5. 一次用户请求的完整流程

```mermaid
sequenceDiagram
  participant UI
  participant AL as Access Layer
  participant BE as Agent Backend
  participant LLM as Provider

  UI->>AL: 新 user message
  AL->>AL: 持久化消息并锁定 session
  AL->>AL: 读取并校验 KAgentContextState
  AL->>BE: active tail + compact state + runtime selection
  loop 每次 Reason 前
    BE->>BE: tool budget → microcompact → token budget
    alt 未超过阈值
      BE->>LLM: 主模型请求
    else 需要 full compact
      BE->>LLM: compact query（无工具）
      LLM-->>BE: 结构化摘要
      BE-->>AL: private CompactionProposal + continuation checkpoint
      AL->>AL: 校验 generation/prefix，原子持久化
      AL->>BE: 自动 continuation（同一 public run）
    end
  end
  BE-->>AL: 标准 AG-UI events
  AL-->>UI: 保持原始到达顺序
```

关键点：K Agent 不是单进程 REPL，不能直接把某个 Python list 当作跨请求状态。这里用
“Backend 提案 → Access Layer 提交 → Backend 自动续跑”实现等价的会话状态转换。

---

## 6. 活动上下文如何构造

Access Layer 在每个新 run 开始时：

1. 读取完整消息和 `KAgentContextState`。
2. 根据 `coveredThroughMessageId` 重新计算前缀 SHA-256。
3. 校验失败则原子移除失效 state，并发送完整历史。
4. 校验成功则只发送 boundary 之后的原始消息和 compact state。
5. 对仍位于活动尾部的 tool 消息应用已持久化 replacement。

Backend 构造 Provider 消息的固定顺序：

```text
1. system prompt（平台契约 + persona）
2. request context（项目指令、Memory、可用 Skill、MCP instructions）
3. conversation summary（若有，明确标记为 continuity context）
4. post-compact working set（文件、Skill 使用记录、plan）
5. boundary 之后的原始消息
6. 本 run 新产生的 assistant/tool 消息
```

Conversation summary 不能写进 system prompt，也不能变成普通用户请求。它继续使用独立的
`<system-reminder># conversation_summary` 上下文段，避免改变平台指令的权威级别。

---

## 7. 分层压缩算法

### 7.1 第一层：单条工具结果预算

每个工具定义 `contextPolicy`：

```text
mode: rerunnable | receipt | retain
maxResultChars: 正整数
```

- `rerunnable`：Read、Grep、Glob 等。超限时保留头尾、统计和“可重新执行”提示。Bash 在没有可靠的
  只读命令分类证据时按 `retain`，不能仅凭工具名假设可重放。
- `receipt`：写文件、发送请求等有副作用工具。保留成功/失败、资源 ID、路径、版本和关键字段，
  不保留巨大原始响应。
- `retain`：无法安全重取或压成回执的工具。默认不机械清空，只能随 full compact 进入摘要。

MCP 工具没有声明策略时默认 `retain`，不能因为结果大就假设它可安全重放。完整工具结果仍保存在
AG-UI Event 或 Artifact；预算只改变 Provider Observation。

### 7.2 第二层：microcompact

每次 Reason 前执行：

1. 按完整 assistant tool-call + tool-result round 分组。
2. 永远保留最近两个工具 round 原文。
3. 只处理 `rerunnable` 和已经生成稳定回执的 `receipt` 结果。
4. 保留 tool 消息及 `tool_call_id`，只替换正文，不能制造孤儿消息。
5. 生成幂等 `ContextStatePatch`；与 full compact 提案一起提交，或在 run 正常结束前统一持久化。

Backend 在一个执行段里只维护 replacement 的本地工作副本，不在每次替换后推进持久 revision。这样后续
full compact 提案仍以该执行段启动时的 revision 做一次 CAS，不需要双向流式确认；进程中途退出最多导致
下一轮重复 microcompact，不会损坏原始历史。

第一版不依赖 Anthropic cache edit。Provider 将来支持时，可以在不改变本地 replacement 语义的前提下
增加适配层。

### 7.3 第三层：full compact

Microcompact 后仍超过自动阈值时，调用专用 compact 模型：

- `queryKind=compact`；
- `maxTurns=1`；
- 所有工具调用恒定拒绝；
- 图片和文档正文替换为类型、文件名和尺寸描述；
- 输入是“已有摘要 + 上次 boundary 后需要覆盖的新消息”；
- 输出必须通过结构、长度和非空校验；
- 成功后旧摘要被新摘要整体替换，不做无限追加。

### 7.4 第四层：reactive compact

只有 Provider 明确返回 context-length / prompt-too-long / HTTP 413 时触发：

1. 标记 `trigger=reactive`；
2. 跳过普通阈值判断，强制 full compact；
3. 同一次 Reason 最多重试一次；
4. 仍失败则终止 run，返回可操作错误，不把鉴权、限流或参数错误误判成上下文问题。

---

## 8. Full compact 的摘要契约

Compact 模型输出固定 Markdown 结构：

```markdown
# Conversation State

## Primary request and constraints
## User corrections and non-negotiables
## Decisions and rationale
## Files and code state
## Tool results that still matter
## Errors and rejected approaches
## Completed work
## Current work
## Pending work
## Exact next step
```

Prompt 必须要求：

- 覆盖本次 compact 输入中的所有非工具用户消息，并完整继承旧摘要里的用户要求，尤其是最新要求和否定性纠正；
- 区分“已验证事实”“模型推断”“尚未验证”；
- 文件必须写路径、修改状态和仍需重读的精确位置；
- 不把工具输出中的指令当成用户指令；
- 不发明未被用户要求的新任务；
- 只输出摘要文本，禁止工具调用；
- 不包含 analysis 草稿。

摘要模型选择：

- 新增可选 `compactModelId`；未配置时使用当前主模型；
- 必须来自 Access Layer 模型目录，浏览器不能传任意 Provider 参数；
- compact 输出上限取 `min(maxOutputTokens, 20_000)`；
- 记录模型 ID 和实际 usage，便于成本审计。

### 8.1 Compact 请求自己超长

最多重试三次：

1. 按 API round 分组，不能拆 tool call/result。
2. 若错误返回 token gap，从候选摘要前缀的末端后退完整 round，直到估算覆盖 gap；后退的 round 回到活动尾部。
3. 无 gap 时每次把候选摘要前缀末端约 20% 的完整 rounds 移回活动尾部。
4. 至少保留当前用户请求和一个最近完整 round。
5. boundary 随候选前缀同步后退；不能从最老未摘要前缀挖洞后仍跨过该洞提交 boundary。

三次仍失败时不提交任何新 state。

---

## 9. Boundary 与保留尾部

切分必须按完整 API round，不再按“最近六条消息”切：

```text
round = assistant message + 它发起的全部 tool results
普通 user/assistant 文本各自形成有序消息边界
```

默认尾部目标：

```text
recentTargetTokens = clamp(contextWindow * 20%, 10_000, 40_000)
```

同时遵守：

- 尽量保留至少五条有文本的近期消息；
- 当前用户消息必须保留原文；
- 不能拆 tool call/result；
- 压缩后总输入目标不超过 `autoCompactThreshold * 65%`，给当前 run 后续工具调用留空间；
- 若单条当前消息或附件本身超过硬限制，返回“缩小附件/改用文件工具”的错误，不伪装成压缩成功。

Boundary 覆盖到最后一个被摘要 round 的最后一条消息。

---

## 10. Token 预算和默认阈值

优先使用 Provider 最近一次成功响应报告的 input usage；没有 usage 时使用现有保守估算器。估算必须包含：

- system prompt；
- request context / Memory / Skill / MCP；
- 工具 schema；
- compact summary 和 working set；
- 活动消息、tool calls、附件描述；
- 当前 run 新增的工具结果。

模型配置增加约束：

```text
contextWindow >= maxOutputTokens + contextSafetyTokens + 8_000
```

默认计算：

```text
growthReserve = max(
  contextSafetyTokens,
  min(13_000, floor(contextWindow * 10%))
)

autoCompactThreshold = contextWindow - maxOutputTokens - growthReserve
hardRequestThreshold  = contextWindow - maxOutputTokens - 3_000
warningThreshold      = autoCompactThreshold - min(20_000, floor(contextWindow * 15%))
```

- 到 warning 线：只提示，不压缩。
- 到 auto 线：先 microcompact，仍超限才 full compact。
- 到 hard 线：不能继续普通请求，必须 compact 或明确失败。

所有阈值进入 `ContextBudgetPayload`，不能只存在于日志字符串。

---

## 11. Post-compact 工作集重建

摘要保存任务状态，工作集保存继续执行需要的精确信息。Compact 成功后：

### 每个 run 本来就会重建

- 平台 system prompt；
- 当前项目 `CLAUDE.md` / `.claude/rules` / Memory；
- 当前已授权 Skill 摘要；
- 当前 MCP instructions 和工具 schema。

这些内容不进入持久 summary，避免配置变化后继续使用旧版本。

### 需要显式补回

- 最近通过可信本地工具读取的文件：最多 5 个；
- 文件总预算：`min(50_000, contextWindow * 15%)` token；
- 单文件最多 5,000 token，超出时标记必须重新读取；
- 本会话已经调用过的 Skill ID 和用途提示；模型若需要精确指令，必须再次调用 `Skill` 工具加载正文；
- 当前 plan：如果存在结构化 plan 状态则补回，不从自然语言猜测；
- 尚未完成的异步任务句柄：只补回状态和 ID，不复制完整输出。

`workingSet` 只保存可信路径、Skill ID 和结构化 plan 摘要，不保存浏览器提交的任意路径。文件内容在补回时重新读取，
因此模型看到的是当前磁盘状态；越出工作区或已删除的文件只产生“不可用”记录。

Skill 正文仍遵守现有懒加载边界：先用当前请求的 `SkillCatalog` 重新授权，再由一次真实 `Skill` 工具调用
读取可信 canonical ID。Post-compact 不在后台偷偷读取 `SKILL.md`，也不把历史授权带到新请求。

---

## 12. 跨服务提交与自动续跑

### 12.1 内部提案

Backend 需要 full compact 时，不直接把内存 list 当成最终状态，而是返回私有控制帧：

```json
{
  "type": "control.context_compaction_required",
  "proposal": {
    "proposalId": "uuid",
    "expectedGeneration": 2,
    "expectedRevision": 11,
    "boundary": {},
    "summary": {},
    "toolReplacements": [],
    "workingSet": {}
  },
  "continuationCheckpoint": {
    "contextGeneration": 3,
    "iteration": 7,
    "modelMessages": [],
    "loadedMemoryPaths": [],
    "approvedTargets": [],
    "requestHash": "sha256:...",
    "resumeContext": {}
  }
}
```

该帧属于 Access Layer ↔ Backend 私有协议，不进入浏览器原始 AG-UI 流。
Backend 发出该帧后必须立即结束当前内部执行段，不能再调用主模型或工具。这样 Access Layer 可以先把
boundary 和 checkpoint 持久化，再允许任何依赖新摘要的模型输出或工具副作用发生。

Backend 只提议 `coveredThroughMessageId`；`coveredPrefixDigest` 必须由持有完整原始历史的 Access Layer
计算，不能接受 Backend 或浏览器提供的哈希值。

### 12.2 Access Layer 提交规则

在同会话 run 锁内：

1. `expectedGeneration` 和 `expectedRevision` 必须分别等于已存 generation/revision；
2. boundary 锚点必须存在，前缀 SHA-256 必须匹配；
3. `proposalId` 必须幂等，重复提交返回已有结果；
4. summary 和 continuation checkpoint 一起原子落盘；
5. 成功后 generation 和 revision 各加一；
6. 使用同一个 public `threadId/runId` 自动调用 Backend continuation；
7. continuation 成功或失败后清理一次性 checkpoint。

`ContextStateStore` 必须提供存储级 `compare_and_swap(expectedRevision, nextState)`，不能只依赖进程内
`asyncio.Lock`。FileStorage 使用跨进程文件锁包住“读取 revision → 临时文件写入 → `os.replace`”；
数据库适配器使用事务或条件更新。该 CAS 不能替代同会话 run 互斥：在分布式 session lease 完成前，
使用 FileStorage 的 Access Layer 必须拒绝 `SERVER_WORKERS > 1`，否则不同 worker 仍可能并行执行同一会话。

前端只看到一个 `RUN_STARTED` 和最终一个 `RUN_FINISHED` / `RUN_ERROR`。中间 Backend 分段的运行边界不
向外透传；标准 `REASONING_*`、`TEXT_MESSAGE_*`、`TOOL_CALL_*` 事件保持原始到达顺序。

压缩成功后可以发一个公开 `CUSTOM context_compacted`：只包含 generation、trigger、前后 token、耗时和
模型 ID，不包含 summary 正文或 checkpoint。

### 12.3 防止续跑死循环

- 同一个 public run 最多进行 2 次 full compact continuation；
- `queryKind=compact` 自身禁止触发 compact；
- continuation 恢复后的第一次预算若仍超过 hard line，直接失败；
- `proposalId + expectedRevision` 同时作为幂等键。
- Access Layer 重启后若发现 `pendingContinuation`，必须先在会话锁下自动恢复该 run，再接受新用户输入；
  continuation 从 compact 边界开始，压缩前不会有尚未记录的后续工具副作用。

不伴随 full compact 的 microcompact patch 在 `RUN_FINISHED` 之前提交：它带 `proposalId/expectedRevision`，
只让 revision 加一，不改变 generation，也不触发 continuation。CAS 冲突时直接丢弃该派生 patch，下一轮
可以从原始工具结果重新计算，不能覆盖更新后的 compact state。

---

## 13. HITL、取消、停止、编辑和分支

### Durable HITL

- HITL checkpoint 增加 `contextGeneration` 和 `boundaryId`。
- 创建 Interrupt 前，待提交的 compact state 必须先持久化。
- Resume 时 generation/boundary 不匹配则拒绝恢复，不能把旧 `modelMessages` 套到新摘要上。
- 存在开放 Interrupt 时禁止手动 `/compact`，返回 409；先处理或取消 Interrupt。

### 取消与停止

- `cancel_run` 删除本轮 user 消息后重新校验 prefix digest；若 boundary 覆盖了被取消消息，废弃 state。
- `stop_run` 保留已接收消息，因此已提交 compact state 继续有效。
- 未提交的 proposal 和一次性 continuation checkpoint 随 run 取消一并删除。

### 编辑与删除历史

- 改动发生在 boundary 之前：摘要立即失效，从完整历史重建。
- 改动发生在 boundary 之后：摘要保留，删除指向被修改 tool message 的 replacement。
- 删除整个会话：同时删除 `context/k_agent.json` 和 continuation checkpoint。
- 新的 full compact 前移 boundary 后，删除所有已落入 boundary 之前的 tool replacements，避免状态无限增长。

### 会话分支

- 分支完整继承原始历史。
- 只有分支前缀仍覆盖原 boundary 且 digest 相同时才复制 compact state。
- 分支获得自己的 generation 空间，之后互不影响。

### 切换模型、Skill 或 MCP

- Summary 是会话事实，可跨主模型复用。
- 新模型窗口更小时立即重新预算，必要时继续 compact。
- Prompt、Skill 和 MCP 每轮按当前授权重建；`workingSet.invokedSkillIds` 必须重新授权，失效 Skill 不加载正文。
- 不同 Agent Runner 的 context state 分文件保存，不能让 K Agent summary 驱动 Codex/Claude CLI resume。

---

## 14. 手动 compact 产品协议

新增：

```http
POST /api/sessions/{session_id}/compact
Content-Type: application/json

{
  "instructions": "可选，说明希望摘要重点保留什么"
}
```

只读状态接口：

```http
GET /api/sessions/{session_id}/context
```

只返回 generation、boundary ID、上次压缩时间、trigger、前后 token 和熔断状态，不返回摘要正文、
prefix digest、replacement 内容或 checkpoint。

行为：

- 获取同会话 run 锁；正在运行或有开放 Interrupt 时返回 409；
- instructions 限 4,000 字符，只影响本次摘要，不能修改平台契约；
- 调用 Backend compact-only 内部入口；
- 成功后原子更新 compact state，不新增一条伪用户消息；
- 返回 generation、boundary、前后 token 和节省量，不返回 summary 正文；
- 前端 `/compact [instructions]` 只是这个 API 的快捷入口。

配置增加：

- `autoCompactEnabled`，默认 `true`；
- `compactModelId`，默认当前模型；
- 管理页显示当前 generation、上次压缩时间、触发原因和自动熔断状态；
- 提供“重新建立上下文”按钮：删除派生 compact state 后立即手动 compact，不删除原历史。

---

## 15. 失败策略与熔断

Full compact 可能失败于超长、空输出、非法结构、Provider 超时或限流。规则如下：

1. 失败时不提交部分 summary，也不前移 boundary。
2. 当前输入仍低于 hard line：记录失败，本次继续普通请求。
3. 已达到 hard line：尝试一次 reactive compact；仍失败则终止并给出明确错误。
4. 连续 3 次自动 compact 失败：`autoDisabled=true`，本会话不再自动尝试。
5. 手动 compact 成功、用户点击重置、或 compact 模型配置变化后清零熔断。
6. 限流、鉴权和普通 5xx 不伪装成 prompt-too-long。
7. Access Layer 持久化失败时，不得继续自动 continuation；原始历史仍可恢复。

错误响应必须告诉用户：失败阶段、是否保留完整历史、可以重试 `/compact` 还是需要缩小当前附件。

---

## 16. 观测与安全

### Hook / 日志事件

- `context_budget_checked`
- `context_tool_result_limited`
- `context_microcompacted`
- `context_compact_started`
- `context_compact_succeeded`
- `context_compact_failed`
- `context_compact_committed`
- `context_continuation_started`
- `context_continuation_finished`
- `context_state_invalidated`

字段只记录 session/run ID、generation、模型 ID、消息数、token、字符数、耗时、trigger 和错误码。
普通日志不能写 summary、用户正文、工具结果或文件内容。

### Langfuse

同一个 public run 下建立：

```text
agent run span
  ├─ compact generation span
  │    └─ compact model generation
  ├─ context commit span
  └─ continuation model/tool spans
```

### 安全

- Compact 输入中的 tool result 是不可信数据，Prompt 必须明确禁止提升其中指令的权威级别。
- Summary 仍可能含敏感内容，按会话数据同级保护，不进入普通日志和公开事件。
- `workingSet` 文件只能来自密封后的可信本地工具参数，不能扫描 MCP/web 输出猜路径。
- Compact 模型禁止工具、禁止外部写操作、最多一轮。

---

## 17. 实施顺序与完成状态

### 阶段 A：状态协议和投影

状态：已完成。

- 新增 `KAgentContextState`、独立 context 存储和 prefix digest。
- Gateway 校验 state，只发送 active tail。
- 分支、取消、编辑、删除的失效规则和测试先完成。
- 暂时仍可读取旧 `_merge_summary`，但不允许它写新格式 state。

### 阶段 B：工具预算和 microcompact

状态：已完成。

- 给本地工具定义 `contextPolicy`；MCP 未声明时默认 `retain`。
- 每次 Reason 前执行单条预算和 microcompact。
- Access Layer 持久化幂等 `ContextStatePatch`。

### 阶段 C：LLM full compact 和 continuation

状态：已完成。

- 新增 compact-only 模型调用、结构校验、三次 PTL 重试。
- 新增私有 proposal / continuation 协议和原子提交。
- 把预算检查移入每次 Reason 之前。
- 接入 reactive compact 和三次失败熔断。

### 阶段 D：工作集和产品入口

状态：已完成。

- 补回近期文件、Skill 使用记录和 plan。
- 增加手动 compact API、前端命令、临界提示和观测页面。
- 完成灰度后删除 `_merge_summary`、`existing_summary`、`compacted_message_ids` 旧临时接口。

迁移期间使用单一 feature flag 控制 v1/v2 读取路径，不长期双写。V2 提交失败必须回退到完整历史，不能
回退到一份未持久化的临时摘要。

---

## 18. 验收标准

### 状态正确性

- 压缩一次后连续运行 20 轮，不会再次读取或重复摘要 boundary 之前的原始消息。
- 重启 Access Layer 和 Backend 后，Provider 活动上下文与重启前一致。
- 编辑 boundary 前消息会使 state 失效；只追加消息不会。
- branch、cancel、stop、delete 均符合第 13 节规则。

### 运行正确性

- 每次 Reason 前都会重新计算预算。
- 任意压缩都不拆 assistant tool call/result。
- full compact 后同一用户回合自动续跑，前端只有一个 public run 生命周期。
- reactive compact 只处理上下文超长错误，且最多一次。
- compact 模型不会调用工具。

### 信息质量

- 测试集必须覆盖用户纠正、未完成任务、关键路径、错误原因和精确下一步。
- 摘要后重新执行代码任务，模型能定位需要重读的文件，不声称仍持有已清理的原文。
- 切换 Skill/MCP 后不会继续注入已失效正文。

### 持久化与恢复

- compact state 和 continuation checkpoint 原子提交。
- 重复 proposal 不会生成两个 generation。
- HITL resume 必须校验 context generation。
- Access Layer 写盘失败时没有后续工具副作用或公开模型输出。

### 回归

- 完整原始 AG-UI Event 顺序不变。
- UI 完整历史不因 compact 减少。
- 现有多模态、Skill、MCP、审批和定时任务测试通过。
- 日志、公开 CUSTOM event 和 Session API 不泄露 summary/checkpoint 正文。

---

## 19. 目标代码地图

| 路径 | 最终职责 |
|---|---|
| `access_layer/sessions/context_store.py` | compact state、digest、CAS、失效与原子提交 |
| `access_layer/sessions/store.py` | 原始历史投影；在消息变更时通知 context store 校验 |
| `access_layer/gateway.py` | active view、私有 proposal、自动 continuation、公开事件过滤 |
| `access_layer/schemas.py` | 手动 compact API 和只读状态响应 |
| `backend/context/models.py` | ContextState/Proposal/Budget/Checkpoint 内部模型 |
| `backend/context/budget.py` | usage 优先的预算与阈值计算 |
| `backend/context/tool_results.py` | 单条预算、策略和 microcompact |
| `backend/context/compact.py` | full compact、结构校验、PTL 重试、boundary 选择 |
| `backend/context/projection.py` | summary、working set、活动尾部的 Provider 投影 |
| `backend/agent/react_agent.py` | 每次 Reason 前调用 ContextController；执行 continuation |
| `backend/prompts/compose.py` | 仍只编译 system/request context，不拥有会话压缩状态 |

旧 `backend/context/manager.py` 在迁移后只保留通用消息配对/估算工具，或拆分后删除；它不再负责生成
逐条 bullet 摘要。

---

## 20. Claude Code 对照代码地图

| 本地源码路径 | 用于验证的设计点 |
|---|---|
| `src/query.ts` | 每次主模型调用前依次执行预算、snip、microcompact、autocompact |
| `src/services/compact/autoCompact.ts` | 阈值、13k buffer、三次失败熔断、递归保护 |
| `src/services/compact/compact.ts` | LLM 摘要、PTL 重试、post-compact messages、boundary |
| `src/services/compact/prompt.ts` | 摘要结构、禁止工具、续写说明 |
| `src/services/compact/grouping.ts` | 按 API round 切分，避免拆 tool call/result |
| `src/services/compact/microCompact.ts` | 旧工具结果替换和 cache-aware 实验路径 |
| `src/services/compact/sessionMemoryCompact.ts` | 实验性 session memory 替代摘要；第一版不采用 |
| `src/services/compact/postCompactCleanup.ts` | 压缩后的缓存与状态清理 |
| `src/commands/compact/compact.ts` | 手动 `/compact` |

K Agent 与 Claude Code 在 full compact 主干上的关键差异是状态承载方式：Claude Code 可以直接替换
单进程活动消息链；K Agent 必须由 Access Layer 持久化 boundary，再通过自动 continuation 跨 Backend
run 继续。两者对模型呈现的主干结果一致，都是“结构化摘要 + 最近原文 + 当前工作集”。

---

## 21. 实施记录（2026-09-04）

本方案已按以下真实调用链落地：

| 能力 | 实现位置 | 结果 |
|---|---|---|
| append-only 完整历史与确定性投影 | `access_layer/sessions/history.py`、`durable_events.py`、`migrate_history.py` | `history.jsonl` 为事实源，UI 与 Provider 投影分离 |
| 派生状态、digest、失效与存储级 CAS | `access_layer/sessions/context_store.py`、`access_layer/storage/file.py` | generation/revision、幂等 proposal、文件锁原子替换已接通 |
| 每次 Reason 预算与 usage 优先 | `backend/context/budget.py`、`backend/agent/react_agent.py` | Provider 支持时读取流式 input usage，否则保守估算 |
| 单条工具预算与 microcompact | `backend/context/tool_results.py`、`backend/tools/cc_like.py`、`cc_extra.py` | 最近两轮完整工具 round 受保护；MCP/未知工具默认 retain |
| LLM full compact | `backend/context/compact.py` | 无工具单轮调用、固定结构校验、整 round PTL 重试、精确 boundary |
| durability barrier 与同 run continuation | `access_layer/gateway.py` | 等公共事件落盘后 CAS；中间 Backend `RUN_STARTED` 不对外重复 |
| 重启恢复 | `AgentAccessLayer.recover_pending_context_continuations` | 服务 ready 前恢复 checkpoint；失败时保留 checkpoint 并阻止新输入越过 |
| 工作集重建 | `backend/context/projection.py`、`backend/runners/k_agent.py` | 可信文件、已授权 Skill ID 与 plan 受预算地恢复 |
| 手动入口与配置 | `access_layer/main.py`、`frontend/src/App.tsx`、`ConfigCenter.tsx` | `/compact`、状态提示、重建按钮、`compactModelId` 与熔断清理 |
| 安全观测 | Backend/Access Layer 的 `context_*` 结构化事件 | 只记录 ID、计数、token、耗时和错误码，不记录摘要/正文/checkpoint |

验证结果：

- `.venv/bin/python -m unittest discover -s backend/tests -p 'test_*.py'`：281 项通过；
- `frontend`: `npm run check`、`npm run build`、`npm run test:history` 通过；
- `cli`: `npm run check`、`npm run build`、`npm test` 通过，87 项测试通过；
- `git diff --check` 通过。

新增回归覆盖 20 个追加回合不越过 boundary、重启投影/续跑、CAS 幂等与冲突、前缀编辑失效、
HITL generation 拒绝、三次自动失败熔断、最近两轮工具保护、compact 禁用工具、usage 记录和整 round
PTL 后退。`history.jsonl` 第一版仍按完整文件加载；这是已记录的容量风险，不影响本方案的一致性边界。
