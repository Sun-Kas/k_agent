# 上下文压缩技术方案：Claude Code vs K Agent

> 状态：对照已落地的 K Agent 实现（2026-08-29 工作区）
>
> Claude Code 对照源码：桌面 `~/Desktop/claude-code-main/`
> （`src/services/compact/`、`src/query.ts`、`src/commands/compact/`、`src/utils/messages.ts`）
>
> 适用范围：K Agent 主 ReAct Runner（`agentKind=k_agent`）。Claude Code / Codex 接入 Runner
> 不走 Agent Backend 这套压缩。
>
> 本文只谈「活动模型上下文」怎么变短。`compact_personal_memory` / `MEMORY.md` 条目裁剪是记忆工具，
> 见 `docs/tools.md`，不在此范围。

---

## 0. 一句话

Claude Code 把压缩做成 **会话内一等事件**：用模型写结构化摘要，**替换 REPL 正在用的消息数组**，
并在同一 `query()` 循环里叠 microcompact / snip /（实验）session-memory compact。

K Agent 把压缩做成 **无状态规划**：每次 HTTP run 用 Access Layer 传来的完整历史做启发式切分 +
抽取拼接，**不改会话存储**，也不再调一次模型去写摘要。对话压缩只发生在 `create_runtime`；
工具结果清理发生在每一次 Reason 之前。

两边都尽量「不删用户可见的完整记录」，但改的对象不同：

- CC 改的是 **进程内活动 transcript**（UI 滚动区可以另留一份）。
- K Agent 改的只是 **本轮发给 Provider 的临时 messages**。

---

## 1. 问题是什么

模型上下文窗口是硬上限。一次请求里同时有：

| 种类 | 是否随对话增长 | 压缩时能不能动 |
|---|---|---|
| 系统提示词 / 工具 schema | 基本稳定 | 一般不动（动了会打 prompt cache） |
| 项目指令 / Memory / MCP 说明 | 按 run 变 | K Agent 算固定开销，不压这段 |
| 聊天 + 工具调用/结果 | 线性甚至超线性增长 | 这是压缩对象 |
| 本轮新工具 Observation | 单 run 内暴涨 | 第二层：清旧 tool 正文 |

不压缩的后果是 Provider `prompt_too_long` / 413。压缩过度的后果是模型忘了文件名、用户纠正、未完成任务。
CC 用「再请一次模型写摘要 + 把关键文件贴回来」换质量；K Agent 用「保尾 + 截断 bullet」换简单和无状态。

---

## 2. Claude Code 实现

### 2.1 在主循环里的位置

主循环：`src/query.ts`。每轮真正打主模型 API **之前**，压缩相关步骤按固定顺序执行，**不是互斥**：

```text
messagesForQuery = 当前活动消息

applyToolResultBudget(...)
  单条 tool_result 超过该工具 maxResultSizeChars 时替换正文
  可持久化 replacement 记录，供 Agent /resume、会话 /resume 回放

HISTORY_SNIP 打开时：
  snipCompactIfNeeded(messagesForQuery)
  按 API round 成组剪掉更早轮次（UI 仍可能留 scrollback）
  算出 snipTokensFreed，后面 autocompact 阈值要减掉它
  （assistant.usage 仍是 snip 前的数，光看 usage 看不见省了多少）

microcompact(messagesForQuery)
  清白名单工具的旧 tool_result
  时间触发（cache 已冷）或 cache-edit（cache 仍热）

CONTEXT_COLLAPSE 打开时：
  applyCollapsesIfNeeded(...)
  折叠视图是对完整历史的只读投影；成功压到阈值以下则 autocompact 变成 no-op
  目的：保住颗粒度，不要被「整段换成一份摘要」抢先

autoCompactIfNeeded(...)
  超阈值 → 先试 session-memory compact，否则 LLM 全量 compactConversation
  querySource 为 session_memory / compact 时直接跳过（fork 死锁保护）
  CONTEXT_COLLAPSE 作为主策略时，主线程 autocompact 也跳过，只留 413 的 reactive 兜底

若得到 compactionResult：
  yield 每条 postCompactMessages   # UI 看到 boundary + 摘要
  messagesForQuery = postCompactMessages
  同一轮 query() 接着用新数组打主模型
```

