# k_agent 前端页面设计评审与改造方案

> 文档类型：前端视觉系统与交互架构技术方案  
> 评审范围：`frontend/src`、全局样式、README 产品能力说明  
> 评审日期：2026-08-24  
> 当前结论：功能能力已达到完整工作台形态，下一阶段重点应从“继续增加能力”转向“统一视觉系统、收敛信息架构、降低复杂度”。

---

## 1. 背景与目标

k_agent 当前已经包含以下产品能力：

- 单 Agent 实时对话与流式输出
- Thinking、Tool Call、Approval、Timeline 等执行过程展示
- Session 历史与后台运行状态保留
- Workspace 文件树与 Markdown、HTML、Code、SVG、Table 预览
- Agent Team：Supervisor、Worker、Task DAG、Mailbox、Artifact、审批
- Automation：一次、每日、每周定时执行与历史结果
- Model、MCP、Skills、Voice 配置中心
- 多主题、字体大小调节、语音交互、桌面宠物

当前主要问题不是缺少页面，而是页面之间的产品语义、视觉 token 和复杂能力的默认呈现方式尚未完全统一。

本方案目标：

1. 建立统一的语义化视觉 token，解决主题和局部硬编码冲突。
2. 建立清晰的 Work / Manage 信息架构，降低首次使用成本。
3. 将 Chat、Agent Team、Automation、Config Center 区分为不同任务心智模型。
4. 对 Trace、Workspace、Composer 等复杂区域实施渐进披露。
5. 为桌面端和窄屏端分别定义稳定的布局规则。
6. 给出可执行的分阶段改造顺序和验收标准。

---

## 2. 当前设计结论

### 2.1 总体评价

| 维度 | 评价 | 说明 |
| --- | --- | --- |
| 视觉完成度 | 7.5 / 10 | 已有品牌方向、主题系统和工作台骨架 |
| 信息架构 | 7 / 10 | 能力覆盖完整，但一级产品边界仍然混合 |
| 视觉一致性 | 6 / 10 | token 已存在，但局部暗色样式仍大量硬编码 |
| 复杂任务可用性 | 6.5 / 10 | 执行信息很完整，但默认信息密度偏高 |
| 产品化成熟度 | 6 / 10 | 更像高级工程师工作台，尚未完全收敛为正式产品 |

### 2.2 当前优势

#### A. 三栏工作台结构合理

当前结构基本符合 Agent 产品的执行闭环：

```text
左侧：Session / 工作模式 / 历史
中间：对话 / 任务执行 / Composer
右侧：Run Details / Workspace / Artifact
```

这个结构比单纯模仿聊天产品更适合 k_agent。右侧区域同时承载过程可观测和产物预览，说明产品已经围绕“任务执行”而不是“消息发送”设计。

#### B. 产品视觉方向明确

当前视觉语言具有较强一致性基础：

- 极简黑白和低饱和主题
- 细边框、小圆角、轻阴影
- Mono 字体承载路径、状态、技术字段
- 展示字体用于品牌和页面标题
- 绿色、蓝色、橙色用于执行状态和主题强调
- 首页网格和渐变氛围强化“执行台”定位

不建议当前阶段整体换风格，应优先做 token 和信息架构收敛。

#### C. 复杂执行状态已经有对应承载

`App.tsx`、`ConversationTranscript.tsx` 和 `ContentStage.tsx` 已覆盖：

- 文本输出
- 思考过程
- 工具调用
- 审批卡片
- 外部动作
- 文件树
- 预览内容
- 运行轨迹

这是当前最有价值的产品设计资产，应在后续改造中复用，而不是推翻重做。

---

## 3. 问题清单与根因

## 3.1 P0：视觉 token 与局部硬编码冲突

### 证据

全局样式已经定义多主题变量：

```css
:root[data-theme="snow"]
:root[data-theme="ink"]
:root[data-theme="blueprint"]
:root[data-theme="forest"]
:root[data-theme="ocean"]
:root[data-theme="sand"]
:root[data-theme="dusk"]
```

