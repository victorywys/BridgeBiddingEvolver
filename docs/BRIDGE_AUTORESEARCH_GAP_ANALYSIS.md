# Bridge Auto-Research — Current State & Gap Analysis

审查日期：2026-11-04。范围：当前源码、配置、测试、结果目录及本地十个进化运行的持久状态。按提供的《Bridge AutoResearch Audit Spec v1.0》检查；只新增本报告，不修改业务代码、配置或实验数据，不启动付费模型调用或新研究实验。

## 1. 一页式结论

**方向基本正确，完整协议设计的搜索空间已经打开；主要问题已从“能不能编辑体系”转为“能不能在有限预算内完成可信实验，并证明方法有效”。现在不能宣称持续自进化成功、胜过专家体系或达到万副目标。**

### 当前里程碑

以下采用审查文档的编号，不采用项目早期路线中“M1=叫牌核心、M5=进化闭环”的编号：

| 审查里程碑 | 当前判断 | 依据 |
|---|---|---|
| M1 / Infrastructure | 大部分基础能力已验证，科研验收仍有条件 | 本次154项针对性回归通过，含真实DDS、合法性、信息隔离、配对统计及研究工具；统计校准、最终报告隔离和基线合同尚需加强 |
| M2 / Convention Discovery | 工程切片成立，论文级发现尚未证明 | 可原子替换整个v2体系；真实研究已从空白产出多轮协议，最新冻结候选83规则、31状态；但没有受控Local vs Global实验及结构新颖性证据 |
| M3 / Continuous Evolution | 尚未达到 | 旧局部Agent完成3代、0晋升；自主研究仅找到一次相对空白父本的64副工程晋升；最新pilot未完成第一代 |
| M4 / Competitive Evaluation | 尚未达到 | 没有强AI同条件对照或人类实验，没有执行万副最终检测 |

### 三个门槛问题

1. **能创造吗？可以提交完整协议，非局部编辑锁死。** `edit_system(path="")`可以替换整个体系，发布时冻结完整IR和同源说明，发送、解释、状态和后续能够一起修改。但“使用传统转移、斯台曼等结构从空白写成体系”不等于发现了新约定。
2. **能研究吗？已有基础能力，但调度尚不可靠。** 能生成牌、写实际断言、调试、DDS探索、记录假设、保留失败并接收下一代反馈；最新真实运行却在其他谱系研究中耗尽模型额度，已发布候选没有进入外层正式筛选。
3. **能证明吗？还不能。** 尚缺方法对照、独立重复运行、经过校准的长期确认流程及足够强的外部基线。现有小样本正收益不能替代这些证据。

### 三大架构瓶颈

