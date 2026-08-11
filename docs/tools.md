# K Agent 工具说明

本文记录当前后端默认 `coding` preset 会暴露给模型的本地工具。MCP 服务暴露的动态工具不在这里固定列出，因为它们取决于运行时配置。

## 文件与代码工具

| 工具 | 作用 | 常见用途 |
| --- | --- | --- |
| `Read` | 读取工作区内 UTF-8 文本文件。 | 查看代码、配置、文档内容。 |
| `Write` | 写入完整文件内容，会创建父目录。 | 创建新文件或覆盖生成类文件。 |
| `Edit` | 对文件中的指定文本做替换。 | 小范围修改代码、配置和文档。 |
| `Glob` | 按 glob 规则查找文件。 | 找某类文件，例如 `**/*.tsx`。 |
| `LS` | 列出工作区目录内容。 | 快速查看目录结构和文件大小。 |
| `Grep` | 用正则搜索文件内容。 | 查函数、变量、接口路径或配置项。 |
| `Bash` | 在工作区执行 shell 命令，并限制超时和输出长度；尽量放进 OS 沙箱。 | 跑测试、构建、格式化、查看 git 状态。 |
| `InstallSandbox` | 在用户明确确认后安装 `srt`（Anthropic sandbox-runtime）。 | 沙箱未就绪时，经用户同意后代为安装。 |
| `NotebookEdit` | 插入、替换或删除 `.ipynb` 单元格。 | 修改 Jupyter notebook 的代码或 Markdown 单元格。 |

`Read` / `Write` / `Edit` / `Glob` / `LS` / `Grep` 会把路径限制在配置的工作区内。`Bash` 额外走下面的沙箱策略，因为 shell 本身可以 `cd` 到工作区外。

## Bash 沙箱

`Bash` 的沙箱逻辑在 `backend/sandbox/`，通过 Anthropic 的 `srt`（`@anthropic-ai/sandbox-runtime`）做 OS 级隔离：

| 平台 | 后端 | 状态 |
| --- | --- | --- |
| macOS | Seatbelt（`sandbox-exec`） | 支持 |
| Linux | bubblewrap（需本机安装 `bwrap`） | 支持 |
| Windows 原生 | — | 不支持；请在 WSL2 里跑后端 |

相关环境变量：

- `BASH_SANDBOX_MODE`：`off` / `auto`（默认）/ `required`。`auto` 在探测不到 `srt` 时退回裸执行，并在工具结果里写明 `sandboxed: false`；`required` 则直接失败，绝不静默降级。
- `BASH_SANDBOX_COMMAND`：包装器命令，默认 `srt`。
- `NETWORK_ACCESS_DEFAULT`：K Agent、Codex、Claude Code 的统一出站开关，默认 `true`；单次运行可用 `agentOptions.networkAccess` 覆盖。
- `CLAUDE_AUTO_APPROVE_TOOLS`：Claude Code 常规工具的默认授权列表。默认包含 Bash、Read、Edit、Write、Glob、Grep、NotebookEdit、WebFetch/WebSearch、TodoWrite、任务板工具和 Skill；MCP 与未列出的工具仍进入 Human Approval。
- `BASH_SANDBOX_ALLOWED_DOMAINS`：K Agent Bash 的出站域名白名单。`srt` 不接受裸 `*` / `*.com`；默认是一组具体域名（GitHub/npm/PyPI/Steam 等）。非法项会被忽略，Bash 仍留在文件系统沙箱内。统一开关为 `false` 时，此列表不会放行网络。
- Bash 的 `sandbox_permissions=require_escalated` 只用于任务必须写入 workspace 外路径、访问被默认沙箱阻止的本机资源，或访问当前域名白名单外的准确 hostname，并且必须同时提交 `escalation_scope` 与具体 `escalation_resource`。Backend 会在 HITL 前匹配实际白名单；白名单内目标以及普通超时、HTTP/DNS/断流错误不会进入审批。
- Bash 可用 `timeout_seconds` 为本来就耗时的命令声明 1–300 秒的执行期限；它只调整当前子进程的 wall-clock deadline，不改变文件、网络或宿主机权限。
- `BASH_SANDBOX_WRITE_PATHS` / `BASH_SANDBOX_DENY_READ`：额外可写路径和额外拒绝读取路径。

默认策略：工作区和系统临时目录可写；`~/.ssh`、`~/.aws`、项目 `.env` 等凭据路径拒绝读取；子进程环境变量按白名单拷贝（`PATH`/`HOME`/`LANG` 等），模型 API key、Langfuse、AWS 密钥不会进入子进程。

沙箱不可用时会这样告知用户：

1. 顶部状态栏（本轮第一次）：说明未就绪 / 不可用，并提示手动安装或对话确认后安装。
2. 对话正文：模型应转述工具结果里的 `userMessage`（含流程与平台限制）。
3. 侧栏健康状态：显示「沙箱就绪 / 未安装 / 不可用」；鼠标悬停可看 `userSummary` 完整说明。

推荐流程：助手说明原因 → 你确认是否安装 → 确认后助手调用 `InstallSandbox`，或你手动执行安装命令。未确认不会安装。

平台说明会写进上述反馈：