但核心组件仍存在大量具体颜色：

```css
.bubble {
  color: #bdc2bc;
  background: #141715;
}

.bubble pre,
.assistant-output pre {
  background: #0c0e0d;
}

.config-topbar,
.config-nav {
  background: #101210;
}

.picker-popover {
  background: rgba(23,26,24,.98);
}
```

### 根因

主题变量与组件样式并非同一层级的设计系统：

- 主题 token 负责页面骨架
- 局部组件保留了早期深色设计的具体值
- 后续通过选择器覆盖进行补救
- 同一种 surface 在不同组件中没有统一语义

### 影响

- 亮色主题下局部仍保持深色，造成视觉断裂
- `ConfigCenter`、聊天气泡、Popover、Inspector 的主题行为不一致
- 新增组件时容易继续复制具体颜色
- 设计调整需要修改大量散落 CSS

### 改造方案

新增语义化 surface 和文字 token，组件禁止直接使用具体颜色：

```css
:root {
  --surface-page: #ffffff;
  --surface-sidebar: #f9f9f9;
  --surface-panel: #ffffff;
  --surface-raised: #ffffff;
  --surface-control: #ffffff;
  --surface-hover: #f0f0f0;
  --surface-active: #ececec;
  --surface-chat-assistant: #f7f7f7;
  --surface-chat-user: #f4f4f4;
  --surface-code: #f1f1f1;
  --surface-overlay: rgba(255, 255, 255, .96);

  --text-primary: #0d0d0d;
  --text-secondary: #676767;
  --text-tertiary: #8e8e8e;
  --text-inverse: #ffffff;

  --border-default: #e5e5e5;
  --border-subtle: #efefef;
  --border-strong: #cfcfcf;

  --status-success: #3c9a64;
  --status-warning: #bd7c18;
  --status-danger: #b95852;
  --status-running: #2b59ff;
}
```

暗色主题只覆盖 token：

```css
:root[data-theme="ink"] {
  --surface-page: #000000;
  --surface-sidebar: #0d0d0d;
  --surface-panel: #141414;
  --surface-raised: #222222;
  --surface-control: #141414;
  --surface-hover: #1c1c1c;
  --surface-active: #262626;
  --surface-chat-assistant: #121212;
  --surface-chat-user: #2a2a2a;
  --surface-code: #0c0e0d;
  --surface-overlay: rgba(20, 20, 20, .96);

  --text-primary: #f5f5f5;
  --text-secondary: #a3a3a3;
  --text-tertiary: #737373;
  --text-inverse: #000000;

  --border-default: #2e2e2e;
  --border-subtle: #1f1f1f;
  --border-strong: #4a4a4a;
}
```

### 验收标准

- 切换任意主题后，Chat、Team、Automation、Config、Inspector、Popover 均使用同一套 surface 语义。
- CSS 组件层不再新增 `#101210`、`#141715`、`#0c0e0d` 等具体页面颜色。
- 亮色主题截图中不得出现无产品语义的深色孤岛。
- 通过主题矩阵截图验证：`snow`、`ink`、`blueprint` 至少各验证一次。

---

## 3.2 P0：一级导航没有形成清晰产品模型

### 当前问题

左侧同时出现：

- 对话
- Team
- 新会话
- 定时任务
- 最近会话
- 配置中心

但用户不容易立即理解：

- Team 是一种会话还是独立工作台？
- Automation 是一级能力还是特殊 Session？
- Workspace 是右侧辅助区还是独立工作空间？
- 配置中心为什么和工作模式混在同一导航层级？

### 建议的信息架构

```text
WORK
  对话
  Agent Teams
  自动化

MANAGE
  配置中心
```

左侧建议采用明确分组：

```text
WORK
  ◇ 对话
  ◆ Agent Teams
  ◷ 自动化

MANAGE
  ⚙ 配置中心
```

其中：

- **对话**：单 Agent 的即时执行入口
- **Agent Teams**：持久化的多 Agent 协作工作台
- **自动化**：无人值守、可重复运行的任务
- **配置中心**：模型、MCP、Skills、语音等系统能力管理