要点：CC 的全量 compact 发生在 **同一条生成器、同一次用户 turn 里**，compact 完立刻继续当前任务。
不是「等用户再发一条消息才换上下文」。

手动 `/compact`：`src/commands/compact/compact.ts`。先 `getMessagesAfterCompactBoundary`，避免把
已经 compact/snip 掉的内容再送进摘要模型。无自定义指令时同样先试 session-memory compact。
成功后 REPL 用 `buildPostCompactMessages` **整体替换** `messages`。

### 2.2 阈值与开关（`autoCompact.ts`）

```text
reservedForSummary = min(模型 maxOutputTokens, 20_000)
                     # p99.99 compact 摘要输出约 17,387 token

contextWindow      = 模型窗口
                     可被 CLAUDE_CODE_AUTO_COMPACT_WINDOW 再收窄（测试/限流）

effectiveWindow    = contextWindow - reservedForSummary

autoCompactThreshold = effectiveWindow - 13_000   # AUTOCOMPACT_BUFFER_TOKENS

警告线 = threshold - 20_000
错误线 = threshold - 20_000   # 与警告共用 ERROR_THRESHOLD_BUFFER
堵死线 = effectiveWindow - 3_000  # MANUAL_COMPACT_BUFFER，逼用户手动 /compact
```

`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` 可把阈值改成 effectiveWindow 的百分比（测试用）。

启用条件（`isAutoCompactEnabled`）：

- `DISABLE_COMPACT` 真 → 全关（含手动路径里部分调用）
- `DISABLE_AUTO_COMPACT` 真 → 只关自动，手动 `/compact` 仍可用
- 用户 settings `autoCompactEnabled`

熔断：连续失败 3 次（`MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES`）本 session 不再自动试。
背景：生产上出现过单 session 连续失败 50+ 次、空烧 compact API。

递归保护：`querySource === 'session_memory' | 'compact'` 不 autocompact。
compact 自己是 forked agent；若它再 compact 会死锁。Collapse 的 ctx-agent
（`marble_origami`）同样禁止，以免 `resetContextCollapse` 清掉主线程 commit log。

Token 计数：`tokenCountWithEstimation(messages)`，再减去 `snipTokensFreed`。

### 2.3 全量 LLM compact（`compactConversation`）

这不是「把旧消息 toString 截断」，而是一次 **禁止工具、只要文本** 的专用请求。

#### 输入处理

- 空消息 → `Not enough messages to compact.`
- `stripImagesFromMessages`：user / tool_result 里的 image、document 换成 `[image]` / `[document]`。
  Compact 请求本身很容易先爆窗（尤其是频繁贴图的会话）。
- 可选 `stripReinjectedAttachments`：拿掉会误导摘要模型的重复 attachment。

#### Hook

```text
PreCompact(trigger: auto|manual, customInstructions)
  → 可追加 compact 指令（用户 /compact 参数在前，Hook 在后）
  → 可返回给 UI 看的 userDisplayMessage
```

#### 摘要请求长什么样

`prompt.ts`：

1. **Preamble（必须在最前）**：`CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.`
   Sonnet 4.6+ 在 fork 仍带着主会话完整 tool set（为了 cache key 一致）时，偶尔会偷调工具；
   `maxTurns: 1` 下一次被拒的 tool_call 等于这次 compact 颗粒无收，只能走 streaming 兜底。
2. **任务**：对「到目前为止的对话」写详细摘要，先 `<analysis>` 再 `<summary>`。
   Analysis 是草稿，`formatCompactSummary` 会剥掉，不进后续上下文。
3. **Summary 必须覆盖的九段**：
   - Primary Request and Intent
   - Key Technical Concepts
   - Files and Code Sections（含为何重要、关键代码片段）
   - Errors and fixes（含用户「别再那样做」的反馈）
   - Problem Solving
   - All user messages（所有非 tool_result 的用户话，防意图漂移）
   - Pending Tasks
   - Current Work（压缩前一刻在干什么）
   - Optional Next Step（必须对齐用户最近明确请求；完成了就不要发明新任务）
4. **Trailer**：再提醒一次禁止工具。
5. `/compact` 自定义指令、CLAUDE.md 里的 Compact Instructions 可追加。

