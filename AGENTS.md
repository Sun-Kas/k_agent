# K Agent 开发约定

- `frontend/` Web 和 `cli/` 中启动 `k-agent` 后的终端交互页面是同一产品的两个客户端。Web 有的功能、必要信息、可见状态和行为语义，CLI 也必须具备；终端布局可按键盘操作适配，网页底部终端不能代替 CLI 页面。
- 用户已明确排除 CLI 语音输入、麦克风、听写、连续语音输入、文字朗读，以及 CLI 桌宠；不要为了双端一致而添加这些功能或请求授权。Web 现有语音功能和桌宠保留。
- 新增或修改任一端的功能时，必须逐项检查并同步另一端，包含公开协议、client、application、页面、状态、错误和审批流程。不得只补最近的功能而忽略现有功能。
- 同时维护 `docs/features/web-cli-parity.md`、`docs/features/web-cli-api-map.json` 和 `cli/src/application/workbench-catalog.ts`；新增 Web API 导出会触发 CLI 对照测试。清单必须明确实际交付和真实缺口，入口或 mock 通过不能写成端到端验证通过。
- 两端共同验证：运行 CLI 类型检查、交互和协议测试及构建；运行 Web 边界检查、类型检查及构建；行为涉及后端时运行相应后端测试。报告实际执行结果和平台能力限制。
- 保持 Access Layer 与 Agent Backend 独立，通过 HTTP 通信。客户端不得读取服务端会话文件或自行决定 Session cwd；使用服务端 `SessionEnvironment`。
- 非显然的业务契约、架构边界、安全约束、并发和生命周期行为应有准确、简洁的中文注释。
- 不提交凭据、`.env` 或 `.k_agent/`。未经用户授权不提交或推送。