### 迁移原则

- 不改变现有 API 和路由行为
- 先调整导航文案、分组和选中态
- Team 和 Automation 继续复用现有页面组件
- 配置中心保留独立页面，只改变入口归属
- Session 列表只展示 Chat Session，不与 Team / Automation 混排

### 验收标准

新用户不看文档，仅通过左侧导航即可回答：

1. 我在哪里进行普通对话？
2. 我在哪里创建多 Agent 团队？
3. 我在哪里设置定时任务？
4. 我在哪里配置模型和 MCP？

---

## 3.3 P1：复杂能力默认暴露过多

### 主要区域

- 顶部工具栏
- Composer 底部控制区
- Trace / Inspector
- Team 成员配置
- Config Center 高级字段

当前的设计倾向是把能力直接放在当前视图中，这对熟悉系统的工程师有帮助，但会增加首次使用成本。

### 建议：渐进披露

#### 顶部工具栏

始终显示：

- 当前 Session / Team 名称
- 运行状态
- Trace / Workspace 入口

更多菜单收纳：

- 主题
- 字体大小
- 桌面宠物
- 其他显示偏好

#### Composer

默认显示一条当前运行摘要：

```text
K Agent · GPT-4.1 · 默认权限
```

点击后再展开：

- Agent
- Model
- Permission Mode
- MCP
- Skills
- Runtime

#### Inspector

将信息分成两类：

```text
执行
  摘要
  思考
  工具
  时间线

产物
  文件
  预览
```

已完成工具默认折叠；运行中、失败、等待审批的活动才高显著度展示。

### 验收标准

- 首次进入 Chat 时，用户无需理解 MCP、Skill、Permission 等术语即可发送任务。
- 常用任务路径不因高级配置控件增加额外点击。
- 运行中、失败、等待审批的状态在 1 秒内可被识别。
- 已完成的工具活动不会占据主要视觉焦点。

---

## 3.4 P1：字号层级偏小，中文长时间阅读负担较高

### 当前 token

```css
--type-micro: 9–11px;
--type-small: 10–13px;
--type-ui: 12–16px;
--type-body: 13–17px;
--type-title: 16–20px;
--type-display: 22–28px;
```

### 问题

实际大量辅助内容落在小字号范围：

- Session 副标题
- Tool 状态
- Trace 元数据
- Team 卡片辅助说明
- 文件路径和大小
- Config Center 说明

### 建议

调整为更少、更稳定的角色：

```css
--type-micro: 11px;
--type-meta: 12px;
--type-ui: 14px;
--type-body: 15px;
--type-title: 18px;
--type-display: 28px;
```

规则：

- 路径、序号、调试字段可使用 `micro`
- 用户需要阅读的说明使用 `meta` 或 `ui`
- 中文正文默认不低于 14px
- 状态标签与按钮文字不低于 12px
- 只在高密度 Trace 内允许使用 11px

### 验收标准

- Chat 正文、Team 描述、Config 说明在 1440px 桌面截图上清晰可读。
- 100% 浏览器缩放下，辅助文字不出现明显发灰或难以辨识。
- 字体调节仍保持原有 `--fs-delta` 能力，不产生布局溢出。

---

## 3.5 P1：Agent Team 已是独立产品，但导航身份不足

`TeamWorkbench.tsx` 和对应样式已经包含：

- 团队创建
- Supervisor / Worker
- 任务依赖
- 成员能力
- 任务板
- Mailbox
- Artifact
- Timeline
- Workspace
- 审批

这已经超过“Chat 的一个模式切换”。

### 建议

进入 Team 后使用独立的工作台标题和区域导航：

```text
Agent Teams
  团队列表
  当前团队
  任务板
  成员
  产物
  事件
```

顶部应明确显示：

```text
AGENT TEAM
团队名称 · 运行状态 · 进度
```

任务板、成员、产物、事件应通过二级 tab 或面板标题区分，而不是全部依赖卡片和滚动。

### 验收标准