`canUseTool` 对 compact fork 恒为 deny：`Tool use is not allowed during compaction`。

#### 怎么打 compact API（`streamCompactSummary`）

默认 `tengu_compact_cache_prefix=true`：走 `runForkedAgent`，
`cacheSafeParams` 里带主会话的 system / tools / 消息前缀，**故意不改 maxOutputTokens**，
以免 thinking budget 变化导致 cache key 失效。实验数据：关掉共享约 98% cache miss。

失败则退回独立 streaming。Compact 期间每 30s 发 session activity / `compacting` status，
避免远程 WebSocket 因 5–10s 无事件被掐。

`querySource: 'compact'`，`maxTurns: 1`，`skipCacheWrite: true`。

#### Compact 请求自己 prompt_too_long（CC-1180）

最多 **3** 次（`MAX_PTL_RETRIES`）：

1. `groupMessagesByApiRound`：新 assistant `message.id` 出现时切一组。
   同一轮 streaming 的多个 content block 共用 id，和夹在中间的 tool_result 算一组。
2. 若错误里能解析 token gap：从最老的组累加估算，直到覆盖 gap。
3. 否则砍掉约 **20%** 的组。
4. 至少留一组可摘要。
5. 砍完若第一条变成 assistant：前面垫一条 meta user
   `[earlier conversation truncated for compaction retry]`，否则 API 拒「必须以 user 开头」。
6. 截断集合同时写进 `forkContextMessages`，因为 fork 读的是 cacheSafeParams 不是外层 messages。

这是有损逃生舱。注释写明 reactive-compact 才有「从尾巴剥」的正经重试；全量/手动路径用的是这个砍头兜底。

#### 成功之后：清空再补回

必须清掉的（否则模型以为文件还在缓存里）：

- `context.readFileState`
- `context.loadedNestedMemoryPaths`

故意 **不清** `sentSkillNames` / invoked skill 正文：每轮 compact 把整个 skill listing（数 K token）
再灌进去几乎纯 cache_creation；Skill 工具还在 schema 里，下面 attachment 会带已用过的 skill 内容。

并行补回：

| 附件 | 上限 |
|---|---|
| 最近读过的文件（按 timestamp，跳过已在 preserved 消息里出现的 Read） | 最多 5 个文件；总预算 50_000 token；单文件 5_000 |
| 仍在跑的 async agent | 按需 |
| 当前 plan 文件 | 有则贴 |
| 仍处于 plan mode 的指令 | 有则贴 |
| 本 session 调用过的 Skill 正文 | 总预算 25_000；单 skill 5_000（指令多半在文件头，截断优于丢弃） |
| deferred tools / agent listing / MCP instructions 的 **全量 delta** | 相对空历史 diff → 等于重新宣布当前工具集 |

然后 `processSessionStartHooks('compact')`，因为对模型来说这像一次新 session。

#### 活动消息被换成什么

```text
buildPostCompactMessages:
  1. SystemCompactBoundaryMessage     # auto | manual，带 preCompactTokenCount、
                                      # 上一消息 uuid、已发现的 deferred tool 名
  2. summaryMessages                  # 一条 isCompactSummary 的 user 消息
  3. messagesToKeep                   # 全量 compact 通常为空
  4. attachments                      # 上面那些文件/Skill/MCP
  5. hookResults                      # SessionStart(compact)
```

`query.ts` 把这个数组 **yield 给 UI 后直接赋给 `messagesForQuery`**，本 turn 继续。

包装给模型看的摘要（`getCompactUserSummaryMessage`）大意是：

- 会话因上下文耗尽而续上，下面是更早部分的摘要。
- 需要精确代码/报错/你写过的内容：去 `transcriptPath` 读全文。
- Auto compact 额外：`Continue ... without asking ... Resume directly`——不要向用户复述摘要。

磁盘上：boundary 的 `preservedSegment` 在 **有 messagesToKeep** 时记录 head/anchor/tail uuid，
resume loader 用来把「磁盘上仍按原 parentUuid 存的保留段」接回新链。全量 compact 没有保留段。
`reAppendSessionMetadata` 把自定义标题重新追加到 16KB 尾巴里，否则 `--resume` 列表会丢用户起的名。

