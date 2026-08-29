# HITL 后端实现对照：K Agent vs Claude Code

> 对照源码：桌面 `claude-code-main/`（Claude Code）与本仓库 `backend/`。
> 只谈后端控制流，不谈 TTY / 审批卡外观。
> 按章节顺序读。

---

## 0. 一句话

两边都是 **「工具副作用之前算出 allow / deny / ask，ask 时挂起执行」**。K Agent 现在还实现了独立的模型工具 `AskUserQuestion`：它复用 Interrupt/Resume 传输层，但不是权限判定。
实现上 **不是同一套管线**：Claude Code 把权限做成 `canUseTool` 回调，在同一 `query()` 生成器里 `await Promise`；K Agent 把权限焊在密封 `preflight` 里，`ask` 用 AG-UI Interrupt **结束本轮 HTTP run**，批准后开新 Run 再执行。

规则文件形态接近；决策引擎、校验顺序、bypass 语义、Hook、沙箱越权字段都不同。

---

## 1. 两边各自的入口函数

读代码从这里进，不要从 UI 进。

### Claude Code

主循环在 `src/query.ts`：模型产出 `tool_use` 后走 `runTools` → `checkPermissionsAndCallTool`（`src/services/tools/toolExecution.ts`）。

真正问人的句柄是 `CanUseToolFn`：

```ts
(tool, input, toolUseContext, assistantMessage, toolUseID, forceDecision?)
  => Promise<PermissionDecision>
```

- 交互式 REPL：`src/hooks/useCanUseTool.tsx` 里 `new Promise`，`ask` 时 `handleInteractivePermission` 把条目推进确认队列，**进程不退出**。
- 无头 `-p`：`src/cli/print.ts` 的 `createCanUseToolWithPermissionPrompt`，`ask` 时 `permissionPromptTool.call(...)`，和 abort signal `Promise.race`。

规则引擎是 `hasPermissionsToUseTool` → `hasPermissionsToUseToolInner`（`src/utils/permissions/permissions.ts`）。每个 Tool 还有自己的 `checkPermissions()`。

### K Agent

主循环在 `backend/agent/react_agent.py` 的 `run_stream_react`：Reason 之后 Act 调 `_run_tool` → `_execute_tool` → `pipeline.run_tool(preflight, execute)`。

`ask` 不返回 `PermissionDecision` 给循环接着跑，而是 `approval_handler` → `ApprovalBroker.request()`（`backend/approvals.py`）：往当前 run 的队列塞 Interrupt payload，然后 `await run_closed`。Broker 吐出 `approval_request` + `interrupt` 后 **取消 Runner**。恢复是另一次 HTTP run + `resume_checkpoint`。

决策集中在 `OpenAIAgent._local_permission_decision` / `_enforce_permission`，规则在 `backend/permissions/rules.py`。工具对象 **没有** `checkPermissions` 方法。

模型调用 `AskUserQuestion` 时，`_local_tool_preflight` 改走 `backend/user_questions.py` 的问题校验和 `source="user_input"` handler。它不经过 `_local_permission_decision`，因此 `full_access` 也不会跳过询问。

---

## 2. 执行顺序（最大的实现差异之一）

### Claude Code：`checkPermissionsAndCallTool`

```text
1. tool.inputSchema.safeParse          # Zod，失败直接 tool_result error
2. tool.validateInput?                 # 工具自己的值校验（仍可做路径 deny）
3. （Bash）startSpeculativeClassifierCheck
4. runPreToolUseHooks                  # 可改 input / allow / deny / ask / stop
5. resolveHookPermissionDecision
      → 多数情况再调 canUseTool
          → hasPermissionsToUseToolInner
          → 若仍 ask：弹队列 或 调 permission-prompt MCP
6. 非 allow：写 is_error tool_result，return，不 call
7. tool.call(callInput, …, canUseTool) # 子工具还可以再问一次
```

要点：

