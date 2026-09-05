# K Agent 文档索引

项目文档按职责分为六类。新增文档应优先放入对应的二级目录，避免 `docs/` 根目录重新堆积。

## 架构与运行时

- [AG-UI 协议约定](architecture/ag-ui-protocol.md)
- [上下文压缩、完整对话存储与 AG-UI 重构计划](architecture/context-compaction-conversation-storage-refactor-plan.md)
- [Agent Hook 现状说明](architecture/agent-hooks.md)
- [Agent Hook 与 Middleware 技术方案](architecture/agent-hooks-and-middleware-technical-solution.md)
- [Tool Runtime 统一封装与执行边界重构技术方案](architecture/tool-runtime-refactor-technical-solution.md)
- [上下文管理总览](architecture/context-management.md)
- [上下文压缩技术方案](architecture/context-compaction-technical-solution.md)
- [系统提示词分层与拼接技术方案](architecture/system-prompt-composition-technical-solution.md)
- [Skill Catalog 与正文懒加载技术方案](architecture/skill-catalog-runtime-boundary-technical-solution.md)
- [权限模式与 HITL 技术方案](architecture/permission-and-hitl-technical-solution.md)
- [可持久化 HITL 检查点技术方案](architecture/durable-hitl-checkpoint-technical-solution.md)
- [K Agent HITL 实现说明](architecture/hitl-implementation.md)
- [HITL 后端实现对照](architecture/hitl-backend-vs-claude-code.md)

## 功能方案

- [Agent Team 技术方案](features/agent-team-technical-solution.md)
- [定时任务技术方案](features/scheduled-task-technical-solution.md)
- [npm CLI / 终端工作台技术方案](features/npm-cli-terminal-technical-solution.md)
- [前端页面设计评审与改造方案](features/frontend-design-review-and-improvement-plan.md)
- [Skill / MCP 广场技术方案](features/marketplace-skill-mcp-technical-solution.md)
- [流式审批卡片渲染待办](features/streaming-approval-card-todo.md)

## 使用指南

- [Docker 部署指南](guides/docker-deployment.md)
- [工具说明](guides/tools.md)

## 调研

- [Agent 递归自进化机制调研](research/agent-recursive-self-evolution-survey.md)

## 参考记录

- [接口与重要修改记录](reference/interface-change-record.md)

## 汇报归档

- [2026-08-11 双周研发汇报](reports/k-agent-biweekly-report-2026-08-11.html)