#### Partial compact

`partialCompactConversation(allMessages, pivotIndex, direction)`：

| direction | 摘要哪一段 | 保留哪一段 | Prompt cache |
|---|---|---|---|
| `from`（默认） | `pivot` 之后 | `pivot` 之前 | 前缀仍在，cache 可命中 |
| `up_to` | `pivot` 之前 | `pivot` 之后 | 摘要插在保留段前面，cache 失效 |

`up_to` 必须从保留段里滤掉旧 boundary / 旧 compact summary，否则
`findLastCompactBoundaryIndex` 从后往前扫会扫到旧 boundary，把新摘要丢掉。

### 2.4 Session memory compact（实验，优先）

`sessionMemoryCompact.ts`。后台 session-memory 抽取若已覆盖足够历史，就 **不必再打 compact 模型**：

- 默认保留：至少 5 条带 text block 的消息；token 夹在 10_000–40_000。
- 中间剪掉，用 session memory 内容当摘要。
- 会 `setLastSummarizedMessageId(undefined)`，因为旧 uuid 不在新数组里。
- `/compact` 带了自定义指令则 **不能**走这条（SM compact 不支持自定义指令）。

`autoCompactIfNeeded` 里 SM 成功也会 `runPostCompactCleanup` + `notifyCompaction`（否则
20% 的 prompt_cache_break 事件是假阳性）。

### 2.5 Microcompact：只清工具结果

`microCompact.ts`。不写会话摘要，只替换 tool_result 正文。

白名单：Read、Bash 系、Grep、Glob、WebSearch、WebFetch、Edit、Write。

两条路径：

1. **时间型**（先跑，命中则短路）：距上一条 assistant 超过 gap，服务端 cache 已过期，
   整段前缀反正要重写 → 现在就清旧结果，缩小重写体积。占位：`[Old tool result content cleared]`。
   `keepRecent` 至少 1。
2. **Cache-edit 型**（`CACHED_MICROCOMPACT`，仅主线程 `repl_main_thread*`）：
   用 API cache 编辑按 `tool_use_id` 删内容，不碰正文也能让 cached MC 工作。
   Forked agent 禁止注册进全局 `cachedMCState`，否则主线程会去删自己对话里不存在的 id。

和 snip、全量 compact 同一轮可以都发生。

估算：文本用 `roughTokenCountEstimation`，图片/文档按 2000 token，最后乘 4/3 偏保守。

### 2.6 压缩后清理（`postCompactCleanup.ts`）

`runPostCompactCleanup(querySource)`：

- 清 system prompt section 缓存、`getUserContext`、CLAUDE.md `getMemoryFiles` 缓存、
  session messages cache、microcompact 状态、Bash speculative check 等。
- **子 agent**（`querySource` 以 `agent:` 开头）**不得**重置主线程 module 级状态
  （collapse store、memory 一次性 hook 标记），同进程会污染主会话。

### 2.7 用户能感知的其它入口

- `/compact [自定义指令]`
- `/config` 开关 autocompact
- 接近阈值时 `contextSuggestions.ts` 提示「autocompact 马上触发，现在 /compact 可控制保留什么」
- compact 失败（仅手动）弹 error；auto 失败静默，下一 turn 再试
- Feature：`REACTIVE_COMPACT`（等 API prompt-too-long 再压）、`CONTEXT_COLLAPSE`、`HISTORY_SNIP`

---

## 3. K Agent 当前实现

### 3.1 双进程数据流

```text
Access Layer
  $K_AGENT_HOME/state/sessions 里完整 messages + AG-UI events
  发送：全文历史 + model/MCP/Skill/permissionMode
        │
        ▼
Agent Backend  create_runtime
  compose_prompt → PromptBundle          # 系统/context，与压缩正交
  build_context_plan(messages)           # 只在这里做对话压缩，一次
  compose_api_messages(...)              # system + reminder(+摘要) + 尾部原文
        │
        ▼
  run_stream_react 每轮 Reason 前
  prune_old_tool_outputs(messages)       # 只清本 run 里的旧 tool 正文
        │
        ▼
  标准 AG-UI Event → Access Layer 原样持久化（含完整工具结果）
```