- **Schema 在权限之前。** 参数都 parse 不过，不会进 HITL。
- Hook 的 `allow` **不能**绕过 settings 里的 deny/ask：`resolveHookPermissionDecision` 仍跑 `checkRuleBasedPermissions`。
- `updatedInput` 是一等公民：Hook / 用户改参后，`call()` 用新 input。
- 同一轮 `query()` 生成器继续 yield tool_result，然后下一轮模型。

对应文件：`claude-code-main/src/services/tools/toolExecution.ts`（约 599 行起）、`toolHooks.ts` 的 `resolveHookPermissionDecision`。

### K Agent：密封 terminal

```text
wrap_tool（洋葱，order 小的在外）
  └─ sealed（写死，wrap 拿不到 raw execute）
        1. await preflight(current)
             Skill 白名单
             普通工具：_local_permission_decision → _enforce_permission
             AskUserQuestion：normalize questions → user_input Interrupt
        2. emit ToolStarted
        3. await execute(current)
             validate_tool_arguments   # schema 在权限之后
             真正执行
        4. emit ToolCompleted
```

对应：`backend/agent/hooks/pipeline.py` 的 `run_tool`，`react_agent.py` 里 local/MCP 两个闭包。

要点：

- **权限在 schema 之前。** 非法参数也可以先弹审批；批准后 execute 里才校验，失败变成 Observation。
- wrap 改参数必须 `override` 再 `call_next`，会 **重新进 sealed**，权限再跑一遍。这点和 Claude「Hook 改 input 后再 checkRuleBasedPermissions」同构，但是 Middleware 不能代替 preflight。
- 没有 `updatedInput` 产品路径：用户不能改参数再批。Resume 只认 `callId + requestHash`。
- `ask` 之后本轮 ReAct **停在工具边界**，不是同一次 generator 里等 Future。
- `AskUserQuestion` 的 executor 故意不可达；用户答案在 Resume 时直接成为该 tool call 的 Observation。

---

## 3. 决策引擎：Claude 分层 vs K Agent 一条函数

### Claude：`hasPermissionsToUseToolInner` 的步骤编号就是源码注释

| 步 | 做什么 | bypass 能否跳过 |
| --- | --- | --- |
| 1a | 整工具 deny 规则 | **否**，直接 deny |
| 1b | 整工具 ask 规则 | 否（沙箱 Bash + autoAllow 可 fall through） |
| 1c | `tool.checkPermissions(parsedInput)` | 工具自己决定 |
| 1d | 工具返回 deny | **否** |
| 1e | `requiresUserInteraction()` 且 ask | **否**（即使 bypass 也要问） |
| 1f | 内容级 ask 规则（如 `Bash(npm publish:*)`） | **否** |
| 1g | `safetyCheck`（`.git/`、`.claude/` 等） | **否** |
| 2a | `bypassPermissions` 或 plan+原本就是 bypass | 到这里才 allow |
| 2b | 整工具 always-allow 规则 | allow |
| 3 | 工具 `passthrough` → 升成 `ask` | 默认要问 |

外层 `hasPermissionsToUseTool` 再变换：

- `dontAsk`：所有 `ask` → `deny`（写在最后，防止被早退绕过）
- `auto`：侧路 classifier 代替人；无头且 `shouldAvoidPermissionPrompts` 时无人可问则 deny / abort
- 连续 classifier deny 超限会退回人工，无头则直接 `AbortError`

`passthrough` 是第四态：工具说「我不管，交给上层」，上层再变成 ask。K Agent 没有这个态。

### K Agent：`_local_permission_decision`

写死的四层（后层覆盖前层）：

1. `check_permissions(tool, subjects)`：规则文件，**书写顺序先命中者胜**；`ask` 会继续扫，后面的 `deny` 仍能赢。
2. Read / Glob / Grep / LS：规则 `ask` **降成 allow**；`deny` 保留。
3. `permission_mode == full_access`：**一律 allow，包括规则 deny。**
4. 可越权工具带 `sandbox_permissions=require_escalated` → 强制 `ask`（Bash 还要合法 `escalation_scope` + `escalation_resource`）。

