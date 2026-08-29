# Agent 递归自进化机制调研与技术方案

> 调研范围：公开论文、研究系统与官方技术文章中的 Agent self-evolution / recursive self-improvement 方案。
> 本文不是对当前项目代码的现状说明，也不假设项目已经实现其中任何机制。
> 调研截止：2026-08-29。

## 1. 结论先行

网上所说的“Agent 递归自进化”，并不是让一个 Agent 无限调用自己，而是建立一个可重复的优化闭环：

```text
任务/环境
  ↓
Agent 执行轨迹
  ↓
可验证反馈（测试、环境状态、评分、人工评价）
  ↓
反思/归纳/候选变更
  ↓
隔离评测
  ↓
保留、淘汰、回滚或进入下一代
```

真正决定系统是否“自进化”的不是是否存在 `while` 循环，而是以下四件事是否同时成立：

1. 有明确的进化对象：记忆、Prompt、Skill、工具、Workflow、Agent 代码或模型参数。
2. 有跨尝试持久化的候选状态，而不是只在当前上下文里自我复述。
3. 有独立于生成 Agent 的反馈或评测机制，能够区分“看起来更好”和“确实更好”。
4. 有选择/晋级/回滚规则，且变更在隔离环境中执行。

当前研究的主流落点是“外部 Agent Scaffold 的可控适应”，即先改变 Prompt、Memory、Skill、Tool 或 Workflow；直接修改基础模型权重的方案已经出现，但成本、稳定性、灾难性遗忘和安全要求显著更高。

## 2. 概念边界

### 2.1 四种容易混淆的机制

| 机制 | 是否跨任务持久化 | 改变什么 | 是否属于自进化 |
|---|---:|---|---:|
| ReAct 循环 | 通常否 | 当前任务的消息和工具调用顺序 | 否，属于执行循环 |
| Self-reflection / retry | 可选 | 当前或下一次尝试的文字策略 | 弱自适应；只有写入长期状态才算进化的雏形 |
| Supervisor 递归拆解 | 是任务状态 | 子任务、依赖、协作路径 | 属于工作流自适应，不等于能力自进化 |
| Evolution / self-improvement | 是 | Agent 的记忆、Prompt、工具、架构、代码或参数 | 是 |

因此，“Supervisor 创建更多子 Agent”本身只是递归编排；只有当系统能从结果中改变未来 Agent 的行为，并在后续任务中验证这种改变，才构成自进化。

### 2.2 进化时间尺度

公开综述通常把机制按发生时间分为：

- **Intra-test-time**：同一任务、同一 rollout 内的重试、反思、工具修复和上下文更新。
- **Inter-test-time**：任务结束后把经验写入 Memory、Skill 库或策略库，供后续任务检索。
- **Training-time / parameter-level**：用经验生成训练数据或更新指令，再执行 SFT、RL、LoRA 等参数更新。