压缩产物（`ContextPlan.summary`、`compacted_message_ids`）**不写回** Access Layer。
下一轮用户消息再把完整历史传来，Backend 重算。

Durable HITL 例外见 §3.5：恢复时用 checkpoint 里的 `modelMessages`，不再用这次
`create_runtime` 刚编的 compact 结果驱动循环。

### 3.2 对话压缩只发生一次：`create_runtime`

```python
context_plan = build_context_plan(
    [m for m in request.messages if m.carries_context()],
    prompt=request.prompt,
    model_config=request.model_config,
    tool_definition_tokens=estimate_text_tokens(json.dumps(tool_specs, ...)),
)
messages = compose_messages(
    context_plan.messages,
    prompt=request.prompt,
    context_summary=context_plan.summary,
    attachments=request.attachments,
)
```

`carries_context()`：有正文，或有 `tool_calls`，或有附件。
空 content 但带 tool_calls 的 assistant **必须留下**，否则后续 tool 结果变成孤儿，Provider 拒请求。

**不传** `existing_summary` / `compacted_message_ids`。函数签名支持增量，测试里有覆盖，
生产 Runner 每次都从零规划。

ReAct 循环 **不会**在第 20 次工具调用后再跑一遍 `build_context_plan`。
本 run 内窗口膨胀只靠 `prune_old_tool_outputs`。这和 CC「每轮 query 入口都检查 autocompact」不同。

### 3.3 `build_context_plan` 逐步做什么

文件：`backend/context/manager.py`。

#### （1）已压缩集合与配对修复

```text
compacted = set(compacted_message_ids or [])   # 生产为空集
active = pair_tool_messages([m for m in messages if m.id not in compacted])
```

`pair_tool_messages`：

- 先收集所有 `role=tool` 且带 `tool_call_id` 的 id。
- 再扫一遍：assistant 的 `tool_calls` 只保留「已经有结果」的；全被剥光且 content 空则丢整条。
- tool 消息若 call_id 还没被前面的 assistant 宣告过，丢弃。

压缩、HITL 中断、前端漏事件都可能拆对；在计量和发送前修，而不是让整次请求在 Provider 失败。

#### （2）预算

```text
contextWindow, maxOutput, safety  ← 模型配置，缺省 1_000_000 / 50_000 / 200_000

inputBudget = max(8_000, contextWindow - maxOutput - safety)

fixed = system_prompt + context_message + existing_summary + tool_definition_tokens
        # PromptBundle 路径下，memory/CLAUDE.md/MCP 都在 context_message 里，
        # breakdown 仍把这块叫做 "memory"

available_for_messages = max(1_000, inputBudget - fixed)
```

当前 `models.config.json` 示例：窗口 128_000，输出 8_192，safety **4_096**
（不是代码里缺配置时的 200_000）。200_000 兜底是为防启发式估少直接 413。

估算（`estimate_text_tokens`）：

```text
ceil( ASCII字符/4 + 非ASCII/1.6 )
```

消息再加 role 文本 + `tool_calls` JSON + 每条固定 4（封装开销）。
**不是** Provider tokenizer，只用来提前触发。

#### （3）是否压缩

```text
should_compact = force_compact or (message_tokens > available_for_messages)
若 should_compact 且 len(active) > 2：
    compact_messages(...)
少于 3 条：无可压空间（至少要留一轮问答）
```

`force_compact` 仅单元测试。没有 `/compact`、没有 PreCompact Hook、没有用户自定义摘要指令。

#### （4）`compact_messages` 切点

设 `n = len(messages)`，`MIN_RECENT_MESSAGES = 6`。

```text
split = max(1, n - 6)          # [:split] 进摘要，[split:] 留原文
若 n <= 6：
    split = max(1, n - 2)      # force 短会话：只留最后 2 条

非 force 且总 token 已在预算内：直接不压

非 force 时回扩保留窗口：
    while split > 0 and tokens(messages[split:]) < budget * 0.70:
        若 tokens(messages[split-1:]) > budget * 0.78: break
        split -= 1

split = _tool_safe_split(messages, split)
    while split < n and messages[split].role == "tool":
        split += 1
    # 禁止「assistant 的 tool_call 进摘要、对应 tool 结果还留在原文」
```