MCP 更简单：full_access 则 allow，否则 `check_permission("mcp", "{server}:{tool}")`。

这和 Claude 的 bypass **不是同一语义**：Claude 的 bypass 仍吃 1a/1d/1e/1f/1g；K Agent 的 `full_access` 在第 3 步短路，**规则 deny 失效**。这是后端安全模型上最大的分叉之一。

---

## 4. 规则匹配：像，但不是同一解析器

Claude 规则值是 `{ toolName, ruleContent? }`，字符串形如 `Bash(git *)`、`Read(*.env)`，多来源合并：`userSettings` / `projectSettings` / `localSettings` / `flagSettings` / `policySettings` / `cliArg` / `command` / `session`。匹配器按工具分发：`preparePermissionMatcher`（路径）、Bash 还有拆命令 + deny 优先于路径约束（`bashPermissions.ts` 里有专门的 SECURITY 注释）。

K Agent 规则是扁平 JSON：`{ tool, pattern, behavior }`，`*` 转成锚定正则，**禁止用户写任意正则**。Bash 的「拆 `&&` `||` `;` `|` 取最严」在 `check_permissions` + `_permission_subjects`，比 Claude 的 AST / `splitCommand` 粗。没有 settings 分层、没有 `PermissionUpdate` 写回用户配置。

Skill `allowedTools`：两边都有「Skill 激活后收窄工具集」。Claude 还有 Skill 级 hooks；K Agent 的 Skill `hooks` 字段 **从不在 Python 里执行**，白名单闩在 `context.skill_allowlist`，preflight 强制。

---

## 5. 工具自己的权限 vs 中心函数

Claude 每个 Tool 实现 `checkPermissions`：

- `FileReadTool` → `checkReadPermissionForTool`
- `FileWriteTool` → `checkWritePermissionForTool`
- Bash → 大文件 `bashPermissions.ts`：子命令、cd+git 复合命令、重定向、沙箱、classifier 元数据

权限知识在工具旁边，规则引擎只做全局排序和 mode 变换。

K Agent 工具（`backend/tools/cc_like.py` 等）只实现 `execute` 和 schema。读不读工作区外、要不要 srt，靠：

- 中心决策函数
- `ContextVar` 里的 `permission_mode`
- execute 里看 `sandbox_permissions` 是否已批准后的 payload

没有 per-tool `checkPermissions` 插件点。要加「写 `.env` 必须问」只能改规则文件或改 `_local_permission_decision`。

---

## 6. ask 之后怎么挂起（控制流）

### Claude：同进程 Promise

`useCanUseTool`：`ask` 不 reject Promise，而是把 `ToolUseConfirm` 推进队列；`onAllow` / `onReject` 才 `resolve`。`query.ts` 一直停在 `await canUseTool`。工具未 `call`。

无头：`permissionPromptTool.call({ tool_name, input, tool_use_id })`，解析返回 JSON 为 `PermissionDecision`。MCP 挂了或 abort → deny，fail-closed。

批准后 `updatedInput` 可带回 `tool.call`。用户「Always」走 `PermissionUpdate` 写 settings / session 规则，下次 `hasPermissionsToUseToolInner` 在 2b 命中 allow。

### K Agent：结束 Run + checkpoint

`ApprovalBroker.request`：

1. 组 payload（含 `requestHash`、内部 `_checkpoint`）
2. `interrupt_queue.put_nowait`（同一 run 只能一个未决 Interrupt）
3. `await run_closed.wait()` —— 此时 **不返回用户决定**
4. stream 侧看到队列后 cancel pump，yield `interrupt`，return
5. `request()` 醒来返回 `{"action": "cancel"}`（占位；真正决定在下次 Resume）

