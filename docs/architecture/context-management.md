# K Agent 上下文管理系统

> 当前状态：已按 2026-09-03 工作区重新核对。

压缩的目标架构、Claude Code 对照和迁移步骤见
[上下文压缩最终技术方案](context-compaction-technical-solution.md)。该方案尚未实现；本文只说明 K Agent 当前实际行为。

当前策略没有升级成 Claude Code 式的持久化 compact。它仍然是：每次 HTTP run 根据 Access Layer
传来的完整历史重新规划一次活动上下文；较早历史通过确定性文本抽取变短，摘要不跨 run 保存；
ReAct 循环中只清理旧工具结果，不再次压缩整段对话。

本系统参考 Claude Code 的公开上下文模型实现，但保持 K Agent 的双进程边界：

- Access Layer 只拥有公开接口、完整会话与 AG-UI Event 持久化、并发控制和流式透传。
- Agent Backend 拥有系统提示词拼接、指令与记忆发现、Skill/MCP 管理、上下文预算与压缩、模型消息组装和旧工具结果裁剪。
- 每次运行时 Access Layer 只传完整历史及选中的 model/MCP/Skill ID；摘要和压缩状态不跨服务持久化。
- 完整会话历史始终保存在 `$K_AGENT_HOME/state/sessions`；压缩只影响活动模型上下文，不删除历史。

## 上下文组成

每次模型请求按以下顺序组装：

1. 系统提示词。
2. 项目与用户指令。
3. 自动记忆 `$K_AGENT_HOME/content/memory/MEMORY.md`。
4. 已选择 Skill 的名称与简介。
5. 已连接 MCP 工具定义。
6. 已压缩的早期会话摘要。
7. 未压缩的最近会话消息。
8. 当前轮新增的工具调用和工具结果。

## 指令与记忆发现

启动时加载：

- `~/.claude/CLAUDE.md`
- 从文件系统根目录到当前工作目录的 `CLAUDE.md`
- `.claude/CLAUDE.md`
- `CLAUDE.local.md`
- `.claude/rules/**/*.md` 中没有 `paths` 的规则
- `$K_AGENT_HOME/content/memory/MEMORY.md`

支持 `@relative/path` 文件导入，最多递归五层。默认禁止导入当前指令目录以外的文件。

带 `paths` frontmatter 的规则不会在启动时加载。用户消息或工具事件出现文件路径后，匹配规则与目标子目录中的指令会在后续请求中加载。

自动记忆入口最多加载前 200 行或 25KB。详细主题应保存在 `$K_AGENT_HOME/content/memory` 的其他文件中，并通过记忆工具按需读取。

## Token 预算

每个模型配置支持：

- `contextWindow`：模型总上下文窗口。
- `maxOutputTokens`：为回答预留的输出 token。
- `contextSafetyTokens`：压缩触发前保留的安全余量。

输入预算计算：

```text
inputBudget = max(8,000, contextWindow - maxOutputTokens - contextSafetyTokens)

messageBudget = max(
  1,000,
  inputBudget - system - contextMessage - existingSummary - toolDefinitions
)
```

系统按分类估算：

- system
- memory
- skillsAndTools
- summary
- messages
- remaining

估算只用于提前触发压缩，不替代模型提供方的实际 tokenizer。

## 自动压缩

当未压缩消息超过剩余输入预算时：

1. 先修复不完整的 `assistant.tool_calls` / `tool` 配对，避免把孤立工具消息发给 Provider。
2. 消息超过六条时，初始保留最近六条，再在预算允许时向前回扩，目标占消息预算的 70%～78%。
3. 消息总数不超过六条但本身已经超限时，退化为保留最后两条，而不是强行保留六条。
4. 更早消息不会交给另一个模型总结；Backend 只按角色抽取、折叠空白并拼成 bullet 摘要。
5. 压缩切点会越过连续的工具结果，避免把 tool call 与其结果拆开。
6. 摘要和已压缩消息 ID 只属于本次 `ContextPlan`，不会写回 Access Layer。
7. 下一次用户消息仍由 Backend 根据完整历史从零重新规划。

摘要最多保留 100,000 字符，单条历史内容最多保留 10,000 字符。完整原消息仍保存在会话的 `messages` 中。

这意味着当前“摘要”更接近有界历史摘录，不是语义重写后的任务状态。优点是不增加一次模型调用、
Backend 继续无状态；缺点是用户意图、失败原因和待办可能被长 bullet 淹没，并且每个新 run 都会重复计算。

## 工具结果清理

同一 Agent 运行内，工具结果累计超过 50,000 字符时：

- 保留最近两个工具结果。
- 优先把更早工具结果替换为清理标记。
- 模型需要精确内容时可以重新调用工具。

该处理只修改当前模型请求中的临时消息，不修改 AG-UI 事件或后端持久化记录。

需要注意：本 run 内每次 Reason 前只执行工具结果清理，不会再次调用 `build_context_plan()`。
如果一次长工具链的非工具消息或最近两个工具结果本身继续膨胀，仍可能触碰 Provider 上下文上限。

## 双进程数据流

```text
Access Layer
  读取并发送完整会话 + model/MCP/Skill ID
        |
        v
Agent Backend
  发现资料 -> 拼接 Prompt -> 预算/压缩 -> 模型/工具循环
        |
        v
标准 AG-UI Event
        |
        v
Access Layer
  原样透传并持久化 Event
```

## 与 Claude Code 的对应关系

| Claude Code 机制 | K Agent 对应实现 |
|---|---|
| CLAUDE.md | `CLAUDE.md` |
| Auto memory | `$K_AGENT_HOME/content/memory/MEMORY.md` |
| `.claude/rules` | `.claude/rules` |
| Auto compact | 上下文预算超限自动压缩 |
| Clear old tool outputs | 50,000 字符工具结果预算与优先裁剪 |
| Skill descriptions | `$K_AGENT_HOME/content/skills` 中已启用 Skill 摘要 |

这里的“对应”只表示解决同一类问题，不表示实现等价。Claude Code 的 full compact 会调用专用模型生成
结构化摘要、建立 boundary、补回文件与 Skill，并在同一用户 turn 继续；K Agent 当前没有这些生命周期状态。