70% / 78% 之间的缝给本轮模型输出和新工具结果。不留缝会「压完下一轮 Reason 立刻又超」，
摘要被反复追加、质量持续劣化。

`force=True` 不做回扩，测试里 5 条短消息会压成只留最后 2 条。

#### （5）抽取摘要 `_merge_summary`

不是第二次 LLM。对 `older` 每条：

- 空白折叠成单行。
- 有 tool_calls 则追加 `(called name1, name2)`。
- 单条超过 `MAX_SUMMARY_MESSAGE_CHARS`（10_000）截断加省略号。
- 标签：`User request` / `Assistant result` / `Tool result (tool_name)` / `System note`。

拼在 `# Compacted conversation` 下。若已有 `existing_summary` 则前置（生产路径没有）。

整份超过 `MAX_SUMMARY_CHARS`（100_000）时 **留尾**，头上加
`# Earlier compacted context omitted`。越靠近当前轮越有用。

对比文档旧稿写的 16KB / 1_500 字符已经过时，以代码常量为准。

#### （6）`ContextPlan` 观测字段

发给 Hook / 日志：`budget`、`breakdown`（system / memory / skillsAndTools / summary /
messages / estimatedInput / remaining）、`autoCompacted`、`compactedMessageIds`、`summary`。
`auto_compacted` 的含义是 **本轮函数调用是否新压了消息**，不是「历史上曾经压过」。

### 3.4 发给 Provider 的最终形状

`compose_api_messages`：

```text
[
  { role: system, content: prompt.system_prompt },
  { role: user,   content: <system-reminder>
                            context_message 各段
                            （若有摘要）
                            # conversation_summary
                            This is continuity context from compacted earlier turns,
                            not a new user request.
                            {summary}
                            </system-reminder> },
  ... 尾部 ChatMessage 转成 OpenAI 协议
      tool → role=tool + tool_call_id
      带 tool_calls 的 assistant → 原生 tool_calls 回放
]
```

摘要和 CLAUDE.md **同一条 meta user**，插在真正历史之前，system 保持稳定，便于前缀缓存。
这条 reminder **不写入** Access Layer 会话。

媒体：挂在对应 user turn 的 `attachments`；旧调用方仍可把 attachments 打在最后一条 user 上。

### 3.5 本 run 内：`prune_old_tool_outputs`

`run_stream_react` 每个 iteration 开头、Reason 之前：

```text
若 Σ tool content 字符 > MAX_TOOL_CONTEXT_CHARS (50_000):
    最近 keep_recent=2 条 tool 留原文
    从最老的开始换成：
      [Older tool output cleared from active context: N characters.
       Run the tool again if the exact output is needed.]
    占位符长度计回总量，降到阈值就停
```

拷贝后再改，避免污染跨 iteration 复用的 list。不按工具名过滤（CC microcompact 有白名单）。
不修改 AG-UI、不修改会话 JSON。模型若还需要那份 grep 结果，应再调一次工具。

Hook：`emit_context_pruned`，带前后字符数和被替换条数。

文档旧稿写 48KB，代码是 50_000 字符（按字符不是按 token）。

### 3.6 Durable HITL 与压缩的交叉

审批会 **结束当前 HTTP run**，checkpoint 包含：

- `modelMessages`：当时已经 compose + 可能已经 prune 过的 Provider 消息
- `pendingCalls` / `pendingIndex`
- `loadedMemoryPaths`
- `iteration`

新 run `create_runtime` 仍会对 AL 全文做一遍 `build_context_plan`（日志里的
`context_plan` 来自这次），但 `run_stream_react` 若发现合法 checkpoint，
**用 `modelMessages` 覆盖** `messages`，从 `iteration+1` 继续 Reason。

因此：

- HITL 恢复 **不会**把「刚重新抽取的摘要」套到正在跑的工具链上。
- 压缩态本身不在 checkpoint 里；跨用户 turn 仍无持久摘要。
- Memory 懒加载路径必须进 checkpoint（见系统提示词方案），那是另一条状态，不是 conversation summary。

### 3.7 和 Prompt 编译、lifecycle 的边界

- `compose_prompt` 不管历史长短；它只编 system/context。
- `reset_prompt_caches` 注释里的 `/compact` 指 **清 CLAUDE.md / memory 发现缓存**，
  并不是实现了 CC 那种会话 compact 命令。
