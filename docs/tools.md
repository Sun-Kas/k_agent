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
| `Bash` | 在工作区执行 shell 命令，并限制超时和输出长度。 | 跑测试、构建、格式化、查看 git 状态。 |
| `NotebookEdit` | 插入、替换或删除 `.ipynb` 单元格。 | 修改 Jupyter notebook 的代码或 Markdown 单元格。 |

这些工具都会把路径限制在配置的工作区内，避免模型越权读写用户其它目录。`Write` 和 `Edit` 属于高影响工具，后续如果做更细的权限 UI，应优先给它们加确认策略。

## 网络工具

| 工具 | 作用 | 常见用途 |
| --- | --- | --- |
| `WebFetch` | 抓取 HTTP/HTTPS 页面，并把 HTML 清洗成可读文本。 | 阅读文档页面、接口说明或普通网页内容。 |
| `WebSearch` | 搜索网页并返回候选结果。 | 查找公开资料入口。 |

当前 `WebSearch` 是轻量实现，适合开发期验证工具链。生产环境如果要稳定搜索结果，建议替换成正式搜索 API，并加入域名白名单、请求审计和缓存。

## 任务与 Skill 工具

| 工具 | 作用 | 常见用途 |
| --- | --- | --- |
| `TodoWrite` | 生成或更新当前任务清单。 | 让模型拆解多步骤任务，并在执行过程中同步状态。 |
| `Skill` | 加载并执行项目或用户定义的 Skill。 | 调用可复用工作流，例如特定业务流程、报告生成、固定操作手册。 |

`Skill` 不是新增一个外部服务，而是读取后端 Skill loader 已载入的能力。对于 MCP Prompt 转换出来的 Skill，执行时会绑定当前请求的 MCP manager，避免跨请求复用连接。

## MCP 资源工具

| 工具 | 作用 | 常见用途 |
| --- | --- | --- |
| `ListMcpResourcesTool` | 列出当前已连接 MCP 服务暴露的资源。 | 查看 MCP 服务提供了哪些可读资源。 |
| `ReadMcpResourceTool` | 按 `server_id` 和 `uri` 读取 MCP 资源。 | 读取远端系统、连接器或 MCP 服务提供的上下文。 |

这两个工具只有在 MCP 配置里存在并成功连接服务时才有实际内容。现在默认 MCP 配置为空，所以它们会存在于本地工具列表里，但不会凭空产生资源。

## 记忆工具

| 工具 | 作用 | 常见用途 |
| --- | --- | --- |
| `read_personal_memory` | 读取项目本地持久化记忆。 | 查看 `data/memory/MEMORY.md` 里记录的长期偏好或事实。 |
| `append_personal_memory` | 追加一条项目本地长期记忆。 | 保存用户明确要求记住的信息到 `data/memory/MEMORY.md`。 |
| `search_personal_memory` | 搜索项目本地长期记忆。 | 在 `data/memory/MEMORY.md` 里查找相关条目。 |
| `compact_personal_memory` | 去重并裁剪项目本地记忆。 | 控制 `data/memory/MEMORY.md` 大小，减少重复内容。 |

记忆工具当前直接读写项目内 `data/memory/MEMORY.md`。对话历史由统一文件存储层保存到 `data/sessions/`。

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