- 用户能明显感知当前已经进入 Team 工作台，而不是普通 Chat。
- 团队状态、任务状态、Agent 状态、Artifact 状态有稳定的视觉分层。
- Team 页面在 1100px 以下仍能保持任务板优先，而不是简单压缩所有列。

---

## 3.6 P1：响应式更像桌面布局收缩，不是移动端信息架构

当前已有 `760px`、`820px`、`900px`、`1100px` 等断点，但核心策略仍然是减少列数和隐藏部分区域。

### 建议布局规则

#### 桌面端，宽度 >= 1100px

```text
Sidebar 240–280px | Main flexible | Inspector 320–380px
```

#### 中等宽度，720px–1099px

```text
Main 单列
Sidebar 作为 drawer
Inspector 作为 drawer
```

#### 窄屏，< 720px

```text
顶部：工作区标题 + 菜单
正文：消息 / 任务内容
底部：Composer
抽屉：Session、Trace、Workspace
```

### 具体调整

- Sidebar 不再参与主网格压缩，改为 overlay drawer。
- Inspector 改为 full-height drawer 或 bottom sheet。
- Config Center 左侧导航改为横向 tab 或下拉选择。
- Team 页面以任务列表优先，成员和 Artifact 改为抽屉。
- Composer 底部工具栏允许横向滚动，但默认只显示主要操作。

### 验收标准

至少验证以下尺寸：

- 1440 × 960：完整三栏工作台
- 1024 × 768：主内容优先，Inspector 可抽屉化
- 768 × 1024：单列工作区
- 390 × 844：移动端 Composer、抽屉和长文本可用

---

## 3.7 P2：首页可以从欢迎页升级为任务派发页

当前欢迎页已经具备品牌和建议任务，但中间区域仍偏空，能力表达不足。

### 建议结构

```text
K Agent
今天要完成什么？
把目标交给 Agent，过程和产物都会保留下来。

[输入任务]

常用入口
[分析代码库] [创建执行计划] [检查 MCP / Skills] [启动 Agent Team]

最近工作
[最近会话 / 最近产物]
```

### 注意

首页不建议做成传统 Dashboard，不要堆指标卡片。首页的作用是任务分流，不是展示所有系统状态。

### 验收标准

- 新用户知道下一步可以做什么。
- 首页仍保持留白和品牌感。
- 常用入口不超过 4 个。
- 最近工作只展示少量高相关内容，不变成第二个 Session 列表。

---

## 4. 推荐的视觉 token 结构

建议将当前 CSS token 分为五组：

```text
1. Color
   page / sidebar / panel / control / chat / code / overlay
   text-primary / secondary / tertiary / inverse
   border-subtle / default / strong
   status-running / success / warning / danger

2. Typography
   micro / meta / ui / body / title / display

3. Spacing
   space-1: 4px
   space-2: 8px
   space-3: 12px
   space-4: 16px
   space-5: 20px
   space-6: 24px
   space-8: 32px
   space-10: 40px

4. Radius
   radius-control: 8px
   radius-card: 12px
   radius-panel: 16px
   radius-pill: 999px

5. Elevation
   shadow-popover
   shadow-card
   shadow-drawer
```

组件只引用语义 token，不再直接维护自己的颜色体系。

---

## 5. 推荐实施顺序

## Phase 1：视觉基础收敛

范围：`frontend/src/styles.css`

1. 建立语义化 surface、text、border、status token。
2. 将聊天气泡、代码块、Popover、Config、Inspector 替换为 token。
3. 删除或收敛重复的主题覆盖规则。
4. 统一按钮、输入框、卡片、Popover 的边框和圆角。
5. 调整字号角色，保留 `--fs-delta` 能力。

交付物：

- 一套统一 token
- `snow` / `ink` / `blueprint` 三套主题通过视觉验收
- 不改变业务交互

## Phase 2：导航和页面心智模型

范围：`App.tsx`、`TeamWorkbench.tsx`、`ConfigCenter.tsx`、相关 CSS