一份 2025 年综述将进化对象归纳为模型、Memory、Tools、Architecture 等组件，并同时讨论标量奖励、文本反馈、单 Agent 与多 Agent 演化方式；2026 年的编码 Agent 综述进一步强调，可执行反馈、仓库级上下文和编码轨迹是软件工程场景的关键条件。[A Survey of Self-Evolving Agents](https://arxiv.org/abs/2507.21046)、[Self-Evolving Coding Agents](https://arxiv.org/abs/2608.03392)

## 3. 典型方案谱系

### 3.1 Reflexion：语言反馈记忆

**核心对象：** episodic textual memory。

**闭环：**

```text
执行任务 → 获得成功/失败反馈 → Agent 生成 verbal reflection
       → 写入情景记忆 → 下一次尝试检索该记忆
```

Reflexion 不更新模型权重，而是把失败原因、改进建议或成功经验写成自然语言，再注入后续尝试。它允许反馈来自外部评分器，也允许来自内部模拟器；因此实现成本低，适合首先为已有 Agent 增加“从错误中学习”的能力。[Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)

**优点：**

- 不需要微调模型。
- 对代码、推理和顺序决策都适用。
- 可以解释某次行为为什么被改变。

**局限：**

- 记忆质量依赖自评，模型可能把错误判断写成“经验”。
- 记忆容易膨胀、重复或互相矛盾。
- 只改变上下文，不改变底层策略；上下文不被召回时能力不再体现。

### 3.2 ReasoningBank：结构化推理经验与测试时扩展

**核心对象：** 从成功和失败轨迹中提炼的可迁移 reasoning strategies。

ReasoningBank 不保存完整轨迹作为唯一记忆，而是让 Agent 对成功/失败经验进行归纳，形成可检索的策略单元；下一次任务先检索相关策略，再把新的经验整合回库。其 MaTTS（memory-aware test-time scaling）进一步把并行探索和顺序 refinement 产生的多条轨迹转化为对比信号和更高质量的记忆。[ReasoningBank 论文](https://arxiv.org/abs/2509.25140)、[Google Research 介绍](https://research.google/blog/reasoningbank-enabling-agents-to-learn-from-experience/)

**与普通 RAG 的区别：**

- 普通 RAG 主要检索外部事实。
- ReasoningBank 检索的是“如何完成任务”的策略。
- 关键写入单元不是原始日志，而是经过判断、抽象和压缩的经验。

**工程启示：**

经验条目应至少包含 `适用条件`、`策略`、`证据`、`失败模式`、`置信度`、`来源任务` 和 `版本`，不能只存一段未经验证的模型总结。

### 3.3 ACE：把上下文当作可演化 Playbook

ACE（Agentic Context Engineering）把系统 Prompt 或长期上下文看作一个不断累积、重组和修订的 playbook。其基本模块是：

```text
Generator：从执行轨迹产生候选经验
Reflector：识别哪些经验值得保留、补充或纠正
Curator：按结构增量写回上下文，避免整体重写
```

ACE 特别针对两个问题：摘要越写越短导致“brevity bias”，以及反复重写上下文造成“context collapse”。它主张结构化增量更新，使上下文可以同时用于离线 Prompt 优化和在线 Agent Memory 更新，并使用自然执行反馈而非必须依赖人工标签。[Agentic Context Engineering](https://arxiv.org/abs/2510.04618)

**工程启示：**

- 经验库不应每轮整体摘要覆盖。
- 追加、合并、废弃和冲突解决应是不同操作。
- 更新后的 Playbook 要保留 lineage，能追溯到原始轨迹和评测结果。

### 3.4 Voyager：自动课程 + 可执行 Skill Library

Voyager 是较完整的“能力积累型”案例。它在 Minecraft 中组合了：

1. 自动课程：不断提出有探索价值的新目标。
2. Skill Library：保存可执行、可组合、可检索的代码技能。
3. 迭代 Prompting：执行代码，收集环境反馈和解释器错误，再生成修正版。
4. Self-verification：由验证 Agent 判断目标是否真的完成。

当验证通过后，程序才进入 Skill Library；如果连续多轮无法完成，则放弃当前目标并请求下一个目标。[Voyager 论文](https://arxiv.org/abs/2305.16291)

这说明一个重要边界：**“生成了新 Skill”不等于“获得了新能力”**。只有执行成功、通过验证并可在相似或新任务中复用，Skill 才应晋级。

### 3.5 Promptbreeder：Prompt 与“变异 Prompt”递归进化

Promptbreeder 进化的不只是任务 Prompt，还进化“如何变异任务 Prompt 的 mutation prompt”：

```text
任务 Prompt P + 变异策略 M
          ↓
      生成 P'
          ↓
     在训练/评测集上评分
          ↓
   保留优秀 P'，并继续改进 M
```

它因此具有 self-referential 特征：系统既优化对象 Prompt，也优化搜索 Prompt 的生成规则。[Promptbreeder](https://arxiv.org/abs/2309.16797)

**关键技术点：**

- 需要 Prompt population，而不是单一当前 Prompt。
- 需要 fitness，而不是只依赖生成模型的主观判断。
- 需要 mutation、selection、diversity 等演化操作。

**主要风险：** 过拟合训练集、评价器偏差、Prompt 变得越来越长、以及为了 benchmark 得分牺牲真实任务可用性。

### 3.6 ADAS：自动设计 Agent Scaffold

ADAS（Automated Design of Agentic Systems）把 Agent 定义为可执行的系统设计，搜索对象可以是 Prompt、工具使用、控制流以及它们的组合。Meta Agent 根据已有候选档案编程出新 Agent，再用目标任务进行评估。[Automated Design of Agentic Systems](https://arxiv.org/abs/2408.08435)

ADAS 把“自进化”从提示词层提升到了架构层：

```text
Agent archive
  ↓ 选择一个候选
Meta Agent 设计/编程新 scaffold
  ↓
隔离运行与评测
  ↓
候选 Agent archive + 新分数
```

它适合研究工具编排、反思模块、搜索策略和多 Agent 结构，但生产化时需要把“可修改代码”和“执行生产任务”严格隔离。

### 3.7 Darwin Gödel Machine：开放式 Agent 代码进化

Darwin Gödel Machine（DGM）是目前最接近通常语境中“递归自我改写”的方案之一。它维护一个 Agent archive：

1. 从档案中选择一个父 Agent。
2. 父 Agent 分析自己的 benchmark 日志，提出下一项改进。
3. Foundation Model 在隔离代码库中实现该改进，生成子 Agent。
4. 运行编码 benchmark，记录分数和修改内容。
5. 把子 Agent 作为新的候选节点保留，并继续从档案中探索。

其“递归”在于 Agent 改进自己的代码，也改进自己未来执行改进的能力；其“开放式”在于不只沿着当前最高分候选做 hill-climbing，而是保留多条 lineage，允许从暂时低分但有潜力的节点继续产生后代。[Darwin Gödel Machine](https://arxiv.org/abs/2505.22954)

论文报告的实验中，DGM 运行 80 轮、并行产生候选，并在 SWE-bench 与 Polyglot 上观察到性能提升；但其 archive maintenance 和 parent selection 在该工作中仍是固定的，不是完全自修改的元系统。这个限制很重要：**“能修改 Agent”不等于“整个演化控制器也已经被证明可靠”。**

### 3.8 AlphaEvolve：自动评估器驱动的算法进化

AlphaEvolve 是 Google DeepMind 公布的算法发现系统。它用 LLM 产生程序候选，以自动 evaluator 验证正确性和质量，再用 evolutionary framework 让后续 Prompt 更常采样有前景的程序。[AlphaEvolve 官方介绍](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/)

其最值得借鉴的不是“LLM 生成代码”，而是：

- 目标必须能转化为可计算的评价指标。
- 正确性和性能同时纳入 evaluator。
- 候选程序和分数进入持久化程序数据库。
- 通过不同能力的模型组合探索广度和深度。
- 人类可以检查可读、可调试、可部署的候选结果。

这类方案非常适合算法、编译器、调度、SQL、测试生成等“有可靠执行评价器”的领域；不适合把开放式审美或复杂社会偏好压缩成单一分数。

### 3.9 Agent0：课程 Agent 与执行 Agent 的共进化

Agent0 使用两个相互施压的角色：

- Curriculum Agent：提出越来越困难、接近能力边界的任务。
- Executor Agent：使用工具解决这些任务。

执行能力变强会迫使课程 Agent 提出更难、更具工具意识的任务；课程变难又为执行 Agent 提供更强的训练信号。这个闭环不是单 Agent 自我反思，而是“任务分布”和“求解策略”的共同进化。[Agent0](https://arxiv.org/abs/2511.16043)

**工程启示：** 如果任务永远简单，系统只能学会重复；要产生持续进步，必须有课程生成、难度控制和防止课程崩坏的评测。

### 3.10 SEAL / TMEM：参数级自适应

SEAL（Self-Adapting Language Models）让模型自己生成两类内容：

- 用于更新的 finetuning data。
- 描述如何进行更新的 self-edit / update directive。

随后通过 SFT 等方式执行持久权重更新，并用下游性能作为 RL 信号训练模型生成更有效的 self-edit。[SEAL 论文](https://arxiv.org/abs/2506.10943)

2026 年出现的 TMEM 则探索把经验蒸馏进快速 LoRA 权重，使同一 episode 后续行为真正由 `θ + Δ` 驱动，而不只是从文本 Memory 中检索经验。[TMEM](https://arxiv.org/abs/2606.04536)

这类机制才是“参数层面的学习”，但并不意味着已经适合普通 Agent 产品：它要求训练执行器、GPU/更新预算、模型版本管理、回归集和灾难性遗忘控制；更新错误还可能永久污染后续行为。

## 4. 共性技术架构

### 4.1 六层分解

```text
┌──────────────────────────────────────────────┐
│ 1. Base Model                                │ 固定或可更新的模型参数
├──────────────────────────────────────────────┤
│ 2. Agent Scaffold                            │ Prompt、Policy、Workflow、Router
├──────────────────────────────────────────────┤
│ 3. Capability Layer                          │ Tool、Skill、代码库、插件
├──────────────────────────────────────────────┤
│ 4. Experience Layer                          │ Trajectory、Memory、ReasoningBank
├──────────────────────────────────────────────┤
│ 5. Evaluator / Verifier                      │ 测试、模拟器、规则、模型裁判、人类
├──────────────────────────────────────────────┤
│ 6. Evolution Controller                      │ 采样、变异、选择、晋级、回滚、预算
└──────────────────────────────────────────────┘
```

最容易落地的是第 3、4 层；最难也最危险的是第 1、2、6 层，因为它们会改变系统行为本身。

### 4.2 标准候选对象

建议把每个可进化对象保存成不可变版本，而不是原地覆盖：

```json
{
  "candidateId": "cand_01J...",
  "parentId": "cand_01H...",
  "generation": 7,
  "kind": "memory|prompt|skill|workflow|agent_code|weights",
  "payloadUri": "artifact://evolution/cand_01J...",
  "baseModel": "model-id@revision",
  "mutation": {
    "operator": "add_rule",
    "description": "在工具失败后增加参数校验步骤"
  },
  "evaluation": {
    "suite": "agent-regression-v3",
    "scores": {
      "taskSuccess": 0.82,
      "safety": 1.0,
      "cost": 0.71,
      "latency": 0.66
    },
    "passedGates": true
  },
  "status": "candidate|verified|staged|active|rejected|rolled_back",
  "createdAt": "..."
}
```

### 4.3 评测与选择

单一成功率不够。一个可用的综合目标可以写成：

```text
fitness =
  w_s * task_success
  + w_q * quality
  + w_g * generalization
  + w_r * reliability
  - w_c * normalized_cost
  - w_l * normalized_latency
  - w_h * safety_violations
```

其中安全违规不是普通负分，而应当是硬门禁：

```text
if safety_gate_failed: reject
elif regression_gate_failed: reject
elif holdout_score < parent_score - tolerance: reject
elif candidate_score > parent_score + promotion_margin: promote
else: keep as exploratory branch
```

评测集至少应拆成：

- `train / search set`：用于产生和筛选候选。
- `holdout set`：防止候选只记住搜索任务。
- `regression set`：保障既有能力不退化。
- `adversarial / safety set`：检查越权、数据泄露、危险工具调用和提示注入。
- `fresh tasks`：验证新任务迁移能力。

### 4.4 为什么必须保留 Archive

只保留最新候选会形成脆弱的 hill-climbing：一次错误更新可能破坏 Agent 的自修改能力，后续也无法回到稳定祖先。DGM 的公开结果特别强调了从非最优节点继续探索的价值：暂时低分的分支可能包含后来可组合的关键创新。[DGM 论文中的实验分析](https://arxiv.org/abs/2505.22954)

因此 Archive 应支持：

- 父子 lineage。
- 按分数、成本、风险和能力标签检索。
- 精英保留（保留少量高质量候选）。
- 多样性保留（避免所有候选收敛到同一种策略）。
- 快速回滚到任意已验证版本。
- 记录“为什么这个候选被保留/淘汰”。

## 5. 递归闭环的参考伪代码

```python
async def evolve(problem, budget):
    archive = await load_archive(problem.domain)
    parent = select_parent(
        archive,
        strategy="quality_plus_diversity",
        exclude_status={"quarantined"},
    )

    proposal = await proposer.propose(
        parent=parent,
        trajectory_logs=await load_recent_evidence(parent),
        failure_patterns=await summarize_failures(parent),
    )

    candidate = await materialize_in_isolated_workspace(
        parent=parent,
        proposal=proposal,
    )

    static_result = await run_static_and_policy_checks(candidate)
    if not static_result.passed:
        return await reject(candidate, reason=static_result.reason)

    evaluation = await evaluator.run(
        candidate,
        suites=["search", "holdout", "regression", "safety", "fresh"],
        budget=budget.evaluation,
    )

    if not passes_hard_gates(evaluation):
        return await reject(candidate, evaluation=evaluation)

    await archive_candidate(candidate, evaluation=evaluation)

    if should_promote(parent, candidate, evaluation):
        await stage_for_canary(candidate)
        return await promote_or_rollback(candidate, canary_window=budget.canary)

    return candidate
```

这段伪代码里最重要的不是 `proposer`，而是 `static checks`、`evaluator`、`archive`、`hard gates` 和 `promote_or_rollback`。没有它们，所谓自进化只是模型自己生成一段新的配置。

## 6. 安全与可靠性边界

### 6.1 自修改权限分级

建议从低到高分为四级：

| 等级 | 可修改内容 | 默认策略 |
|---|---|---|
| L0 | 当前轮临时消息/草稿 | 自动允许，不持久化 |
| L1 | 经验条目、失败模式、检索索引 | 自动写入，但必须可删除、可追溯 |
| L2 | Prompt、Skill、Workflow、Tool schema | 隔离评测后 canary；生产启用需人工或策略审批 |
| L3 | Agent 代码、运行时、模型权重、权限策略 | 默认禁止自动生产晋级，必须人工审批 |

### 6.2 必须隔离的资源

- 候选代码执行环境与生产工作区。
- 候选 Agent 的网络、凭据和 MCP 权限。
- 评测任务和候选可修改的测试代码；否则候选可能通过篡改测试获得高分。
- 生成器、评估器和被评估 Agent 的身份与权限。
- 候选 Artifact 与当前 active 版本。

### 6.3 评价器不能完全由被评价 Agent 自己担任

Self-verification 很有价值，但不能成为唯一质量门。被评价 Agent 可能：

- 把部分完成误判成成功。
- 选择性隐藏错误。
- 通过提示注入影响裁判。
- 生成看似合理但不可执行的证明。

推荐至少使用“执行事实 + 独立规则/测试 + 模型裁判”的组合；高风险场景再加入人工抽检或双模型交叉评价。

### 6.4 防止递归失控

递归维度至少有四个，不应只限制调用深度：

```text
depth             子 Agent / 子任务层数
branching         每个候选产生的后代数量
iterations        演化代数
resource_budget   token、时间、GPU、工具调用、网络和费用
```

还需要：

- 单个候选的最大执行时间。
- 单个父节点的最大后代数。
- 全局并发上限。
- 失败候选冷却和 quarantine。
- 递归事件的幂等 ID。
- 任何 active 版本的可逆发布。

## 7. 方案对比与选型建议

| 方案 | 进化对象 | 反馈来源 | 适合场景 | 实现难度 | 主要风险 |
|---|---|---|---|---:|---|
| Reflexion | 文字经验 | 外部/内部反馈 | 快速增加自修复 | 低 | 错误记忆、上下文膨胀 |
| ReasoningBank | 推理策略 Memory | 成功/失败轨迹 | 长期重复任务 | 中 | 检索污染、策略泛化误判 |
| ACE | Context Playbook | 执行反馈 | Prompt/记忆持续优化 | 中 | 结构漂移、上下文崩塌 |
| Voyager | 可执行 Skill | 环境、错误、验证器 | 有模拟器或代码执行器的领域 | 中高 | Skill 污染、环境依赖 |
| Promptbreeder | Prompt + mutation Prompt | 任务集 fitness | Prompt 优化、分类、推理 | 中 | benchmark 过拟合 |
| ADAS | Agent scaffold | 多任务评测 | 自动发现工作流/工具编排 | 高 | 生成代码越权、评测成本 |
| DGM | Agent 代码 | 编码 benchmark | 编码 Agent 自我改进研究 | 很高 | 代码自修改失控、回归 |
| AlphaEvolve | 算法/程序 | 自动 evaluator | 可计算目标的算法发现 | 很高 | 指标投机、领域迁移 |
| Agent0 | 课程 + 执行策略 | 工具执行与难度进展 | 推理训练、零外部数据探索 | 高 | 课程崩坏、共适应 |
| SEAL/TMEM | 权重/快速权重 | 下游任务表现 | 真正参数级适应 | 极高 | 遗忘、训练污染、成本 |

## 8. 推荐的落地路线

### 阶段 1：可验证的任务内自修复

目标不是改系统，而是把失败变成下一轮可用的结构化输入：

- 工具失败 → 错误分类 → 参数修正/换工具/请求人工。
- 任务失败 → 生成短反思 → 仅作用于当前重试。
- 记录完整执行轨迹、工具结果、成本和最终质量。

验收：同一任务的第二次尝试成功率提高，且错误没有被静默吞掉。

### 阶段 2：持久化 Reasoning Memory

新增独立 Memory Service 或数据表，建议字段：

```text
memory_id, scope, trigger, strategy, evidence, confidence,
source_run_id, source_task_id, created_generation, supersedes,
status, last_used_at, success_count, failure_count
```

写入需要经过 extractor/curator；读取需要按任务触发条件检索；Memory 条目更新不应直接覆盖原文。

验收：跨任务复用策略有效，并在 holdout 任务上不低于无 Memory 基线。

### 阶段 3：Skill / Workflow 候选进化

- Agent 可以提出新 Skill 或 Workflow patch。
- 候选写入隔离 Artifact，不直接覆盖 active 版本。
- 自动生成针对性测试和回归测试。
- 通过 evaluator 后进入 canary。
- canary 期间同时记录新旧版本的 success、cost、latency、安全违规。

验收：候选在 holdout、regression 和 safety 集合全部过门禁，且 canary 指标达到晋级阈值。

### 阶段 4：多候选 Archive 与开放式搜索

只有当阶段 3 具备稳定评测和回滚能力后，才引入：

- parent selection。
- mutation operator。
- 多分支 lineage。
- diversity 保留。
- 预算感知的并行评测。

这一步才接近 Promptbreeder、ADAS 和 DGM 的搜索范式。

### 阶段 5：参数级更新

仅在拥有稳定数据治理、训练资源、模型注册和灾难恢复能力时考虑 SEAL/TMEM 类路线。参数更新必须与普通在线 Agent 执行隔离，至少支持：

- base checkpoint 不可变。
- adapter/LoRA 独立版本。
- 更新前后固定回归集比较。
- 旧版本即时回滚。
- 训练数据来源与授权审计。

## 9. 建议的最小生产协议

一个可审计的 `EvolutionProposal` 至少包含：

```json
{
  "proposalId": "proposal_...",
  "parentVersion": "agent-scaffold@42",
  "scope": "skill|memory|workflow|prompt",
  "problemEvidence": ["run_...", "eval_..."],
  "change": "...",
  "riskAssessment": {
    "newTools": [],
    "newPermissions": [],
    "networkChange": false,
    "filesystemChange": false
  },
  "evaluationPlan": {
    "suites": ["holdout", "regression", "safety"],
    "promotionMargin": 0.03
  },
  "approvalPolicy": "auto_canary_then_human",
  "rollbackTo": "agent-scaffold@42"
}
```

状态建议：

```text
proposed → materialized → checked → evaluated → staged → canary → active
     └──────────────→ rejected
                                      └→ rolled_back
```

任何状态变更都写 append-only event；candidate payload、评测结果和运行日志使用不可变 Artifact 引用；active 指针单独存储并支持原子切换。

## 10. 最终判断

目前网上最成熟、最值得工程化的路径不是立即让 Agent 修改自身代码或权重，而是：

```text
结构化执行轨迹
  → 可验证失败/成功信号
  → 受控经验归纳
  → 检索增强下一次执行
  → Skill / Workflow 候选
  → 隔离评测与 canary
  → Archive、晋级、回滚
```

其中，Reflexion/ReasoningBank/ACE 解决“记住并改进策略”，Voyager 解决“把成功行为固化为可组合 Skill”，Promptbreeder/ADAS/DGM 解决“搜索并改变 Agent scaffold”，AlphaEvolve 解决“在可计算评价器中进化程序”，SEAL/TMEM 才进一步触及“改变模型参数”。

所以，针对一般 Agent 产品，推荐把“递归自进化”定义为**可验证候选版本的持续产生与选择**，而不是定义为“Agent 可以递归调用 Agent”。递归调用只是执行结构；版本、证据、评测、选择和回滚才是进化机制。

## 参考资料

1. [A Survey of Self-Evolving Agents: On Path to Artificial Super Intelligence](https://arxiv.org/abs/2507.21046)
2. [Self-Evolving Coding Agents](https://arxiv.org/abs/2608.03392)
3. [Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)
4. [Voyager: An Open-Ended Embodied Agent with Large Language Models](https://arxiv.org/abs/2305.16291)
5. [Promptbreeder: Self-Referential Self-Improvement Via Prompt Evolution](https://arxiv.org/abs/2309.16797)
6. [Automated Design of Agentic Systems](https://arxiv.org/abs/2408.08435)
7. [Darwin Gödel Machine: Open-Ended Evolution of Self-Improving Agents](https://arxiv.org/abs/2505.22954)
8. [Agentic Context Engineering](https://arxiv.org/abs/2510.04618)
9. [ReasoningBank: Scaling Agent Self-Evolving with Reasoning Memory](https://arxiv.org/abs/2509.25140)
10. [Agent0: Unleashing Self-Evolving Agents from Zero Data via Tool-Integrated Reasoning](https://arxiv.org/abs/2511.16043)
11. [Self-Adapting Language Models](https://arxiv.org/abs/2506.10943)
12. [Scaling Self-Evolving Agents via Parametric Memory](https://arxiv.org/abs/2606.04536)
13. [AlphaEvolve: A Gemini-powered coding agent for designing advanced algorithms](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/)
14. [The AI Scientist: Towards Fully Automated Open-Ended Scientific Discovery](https://sakana.ai/ai-scientist/)