- 标题/摘要/权限分类等旁路 `query_kind` 不应调用这套主 Agent compact。

### 3.8 测试钉住的行为

`backend/tests/test_context_manager.py`：

- 超预算时保最后一条、生成 `# Compacted conversation`、活动 token 下降。
- 传入 `compacted_message_ids` 时那些 id 不再出现在 `plan.messages`，摘要保持 `existing_summary`。
- `force_compact` 短会话只留最后 2 条。
- prune：超字符时先清最老的 tool，最近 2 条不动。

`test_tool_history.py`：压缩后活动窗口里不得出现无宣告的 tool 结果。

---

## 4. 对照表（细）

### 4.1 生命周期

```mermaid
flowchart TB
  subgraph cc [Claude Code 同一次 query]
    Q[query 入口] --> B[tool result budget]
    B --> S[snip]
    S --> M[microcompact]
    M --> C[collapse 实验]
    C --> A[autocompact / LLM 摘要]
    A --> R[messagesForQuery 换成摘要链]
    R --> API[主模型 API]
  end

  subgraph ka [K Agent 一次 HTTP run]
    AL[AL 全文] --> RT[create_runtime]
    RT --> P[compose_prompt]
    P --> PL[build_context_plan 一次]
    PL --> CM[compose_api_messages]
    CM --> L[ReAct 循环]
    L --> PR[每轮 prune tool 正文]
    PR --> API2[主模型 API]
  end
```

| | Claude Code | K Agent |
|---|---|---|
| 何时检查对话是否该压 | 每轮 query 入口 | 仅 create_runtime |
| 压完是否继续当前 turn | 是，同一生成器 | 对话压缩在循环外；循环内只 prune |
| 下一用户消息 | 用已替换的 messages | AL 全文再规划，上次摘要丢弃 |
| 手动触发 | `/compact` | 无产品入口 |

### 4.2 摘要质量与成本

| | Claude Code | K Agent |
|---|---|---|
| 算法 | 专用模型，9 段结构 + analysis scratchpad | 按条截断拼接 bullet |
| 额外 token 费用 | 一次完整对话进 compact 模型（可 cache 共享） | 无 |
| 失败 | API 错误 / 无文本 / PTL；熔断 | 纯函数，无失败；估少了仍可能 413 |
| 用户意图、纠正、下一步 | Prompt 强制写入摘要 | 可能被 10k 截断或淹没在 bullet 里 |
| 精确代码 | 摘要内片段 + transcript 路径 + 重读最多 5 个文件 | 只有留在尾部原文里的；摘要里至多 10k 字符/条 |

### 4.3 工具结果

| | Claude Code | K Agent |
|---|---|---|
| 单条过大 | 按工具 `maxResultSizeChars` | 无单独限制 |
| 累计过大 | microcompact 白名单 + 时间/cache-edit | 全 tool 角色、50k 字符、保 2 条 |
| 历史轮次整段丢掉 | HISTORY_SNIP 按 API round | 对话 compact 把旧轮次打进 bullet |
| 与 cache | cache-edit 尽量保前缀 | 无 Anthropic cache_edits |

### 4.4 状态与恢复

| | Claude Code | K Agent |
|---|---|---|
| 活动 transcript 谁说了算 | REPL + 本地 session 文件 | Access Layer |
| compact boundary | 消息链上一等类型，resume 用 | 无 |
| Skill / 文件缓存跨 compact | 显式 attachment 再注入 | 无对应机制 |
| HITL | 同进程 await Promise，compact 与审批都在 query 内 | 结束 run；恢复吃 checkpoint.modelMessages |

### 4.5 数字速查（以源码常量为准）

Claude Code：

- compact 输出预留 ≤ 20k；autocompact 缓冲 13k；警告/错误再 20k；手动堵死缓冲 3k
- PTL 重试 3 次；SM compact 尾部 10k–40k token、至少 5 条 text 消息
- 文件再注入：5 文件 / 50k / 单文件 5k；Skill 25k / 单 5k
- 熔断 3 次连续失败

K Agent：