1. 将左侧导航分为 Work / Manage。
2. 将 Chat Session 与 Team / Automation 入口分开。
3. 强化 Team 工作台标题、状态和二级区域。
4. 将 Config Center 明确归入 Manage。
5. 保持现有 API、状态和路由逻辑不变。

交付物：

- 新用户可理解的一级导航
- Chat / Team / Automation / Config 的入口边界清晰

## Phase 3：渐进披露与复杂度治理

范围：`App.tsx`、`ConversationTranscript.tsx`、`ContentStage.tsx`

1. 顶部显示设置收进更多菜单。
2. Composer 默认显示运行配置摘要。
3. 已完成 Tool / Thinking 默认折叠。
4. Trace 内部拆分执行与产物语义。
5. 运行中、失败、审批状态做高优先级视觉反馈。

交付物：

- 首次使用路径更简单
- 高级能力仍然可访问
- 运行信息可观测性不下降

## Phase 4：响应式和视觉回归

1. 建立桌面、中等宽度、移动端三套布局策略。
2. 对 Chat、Team、Automation、Config 分别截图验证。
3. 对主题矩阵做回归。
4. 对长文本、长路径、长工具输出做溢出验证。
5. 对运行中、失败、审批、空状态、加载态做状态截图。

---

## 6. 技术验收清单

### 6.1 视觉系统

- [ ] 所有页面使用同一套语义化颜色 token。
- [ ] 不存在亮色主题下无语义的深色组件孤岛。
- [ ] 卡片、Popover、Drawer、Input、Button 的圆角层级统一。
- [ ] 中文正文默认字号不低于 14px。
- [ ] 状态颜色有统一含义，不在组件内随意新增颜色。

### 6.2 信息架构

- [ ] Work / Manage 一级分组明确。
- [ ] Chat、Team、Automation、Config 四类入口可直接区分。
- [ ] Session 列表不与 Team、Automation 混排。
- [ ] Team 页面具有独立的工作台身份。
- [ ] Trace 与 Workspace 的职责边界清晰。

### 6.3 交互层级

- [ ] 首次进入 Chat 时无需理解高级配置即可发送任务。
- [ ] Composer 高级配置支持展开和收起。
- [ ] 已完成工具默认低显著度展示。
- [ ] 运行中、失败、审批状态优先级明确。
- [ ] 高级设置可访问但不抢占主任务空间。

### 6.4 响应式

- [ ] 1440px 桌面三栏布局稳定。
- [ ] 1024px 下 Inspector 可抽屉化。
- [ ] 768px 下主内容不被侧栏挤压。
- [ ] 390px 下 Composer、Drawer、长文本均可操作。
- [ ] 不出现横向滚动页面或不可关闭的浮层。

### 6.5 可观测性

- [ ] Tool Call、Thinking、Approval、Timeline 仍可追溯。
- [ ] 运行失败原因不因折叠而丢失。
- [ ] Workspace 产物和执行轨迹可以快速切换。
- [ ] 后台运行状态在 Session、顶部状态和 Trace 中保持一致。

---

## 7. 不建议当前阶段做的事情

当前不建议：

- 重新更换整体品牌风格。
- 继续增加更多主题。
- 为每个页面设计一套独立视觉语言。
- 继续增加首页装饰元素。
- 增加更多状态颜色。
- 将所有 Agent、MCP、Skill 能力都做成显眼入口。
- 在视觉 token 未统一前继续大规模扩展新页面。

优先策略应是：

> **先统一，再分层；先降低默认复杂度，再增强高级能力。**

---

## 8. 最终建议

最值得优先落地的三个改动：

1. **统一语义化视觉 token**：解决主题不一致和局部深色硬编码问题。
2. **重构左侧导航**：建立 Work / Manage 信息架构，明确 Chat、Team、Automation、Config 的产品边界。
3. **实施渐进披露**：收敛 Composer、Trace、顶部控制和 Team 高级字段，让主任务成为视觉焦点。

完成这三项后，k_agent 将从“能力强的工程师内部工具”进一步收敛为“可长期使用的 Agent 工作台产品”。
