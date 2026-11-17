# Reflexion、OpenJudge、Claw-Eval 与 AI-Infra-Guard 项目分析

核查日期：2026-11-16。根据公开仓库、官方文档、原论文和关键源码分析；本次未安装或运行这些项目，因此不对实际部署成功率、检测准确率或榜单成绩做独立背书。

## 核心结论

四者分别覆盖 Agent 的能力改进、质量评价、任务基准和安全测试，解决的问题不同。

| 项目 | 主要完成的工作 | 典型产物 |
| --- | --- | --- |
| Reflexion | 建立执行、评价、反思、记忆和重试机制 | 改进后的任务结果、文本经验 |
| OpenJudge | 统一评分器、评分标准、批量评测、分析和奖励对接 | 分数、排名、解释、聚合结果或训练奖励 |
| Claw-Eval | 提供真实工作流任务、可控执行环境、过程审计与多维评分 | 执行轨迹、审计证据、任务得分、重复运行指标 |
| A.I.G / AI-Infra-Guard | 汇集 AI 基础设施、MCP、Skill、Agent 和模型安全测试 | 漏洞和风险报告、严重程度、修复建议 |

具体证据及来源见各项目章节。

## 链接与归属核查

- 用户给出的 `langchain-ai/reflexion` 本次访问返回 404。以下依据原论文实现 `noahshinn/reflexion` 和 LangChain 官方 Reflexion 示例分析，并明确区分二者。
- `agentscope-ai/openjudge` 可正常访问，仓库规范名称为 `agentscope-ai/OpenJudge`。
- 用户给出的 `agentsea/claw-eval` 本次访问返回 404；用户已确认按 [claw-eval/claw-eval](https://github.com/claw-eval/claw-eval) 分析。其论文署名包含北京大学和香港大学，不能据此称为“阿里 ClawEval”。[论文作者信息](https://arxiv.org/html/2604.06132v1)
- 腾讯项目按指定的 [Tencent/AI-Infra-Guard](https://github.com/Tencent/AI-Infra-Guard) 分析，其官方名称为 A.I.G / AI-Infra-Guard。

## 1. Reflexion：把失败反馈转成下一次尝试的经验

### 链接与项目身份

用户提供的 `https://github.com/langchain-ai/reflexion` 在本次访问时返回 404，不能据此认定存在可用的同名 LangChain 仓库。可以核实的相关一手来源有两个：

- 原论文及研究实现是 [noahshinn/reflexion](https://github.com/noahshinn/reflexion)，对应 Shinn 等人的 NeurIPS 2023 论文 [Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)。
- LangChain 官方的 [Reflection Agents 博文](https://www.langchain.com/blog/reflection-agents) 展示了用 LangGraph 实现 Reflexion 的示例。它链接到 `langgraph/examples/reflexion/reflexion.ipynb`；该路径[目前只保留迁移及归档声明](https://raw.githubusercontent.com/langchain-ai/langgraph/main/examples/reflexion/reflexion.ipynb)，完整示例可查看[官方仓库历史快照](https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/reflexion/reflexion.ipynb)。因此应把“原方法”和“LangGraph 示例实现”分开说明。

### 解决什么问题

普通 Agent 失败后，重新执行可能重复相同错误。Reflexion 的工作是让 Agent 从一次尝试的结果中生成具体的自然语言经验，并将经验放入后续尝试的上下文。它的“语言强化学习”通过文本反馈发挥作用，**不更新大模型权重，也不等于微调**。输入可以包含任务、动作轨迹、环境观察、分数或文本反馈；输出是新的任务结果及可复用的反思记录。[原论文摘要](https://arxiv.org/abs/2303.11366)

### 实际实现了什么

1. **执行与评价闭环**：Actor 尝试完成任务，Evaluator 判断结果，自我反思模块根据失败形成改进意见，情景记忆保存意见，Actor 携带记忆再次尝试。原仓库提供 HotPotQA 问答推理、ALFWorld 环境决策、编程实验的代码、notebook、运行日志；还包含 WebShop 目录和 LeetcodeHardGym 链接。[研究仓库](https://github.com/noahshinn/reflexion)
2. **具体的反思记忆处理**：ALFWorld 的 `update_memory` 对尚未成功且未跳过的任务读取尝试日志，提示模型针对具体错误生成下一次行动计划，将文本追加到任务记忆。该函数生成新反思时最多引用最近三条旧反思；不能据此将整个项目描述为通用跨任务知识库。[generate_reflections.py](https://github.com/noahshinn/reflexion/blob/main/alfworld_runs/generate_reflections.py)
3. **可比较的反思策略**：HotPotQA 代码支持不带经验、仅上次推理轨迹、仅反思、同时带轨迹与反思等策略。其 CoT Agent 使用标准答案进行 Exact Match 来判断正确性，失败后才反思；这说明研究实验里部分反馈来自已知答案，不能假定实际开放任务也自动拥有正确的评价信号。[agents.py](https://github.com/noahshinn/reflexion/blob/main/hotpotqa_runs/agents.py)
4. **LangGraph 的检索修订示例**：`draft → execute_tools → revise → 循环/结束`。模型输出草稿、指出内容缺失和冗余、生成搜索查询，Tavily 搜索补充外部材料，修订阶段结合材料并生成引用；通过图状态保存消息，按设定迭代上限终止。[官方示例源码](https://raw.githubusercontent.com/langchain-ai/langgraph/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/reflexion/reflexion.ipynb)

### 价值与边界

适合有明确反馈、允许多次尝试的代码生成、复杂问答和工具任务。项目证明并演示的是“利用反馈改善后续尝试”的机制。论文报告的 HumanEval 91% pass@1 属于当时特定模型、实验设置和评测协议下的结果，不能当作任意新任务的成功率承诺。[原论文](https://arxiv.org/abs/2303.11366)

分析判断：反思质量受评价信号和基础模型能力限制；错误反馈可能形成错误经验，多轮模型调用与检索增加成本和延迟。LangChain 也明确说明反思需要额外计算，而且其 Reflexion 示例沿单条轨迹迭代，早期错误可能影响后续决策。[官方博文](https://www.langchain.com/blog/reflection-agents)

一句话：**Reflexion 完成的是 Agent 的反馈、反思、记忆和重试机制，而非通用评测平台。**

## 2. OpenJudge：把 AI 应用质量评估和奖励计算组织成统一框架

### 项目身份与解决的问题

用户链接有效，规范仓库名是 [agentscope-ai/OpenJudge](https://github.com/agentscope-ai/OpenJudge)，当前 Python 包名为 `py-openjudge`，导入命名空间为 `openjudge`。其定位是 AI 应用的整体质量评估与质量奖励框架；核心工作流为收集测试数据、定义评分器、批量评估、分析薄弱点、迭代应用。[README](https://github.com/agentscope-ai/OpenJudge)

### 实际实现了什么

1. **统一评分器接口和结构化结果**：`BaseGrader` 支持 Pointwise 单独评分与 Listwise 多候选排序；输出 `GraderScore`、`GraderRank` 或错误，包含分数/排名、解释、元数据。评分实现既可以是确定性的代码规则，也可以是调用模型的 LLM-as-a-Judge。[BaseGrader 源码](https://github.com/agentscope-ai/OpenJudge/blob/main/openjudge/graders/base_grader.py)、[自定义评分器文档](https://agentscope-ai.github.io/OpenJudge/building_graders/create_custom_graders/)
2. **可直接使用的多维度评分器**：覆盖文本、代码、数学、多模态、格式和 Agent 行为。Agent 维度具体包括动作与计划是否一致、工具是否选对和参数是否正确、记忆是否准确、计划是否可执行、反思是否符合观察、轨迹是否完成目标；既有规则评分器，也有模型评分器。它能评价反思的质量，这与 Reflexion 自身“生成反思并重试”的职责不同。[Agent Graders 文档](https://agentscope-ai.github.io/OpenJudge/built_in_graders/agent_graders/)
3. **批量运行、聚合和分析**：`GradingRunner` 将数据字段映射到各评分器，控制并发，收集多评分器结果，并可调用聚合器组合指标。分析模块用于查看分数分布、与人工标签的一致性等。[Runner 源码](https://github.com/agentscope-ai/OpenJudge/blob/main/openjudge/runner/grading_runner.py)、[结果分析文档](https://agentscope-ai.github.io/OpenJudge/running_graders/grader_analysis/)
4. **自动构建业务评分标准**：`SimpleRubricsGenerator` 从任务描述及可选查询生成评分标准；`IterativeRubricsGenerator` 根据带标注的样本提炼、迭代评分标准并形成评分器。它主要自动化“如何评分”的配置，不等于自动发现不可质疑的真实标准。[Rubrics 生成文档](https://agentscope-ai.github.io/OpenJudge/building_graders/generate_rubrics_as_graders/)
5. **连接评估与训练**：提供 SFT、Bradley–Terry、GRPO 的 Judge Model 训练流水线；支持把评分器组合成 VERL 的奖励函数，并提供 LangSmith/Langfuse 集成。这里需要区分“训练用于评价的裁判模型”与“将评分作为奖励训练业务模型”。[Judge 训练文档](https://agentscope-ai.github.io/OpenJudge/building_graders/training_judge_models/)、[VERL 集成](https://agentscope-ai.github.io/OpenJudge/integrations/verl/)、[官方概览](https://agentscope-ai.github.io/OpenJudge/)
6. **可操作的应用示例**：仓库包含 Streamlit UI、在线 grader 试用、Auto Arena、论文评审和参考文献幻觉评测等；这说明它不只是一个 Judge Prompt 集合。[README](https://github.com/agentscope-ai/OpenJudge)

### 输入、输出和适用场景

- 输入：根据评分器需要提供问题、模型回复、参考上下文、候选答案、工具定义与调用、动作轨迹、记忆/反思等；并非任意轨迹直接丢入就能得到全部结论。[BaseGrader 源码](https://github.com/agentscope-ai/OpenJudge/blob/main/openjudge/graders/base_grader.py)、[Agent Graders](https://agentscope-ai.github.io/OpenJudge/built_in_graders/agent_graders/)
- 输出：逐样本分数或候选排名、解释、聚合分数与统计分析，或下游训练奖励。[结果 Schema](https://github.com/agentscope-ai/OpenJudge/blob/main/openjudge/graders/schema.py)、[Runner 源码](https://github.com/agentscope-ai/OpenJudge/blob/main/openjudge/runner/grading_runner.py)
- 适合：Agent 版本回归、模型/提示词对比、业务场景定制评估、训练数据筛选与奖励计算。上述场景属于评价框架能力的应用，不代表已为特定业务建立有效测试集。

### 价值与边界

其主要成果是把“评分器开发、评分标准生成、批量运行、结果分析、奖励对接”放进一个可组合系统。它包含安全相关评分能力，但核心定位仍是评价与奖励基础设施，不能直接当成覆盖全部威胁的实时安全防护产品。[官方概览](https://agentscope-ai.github.io/OpenJudge/)

分析判断：LLM 裁判的结论依赖裁判模型、rubric 和输入证据，需要用业务人工标注校准。文档的 Agent Grader 性能表使用不同且部分很小的样本集，不能将表中某个高准确率推广为所有业务上的事实真伪证明。[Agent Graders 性能与评价指标](https://agentscope-ai.github.io/OpenJudge/built_in_graders/agent_graders/)

一句话：**OpenJudge 完成的是可定制、可批量运行、可接入训练的质量评估系统。**
## 3. Claw-Eval：验证 Agent 是否实际、稳定且安全地完成任务

### 解决的问题

只检查最终回复会遗漏执行过程中发生的越权操作、虚构步骤和故障恢复失败。Claw-Eval 建立端到端执行与评测环境，将任务结果与过程证据结合。论文提出的重点包括执行过程可审计、任务内安全约束、环境故障下的鲁棒性以及多模态和多轮交互。[原论文](https://arxiv.org/html/2604.06132v1)

### 实际实现了什么

1. **可运行的任务集**：当前 README 列出 300 个人工核验任务、2,159 条细粒度评分标准，分为通用任务 161 个、多模态任务 101 个、多轮对话任务 38 个。覆盖工作流操作、文档/图像/视频处理、生成产物和专业咨询等。[README](https://github.com/claw-eval/claw-eval)
2. **任务执行框架和环境**：仓库包含任务定义、模型适配、Agent 循环、Docker 沙箱及模拟服务。执行中可以读写文件、运行命令、检索网页、处理媒体，或调用邮件、日历、CRM 等任务相关模拟 API。[执行代码](https://github.com/claw-eval/claw-eval/blob/main/src/claw_eval/runner/loop.py)、[模拟服务目录](https://github.com/claw-eval/claw-eval/tree/main/mock_services)
3. **三种证据交叉核验**：保留模型和工具调用轨迹、服务端实际收到请求的审计日志、任务结束后的环境快照和产物。源码提供结束后执行验证命令、收集文件和保存快照的逻辑。由此能区分“声称发了邮件”和“服务实际记录到发送请求”。[CLI 与快照代码](https://github.com/claw-eval/claw-eval/blob/main/src/claw_eval/cli.py)
4. **规则和模型联合评分**：任务可以使用确定性检查与 LLM 裁判；模型裁判支持对回复、服务端动作及视觉产物评分，并记录解释。[LLMJudge 源码](https://github.com/claw-eval/claw-eval/blob/main/src/claw_eval/graders/llm_judge.py)
5. **多维度和重复试验**：评价任务完成度、安全性和鲁棒性。当前评分函数为 `安全分 × (0.8 × 完成度 + 0.2 × 鲁棒性)`，默认通过阈值是 0.75；CLI 实现多次运行、批量执行和结果汇总。[评分函数](https://github.com/claw-eval/claw-eval/blob/main/src/claw_eval/models/scoring.py)、[CLI](https://github.com/claw-eval/claw-eval/blob/main/src/claw_eval/cli.py)

### 输入输出和适用场景

输入包括待测模型接口及配置、任务定义、工作文件、模拟服务和评分标准；多轮任务还会配置模拟用户。输出包括轨迹、服务审计、环境快照、逐任务与各维度评分、重复运行统计。适合比较不同模型在统一 Agent 执行框架下的能力，或借鉴其任务和证据机制建立业务回归基准。[论文执行协议](https://arxiv.org/html/2604.06132v1)、[任务和执行入口](https://github.com/claw-eval/claw-eval/blob/main/src/claw_eval/cli.py)

例如，任务只授权“整理邮件并生成处理建议”时，评测可以同时检查建议是否正确，以及是否擅自发送、删除邮件。这个例子说明任务正确性和行为合规性需要分别核验；不应只凭 Agent 的完成声明判定成功。[论文邮件分拣案例](https://arxiv.org/html/2604.06132v1#A1)

### 边界与源码观察

- 它提供固定任务集和可控环境中的评测结果。将这些结果用于生产能力判断时，仍需验证业务覆盖、真实服务差异、裁判模型与执行配置。此为基于其架构的分析判断。
- README 仍声明在审计代码，以实现榜单结果端到端可复现；不能把这一目标表述成已经独立验证的事实。[README](https://github.com/claw-eval/claw-eval)
- **Pass^3 的文档与实现口径不同**：README 将其描述为三次都通过才计成功；本次查看的 `compute_pass_hat_k` 返回 `(成功次数 / 运行次数)^k`，CLI 调用此函数产生多次运行指标。例如三次通过两次时，严格全通过计分应为 0，函数在 `k=3` 时返回约 0.296。因此复现实验需要固定提交和明确统计口径，不能将两种计算直接等同。[评分源码](https://github.com/claw-eval/claw-eval/blob/main/src/claw_eval/models/scoring.py)、[调用处](https://github.com/claw-eval/claw-eval/blob/main/src/claw_eval/cli.py)

一句话：**Claw-Eval 完成的是一套“给任务、实际运行、收集证据、按标准判分并重复验证”的 Agent 基准评测系统。**

## 4. A.I.G / AI-Infra-Guard：对 AI 系统进行分层安全扫描与红队测试

### 项目定位

腾讯朱雀实验室将 AI-Infra-Guard 定位为一站式 AI 红队测试平台。目前功能已从 AI 基础设施漏洞扫描扩展到 Agent、MCP、Skill 和大模型安全评估，并提供 Web 界面及可独立运行的命令行工具。[官方仓库](https://github.com/Tencent/AI-Infra-Guard)

### 实际实现了什么

| 模块 | 测试对象与实现工作 | 典型输出 |
| --- | --- | --- |
| Infra Scan | 接收域名、IP 或 URL，进行 AI 基础设施指纹识别与漏洞规则匹配；Go 扫描器也封装为可安装工具。[模块说明](https://github.com/Tencent/AI-Infra-Guard/blob/main/infra-scan/README.md) | 资产/组件识别和漏洞结果 |
| MCP Scan | 分析 MCP Server 项目代码，也可连接运行中的服务测试；支持代码审计及分阶段复核。[模块说明](https://github.com/Tencent/AI-Infra-Guard/blob/main/mcp-scan/README.md) | 风险分数、漏洞发现、SARIF 报告 |
| Skill Scan | 审计 Agent Skill 文本、脚本及配置；识别指令劫持、记忆投毒、恶意载荷、越权、后门、不安全依赖等类别。[审计源码](https://github.com/Tencent/AI-Infra-Guard/blob/main/skill-scan/skill_scan/agent/agent.py) | 风险发现、normal/suspicious/malicious 项目结论、SARIF 或内部 JSON |
| Agent Scan | 通过 API 向运行中的 Agent 发送测试提示，检测数据泄露、授权绕过、间接提示注入、工具滥用和数据外传等风险；具备多平台适配。[模块说明](https://github.com/Tencent/AI-Infra-Guard/blob/main/agent-scan/README.md) | 严重程度、漏洞分类、测试记录和 JSON 报告 |
| Model Scan | 对模型接口执行安全场景和单轮/多轮越狱测试；支持自定义测试数据、攻击生成模型和评价模型。[模块说明](https://github.com/Tencent/AI-Infra-Guard/blob/main/AIG-PromptSecurity/README.md) | 安全评测和越狱测试结果 |

### 它将安全测试组织成了什么流程

Agent Scan 的源码实现信息收集、按风险类别检测、漏洞复核三个阶段，最终形成带严重程度和 OWASP ASI 分类的结构化报告。测试针对运行中的 Agent，其目标接口配置与扫描器使用的模型配置是两个不同输入。[Agent Scan 核心代码](https://github.com/Tencent/AI-Infra-Guard/blob/main/agent-scan/agent_scan/core/agent.py)

Skill Scan 的独立模式先进行代码审计，并对 `suspicious` 结论进行针对性复核；平台模式提供信息收集、代码审计、漏洞复核三阶段显示。SARIF 输出能被支持该格式的开发工具消费；修复建议是报告内容，不能直接当作已经执行的自动修复。[Skill Scan 文档](https://github.com/Tencent/AI-Infra-Guard/blob/main/skill-scan/README.md)

例如，对于会读取网页、查询知识库并调用内部接口的客服 Agent，该平台可以测试外部内容是否诱导其泄露信息或滥用工具；对其接入的 MCP 服务和 Skill 还可以分别开展源码审计。这是根据模块能力给出的应用示例。[Agent 测试用例](https://github.com/Tencent/AI-Infra-Guard/blob/main/agent-scan/README.md)、[MCP Scan](https://github.com/Tencent/AI-Infra-Guard/blob/main/mcp-scan/README.md)

### 价值与边界

- 其主要成果是将 AI 应用不同层次的安全测试整合成可执行的平台和独立工具。扫描结果可用于修复与复测；作为在线请求拦截或权限控制使用，还需相应的产品集成，不能由项目名称推断已经提供所有运行时防护能力。这属于依据其测试流程作出的分析判断。
- 扫描结果受规则覆盖、目标可观测性和扫描模型能力影响。一次未发现问题不能证明系统无漏洞，此为测试方法的边界判断。
- 当前主 README 明确说明平台缺少认证机制，定位为内部使用，不应直接部署到公网。这是项目自身已披露的部署限制。[README 部署说明](https://github.com/Tencent/AI-Infra-Guard#-quick-start)
- 本次源码观察到，Agent Scan 函数和注释使用 parallel 命名，但风险检测 worker 的调度循环是逐个 `await`。因此本文只确认其具备按类别组织的检测流程，不将文档中的并发描述作为已验证性能结论。[调度实现](https://github.com/Tencent/AI-Infra-Guard/blob/main/agent-scan/agent_scan/core/agent.py)

一句话：**AI-Infra-Guard 完成的是面向 AI 技术栈的漏洞发现、行为风险测试与安全报告体系。**

## 5. 四者如何联系

可以用四个问题理解它们：

- **Reflexion：上一次为什么失败，下一次如何改进？**
- **OpenJudge：这次输出和行为的质量如何，应该打多少分？**
- **Claw-Eval：放入一组可控任务后，是否真正完成，并且重复运行仍然可靠？**
- **AI-Infra-Guard：受到恶意输入或攻击时，哪里会泄露、越权或被利用？**

从架构上，可以用评分器生成反馈，再由反思机制改进行动；用任务基准验证版本变化，用安全扫描发现攻击面。这里描述的是可组合的设计思路，本次没有核实四个项目之间存在开箱即用的官方集成，也没有执行任何集成或模型训练。

两类评价需要区分：OpenJudge 侧重可定制的评价组件和运行框架；Claw-Eval 包含具体任务、服务环境、轨迹证据与评分协议。两类安全检查也需要区分：Claw-Eval 在任务执行中检查约束和安全表现；AI-Infra-Guard 更集中于主动发现漏洞和攻击路径。这个职责划分是基于上述实现作出的比较分析。

## 6. 结合 Devcooper 当前代码的适配建议

本节依据本地源码静态阅读提出建议；未安装上述依赖、未运行模型或安全扫描，也未修改应用代码。

### 已有基础

Devcooper 已有真实 ChatAgent 评测、RAGAS 的检索/答案/目标/工具指标、固定题集及版本标记、逐条硬规则、错误补评分和版本对比。当前捆绑固定题集为 `docs/datas/evals/rag_cases.json`，共 5 条。它已有评价基础，新增工具的价值应体现在新增业务覆盖或更可靠的证据上。[评分实现](../../backend/app/services/evals/ragas_runner.py)、[题集加载](../../backend/app/services/evals/suites.py)、[版本对比](../../backend/app/api/routes/evaluations.py)

编排器已有 Planner、专用 Agent、Observer、Synthesis，以及默认一次、最多两次的有限重规划；规划器的恢复策略主要是追加缺失任务。已有记忆候选审批、Skill 工具范围约束和 MCP 记忆服务。[编排器](../../backend/app/services/agents/workflow_orchestrator.py)、[重规划](../../backend/app/services/agents/planner_agent.py)、[记忆审批](../../backend/app/services/memory_hub.py)、[Skill 注册表](../../backend/app/services/skills/registry.py)、[MCP 服务](../../backend/app/mcp/memory_server.py)

### OpenJudge：优先补充研发业务评分

建议保留 RAGAS 及现有硬门禁，新增独立业务评分层。第一批评分标准可包括 PR 审查结论与 diff 的一致性、CI 根因是否由日志支持、负责人是否来自当前团队，以及综合结论是否保留阻塞条件。评分需要提供对应事实和必要参考标注；业务指标不是安装包后自动获得的有效标准。[自定义评分器支持](https://agentscope-ai.github.io/OpenJudge/)

可复用 ChatAgent 的 `evaluation_trace`、`tool_calls`、`tool_observations`，以及工作流的任务结果和 Observer findings；根据评分器要求，补充工具定义、版本和可核验事实快照。[轨迹采集](../../backend/app/services/agents/chat_agent.py)、[OpenJudge 输入要求](https://agentscope-ai.github.io/OpenJudge/get_started/evaluate_ai_agents/)

当前虽然有 `SemanticScorer` 协议，但 `_finalize_result` 将完整性和指标绑定 RAGAS，主循环还会在缺少检索上下文时跳过语义评分。因而不能仅替换 scorer 就宣称支持完整 OpenJudge 业务评价。建议增加单独的适配器和 `business_quality` 结果区域，单独记录评分器、标准版本、分数、解释及错误；其分数不能覆盖规则阻塞或外部写入权限。[现有评分契约与汇总](../../backend/app/services/evals/ragas_runner.py)

### AI-Infra-Guard：优先作为外部安全测试工具

最贴合的是 Skill Scan、MCP Scan 和 Agent Scan：分别覆盖本地 `backend/app/skills`、`memory_server.py` 与对话接口。当前已有非流式 `POST /api/chat`，请求包含 `repo_id`、`message`，响应包含 `answer`；可在隔离测试部署中按 HTTP provider 配置接入 Agent Scan，并处理会话创建和测试数据重置。[本地接口](../../backend/app/api/routes/chat.py)、[请求响应](../../backend/app/schemas/chat.py)、[HTTP provider 说明](https://github.com/Tencent/AI-Infra-Guard/blob/main/agent-scan/README.md)

建议针对恶意 Issue/PR 描述、CI 日志或知识文档中的间接提示注入、跨仓库数据访问、敏感文件读取、记忆投毒和未经授权的草稿确认进行测试。复用隔离数据与模拟 GitHub 服务，不向真实业务资源执行攻击性测试。这是适配方案，本文没有执行这些测试。

有一处实际代码会影响优先级：`confirm_action_draft` 已会调用 GitHub 写接口，支持评论、建 Issue、关闭 Issue 和加标签；`require_permission` 通过 `ensure_demo_user` 获取演示用户，尚未体现对当前请求者的真实身份认证。因此对外部署前应先完善鉴权、仓库授权和写入确认边界，再用安全扫描验证；扫描工具本身不能补上这些访问控制。[写操作与确认接口](../../backend/app/api/routes/action_drafts.py)、[权限实现](../../backend/app/services/permissions.py)

### Reflexion：先在单个分析 Agent 内试验一次修订

当前 Observer 和重规划已能发现缺口并追加任务，但不等同于带失败经验的反思修订。建议先选择 PRReviewAgent 或 CIDebugAgent，在明确缺证据、与已知事实矛盾等情形下，执行“初稿—基于证据的评价—一次修订”。保留初稿、反馈、修订结果和成本，并用固定题集比较修订是否增加正确结论、减少无依据判断。[PR Agent](../../backend/app/services/agents/pr_review_agent.py)、[CI Agent](../../backend/app/services/agents/ci_debug_agent.py)、[Observer](../../backend/app/services/agents/observer_agent.py)

该试验应放在单个分析任务内部，不绕过现有重规划次数和成功任务不重跑的约束。先采用会话内经验；需要长期保存时，映射或扩展记忆候选类型，并沿用人工审批入库流程。[任务调度](../../backend/app/services/agents/workflow_orchestrator.py)、[记忆候选与审批](../../backend/app/services/memory_hub.py)

### Claw-Eval：借鉴过程审计和重复试验，建设研发任务基准

Claw-Eval 当前执行代码包含自己的 Agent loop、工具与模型 provider。直接给它配置 Devcooper 使用的模型，主要衡量的是该模型在 Claw-Eval 框架中的表现，不能自动代表 Devcooper 的 Skill、记忆与编排质量。测试完整 Devcooper 需要任务、执行和轨迹格式适配。[Claw-Eval 执行循环](https://github.com/claw-eval/claw-eval/blob/main/src/claw_eval/runner/loop.py)

更适合当前阶段的做法，是借鉴其证据和复跑设计，扩充本地研发任务基准：CI 失败必须阻塞、PR 高优先级问题必须保留、缺复现信息不得编造、非当前仓库证据不得混入、恶意文档不得改变工具权限。每项任务固定仓库数据和代码版本，多次运行并分别统计成功率、全部通过比例、延迟和调用成本。

已有 `evaluation_session` 会复制关系库输入，但并未冻结工作区和向量服务；要做可复现的重复试验，还需固定这些外部输入。当前数据库隔离能力可以继续复用。[评测副本实现](../../backend/app/services/evals/sandbox.py)、[实际 Agent 执行入口](../../backend/app/services/evals/ragas_runner.py)

### 建议的实施次序

1. 完善外部写操作的真实鉴权与仓库授权，同时建立安全测试环境。
2. 在现有评测旁新增 OpenJudge 业务评分，并将固定题集扩大到覆盖 PR、Issue、CI 和跨 Agent 决策的代表性场景。
3. 用 AI-Infra-Guard 的相关模块进行独立扫描，修复后将有效攻击样本加入固定回归。
4. 在一类分析任务上进行受限反思修订的对照试验，依据正确率、回归案例和成本决定是否扩大。
5. 扩充任务环境快照与重复试验，再评估是否需要适配完整 Claw-Eval 框架。