- 保尾默认 6 条，短会话 force 留 2；回扩 70%–78%
- 摘要 10 万字符总长、单条 1 万
- 工具 prune 5 万字符、保最近 2 条
- 消息额度下限 1_000；inputBudget 下限 8_000

---

## 5. 根因：不是漏抄了一个函数

CC 可以改掉 **正在跑的那份 messages**，所以：

- LLM 摘要有地方住，下一轮 query 自然接着用；
- 必须把文件/Skill/MCP 再贴回去，否则模型失忆；
- `/resume` 必须理解 boundary 和 preservedSegment。

K Agent **禁止 Backend 当会话真相源**（Access Layer 拥有公开 API 与持久化），所以：

- 只能在本轮 Provider 请求上切片；
- 若把 LLM 摘要写入 AL，就要定义：谁更新、是否出现在 UI、HITL checkpoint 如何与摘要合并、
  用户编辑/删除历史时摘要是否作废——这是第二条持久化协议，不是加一个 `compactConversation` 能结束的。

当前取舍是刻意的：实现面小、双进程干净、不额外烧 compact token；
代价是长会话摘要质量低于 CC，且每条用户消息都重复抽取。

---

## 6. 若要对齐 CC，缺什么（不表示现在要做）

按依赖顺序：

1. **跨 run 的 compact 真相源**  
   摘要 + compacted ids（或 boundary 等价物）进入 Access Layer 或 durable checkpoint。
   没有这一步，LLM 摘要下一轮用户消息就会丢，白烧 token。

2. **专用 compact 请求**  
   独立 `query_kind`，禁止工具，结构化摘要；不得复用主 ReAct `PromptBundle` /
   `override_system_prompt`（见系统提示词方案）。需要 PTL 砍头或等价逃生舱。

3. **对话摘要层与工具结果层继续分开**  
   K Agent 已有 prune + pair；要对齐还差工具白名单、单条 max size、以及「是否在循环中途
   再跑对话 compact」（现在只在 create_runtime）。

4. **Post-compact 再注入**  
   最近文件、已用 Skill、plan。没有持久摘要时收益很小。

5. **产品入口**  
   `/compact`、自定义指令、接近上限时的提示、失败熔断与用户可见状态。
   以及是否要做 session-memory compact / collapse 这类实验层。

6. **观测**  
   CC 有 `tengu_compact` / `tengu_auto_compact_succeeded` 和 willRetriggerNextTurn。
   K Agent 已有 `ContextPlanPayload` 和 prune Hook，没有 compact API 用量。

---

## 7. 代码地图

Claude Code（`~/Desktop/claude-code-main`）：

| 路径 | 职责 |
|---|---|
| `src/query.ts` | 每轮顺序：budget → snip → microcompact → collapse → autocompact，成功则替换 messagesForQuery |
| `src/services/compact/autoCompact.ts` | 有效窗口、阈值、熔断、SM 优先、调用 compactConversation |
| `src/services/compact/compact.ts` | 全量/部分 LLM 摘要、PTL 砍头、post-compact 附件、boundary、forked agent |
| `src/services/compact/prompt.ts` | compact 任务书、剥 analysis、包装续写说明 |
| `src/services/compact/grouping.ts` | 按 assistant message.id 切 API round |
| `src/services/compact/microCompact.ts` | 清旧 tool_result |
| `src/services/compact/sessionMemoryCompact.ts` | 用 session memory 代替全量 LLM compact |
| `src/services/compact/postCompactCleanup.ts` | 缓存与主/子线程隔离 |
| `src/commands/compact/compact.ts` | `/compact` |
| `src/utils/contextSuggestions.ts` | 接近阈值时的用户文案 |

K Agent：

| 路径 | 职责 |
|---|---|
| `backend/context/manager.py` | 预算、配对修复、切分、抽取摘要、compose、prune |
| `backend/agent/react_agent.py` | create_runtime 规划一次；循环 prune；HITL 用 checkpoint.modelMessages |
| `backend/runners/k_agent.py` | 组 PromptInputs，不参与压缩 |
| `backend/tests/test_context_manager.py` | 自动压、增量 ID、force、prune 顺序 |
| `backend/tests/test_tool_history.py` | 压缩不拆 tool 对 |
| `docs/context-management.md` | 组成与数据流总览（数字以本文为准） |