K Agent Runner 在抛出控制流之前把 `_react_tool_boundary` 拍进 checkpoint：iteration、pending 工具列表、当时的 provider messages。Resume 时：

- 拒绝：给那一个 `tool_call_id` 写 Observation，不执行
- 批准：只给匹配 hash 的那一次带 `_resume_authorization`，然后 `_run_tool`；同批后面的工具若仍 ask，会再 Interrupt

这不是 Claude 的 `await canUseTool`。协程栈、CLI 子进程、MCP 连接都不为等审批而活着。

Claude Code Runner（本仓库 `backend/runners/claude_code.py`）走第三条路：子进程里跑 Claude 自己的 `checkPermissionsAndCallTool` + `--permission-prompt-tool`，MCP 再桥回同一个 `ApprovalBroker`。**提问算法是 Claude 的，等待模型是 K Agent 的 Interrupt。** 恢复只能 hash 一次性授权，因为 Claude 进程已经没了。

当前 bridge 也识别 Claude 原生 `AskUserQuestion`：原 Run 产生 `user_input` terminal
Interrupt；Resume 把共享表单的 `selected/custom` 转成 Claude 的
`updatedInput.answers/annotations`。在 `bypassPermissions` 下 bridge 仍安装，因为 bypass
只能放过普通权限，不能替用户回答 `requiresUserInteraction` 工具。

### K Agent 的 `AskUserQuestion` 与 Claude Code 用户提问

两边都把“需要用户给出业务选择”视为不可由权限 bypass 自动回答的交互工具，但等待模型不同：

| | Claude Code | K Agent |
| --- | --- | --- |
| 调用目的 | 收集用户选择，属于 `requiresUserInteraction` | 收集用户选择，固定 `source=user_input` |
| 等待方式 | Claude 原生 TUI 是进程内 Promise；本仓库无头 Runner 把它桥接成 terminal Interrupt | terminal Interrupt 结束原 Run，新 Run Resume |
| 问题约束 | 由 Claude 工具 schema 与交互 UI 校验 | `user_question.py` schema + `user_questions.py` 二次规范化 |
| 回答形态 | 选择项或 UI 提供的自由回答 | 每题 `{selected: string[], custom: string}` |
| 选择并补充 | 本仓库 bridge 明确合并进 answers，并保留 annotations.notes | 明确支持，selected 与 custom 相互独立 |
| 权限 bypass | `requiresUserInteraction()` 不被 bypass 跳过 | `full_access` 不跳过专用问题门 |
| 恢复防篡改 | 本仓库用 checkpoint 原问题 + tool input hash；原生 TUI 由同一进程持有 | checkpoint 原问题 + requestHash 共同校验 |

K Agent 单次允许 1–4 个问题，每题 2–4 个选项。用户可以只选选项、只写自定义答案，或在选择后继续补充文本；单选题至多选一个，多选题可选多个。Backend 以 checkpoint 中的问题定义校验 selected label，不信任浏览器重传的问题或选项。

---

## 7. 沙箱越权：字段都不同

Claude Bash 用 `dangerouslyDisableSandbox?: boolean`。策略在 `shouldUseSandbox.ts`；权限理由类型有 `sandboxOverride`（`excludedCommand` | `dangerouslyDisableSandbox`）。模型提示里写：默认沙箱，只有必要时才设 true，且每次命令单独判断。这是 **工具参数上的逃逸开关**，仍要过 `checkPermissions`（除非已 bypass）。

K Agent 用 `sandbox_permissions: "require_escalated"`，外加 Bash 的 `escalation_scope` / `escalation_resource`。决策层把该字段 **强制变成 ask**，批准后 execute 里才允许跳过 srt。白名单内域名再申请越权直接 deny。字段名接近早期 Claude 文档，但对照当前 `claude-code-main` **没有** `require_escalated` 这个字符串。

不要把两边的逃逸参数当成可互换协议。