1. **研究会话阻塞评测。** [Controller.generation](../src/bridge_evolve/evolution/controller.py#L385)先完成所有谱系的研究，之后才统一做有效性、筛选、复筛和确认；后一个会话的模型预算异常能使前面已经付费发布的候选一直没有正式成绩。
2. **验证器主要证明“类型正确且可以运行”，不是“协议覆盖完整”。** [validate_v2](../src/bridge_evolve/systems/v2_language.py#L165)检查类型、引用、公开信息边界和状态图；语义重叠、真实可达性、全部入口出口和漏分支仍主要依赖运行抽样及Agent自己编写的测试。
3. **首个方法实验范围过大。** E005同时研究完整Z体系和数千规则的经典体系草稿。导航工具已经改善读取方式，但“可以全局重写”不意味着首个论文实验应同时重构全部开叫、竞争和满贯；缺少冻结入口与外围后的完整模块设计实验。

### 三大科研风险

1. **把胜过全Pass父本或探索性正增益误当成竞争力。** 唯一晋升相对空白；最新候选对两个修复后的工程基线的探索均分仍为负。
2. **把名义统计控制当成长期有效性保证。** 有run内确认次数和错误预算，但bootstrap尾部精度、重复新建run及最终结果再用于研究的campaign级控制尚不完整。
3. **把结构编辑能力当成方法贡献。** 无同预算Local/Global对照、Memory/Archive消融和多次独立运行；行为改变见证不是结构创新指标。DDS也不是不完全信息实际打牌。

**本周最值得做：先让现成冻结候选不依赖新增LLM请求即可完成正式评测；再建立一个完整模块的覆盖合同；最后开展小规模、同预算的Local vs Global对照。暂不扩展RSI、联赛或复杂多Agent。**

## 2. 当前实际进度：不要把历史“运行中”当成现在

### 2.1 最新E005已经停止，尚未完成第一代

直接证据：[最新state](../.runtime/evolution/evo-4490f1d93de14d4f9f42fd2585872191/state.json#L5)、[运行报告](../.runtime/evolution/evo-4490f1d93de14d4f9f42fd2585872191/report.md#L5)、[冻结配置](../.runtime/evolution/evo-4490f1d93de14d4f9f42fd2585872191/config.json)。

| 项目 | 实际状态 |
|---|---|
| 最后更新时间 | 2026-11-04 07:17:22 UTC |
| run状态 | `budget_exhausted`，不是仍在后台进化 |
| 完成代数 / 晋升尝试 / 万副检测 | 0 / 0 / 0 |
| 本run模型请求 | 95次 |
| 已结算token | 6,156,714：输入6,118,594，输出38,120 |
| 本run token上限 | 6,628,707；尚余471,993，但不足覆盖下一请求的保守预留，故停止 |
| 配对预算计数 | 1,539；这是预留/计费计数，不是1,539个独立牌族，也不全等于成功成绩 |
| 实际完成的轮次 | 128副诊断、64副探索、128副探索；没有正式validity/screen/full/promotion轮次 |
| Z谱系 | 48轮工具研究，已发布2个完整候选 |
| A谱系 | 停在第45轮；2,827规则草稿、13个案例，尚无已编写测试或发布 |
| B谱系 | 本代尚未进入研究 |

前一个E004在82次请求后因HTTP 503失败，已用5,371,293个结算/保守预留token。E005配置明确使用其剩余额度；两段合计177次请求、11,528,007个计入预算的token，不能说新建run抹掉了旧消耗。参见[续跑配置](../experiments/e005-corrected-reference-continuation.json#L8)和[续跑说明](12-self-evolution.md#20-修复参考后的显式续跑与网关重试)。

### 2.2 最新冻结候选有真实进展，但不是新冠军

[Z第二个发布物](../.runtime/evolution/evo-4490f1d93de14d4f9f42fd2585872191/research/g001-Z/release-2.json#L2)：

- 哈希：`7a68e80669e1488bbcdc910ec6a61d78b6648478da888fbe436158e01a0e67f5`。
- 83规则、31状态；包含自然竞争及控制/关键张满贯后续。
- 历史发布门禁记录72项断言通过、8个自然完整拍卖的行为改变见证。
- 本次实际重新编译冻结IR，重新生成63,755字节说明，与保存的说明逐字相同。
- 当前工作草稿已经继续修改，持久状态明确显示`tests_current=false`；不能把冻结候选的72项通过移植到后来的工作草稿上。
- 128副探索的收益差为`+0.390625 IMP/副`，但候选对CCBA/精确v5.5.4的均分分别为`-0.34375`、`-0.2109375`。
- 本次核对该轮summary及逐副日志哈希，从原始IMP向量复算结果一致。收益差的描述性95%配对bootstrap区间为`[+0.02734375,+0.8046875]`；按当前晋升单次名义`alpha=0.005`复算单侧下界为`-0.0546875`。这是适应性开发轮次，无论描述性区间如何，都不能充当独立确认。

128副内43个牌族改变拍卖，说明不只是重命名；仍不能证明协议编码具有结构创新，更不能证明长期收益。证据：[探索轮次](../.runtime/evolution/evo-4490f1d93de14d4f9f42fd2585872191/rounds/r-g001-Z-cd30ef0a3aed/summary.json)、[工作状态](../.runtime/evolution/evo-4490f1d93de14d4f9f42fd2585872191/state.json#L303)。

### 2.3 已有最强证据应怎样表述

| 已有记录 | 能支持什么 | 不能支持什么 |
|---|---|---|
| [旧Agent烟测](../results/evolution-validation/live-agent-validation.json#L1)：3代、6次请求、1,200配对、0晋升 | 局部Agent工程循环曾真实跑通；不强制产生“进步” | 多代自主协议设计有效 |
| [空白起点研究](../results/astra-research-validation/validation.json#L35)：58规则、22状态、40项测试 | 工具式Agent确实构造并发布了小型完整多轮体系 | 零桥牌先验、新约定发现或胜过人工体系 |
| [64副确认](../results/astra-research-validation/promotion-validation.json#L12)：相对空白`+3.1875` | 一次工程晋升；本次从原始日志精确复算均值和下界 | 相对经典基线提升；该批对两基线仍为负 |
| [历史1,000副工程比较](../results/ccba-prec-v5.5.2-potential-audit-1000-20261029/summary.json#L1)：均值`-0.015`，95%区间`[-0.238,+0.217]` | 工程版之间有完整同牌结果 | Agent带来提升；两体系有确定强弱 |

“results中有很多目录”不是很多成功实验。例如[旧v5.1运行](../results/ccba-prec-translation-v5.1-1000/run.json#L1)记录在第345副遇到未实现分支，不能计作完成1,000副。旧版本、不同牌集、不同参考体系的分数也不能直接连成进化曲线。

## 3. 仓库地图与真实控制流

### 3.1 主要入口

| 能力 | 实现 |
|---|---|
| 命令与配置冻结 | [CLI](../src/bridge_evolve/cli.py#L109)、[normalize_config](../src/bridge_evolve/evolution/config.py#L20)、[initialize](../src/bridge_evolve/evolution/controller.py#L54) |
| 持续研究会话 | [ResearchSession](../src/bridge_evolve/evolution/research.py#L63)、[研究提示](../src/bridge_evolve/evolution/research.py#L11) |
| 体系编辑、案例、断言和发布 | [ResearchTools](../src/bridge_evolve/evolution/research_tools.py#L151) |
| IR编译与纯观察执行 | [validate_v2](../src/bridge_evolve/systems/v2_language.py#L165)、[Observation](../src/bridge_evolve/systems/runtime.py#L31)、[decide](../src/bridge_evolve/systems/runtime.py#L260) |
| 全牌叫牌与局部入口 | [run_auction](../src/bridge_evolve/systems/inputs.py#L142)、[run_uncontested](../src/bridge_evolve/systems/inputs.py#L220) |
| DDS、两桌审计与统计 | [evaluate_pair](../src/bridge_evolve/evolution/execution.py#L58)、[BoardAuditEngine.batch_board](../tools/board_viewer.py#L1630)、[paired_gain](../src/bridge_evolve/evolution/execution.py#L101) |
| 晋升、最终检测与归档 | [Controller.generation](../src/bridge_evolve/evolution/controller.py#L385)、[verify_goal](../src/bridge_evolve/evolution/controller.py#L580)、[register_system](../src/bridge_evolve/evolution/controller.py#L27) |

### 3.2 当前真实调用链

```text
evolve run -> initialize -> Controller.run
  -> generation：冻结各谱系parent
  -> diagnosis：新共同随机牌 -> evaluator -> 历史记录/问题分组
  -> 逐谱系 proposals -> ResearchSession.run
       -> 模型工具调用
       -> read/navigate/evidence -> research_note
       -> edit_system：深拷贝、原子修改整个工作IR
       -> validate_system / cases / tests / debug / trial / consult
       -> publish_candidate
            -> 完整编译 -> 实际执行作者断言
            -> 至少一个行为变化见证
            -> 冻结完整JSON、同源说明、测试与案例
       -> 研究结束或会话预算到达 -> 下一谱系
  -> 所有谱系返回后，统一执行：
       validity -> screen -> full -> 新牌promotion
       -> paired_gain + 固定阈值 -> 更新champion或保留原冠军
  -> 下一代：诊断、新反馈、携带工作区或发布物回归
  -> 可选：冻结候选后新生成10,000副 -> 分别比较两个固定锚点
```

评测内部：[Evaluator._round_data](../src/bridge_evolve/evolution/execution.py#L148)保存种子和manifest，恢复复用原输入；[Evaluator.evaluate](../src/bridge_evolve/evolution/execution.py#L181)对每个候选、固定对手、共同Board运行两桌，保存完整日志和逐副向量。

**当前断点就在“下一谱系”与“统一评测”之间。** E005的Z已发布两个候选，A的模型请求触发预算停止；[Controller.run](../src/bridge_evolve/evolution/controller.py#L658)捕获全局停止并退出，没有把已有候选送完外层评测。

### 3.3 保护边界

- 研究Agent只能使用[工具白名单](../src/bridge_evolve/evolution/research_tools.py#L111)，不能任意shell、写评分器或改DDS。工具读取历史完成轮次，不读取未完成轮次。
- 执行期策略只收到本人手牌、座位、局况、公开历史与静态体系定义；[run_auction](../src/bridge_evolve/systems/inputs.py#L170)不把Board或DDS传进策略。
- 公开协议解释使用没有手牌可读的[PublicView](../src/bridge_evolve/systems/runtime.py#L90)；私有trace只供离线审计。
- config、引擎、体系、说明和分析产物都有身份校验；这是工具和解释器边界，不是对拥有宿主权限者的操作系统级隔离。
- 当前引擎身份`a5d5aed7…`与E005冻结身份不同。[load_frozen_state](../src/bridge_evolve/evolution/controller.py#L241)会拒绝在变化后的引擎上直接resume。后续需要显式迁移/新实验并重验，不应篡改旧哈希绕过检查。

## 4. 第7节逐项清单

状态含义：**Verified**=本次实际检查/测试支持该项限定能力；**Partial**=存在实现但未满足全部完成标准；**Missing**=在相关实现、配置和结果中未找到；**Unclear**=证据不足；**N/A**=当前论文不必实现。历史验证文件只作为历史证据，未自动当成本次重跑结果。

### A：基础门槛

| ID | 状态 | 证据 | 缺口 | 建议 | 优先级 |
|---|---|---|---|---|---|
| A01 | Verified | [决策合法性门禁](../src/bridge_evolve/systems/runtime.py#L301)；本次核心及`DecisionBoundaryTests`通过 | 保证不返回非法动作，不保证任意候选对任意状态都能成功产生动作 | 保持非法动作显式失败；候选覆盖另由A09验收 | P0 |
| A02 | Verified | [Observation](../src/bridge_evolve/systems/runtime.py#L31)、[公开/私有表达式检查](../src/bridge_evolve/systems/v2_language.py#L243)；本次隐藏手牌扰动、公开解释隔离测试通过 | 结论限受限IR运行路径，不是任意宿主代码的沙箱证明 | 新算子必须继续满足公开/私有信息类型边界 | P0 |
| A03 | Verified | [确定性决策](../src/bridge_evolve/systems/runtime.py#L269)、[全牌调度](../src/bridge_evolve/systems/inputs.py#L170)；本次重复执行及状态重建测试通过 | LLM研究可能随机，但批量叫牌不调用LLM | 保持版本、可见状态到叫品的确定性 | P0 |
| A04 | Verified | [轮次输入冻结](../src/bridge_evolve/evolution/execution.py#L148)、[同牌双桌](../src/bridge_evolve/evaluation/experiment.py#L49)；本次新牌/恢复、换位反号、真实DDS缓存测试通过 | 历史轮次不都已按当前引擎整批重放 | 继续保存原始manifest、引擎和DDS身份，不混版本 | P0 |
| A05 | Partial | [paired_gain / column_summary](../src/bridge_evolve/evolution/execution.py#L80)、[family_hash](../src/bridge_evolve/evaluation/data.py#L36)；本次配对统计测试及两轮原始向量复算通过 | 小样本和极端尾部分位数精度不足；未见覆盖率/功效模拟及campaign级重复检验控制 | 按牌族统计；校准确认规则，预注册跨run错误预算 | P0 |
| A06 | Partial | [完成轮次读取限制](../src/bridge_evolve/evolution/research_tools.py#L306)、[新牌生成](../src/bridge_evolve/evolution/execution.py#L148)；本次未完成轮次拒读测试通过 | 无长期封存Test；完成后的最终轮次与其他轮次一样可读；每run一次检测不是跨run一次 | 保留“候选冻结后现场生成”的既定方案，补campaign级最终检测审批/结果隔离和消耗台账 | P0 |
| A07 | Verified | [v2编译器](../src/bridge_evolve/systems/v2_language.py#L165)、[动态动作](../src/bridge_evolve/systems/v2_runtime.py#L432)；本次v2测试及冻结83规则候选重新编译通过 | scope、状态和条件可表达边界，但不是独立模块接口类型系统 | 对近期研究模块补入口/出口合同，无需重写整个DSL | P0 |
| A08 | Verified | [edit_document](../src/bridge_evolve/evolution/research_tools.py#L27)、[publish_candidate](../src/bridge_evolve/evolution/research_tools.py#L560)；本次整文档替换原子性、失败不改父本及多轮含义同步测试通过 | 没有独立模块级回滚命令；目前用保留父快照和冠军指针实现不晋升/恢复起点 | 增加一个2+耦合分支的v2模块替换与回滚组合验收 | P0 |
| A09 | Partial | [状态图检查](../src/bridge_evolve/systems/v2_language.py#L273)、[规则检查](../src/bridge_evolve/systems/v2_language.py#L311)、[作者测试](../src/bridge_evolve/evolution/research_tools.py#L475) | 图可达不等于条件可达；仅静态拒绝相同优先级的相同条件，其他重叠运行时才发现；无模块完整覆盖证明 | 有限抽象枚举+边界/干扰反例+明确出口；报告分别标明静态、枚举、抽样覆盖 | P0 |

### B：Auto-Research能力

| ID | 状态 | 证据 | 缺口 | 建议 | 优先级 |
|---|---|---|---|---|---|
| B01 | Partial | [研究提示](../src/bridge_evolve/evolution/research.py#L11)、[发布契约](../src/bridge_evolve/evolution/research_tools.py#L560) | 正式发布强制项主要是hypothesis字符串、完整体系和测试；Mechanism、预期收益/退化、Plan无结构化必填契约 | 扩展发布元数据，绑定可证伪预测、切片和实验计划 | P1 |
| B02 | Partial | [evidence_packet](../src/bridge_evolve/evolution/proposals.py#L69)、[单叫/整牌读取](../src/bridge_evolve/evolution/research_tools.py#L306)、[行为变化计数](../src/bridge_evolve/evolution/execution.py#L312) | 问题分组把最终IMP归给每个经过的规则，不是该规则的因果贡献；缺统一父子收益切片、覆盖分母及信息混合诊断 | 加共同牌父子差值切片与正/负代表例，明确描述性关联而非因果归因 | P1 |
| B03 | Partial | [完整控制流](../src/bridge_evolve/evolution/controller.py#L385)、[研究继续/反馈](../src/bridge_evolve/evolution/research.py#L116)；本次真实三代number-source和录制模型修复循环测试通过 | 真实自主pilot在第一代中断；已发布候选被其他谱系会话阻塞 | 先解耦已发布候选评测与剩余模型会话，做到低预算下仍能闭合一个研究单元 | P1，当前阻塞提升为P0 |
| B04 | Partial | [candidate_archive](../src/bridge_evolve/evolution/controller.py#L450)、[工作分支继承](../src/bridge_evolve/evolution/research.py#L64)；本次未发布分支/冠军续接测试通过 | 有多个谱系、同会话替代方案和失败记录，但无基于archive的通用父分支选择或探索资源调度 | 先保留已具备的多方案，不急于增加复杂种群算法 | P1 |
| B05 | Partial | [research_note](../src/bridge_evolve/evolution/research_tools.py#L554)、[研究反馈](../src/bridge_evolve/evolution/research.py#L116)、[context handoff](../src/bridge_evolve/evolution/research.py#L178) | 有持久中文笔记、案例和试验；未强制假设状态、证据引用、反例、适用条件，失败知识缺可检索结构 | 简单结构化研究台账即可，暂不需要向量数据库 | P1 |
| B06 | Partial | [模型预算](../src/bridge_evolve/evolution/models.py#L65)、[配对预算](../src/bridge_evolve/evolution/execution.py#L130)、[时间预算](../src/bridge_evolve/evolution/controller.py#L262)；本次预留/恢复/重试测试通过 | 硬上限真实，但未保证在上限内为候选评测留足资源；不同新run没有自动共享campaign账本 | 保持硬上限，增加发布后评测优先和资源预留，所有重启成本归同campaign | P1，调度部分P0 |
| B07 | Verified | [晋升规则](../src/bridge_evolve/evolution/controller.py#L550)、[冻结父子](../src/bridge_evolve/evolution/controller.py#L27)；本次中断恢复、错误不获胜和门槛测试通过；历史64副晋升已复算 | 工程门禁成立；有限样本bootstrap不构成统计有效性的完全证明 | 保留冻结标准、父本和不晋升路径；统计局限按A05处理 | P1 |
| B08 | Partial | [规则/状态计数](../src/bridge_evolve/evolution/controller.py#L47)、[变化见证](../src/bridge_evolve/evolution/research_tools.py#L475) | 可排除纯重命名和某些行为不变提交；不能区分阈值编辑、编码重分配与树同构 | 增加语义树/类别划分/消息映射差异指标；仅作研究诊断，不加入适应度惩罚 | P1 |

### C：论文证据

| ID | 状态 | 证据 | 缺口 | 建议 | 优先级 |
|---|---|---|---|---|---|
| C01 | Missing | [source配置](../src/bridge_evolve/evolution/config.py#L55)同时有agent/numbers/research；[旧局部记录](../results/evolution-validation/live-agent-validation.json#L1) | 有两类工具不等于完成同预算方法对照；历史模型、起点和规模也不同 | 同模型、入口、初始模块、token/DDS预算开展Local vs Global | P2，但近期方法实验优先 |
| C02 | Missing | [会话继承](../src/bridge_evolve/evolution/research.py#L64)与archive代码存在 | 未找到Memory-only、Archive-only的独立开关和同预算结果 | 先完成C01，再分离消融记忆与分支选择贡献 | P2 |
| C03 | Partial | [全分布随机牌](../src/bridge_evolve/evaluation/data.py#L53)、[条件开发案例](../src/bridge_evolve/evolution/research_tools.py#L357) | global工程指标存在；条件采样只标为开发，不是标准化Conditional IMP及触发概率报告；局部1NT入口没有接入当前进化评测器 | 在同一完整牌分布上报告触发率、条件收益与全局收益；条件额外采样单独标注 | P2 |
| C04 | Partial | [基线配置](../experiments/e005-corrected-reference-continuation.json#L8)、[基线迁移口径](../results/ccba-prec-v5.5.2-potential-audit-1000-20261029/run.json#L32) | 有两个人工体系工程版，但不是手册完整等价实现；未找到可运行BEN/BRL/Pidgin同条件接入和权重 | 冻结并审计工程基线，后接至少一个取得代码/权重的强外部体系，核对许可与评测条件 | P2 |
| C05 | Missing | [十个本地run](../.runtime/evolution/)和[研究来源记录](../results/astra-research-validation/validation.json#L35) | 多个失败、续跑或调试run不是多次独立、同配置随机重复实验 | 预注册独立种子与共同预算，分别报告轨迹、方差和失败率 | P2，近期与C01合并 |
| C06 | Partial | [完整四人执行](../src/bridge_evolve/systems/inputs.py#L142)、[83规则发布物](../.runtime/evolution/evo-4490f1d93de14d4f9f42fd2585872191/research/g001-Z/release-2.json#L4) | 可组合自然应叫、竞争和满贯，已有回归；没有模块组合前后、固定基准下的独立全局非退化证据 | 建模块接口及固定回归簇，测组合收益与非目标分支退化 | P2 |

### D：后续增强

| ID | 状态 | 证据 | 缺口 | 建议 | 优先级 |
|---|---|---|---|---|---|
| D01 | Verified | [render_v2](../src/bridge_evolve/systems/v2_render.py#L10)、[发布时同源说明](../src/bridge_evolve/evolution/controller.py#L27)；本次冻结候选重新生成说明与存档一致 | 可生成完整规范，不等于人类容易学习或独立重建已验收 | 保持同源规范；学习手册的易用性另测 | P3 |
| D02 | Missing | [研究目标边界](01-research-brief.md#3-有效性条件与成绩分开)与相关结果中未找到搭档实验 | 无学习耗时、执行fidelity、违规率和人类成绩数据 | 第一篇不声称Human-Executable或beat humans；之后专门实验 | P3 |
| D03 | Missing | [固定anchors](../src/bridge_evolve/evolution/execution.py#L201)、[候选归档](../src/bridge_evolve/evolution/controller.py#L450) | archive没有自动成为历史对手池，当前也无自适应对手训练 | 先保留独立固定锚点，强基线后再做league | P3 |
| D04 | N/A | [研究工具限制](../src/bridge_evolve/evolution/research_tools.py#L111)禁止Agent改引擎 | 没有跨任务、同预算下的研究方法自改进证明 | 当前不应引入，不阻塞第一篇 | P3 |

## 5. Benchmark与统计有效性审计

### 5.1 已经做对的部分

- **分数正确的计算路径**：真实庄位、局况和加倍状态进入计分；`signed_imp(table1_NS-table2_NS)`先逐副转换再汇总。两桌只形成一个基础样本；两个对手也不当成两倍独立牌数。[实现](../src/bridge_evolve/evaluation/experiment.py#L39)与本次对应测试通过。
- **样本去重与复现**：牌族哈希归并座位循环旋转；生成时拒绝重复牌族，run内跨轮次也检查重复；同一轮共享原始牌、发牌人和局况，恢复不重新抽牌。[生成](../src/bridge_evolve/evaluation/data.py#L36)、[轮次](../src/bridge_evolve/evolution/execution.py#L148)。
- **真实DDS身份**：有DDS版本、构建和库身份校验；本次真实求解与缓存测试通过，没有以fixture替代正式结果。[适配调用](../src/bridge_evolve/evolution/execution.py#L109)。
- **不把错误当胜利**：候选执行错误使其无效，固定对手或DDS/评分错误阻塞比较；保留失败向量，不靠删除错误牌进入排名。[结果分类](../src/bridge_evolve/evolution/execution.py#L281)、[固定对手处理](../src/bridge_evolve/evolution/controller.py#L294)。
- **正式确认独立于开发选择**：通过full后生成新promotion牌，以候选与父本逐副收益差确认；探索发布不自动晋升。均值至少`0.05`、校正单侧下界大于0、任一对手的点估计退化不超过`0.05`才接受。[晋升](../src/bridge_evolve/evolution/controller.py#L550)。
- **最终目标与工程晋升分开**：选择候选后另发10,000副，对两个锚点联合配对重采样，至少20,000次bootstrap，每个比较名义alpha为0.025；强目标看两个下界均超过0.20。本地十个run没有消耗过goal检测。[目标检测](../src/bridge_evolve/evolution/controller.py#L580)。

### 5.2 仍需解决的统计问题

1. **Bootstrap精度和覆盖率没有被证实。** smoke的64副确认只做200次重采样，`alpha=.005`取的是排序样本的极端尾部，分位估计很粗；pilot的2,000次也只有约10个尾部样本。代码有Bonferroni分配不等于实际一类错误率已校准。建议用与真实IMP稀疏/重尾分布相符的零效应与小效应模拟，报告覆盖率、错误晋升率、功效及尾部Monte Carlo误差。
2. **每run的名义错误预算不是整个项目的预算。** 单run最多10次晋升确认、1次goal检查；显式新建实验可以带入工作区，但没有自动继承整个campaign的确认/最终尝试账本。E004到E005人工扣减了资源，但不能据此认定普遍实现了跨run统计预算。
3. **没有封存池是既定研究决定，不应被误判为自动信息泄露。** 候选先冻结、后发新牌，本身可以形成有效的前瞻确认。缺的是持续的审批/尝试台账与报告边界，而不是必须建立固定Test牌库。
4. **已完成最终轮次可被研究工具读取。** 当前[read_evidence](../src/bridge_evolve/evolution/research_tools.py#L306)没有区分开发完成轮次与最终报告轮次。若之后继续研究，应将其标记为已消耗历史证据，之后的新结论必须使用预先分配的另一独立确认，不能再叫sealed test。
5. **固定对手保护只有点估计约束。** “任一对手delta不低于-0.05”并非该对手上的统计非劣证明。报告必须按实际门槛措辞，不能外推。
6. **样本量没有功效依据。** 200/1,000/2,000/10,000是工程配置，不能直接当成“足够”。尤其低触发率约定需同时报告有效触发牌族数和全局效应。

### 5.3 基线与代理奖励边界

- CCBA/精确是明确带有压缩、替代及假设的工程移植。没有完整手册等价或外部强机器人对照，因此不能把击败工程版称作击败完整专家体系。
- v5.5.3曾在原128副的第86副出现公开套长保证不真实；v5.5.4修复后对原128副/256桌、2,399次决策的检查记录通过，并有6个真实DDS配对回放。该修复[证据](../results/public-bound-fix-validation/validation.json#L14)保留，但本次没有重新执行整个经典体系审计。
- E005明确记录旧/新参考哈希，旧成绩没有拼到新参考下，这是正确处理。128副旧反例通过不等于全分布无缺陷，近期仍应加强基线和候选的公开合同回归。
- 双明手墩数用于定约计分，不是实际不完全信息打牌。当前只能报告“固定对手与DDS计分下的叫牌体系表现”，不能说人类比赛胜率或战胜人类。
- 固定规则对手能收到公开定义，不等于会主动学习适应新约定；未知协议利用固定对手弱点的收益需与普遍叫牌质量区分。

## 6. 研究空间、反馈、记忆与新颖性

### 6.1 搜索空间没有被局部API锁死

旧[apply_proposal](../src/bridge_evolve/evolution/proposals.py#L147)确实有局部编辑约束；但默认`source=research`进入另一条完整研究路径。不能只审旧提案器就得出“只能调HCP”的结论。

当前可以修改全部特征、叫品动作、优先级、公开声明、状态转移、参数和fallback，允许中间退步或暂时无效；只有发布时才编译、跑测试和冻结。预算/16MiB存储限制是资源限制，不是规则复杂度适应度惩罚。新算子仍需独立语言扩展，这是合理的可信边界。

近期真正需要的是**实验对象约束**：固定15–17均型1NT入口和外围体系，但允许应叫、再叫、信息分类及编码方式整体重设计。不要把它变成长期的规则白名单。

### 6.2 当前研究反馈不是单一分数，但尚不充分

可以读取完整历史案例、逐叫条件、公开状态、作者测试失败及真实探索结果；后续会话可继承工作区，下一代也有最近候选状态和收益差。这足以开展原型研究。

不足在于：

- 初始问题组按最终IMP排序，不能说明哪个具体信息分割导致损失。
- 罕见复杂分支可能形成很多单例组；缺稳定的上层损失切片和覆盖分母。
- Hypothesis/Mechanism/Expected Outcomes/Plan并非结构化发布必填。
- 研究记忆仍是笔记和最近反馈，不是带证据状态的可证伪结论库。
- 归档保存了多个候选和未发布分支，但还不是完整的population搜索策略。

### 6.3 “有行为改变”不能代替“发现新约定”

[test_results](../src/bridge_evolve/evolution/research_tools.py#L475)只要求某个声明为变化见证的测试产生不同叫品/序列；发布接口允许带人工prefix的decision测试，通用门禁没有强制所有创新见证都自然可达。full阶段的随机拍卖变化检查能补充实际到达性，但不是结构新颖性度量。

最新第二发布物的8个见证实际都是无人工前缀的完整拍卖，这是好证据；仍需比较类别划分、消息含义和后续树结构，排除仅阈值变化或重命名同构。结构复杂度只作报告维度，**不得偷偷加入适应度或并列惩罚**。

## 7. 按依赖关系排序的开发计划

粗略复杂度为熟悉现有实现者的工程人日，不包含付费研究等待、DDS大批量耗时或外部基线获取。

| 顺序 | 任务 | 最小改动方向 | 最小可验收测试 | 依赖 | 复杂度 |
|---|---|---|---|---|---|
| P0-1 | 让冻结候选先获得成绩 | 在发布/谱系边界持久化可评测队列；允许模型会话预算停止后处理已有release，或提供不请求模型的评测阶段；评测仍受总时间/配对硬上限约束 | Z发布、A触发模型预算异常：Z仍能在已预留预算内跑validity/screen/full/confirmation；恢复不重复花模型额度或确认次数；墙钟/配对耗尽必须真正停止 | 现有冻结发布、Evaluator | 2–3日 |
| P0-2 | 一个模块的可信覆盖合同 | 先限定1NT研究入口和外围；复用v2，不新造DSL；明确退出/回交、边界及干扰策略，生成反例报告 | 原子替换至少两个耦合分支，父本不变且可回滚；有限抽象状态覆盖；人为制造同优先级重叠、接收方漏分支、无合法出口都能发现并报告反例 | 现有IR；与P0-1可并行 | 2–4日 |
| P0-3 | 统一统计与最终确认账本 | 加campaign身份、候选选择冻结、确认/goal尝试和参考修订记录；保持现场新牌，不建强制固定池；增加预算规则校准工具 | 新建续跑不能重置campaign已用尝试；未完成/未批准最终数据拒读；完成反馈后明确消耗；零效应模拟的假阳性率与名义门槛有报告，不能仅检查字段 | 现有冻结配置；不依赖P0-1 | 2–4日 |
| P1-1 | 先完成一个可分析工程小循环 | 单研究谱系、优先现有候选；每个发布都得到完整结果后再扩搜索 | 在低预算下自动完成5–10个可分析候选；至少20–30个小研究单元可恢复、有明确假设/失败/结论，不要求正收益 | P0-1、P0-2；正式提升措辞另依赖P0-3 | 1–2日接线，运行另计 |
| P1-2 | 结构化提案与反馈 | 扩展release元数据，添加配对收益切片、触发率、适用条件与反例链接；记忆区分proposed/supported/refuted | 一个失败反例能关联原假设、实测结果和下一轮可检验预测；被阻塞/缺失的分数不作为真实零收益统计 | B01/B02现有实现，可与小循环并行 | 2–3日 |
| P1-3 | 创新与资源诊断 | 对IR语义树、类别划分、消息映射和可达行为变化做差异报告，不进入fitness | 纯名称变化、阈值变化、整套消息重编码三类fixture可区分；同构/重排不被报告成发现 | P0-2；独立于记忆 | 2–3日 |
| P2-1 | 受控Local vs Global与独立重复 | 同模型、同入口、同外围、同预算；先证明完整模块设计相对局部编辑的增益 | 多个独立研究种子；预注册主比较、总预算和失败计入规则；同时报告Conditional/Global IMP及置信区间；不以token未花完奖励某组更多重试 | P0-1/2/3、P1-1；新颖性主张另依赖P1-3 | 2–4日接线，运行另计 |
| P2-2 | Memory/Archive分离消融 | 只在基本方法值得继续后增加独立开关与固定资源分配 | 基础Global、+Memory、+Memory+Archive同预算同评测；不得把三谱系并行误称archive消融 | P2-1、P1-2 | 2–4日 |
| P2-3 | 强基线与模块组合 | 接一个可取得代码/权重的强AI；审核人工工程体系合同；做组合前后回归 | 明确版本/权重/许可与公开信息条件；同共同牌/评分/对手评测，未获得资源时保持Unclear；模块组合不得破坏外围回归 | 评测可信；资源获取独立 | 外部基线另评估 |

不建议当前做：自修改Harness、RSI、多Agent辩论框架、先把所有经典手册分支精修完、仅为审查字面要求重建固定Test池，或继续放大预算但仍让已发布候选等待后续研究完成。

## 8. 建议下一实验

### 8.1 先收回已有研究投入

优先将E005的两个**冻结**发布物作为待评测对象，而不是继续其未验证工作草稿。当前引擎已变化，需要显式新实验/迁移、记录父本和来源、重新验收；不更改旧run的身份，不复制旧探索分数，不因此自动追加LLM预算。

用修复后的同一对固定工程基线，先做共同牌有效性和筛选，再对合适候选进行独立确认。即使结果为无提升，也应交付完整可解释的负结果。

### 8.2 第一组论文方法实验

- **研究对象**：固定N为15–17 HCP、4333/4432/5332的1NT开叫，固定敌方Pass；允许完整应叫/再叫子树整体替换。
- **A**：固定人工模块。
- **B**：同一LLM局部规则编辑；限制的是实验条件，不是永久限制DSL。
- **C**：同一LLM整体模块重设计，从A起点出发。
- **D**：没有人工模块初始化，保留基本规则和合法退出。D可晚于B/C，不成功不等于整条路线失败。
- **资源**：预注册相同累计输入/输出token及DDS配对上限，开发与独立确认分别记账；讨论请求也计入。先做一个小pilot测实际方差、触发率和成本，再冻结正式预算与样本量，不把500/2,000/10,000当作显著性保证。
- **重复**：建议pilot从3个独立研究种子开始，仅用于估方差和可行性；正式次数根据可检测效应和预算预先确定。
- **输出**：合法完整但无增益的候选也计入；报告可运行率、结构差异、每单位研究成本、独立确认增益、条件收益、全局收益及失败原因。
- **全局检验**：局部结论成立后，接回固定完整体系，在完整发牌分布评估总体收益；不能把只在条件牌中的增益直接称为Global IMP。

**接线缺口必须先补**：当前[Evaluator](../src/bridge_evolve/evolution/execution.py#L181)固定生成完整随机牌并调用全体系双桌审计；[单次evaluate](../src/bridge_evolve/evaluation/experiment.py#L89)也拒绝非general体系。虽然已有局部叫牌入口，但尚无可直接运行上述A/B/C/D实验的现成命令。不要把`auction run`无评分结果当作局部IMP评测。

### 8.3 现有准确命令与预算边界

以下状态命令不发模型请求；预算停止状态可能返回非零退出码，这是显式失败状态而非工具坏了：

```bash
.venv/bin/bridge-evolve evolve status \
  --run .runtime/evolution/evo-4490f1d93de14d4f9f42fd2585872191
```

现成研究配置的命令确实存在，但**本次未执行**：

```bash
.venv/bin/bridge-evolve evolve run --config experiments/e003-smoke.json
.venv/bin/bridge-evolve evolve verify --run <兼容当前引擎的冻结run目录>
```

前者可能消耗64次模型请求、800万token、10,000配对、最多1小时，并非本报告建议立刻追加运行；其结果仍只是工程验证。后者会先选择候选，再可能进行10,000副、20,000双桌配对/40,000桌检测，另有选择阶段开销；需先冻结campaign错误预算并获得运行批准。实际时间应由当前体系和真实DDS小样本计时外推，不虚构精确时长。

## 9. 本次实际验证与未验证范围

### 已执行

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests BRIDGE_DDS_REQUIRED=1 \
  .venv/bin/python -m unittest \
  core.test_cards core.test_auction core.test_scoring \
  systems.test_language systems.test_runtime systems.test_baseline systems.test_v2 \
  test_evaluation test_evolution test_research
```

结果：**154 tests，98.064秒，OK，零跳过**。包含真实DDS小规模集成；研究模型响应采用测试替身，不意味着本次重新完成付费自主研究。

此外只读执行：

- 核对最近六个运行的详细状态，并检查全部十个运行的晋升与goal计数；只找到一次64副工程晋升，未找到万副检测。
- 对旧64副确认及最新128副探索，核对summary与全部相关逐副日志哈希、IMP向量，复算配对收益和区间。
- 重新编译最新冻结83规则/31状态体系；重新生成说明，与保存的63,755字节说明完全一致。
- 对比当前引擎与E005冻结身份，确认不同，因此直接resume会被保护性拒绝。

### 没有完成或没有执行

- 另尝试重跑冻结候选全部72项断言，因耗时超出轻量审查范围主动停止，**未取得完整通过结果**。72/72及8个见证是发布物中的历史记录，本次不将其标为全部重跑通过。
- 未重跑完整Python套件、前端或所有经典体系的大规模审计。
- 未用当前引擎重新执行旧64副/128副全部拍卖与DDS；“日志复算一致”不是“整批当前引擎重放一致”。
- 未下载模型、安装依赖、调用付费LLM、启动新pilot或万副评测。
- 未验证BEN/BRL/Pidgin权重、外部基线成绩或人工手册完整等价性。
- 项目目录没有可用Git元数据，无法审核提交历史或生成可靠branch diff；本报告依据当前文件和持久记录，不声称确认了未提交变更边界。

## 10. 最终建议

**不用推倒重来，也不要继续把主要精力放在“增加Agent能力”。** 完整IR、原子替换、受限执行、真实DDS、持久研究和版本归档已经形成有价值的底座。

下一步应以可测量交付为中心：**让一个已经发布的协议完成实验，让一个完整模块有明确覆盖与出口，让整体设计在同预算独立对照中接受证伪。** 达到这三点，才能把“工程上能自研究”推进到“科研上证明自研究有效”。