- macOS：可装 `srt`，建议再装 `ripgrep`
- Linux：可装 `srt`，还需本机 `bubblewrap` / `socat`
- Windows 原生：不支持；提示改用 WSL2，不会提供 `InstallSandbox`

## 权限模型

每次工具调用在执行前都要过一遍权限规则（`backend/permissions/`）：

- 规则来自权限规则文件，按文件签名缓存，改动后自动失效重载，不会每次调用都读盘。
- 默认策略由 `K_AGENT_PERMISSION_DEFAULT` 控制，可以设成 `deny` 让未匹配的工具默认拒绝。
- `ask` 目前没有前端确认通道，因此按拒绝处理，而不是静默放行。
- `Bash` 命令会按 `&&`、`||`、`;`、`|`、换行拆成多个子命令分别匹配，取最严格的结果，避免用管道拼接绕过规则。
- Skill 声明的 `allowedTools` 在该 Skill 激活期间强制生效，Skill 之外的工具会被拒绝。

## 网络工具

| 工具 | 作用 | 常见用途 |
| --- | --- | --- |
| `WebFetch` | 抓取 HTTP/HTTPS 页面，并把 HTML 清洗成可读文本。 | 阅读文档页面、接口说明或普通网页内容。 |
| `WebSearch` | 搜索网页并返回候选结果。 | 查找公开资料入口。 |

`WebFetch` 会先把目标主机解析成 IP，再拒绝回环、私网、链路本地和组播地址，重定向的每一跳都重新校验，防止模型被诱导去读内网服务或云元数据接口。本地开发确实需要抓 `localhost` 时，可以用环境变量显式放开。响应还有下载字节上限，避免一个大文件把上下文和内存吃满。

当前 `WebSearch` 是轻量实现，适合开发期验证工具链。生产环境如果要稳定搜索结果，建议替换成正式搜索 API，并加入域名白名单、请求审计和缓存。

## 任务与 Skill 工具

| 工具 | 作用 | 常见用途 |
| --- | --- | --- |
| `TodoWrite` | 生成或更新当前任务清单。 | 让模型拆解多步骤任务，并在执行过程中同步状态。 |
| `Skill` | 加载并执行项目或用户定义的 Skill。 | 调用可复用工作流，例如特定业务流程、报告生成、固定操作手册。 |

`Skill` 不是新增一个外部服务，而是读取本次请求随 `AgentRunRequest` 带过来的 Skill 定义。对于 MCP Prompt 转换出来的 Skill，执行时会绑定当前请求的 MCP manager。

## MCP 资源工具

| 工具 | 作用 | 常见用途 |
| --- | --- | --- |
| `ListMcpResourcesTool` | 列出当前已连接 MCP 服务暴露的资源。 | 查看 MCP 服务提供了哪些可读资源。 |
| `ReadMcpResourceTool` | 按 `server_id` 和 `uri` 读取 MCP 资源。 | 读取远端系统、连接器或 MCP 服务提供的上下文。 |

这两个工具只有在 MCP 配置里存在并成功连接服务时才有实际内容。现在默认 MCP 配置为空，所以它们会存在于本地工具列表里，但不会凭空产生资源。

## 记忆工具

| 工具 | 作用 | 常见用途 |
| --- | --- | --- |
| `read_personal_memory` | 读取持久化记忆。 | 查看 `$K_AGENT_HOME/content/memory/MEMORY.md`。 |
| `append_personal_memory` | 追加一条长期记忆。 | 写入 `$K_AGENT_HOME/content/memory/MEMORY.md`。 |
| `search_personal_memory` | 搜索长期记忆。 | 在 MEMORY.md 里查找相关条目。 |
| `compact_personal_memory` | 去重并裁剪记忆。 | 控制 MEMORY.md 大小。 |

记忆在 `$K_AGENT_HOME/content/memory/`；会话历史在 `$K_AGENT_HOME/state/sessions/`。

## 其它辅助工具

| 工具 | 作用 | 常见用途 |
| --- | --- | --- |
| `get_current_time` | 返回当前服务端 UTC 时间。 | 处理需要当前时间的回答或计划。 |

`echo_text` 仍保留在 `legacy` preset 中，用于测试工具链；默认 `coding` preset 不暴露它。

## 暂未接入的 Claude Code 工具

以下工具目前没有接入默认工具集：

| 工具 | 暂不接入原因 |
| --- | --- |
| `Agent` / `TeamCreate` / `SendMessage` | 需要完整 subagent/team 运行时、生命周期管理和隔离上下文。 |
| `Monitor` / `TaskCreate` / `TaskList` / `TaskUpdate` / `TaskStop` | 需要后台任务状态机、输出持久化和取消机制。 |
| `LSP` | 需要接入语言服务器，并定义诊断、跳转、引用查询的协议。 |
| `CronCreate` / `CronList` / `CronDelete` | 需要会话级定时任务恢复、过期处理和安全边界。 |
| `EnterPlanMode` / `ExitPlanMode` | 属于交互模式控制，当前前端还没有对应状态。 |
| `EnterWorktree` / `ExitWorktree` | 需要 git worktree 生命周期和文件系统隔离策略。 |
| `PowerShell` | 当前项目运行环境主要面向 macOS/Linux shell。 |

后续如果继续补工具，建议按“先状态机，后工具入口”的顺序做，避免模型看到一个名字但实际无法可靠执行。