---

## 8. Hook：名字容易混

| | Claude Code | K Agent |
| --- | --- | --- |
| 工具执行前可改权限 | **PreToolUse**，`permissionDecision: allow\|deny\|ask`，可 `updatedInput` | **没有**对等物 |
| 用户可配、可拦工具 | settings / Skill frontmatter 的 hooks，进程内执行 | Skill `hooks` 只当文本给模型 |
| 中间件 | 不叫 middleware；权限在 toolExecution | `wrap_tool` 可改请求，**拆不掉** sealed preflight |
| 观测 | 另有 analytics / OTel `tool_decision` | Observer fail-open，与 HITL 无关 |

K Agent 的 Hook 管线（`backend/agent/hooks/`）对齐的是「观测 + 可排序 wrap」，不是 Claude 的 PreToolUse 权限协议。HITL 注释也写了：权限不是 Middleware。

---

## 9. 对照清单（后端）

| 机制 | Claude Code | K Agent |
| --- | --- | --- |
| 问人的类型 | `CanUseToolFn` → `Promise<PermissionDecision>` | `approval_handler` → Interrupt 结束 run |
| 用户提问工具 | 交互工具，`requiresUserInteraction` | `AskUserQuestion`，`category=user_input` |
| 决策核心 | `hasPermissionsToUseToolInner` + 每工具 `checkPermissions` | `_local_permission_decision` + `rules.py` |
| Schema vs 权限 | schema → validate → 权限 → call | 权限 → ToolStarted → schema → execute |
| 第四态 passthrough | 有 | 无 |
| bypass / full_access | bypass **仍尊重** deny、内容 ask、safetyCheck、交互工具 | 普通权限上 full_access **吞掉 deny**；用户提问仍会 Interrupt |
| 只读默认 | 由规则 / 路径检查决定 | 代码把 Read 等 ask 降 allow |
| 改参数再执行 | `updatedInput` | 无；hash 必须一致 |
| Always 写入配置 | `PermissionUpdate` 多 destination | 仅内存 `approved_targets`（当前 continuation） |
| 无头提问 | `--permission-prompt-tool` 阻塞 call | 本系统 Interrupt；Claude Runner 才用该旗标 |
| Classifier / auto mode | 有（feature flag） | 无 |
| dontAsk | ask→deny | 无同名模式 |
| 复合 Bash | AST/拆命令 + cd+git 等安全特例 | 简单分隔符拆分取最严 |
| 等待期间进程 | 活着 | Backend run 结束，checkpoint 在 Access Layer |

---

## 10. 建议怎么对着读源码

1. Claude：`toolExecution.ts` 的 `checkPermissionsAndCallTool` 从头读到 `tool.call`。
2. Claude：`permissions.ts` 的 Inner 1a–3 和外层 dontAsk/auto。
3. Claude：`useCanUseTool.tsx` 的 Promise + `print.ts` 的 permission-prompt 包装。
4. 本仓库：`pipeline.py` `run_tool` sealed。
5. 本仓库：`react_agent.py` `_local_permission_decision` / `_enforce_permission` / resume 那段 pendingIndex。
6. 本仓库：`user_question.py`、`user_questions.py` 与 AskUserQuestion resume 特殊分支。
7. 本仓库：`approvals.py` `request()` 为什么返回 cancel 占位。
8. 若关心「跑 Claude CLI 时」：`claude_approval_bridge.py` 如何把 Claude 的 `{behavior, updatedInput}` 接到 Broker。

读完后用三条尺子判断「算不算同一实现」：

1. 人点批准之前有没有副作用？（两边都是没有，这条对齐。）
2. 决策是不是同一套步骤函数？（不是：Claude 分工具 + 免疫 bypass 的规则层；K Agent 是中心函数 + full_access 短路。）
3. ask 是阻塞当前 generator 还是结束 HTTP run？（不是同一控制流。）
