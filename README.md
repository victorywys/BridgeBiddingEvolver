# Bridge Bidding Evolution

桥牌叫牌体系自进化实验项目。已有基础核心、开发自动迭代、兼容v1的v2公开参数体系语言，以及四人通用叫牌和局部1NT运行模式。

项目目标：从可执行、可用自然语言完整描述的搭档叫牌协议出发，通过同牌评测、自动变异和种群筛选，持续寻找成绩更好的体系。

**已接通真实DDS、两桌同牌IMP评测及首版Agent体系进化闭环，并提供CCBA/精确可执行工程基线。** 它们保留主要结构，但不是手册全部约定的逐条完整移植；压缩/替代部分明确列于迁移清单。不把工程基线结果冒称原版体系的实战实力。

## Agent自进化与10000副目标

当前[E004持续迭代](docs/12-self-evolution.md#19-e004持续迭代与实时看板)已接通已发布冠军续跑及实时看板。首页点击“实时追踪”，可看当前步骤、预算、重要里程碑、同轮成绩图表、研究草稿与冻结冠军，并直接阅读/下载当前体系；关闭页面不停止独立后台进化。固定对手检查失败会明确显示，研究可继续，但不会据此确认排名或晋级。

2026-11-04已发布[v5.5.4公开承诺修复](docs/09-classic-evaluation.md#23-v554自然再叫与满贯探索的公开编码消歧2026-11-04)：原128副/256桌重放无错误。[E005续跑](experiments/e005-corrected-reference-continuation.json)明确使用修复后的参考，保留旧研究、旧成绩与已用额度；网关503等暂时错误使用同模型有限重试，不自动降级。

[E003自主体系研究](docs/12-self-evolution.md#17-e003自主研究agent)：默认使用**GPT-6 Astra**，由持续会话自主读取体系、整体增删规则/含义/状态/优先级/fallback，生成牌例、编写实际测试、调试、讨论和试验，再提交完整冻结候选。支持CCBA/精确现有起点及空白原创谱系，原始两个对手不随冠军更替。**不预留10000副或其他测试池，每个新评测轮次现场随机发牌**；同轮各版本用相同牌，恢复/重试保留该轮原输入。

```bash
# 验证实际可用模型；不输出密钥
.venv/bin/bridge-evolve evolve models
# 一代空白起点研究验证，不自动运行10000副
.venv/bin/bridge-evolve evolve run --config experiments/e003-smoke.json
# 现有双谱系＋空白原创，三代pilot
.venv/bin/bridge-evolve evolve run --config experiments/e003-research.json
```

首页“体系自进化”可设置模型、起点、研究任务和预算，查看实际prompt、研究记录、讨论、工具输入/输出、测试和完整候选，取消/恢复及打开牌例。研究权限通过数据工具落实，不执行任意主机shell；新算子仍走独立引擎扩展。Agent失败不会伪装成数字变异成功或自动切换便宜模型。**可运行、某谱系晋级和万副双基线目标是三种不同结论。**

尚未发布的工作可通过“承接工作区，创建新实验”或`--from-research OLD_RUN_ID`继续，保留体系/案例/断言和来源哈希，而不复制成绩或抹掉旧用量。

新实验默认模型为`gpt-6-astra`，推理`high`；研究配置的共享上限为576次模型请求、1200万token、20万个比赛配对、6小时。每次研究最多48轮（smoke默认16轮），讨论也消耗同一预算。请求/token先预留，未知用量保留额度，恢复不重置；没有可靠货币价格，不伪造美元成本。详见[模型与预算](docs/12-self-evolution.md#173-模型和预算)。

之前E002的[真实Agent闭环](results/evolution-validation/live-agent-validation.json)完成3代、6个局部提案和1200个配对，0次晋级；这是旧版工程验证，不是当前自主研究能力或万副提升结论。旧记录及其模型身份保持不变。

E003已完成[真实Astra研究验证](results/astra-research-validation/validation.json)：从空白自行编制并发布58规则、22状态的体系，40项执行测试通过，经新64副确认相对空白父本晋级；同批对原两体系的观测成绩仍为负，**不是万副目标达成**。两段研究94次请求、约343万token，实际过程、用量及限制见[验证记录](docs/12-self-evolution.md#18-e003真实astra研究验证)。

## 紧凑记录与Agent分析（2026-11-03）

新审计及正式评测采用共享字段／对象布局、长文本和表达式字典，以及按搭档方记录的公开状态增量；完整信息无损保留，显示或选择分析时才展开。旧JSON/JSONL仍可读，不覆盖历史文件。

同一1000副记录由约145MB降至**13.65MB（减少90.6%）**，再gzip约2.09MB；全部短摘要约2.46MB。Agent可先用`records stats`读取约1.2KB的整批汇总，再通过`records show --board ...`或`--view decision --table ... --turn ...`展开问题。显示器新增无损压缩记录和Agent摘要导出，压缩记录及新审计ZIP可完整导入。

格式、CLI及HTTP示例见[紧凑记录说明](docs/11-compact-records.md)；可直接查看[1000副紧凑样例](results/compact-record-validation/boards.compact.jsonl)、[短摘要](results/compact-record-validation/boards.agent.jsonl)及[统计](results/compact-record-validation/statistics.json)。短摘要明确区别于完整审计，不会被误当作完整记录导入。

## 审计工作台（2026-11-02）

普通界面聚焦手牌、叫品含义、联手信息和成绩，工程检查及规则数据收进折叠详情。首页可重命名、分类、批量删除/恢复审计；删除进入回收站，不物理清除原文件。

1000副结果现先加载轻量列表，完整逐叫信息按副读取，频率和筛选仍覆盖整批。实测初始JSON由145MB降至5.01MB（gzip约606KB），浏览器首屏约3秒，原始详情与完整导出不减少。加载口径、管理方式和API见[工作台说明](docs/10-audit-workspace.md)。

显示器根页面新增首页，可查看历史审计并按自定义牌数、可选种子及四家HCP/套长上下界开启条件批次。种子留空会自动生成并记录，进度按钮打开独立任务窗口。单副出错继续后续牌例，最终汇总原因并可定位/筛选失败牌；失败不补Pass或零分，成绩只取明确标注的有效配对子集。任务保存实际两桌输出、可用的真实DDS/par及报告，支持取消和原输入重试。

每副可编辑、预览并保存**人类参考叫牌**，允许完整序列或部分前缀，保留版本、体系来源和建议理由，可独立导出JSONL。点击系统叫品可看完整前缀的出现次数、同局面选择率及占全批不同牌例的比例；两桌不会误当成两副，人类参考不混入策略和系统频率。

详细口径、存储、限制和API见[审计工作台说明](docs/10-audit-workspace.md)。既有`?run=...&board=...`链接保持直接审查，原冻结成绩不改写。

打开结果会立即显示加载阶段和耗时，失败可重试。完整任务核对证据后直接读取已保存的叫品说明，不再为打开1000副结果重复重放整批解释。

审计执行现支持逐叫增量公开状态、原生DDS与叫牌双线程重叠，以及牌例级多进程。首页可选1/2/4进程，自动模式64副起最多2进程；冻结体系、输入副号顺序、完整两桌确定性复跑和前5副新鲜DDS核验均保留。小批次默认单进程以避免体系加载开销，报告单独记录DDS缓存命中与新求解数量，详见[并行边界](docs/10-audit-workspace.md#并行执行与信息边界)。

同牌性能验证：20副无DDS缓存时，先前30.01秒、优化后单进程14.31秒；128副同为缓存命中时，优化后单进程52.97秒、双进程33.00秒（含进程启动）。原1000副已另存为[严格等价复验](http://127.0.0.1:8765/?run=audit-5f923635801b413da7eec11a7c2e19f8&filter=all&sort=number&board=1)，228.35秒完成，2000桌及18,502次逐叫记录、DDS和分数全部相同；本次使用1000次DDS缓存命中，不能与旧批次直接归因比较。436项Python回归、22项前端测试及浏览器验证通过，见[机器验证记录](results/audit-performance-validation/validation.json)。

## v5.5.3：第55副的无将定量邀贯入口（2026-11-02）

已修正最新v5.5.2批次第55副：精确桌原走`1NT–4C（Gerber）–4S（两A）–4NT（止叫）`，不是直接的定量邀请。北家19点对同伴13–15点只能保证联手32点，Gerber后没有达到直接叫6NT的33点下界，提前结束协议。

新[CCBA](systems/classics-v5.5.3-compact/ccba.json)/[精确](systems/classics-v5.5.3-compact/prec.json)将Gerber入口限于已保证33+点；均型联手32–34等仍需确认点力的情况走已有4NT定量邀贯。南家15点加上北家公开18+点可保证33，接受到6NT；13/14点仍可拒绝。真正Gerber后的4NT止叫不改成邀请。第55副新鲜真实DDS为12墩，精确桌NS分数由690变1440。

修正作为[独立单副重跑](http://127.0.0.1:8765/?run=audit-8cee2f7aeac845c0883404c3649a153d&filter=all&board=55&table=2&turn=3)保存在审计台，不覆盖原1000副成绩。[验证记录](results/classics-v5.5.3-nt55-validation/published-replay.json)及[原因与边界](docs/09-classic-evaluation.md#nt55-quantitative)包含详细来源；55项针对性测试、150项语义契约，以及原批次全部200副Gerber/定量相关牌的400桌重放通过。可在单副审计中选v5.5.3；原批次第55副仍如实保留旧结果。

## v5.5.2：可能性邀请与满贯探索（2026-10-29）

新增独立[CCBA](systems/classics-v5.5.2-compact/ccba.json)与[精确](systems/classics-v5.5.2-compact/prec.json)，不覆盖旧体系：

- **试探不再先要求30点下界**：结合联手可能上界、本人非低限、控制、短套、将牌长度及浪费点；未知40上界本身不够触发。二盖一后低位叫回原花色先定将、保留满贯空间，Splinter明确记录四张支持。
- **邀请与承诺分开**：普通扣叫可低成本继续或回局；问关键张/问Q若可能越过五阶退路，另外检查六阶可接受性，不能把邀请逼成满贯。实际缺门及自然叫/排除问叫的解释一致性纳入审计。
- **确认并修复了邀局过低**：通用2NT不再只能接在1NT后；三阶高花邀请公开额外牌力的共同保证和邀局状态。完整合法牌例中，原17+10点停3S，现到4S；真实DDS均12墩，230分变480分。这个定向全Pass反例不计入1000副审计，也不冒称强度提升。
- **显式换将**：原高花未够八张配合时，先低位说明另一门配合；不再自然叫5C/5D，却被按旧高花的排除关键张解读。

下一批使用1000个不同validation牌族，种子`2026102903`，完整保存两桌历史、真实DDS/par、邀局/满贯探索次数、叫成宕贯及牌后漏局/漏贯诊断。详细门槛、退路、复查记录见[可能性邀请说明](docs/09-classic-evaluation.md#invitation-potential)；本轮是开发验证，不是独立强度确认。

本轮已完成：[1000副审计台](http://127.0.0.1:8765/?run=ccba-prec-v5.5.2-potential-audit-1000-20261029&filter=all&sort=number&board=1)、[专项报告](results/ccba-prec-v5.5.2-potential-audit-1000-20261029/audit-report.md)。2000桌记录229次邀局、63次定将/满贯探索、34次满贯问叫；最终叫到9个小满贯、DDS打成9个，0个大满贯。按桌次×定约方，未叫小/大满贯机会275/84；不能把全知机会当必叫标签。389项完整Python测试、147项契约和18,709次决策检查通过；本轮定向审计通过不等于全面体系验收，仍无独立强度结论。

## v5.4.2：支持问叫、满贯续叫与DDS/Par（2026-10-29）

- 精确`1C–1NT–2H–3S`等六级支持问答现在公开问叫套5+、答叫套3+及“至少Qxx（含Axx/Kxx）或xxxx”的支持质量；无支持档保留否定和张数上界。答叫字面花色不是自然长套。旧第772副已验证经控制扣叫、关键张问叫到达6H；私有AKQ排除推断与公共保证分开。
- 已确认配合、逼局状态的低于成局将牌邀请会启动控制续叫，边花扣叫记录首轮或次轮控制，不误写成自然长套。满贯邀请不等于承诺六阶；保留低限退局、关键张歧义、Q/K及排除问叫的安全后续。人工Splinter/控制叫不再仅凭字面阶数清除逼局。
- 新评分记录同时冻结四家×五花色的DDS墩数/可成阶数和**实际发牌人、局况下的DealerParBin结果**，含坐庄位置、可能的加倍牺牲和统一NS分数。审计台在手牌右下方展示；旧记录可单独求解，不重新叫牌或改写原成绩。
- 新[1000副审计配置](experiments/ccba-prec-v5.4.2-slam-audit-1000-20261029.json)固定validation种子`2026102901`。逐桌记录叫贯、DDS成贯、可成却未叫及叫错定约的独立统计，并可在界面筛选。新牌曾用于开发排错，不冒称独立保留测试；早期候选失败记录保留，不删牌换牌。

入口：[本批1000副](http://127.0.0.1:8765/?run=ccba-prec-v5.4.2-slam-audit-1000-20261029&filter=all&sort=number&board=1)、[专项报告](results/ccba-prec-v5.4.2-slam-audit-1000-20261029/audit-report.md)。详细口径和额外发现见[支持与满贯专项](docs/09-classic-evaluation.md#slam-support-par)。四手牌、DDS和par始终只供牌后使用，不进入策略观察。

本轮结果：**2000桌叫到5个小满贯、DDS打成5个，0个大满贯**。149个基础牌族有可成小满贯、37个有可成大满贯；按“桌次×定约方”计，未叫小满贯293次、未叫大满贯74次。不能把大小贯/两桌当独立样本相加，也不能把全知可成全部当成当时必叫。137项契约、18,069次决策区间/质量检查和独立计分通过；新第717副仍有精确短套答叫被停成终局，审计保持`completed-with-findings`。满贯探索仍偏保守，不宣称全部漏贯或协议缺陷已解决。

## v5.3：公开套长区间与套间关系（2026-10-28）

新增独立[CCBA v5.3](systems/classics-v5.3-compact/ccba.json)及[精确 v5.3](systems/classics-v5.3-compact/prec.json)，不覆盖旧IR或成绩：

- 原v4第580副中的精确1C-2D-2S按原文第333段保证5+黑桃；1C-1D后的四张例外不再被泛化到正应叫分支。低花正应叫后的4441和2NT等待另行处理，不靠虚报五张套兜底。
- 公开账本保存双方四套上下界与套间长短关系，结合13张牌传播；自然1D明确排除五张高花，自然1S后再叫2H保留S不少于H。未知套长上界为13，空牌型域或遗忘信息会报错。
- 私有联手估计和审计同步检查上下界/关系；显示器新增双方套长区间、已知关系及“叫牌后公开更新”，与决策前私有估计分开。旧冻结记录不回填。

验证结果：304项完整Python回归在强制真实DDS模式通过、零跳过；132项严格契约通过；本批1000副validation牌、2000桌、17,947次决策的叫牌前/后区间和关系检查通过。原第580副另作新鲜DDS回归，v5.3为W庄4S、11墩，东家的私有联手黑桃区间为9–13。见[机器验证报告](results/classics-v5.3-shape-validation/validation.json)。

**整批v5.3开发重跑已登记到审计台**：[全部1000副](http://127.0.0.1:8765/?run=ccba-prec-v5.3-compact-audit-1000-20261028&filter=all&board=1)、[只看审计问题牌](http://127.0.0.1:8765/?run=ccba-prec-v5.3-compact-audit-1000-20261028&filter=audit&board=92)。这次是独立冻结的1000副/2000桌完整运行，不是旧v4的单副交互；保留全部17,947次决策的前/后公开信息、私有估计、真实来源DDS计分及[CSV](results/ccba-prec-v5.3-compact-audit-1000-20261028/boards.csv)。[本次审计报告](results/ccba-prec-v5.3-compact-audit-1000-20261028/audit-report.json)仍为`completed-with-findings`，没有把尚未修复的人工终局问题写成通过。

**逐叫品中文解释已接入审计台**：悬停看含义摘要，点击看本次新增公开信息、继续有效的此前承诺、逼叫义务和歧义答档。说明按冻结公开定义只读重建，并逐项核对已存状态；不读实际手牌、私有命中规则或DDS来补造承诺。整批17,947条另存[中文解释JSONL](results/ccba-prec-v5.3-compact-audit-1000-20261028/public-meanings.jsonl)及[派生清单](results/ccba-prec-v5.3-compact-audit-1000-20261028/public-meanings-manifest.json)，原始叫牌/成绩不改写。例子与范围见[中文含义说明](docs/09-classic-evaluation.md#public-bid-meanings)。

使用显示器“体系对比”选择v5.3仍可对旧第580副单独重新叫牌；旧v4牌号与本批开发集牌号不是同一批手牌。规则、边界及验证证据见[v5.3说明](docs/09-classic-evaluation.md#v53-public-shape)。本轮复用已用于诊断的validation牌，不发布新的独立最终测试结论。

## 最新1000副开发审计（2026-10-28）

当前v5.2压缩版完成全新1000副/2000桌真实DDS评测及审计，**最终为 `completed-with-findings`，不是候选语义验收通过**。附件CCBA与本次输入逐字节一致，未修改体系规则或覆盖旧结果。

- 127项严格契约、17,927次决策区间检查、2000桌合法性/庄家核对、独立公式重算2000个分数及1000个IMP、1000行CSV全部通过。
- 定向后审仍确认两类缺陷：人工Splinter 3NT被自动当成已完成成局而停叫（第92、172副，共3桌）；精确2C-2D后的短套3S落入自然停叫，最终打三张联手黑桃的3S（第487副，1桌）。第172副两桌同错、IMP为0，说明配对成绩会抵消共同错误。
- CCBA描述性成绩为-0.141 IMP/副，95%区间[-0.361,+0.089]跨0；结合已发现的协议缺陷，不能作为有效候选排名或确认体系强弱。

见[完整审计与问题复现](results/ccba-prec-v5.2-compact-audit-1000-20261028/audit-report.md)、[机器证据](results/ccba-prec-v5.2-compact-audit-1000-20261028/audit-report.json)及[逐副CSV](results/ccba-prec-v5.2-compact-audit-1000-20261028/boards.csv)。本批是validation开发审计，不是新的独立最终测试；修复和剩余验收须另行执行。

自带牌例台已接入本轮冻结成绩和经过身份/哈希核对的审计证据：[全部1000副](http://127.0.0.1:8765/?run=ccba-prec-v5.2-compact-audit-1000-20261028&filter=all&board=1)、[只看审计问题牌](http://127.0.0.1:8765/?run=ccba-prec-v5.2-compact-audit-1000-20261028&filter=audit&board=92)。页面显示“机械检查通过、语义后审发现问题”，支持跳转第92、172、487副；审计筛选包含两桌同错、IMP为0的第172副，不会改写个人待核查标记。原v4默认入口不变，启动和端口转发见[显示器说明](docs/09-classic-evaluation.md#本机运行)。

## 体系文件无损压缩（2026-10-27）

新增可选的共享表达式导出，继续使用现有v2 `features/ref`，不改叫牌条件、规则优先级、公共账本或逼叫语义。独立产物：[CCBA压缩JSON](systems/classics-v5.2-compact/ccba.json)及[同源规范](systems/classics-v5.2-compact/ccba.md)、[精确压缩JSON](systems/classics-v5.2-compact/prec.json)及[同源规范](systems/classics-v5.2-compact/prec.md)。

- 当前烟测候选的JSON从88.08/110.64MB降到3.45/4.35MB，均减少约96%；结构保留缩进，短表达式内联。
- v2中文规范完整定义每个特征一次，使用处引用名称，不再反复全文展开；两套规范从11.52/14.71MB降到2.10/2.63MB。
- 171项相关回归测试、127项严格语义契约通过；同一批100副/200桌、1741次决策的叫品、规则、公开状态、私有推断、定约和逐副IMP与冻结记录一致。
- 单套独立新进程的加载/哈希/说明初始化从约9–12秒降到1.1–1.4秒；同牌热运行100副的纯叫牌为22.01秒与19.75秒，单次测量不代表固定吞吐保证。

旧IR和历史成绩未覆盖，也未更换默认评测。压缩父版本是上次“当前源码100副烟测”的实际候选，不是仍有已知缺陷的旧v5.2导出；新哈希和父身份见[迁移清单](systems/classics-v5.2-compact/migration.json)。复现命令、测量与边界见[压缩说明](docs/09-classic-evaluation.md#15-体系文件无损压缩与等价核对2026-10-27)。

## 当前进度与100副烟测审计（2026-10-27）

实际进度已超过下方v5历史记录，但尚未完成新版本正式验收：v5/v5.1的1000副尝试分别在0基索引255/345失败；已导出v5.2的2048副开发审计在索引1206发现公开HCP下界12高于真实10点。

当前源码已有后续修正，但常规v5.2 IR尚未同步。本轮不覆盖旧IR或历史成绩，而将当前编译器隔离导出，在100副新validation牌上完成真实DDS两桌烟测：**100副/200桌、1741次决策区间检查、127项严格语义契约全部通过，0无效/删除/替换**；另通过200桌合法性/计分核对、5副新鲜DDS交叉核对及100行CSV检查。旧IR的索引1206反例仍能复现，当前候选在该副两桌均通过。

[完整烟测审计](results/ccba-prec-translation-current-smoke-100-20261027/smoke-audit.md)和[机器报告](results/ccba-prec-translation-current-smoke-100-20261027/smoke-audit.json)区分烟测通过、版本漂移及剩余验收缺口。CCBA相对精确+0.010 IMP/副，95%区间[-0.720,+0.730]跨0，不确认强弱或版本提升。本轮未执行当前候选的全部2048副开发审计、完整Python回归或新的1000副正式测试，也没有实现M5体系进化。

本地显示器支持[直接打开这100副烟测](http://127.0.0.1:8765/?run=ccba-prec-translation-current-smoke-100-20261027&filter=all&board=1)，展示冻结成绩、审计检查数及未完成验收事项；原v4默认入口不变。启动与端口转发见[显示器说明](docs/09-classic-evaluation.md#本机运行)。

## 翻译v5：自然叫品与公开语义（2026-10-27）

已确认第465副是翻译错误，不是手册要求叫单张：精确原文第108段的1H/1S是自然套，通常5+，4441等例外允许4张。旧的无条件兜底只选最便宜合法花色；v5要求实际4+，第465副精确改1S、CCBA改2S，第994副精确改1S。

同时补齐自然应叫/再叫、支持、精确4441/牌型接力、Smolen、傀儡、低花问叫和竞争叫牌的公共长度/点力信息；支持同步确认将牌。转移后5张套加开叫人仅保证2张不能直接按八张配合支持成局。第580副精确现在实际叫到W庄4S，支持后公开黑桃8+，真实DDS为11墩。

- [CCBA v5](systems/classics-v5/ccba.json)及[完整规范](systems/classics-v5/ccba.md)：2274条规则、76状态；[精确 v5](systems/classics-v5/prec.json)及[完整规范](systems/classics-v5/prec.md)：3042条规则、93状态。
- [验证报告](results/ccba-prec-translation-v5-validation/report.md)：117项严格语义契约全部通过；新1024副开发牌、2048桌、18,440次决策的公开长度/HCP区间审计通过；263项Python测试在强制真实DDS模式下通过，零跳过。
- 共同公共保证取所有有效发送分支的交集：自然强开叫再叫保守保证4张，原套重叫保守保证5张，不从私有命中分支宣称5/6张。人工花色和双套示选不硬填自然长度。

显示器已登记v5，可在旧牌例的“体系对比”中选择CCBA v5.0与精确 v5.0重新叫牌；原始v4记录和成绩不变。此轮交付是语义修正版，**没有发布新的1000副正式成绩，也不是对全部手牌/历史的形式证明**。范围、命令和原文证据见[v5说明](docs/09-classic-evaluation.md#14-v5自然叫品与公开语义修正2026-10-27)。

## v4历史语义完整性审查（2026-10-27）

第580副之后的[全面语义审查](results/ccba-prec-translation-v4-1000/semantic-review/audit.md)确认，遗漏不限于该副：自然新套/重叫、简单加叫/Drury、二盖一低花、部分无将和精确牌型接力没有完整写入公开账本；通用成局支持后未确认将牌；个别自然兜底甚至可叫出单张花色。**v4合法性/点力区间审计通过，不等于搭档协议含义完整。**

- 静态阅读全部15个行为编译模块，执行112次定向契约检查：75次问题实例、37次正确对照、0未复现。两体系同模板重复计数，不等于75个独立bug；定向场景也不能当作随机总体错误率。
- 原1000副/2000桌、17,756次决策重放中，找到3次单张红心的兜底发送反例（第465副两桌、第994副一桌），以及31次自然成局后未登记将牌。规则局部候选统计只用于定位，不能把命中私有分支的保证直接当作公开含义。
- [机器可读审查证据](results/ccba-prec-translation-v4-1000/semantic-review/audit.json)保留前缀、手牌、规则、预期/实际写入值及覆盖范围。检查器状态为`completed-with-findings`，不是策略验收通过。

该轮只新增只读审查工具、报告和测试，审查当时执行期源码、v4 IR及原成绩哈希均未变化。其248项Python回归通过表示工具兼容且能识别已知缺陷，**不表示当时已修好策略**；后续修复见上方独立v5。历史复查边界见[语义审查说明](docs/09-classic-evaluation.md#13-自然叫品语义完整性审查2026-10-27)。

## 翻译v4：点力区间与自然成局（2026-10-26）

现在同时记录公开/私有HCP上下界：例如精确1D为11-15、CCBA1C为12-21；本人13点面对搭档11-15时，私有联手估计为24-28。开叫前Pass的精确<=10、CCBA<=11按座位保存，角色确定后仍有效；竞争或埋伏Pass不自动限点。

已修复竞争分支只因本人13点便自然跳3NT的错误，普通自然3NT须检查联手下界25，不能拿上界或区间中值当保证。旧第260副同一决策由3NT改为1NT，显示私有21-24。另修正精确2C后续、莱本索尔、弱无将邀请与低花转移冲突，以及精确Jacoby有限高限等声明。

- [CCBA v4](systems/classics-v4/ccba.json)与[规范](systems/classics-v4/ccba.md)：2261条规则、76状态；[精确 v4](systems/classics-v4/prec.json)与[规范](systems/classics-v4/prec.md)：3029条规则、93状态。
- [区间政策](systems/classics-v4/translation.json)与[迁移清单](systems/classics-v4/migration.json)记录上下界、Pass推断、空区间失败和人工/自然3NT区别。
- 246项真实DDS模式测试通过、零跳过；[开发审计](results/ccba-prec-translation-v4-1000/development-audit.json)通过1024副/2048桌、18,247次决策的公开/私有区间检查。
- [新1000副报告](results/ccba-prec-translation-v4-1000/report.md)：2000桌completed、0无效/0删除/0替换；[正式区间审计](results/ccba-prec-translation-v4-1000/audit.json)通过17,756次决策。均值-0.156 IMP/副，95%区间[-0.365,+0.071]跨0，不确认哪套更强。

显示器默认打开v4，显示联手/双方HCP区间及Pass座位上界；旧记录无上界时仍显示下界。详见[v4交付说明](docs/09-classic-evaluation.md#12-v4点力上下界与自然3nt修正)。这是有限样本上的区间审计通过，不是所有牌型证明；不同牌集的v3/v4成绩不能直接相减当作改进。

## 翻译v3：联手账本与审计通过（2026-10-26）

已补充公开的确定将牌、双方HCP/各套张数下界、八级叫牌目标，以及独立的私有联手估计。未展开的后续按配合与承诺等级继续；限制重复否定性X和高阶盲目XX，并修正Landy/马歇、无将转移、轻开叫再叫等发送/解释冲突。v3为策略版本，仍使用兼容扩展后的v2 IR。

- [CCBA v3](systems/classics-v3/ccba.json)与[规范](systems/classics-v3/ccba.md)：2260条规则、76个公开状态；[精确 v3](systems/classics-v3/prec.json)与[规范](systems/classics-v3/prec.md)：3027条规则、93个公开状态。
- [翻译与联手状态政策](systems/classics-v3/translation.json)明确八级目标、公开下界、私有估计、竞争处理和默认续叫。
- [完整开发审计](results/ccba-prec-translation-v3-1000/development-audit.json)：1024副/2048桌、18,203次决策的公开下界检查通过；232项Python测试在强制真实DDS模式下全部通过，零跳过。
- [新1000副正式报告](results/ccba-prec-translation-v3-1000/report.md)：2000桌全部完成，0无效/0删除/0替换；CCBA相对精确-82总IMP、-0.082 IMP/副，95%区间[-0.311,+0.130]跨0。
- [正式审计](results/ccba-prec-translation-v3-1000/audit.json)：2000桌合法性/庄家/计分/统计/哈希及17,709次决策下界检查通过，另有5副真实DDS交叉求解、10桌策略重放。新运行X/XX为330/9，9次XX均为明确的无将逃叫。

v3记录仍可在显示器选择查看；点击叫品可看当时的“联手信息”及私有估计。详见[v3交付说明](docs/09-classic-evaluation.md#11-v3联手下界目标续叫与审计)。旧版本和失败开发记录保留，不把不同牌集的成绩差或X次数差当成版本提升证明。该轮是有限样本审计通过，不是所有叫牌状态的形式正确性证明。

## 翻译v2与自动运行（2026-10-26）

已按工作区两份原始DOCX补齐双路重询/XYZ、第四花色、逆叫出口、Mini-Splinter、帮张、精确牌型接力、Michaels/马歇/Landy、莱本索尔以及RKCB后问Q/问K/缺门关键张等协议。模糊评估采用明确的暂定口径，原文冲突单独记录，不声称逐条形式等价。

- [CCBA v2 JSON](systems/classics-v2/ccba.json)及[中文规范](systems/classics-v2/ccba.md)：1143条规则、75个公开状态。
- [精确 v2 JSON](systems/classics-v2/prec.json)及[中文规范](systems/classics-v2/prec.md)：1460条规则、92个公开状态。
- [翻译与假设清单](systems/classics-v2/translation.json)记录原文段落、规则ID、代表性测试及高低限/邀请/好套等推断；[迁移清单](systems/classics-v2/migration.json)记录版本和来源。
- 1024副独立开发牌、2048桌叫牌检查通过；195项Python测试在强制真实DDS模式通过，零跳过。
- 另完成16副真实DDS烟测和新的1000副正式测试。正式结果为0无效/0删除/0替换，CCBA相对精确总IMP为-107、均值-0.107，95%区间[-0.401,+0.180]跨0，不能确认谁更强。

[正式报告](results/ccba-prec-translation-v2-1000/report.md)、[逐副CSV](results/ccba-prec-translation-v2-1000/boards.csv)、[审计](results/ccba-prec-translation-v2-1000/audit.json)、[配置](experiments/ccba-prec-translation-v2-1000.json)。该版本与此前v1.1使用不同牌集，不能直接相减声称版本提升；旧体系和结果保留不覆盖。详情见[本轮交付与限制](docs/09-classic-evaluation.md#9-翻译v2与本轮自动运行2026-10-26)。

## 牌例审查与显示器

此前v2的1000副中，587副叫牌不同、479副定约不同、346副IMP非零，另有213副叫牌不同但同分。[12副代表性审查记录](results/ccba-prec-translation-v2-1000/review/review.md)包含四手牌、两桌逐步叫牌及命中规则，附[完整审查JSON](results/ccba-prec-translation-v2-1000/review/cases.bridge.json)和[PBN](results/ccba-prec-translation-v2-1000/review/cases.pbn)。第639副和第39副的大分差涉及加倍/再加倍，历史记录保持原样；本轮规则修正在独立v3中交付。

启动本机[牌例显示器](tools/board_viewer.py)：

```bash
npm --prefix tools/board-viewer ci --ignore-scripts
.venv/bin/python tools/board_viewer.py --port 8765
```

打开 `http://127.0.0.1:8765`。支持差异/副号/规则搜索、四手牌、双桌叫牌和私有trace、审查备注、JSON/JSONL/PBN/运行ZIP导入、JSON/PBN导出，以及已登记体系的单副真实重叫。图标与字体均为项目内依赖，不需要外部CDN。交互重叫与冻结记录分开显示，不覆盖原实验。

审计台现支持：

- **自定义牌例**：逐家填写四手牌、粘贴完整PBN/引擎JSON、复制当前手牌编辑，或按整数种子可复现地随机发牌。完整校验后保存到独立牌例库，刷新后仍在。
- **单副审计**：在任意冻结、导入或自定义牌例上选择A/B体系，后台重跑两桌，显示进度并持久保存中文报告、机器报告、原输入/逐叫记录/冻结IR的证据ZIP。默认另做新鲜DDS交叉核验；历史面板可查看或重试，失败不补零分。
- **组合筛选**：座位/联手、均型/双套/三套/单缺/长套、S-H-D-C通配牌型、HCP/套长上下界，以及最终定约阶数/花色/庄家/加倍/成宕结果；可指定任一桌、两桌都满足或单桌。同手、同桌条件不会错误地跨人/跨桌拼接。
- **定位与分享**：直接跳到副号、一键定位首个分歧、重置全部筛选、复制含筛选与选中叫品的链接；支持`/`搜索、左右箭头翻牌、`F`标记、备注中`Ctrl/⌘+Enter`保存。

单副审计是开发诊断，不是整批验收或新的独立测试。工作数据在`.runtime/board-viewer/`，不改写`results/`的冻结证据；备份该目录可保留自定义牌例和任务报告。详细检查边界与API见[审计控制台](docs/09-classic-evaluation.md#audit-console)。

远程服务器使用VS Code“端口”面板转发服务器8765，本地端口可以不同：例如本地18765对应服务器8765，就打开 `http://localhost:18765`，不是服务器地址。标准SSH转发同样支持，无需改成监听`0.0.0.0`。若代理改写Host/协议导致来源校验失败，可在服务器启动时重复指定`--allow-origin <浏览器实际使用的完整来源>`；只信任明确地址，不关闭同源保护。详见[远程端口转发](docs/09-classic-evaluation.md#远程端口转发与可信来源)。

此前v2 test已因逐牌审查[标记消耗](datasets/ccba-prec-translation-v2-test-1000/consumed.json)，历史成绩不改写；后续根据牌例改规则必须用新保留集确认。[格式、操作与边界](docs/09-classic-evaluation.md#10-差异牌例与本地显示器)见说明。当前263项Python测试通过、零跳过；v5语义修正和有限验证范围见上方说明。

## CCBA / 精确：1000副实验

入口见 [移植与真实评测说明](docs/09-classic-evaluation.md)、[实验配置](experiments/ccba-prec-1000.json)和 [迁移清单](systems/classics/migration.json)。两套体系的实际定义和完整生成说明在 [systems/classics/](systems/classics/)。

```bash
# 本机已接入真实DDS；新机器先按说明构建。
.venv/bin/bridge-evolve doctor --json --require-dds

# 1000副冻结数据的两桌比较；输出目录存在时拒绝覆盖。
.venv/bin/bridge-evolve evaluate --config experiments/ccba-prec-1000.json
```

每副四手牌完整、无点力筛选；两桌不移动手牌，只互换体系位置。1000副共2000桌，但只有1000个基础样本。逐副转换IMP、按牌族bootstrap，保存实际庄位、DDS墩数、分数、完整叫牌和私有原子trace。

[报告](results/ccba-prec-1000-v2/report.md)、[逐副CSV](results/ccba-prec-1000-v2/boards.csv)、[运行清单](results/ccba-prec-1000-v2/run.json)。请先核对运行状态为completed，再引用成绩。第一批因协议错误停止的尝试保留为failed，其数据已标记消耗；最终实验使用新生成的1000副，不删除或修补失败样本后继续算均值。

本次已实际完成：1000副、2000桌，最终0无效/0删除/0替换。CCBA工程v1.1相对精确工程v1.1为 **-50总IMP、-0.050 IMP/副**，95%配对区间 **[-0.270, +0.180]**，区间跨0，不能确认哪个版本更强。另有 [独立审计](results/ccba-prec-1000-v2/audit.json)：重放2000桌合法性、独立重算2000个分数、5副真实DDS重新求解交叉核对、10桌策略重放及全部产物哈希检查均通过。

## 查看体系与验证牌例

首个基准：[权威 JSON 体系](systems/1nt-v1.json)、[同源生成的完整中文规范](systems/1nt-v1.md)。它包含无将邀请与双高花转移，不宣称是完整标准体系；Stayman、满贯和竞争处理尚未实现。

在本目录运行：

```bash
# 校验并显示内容哈希。
.venv/bin/bridge-evolve system validate systems/1nt-v1.json

# 查看说明，或用 --output <路径> 导出。
.venv/bin/bridge-evolve system render systems/1nt-v1.json

# 指定手牌及公开历史，查看动作和私有诊断。
.venv/bin/bridge-evolve system decide systems/1nt-v1.json \
  --observation examples/observation.json --trace

# 指定完整牌例；调度器只把自己的牌和公开信息交给每个策略。
.venv/bin/bridge-evolve auction run --system systems/1nt-v1.json \
  --board examples/board.json --trace
```

[观察输入](examples/observation.json) 使用 `S.H.D.C` 手牌记法，缺门用 `-`、十用 `T`。示例南家输入在 `1NT-P` 后输出 `2H`（向黑桃转移）；[完整牌例](examples/board.json) 最终叫到北家4S。叫牌由规则实际执行产生，但尚未求打牌墩数或得分。

`--trace` 显式输出私有命中规则和特征值，仅供研究者牌后查看，不可给搭档或对手。默认公开解释从静态协议的全部相关分支生成，不泄露本次私有命中分支。

详细语言、控制/套质量表达方式、协议状态和边界见 [体系执行器使用说明](docs/07-executable-systems.md)。无需Copilot网关、LLM或DDS即可运行这些命令。

### 通用四人叫牌与参数化协议 v2

```bash
.venv/bin/bridge-evolve system validate systems/general-v2.json
.venv/bin/bridge-evolve system render systems/general-v2.json

.venv/bin/bridge-evolve auction run --mode general \
  --ns systems/general-v2.json --ew systems/pass-v2.json \
  --board examples/general-board.json --trace

.venv/bin/bridge-evolve system context systems/general-v2.json \
  --observation examples/general-observation.json

.venv/bin/bridge-evolve system decide systems/general-v2.json \
  --observation examples/general-observation.json --trace
```

v2支持双方局况、开叫/应叫/介入角色、实际叫过与公开显示的花色、对手预先声明的自然/人工/强弱标签。有类型的公开参数只能由公开事件更新，不能读取手牌或私有trace；动态花色、关键张、加级回答、最经济合法叫品可复用同一套协议。

[通用演示](systems/general-v2.json)与[完整中文规范](systems/general-v2.md)用于测试底座：北1S、南3S，北4NT问叫、南5H回答、北6S结束。回答后选择小满贯是写明的测试退出策略，不是完整的安全满贯决策，也不证明可成约。[Pass对照](systems/pass-v2.json)不代表竞争强度。

可加`--prefix <叫品列表JSON>`从指定合法历史继续。单手命令可用`--public-ns/--public-ew`加载双方事先公开的静态体系定义；缺少对手定义时，不会猜测其叫品含义。详细语法、参数与逼叫语义见 [通用协议v2说明](docs/08-general-protocols-v2.md)。

通用运行是单桌叫牌，不是DDS或两桌配对IMP评测。原局部1NT命令及其固定抽样约束不变。

## 开发与运行

要求 Python 3.11 或更新版本；当前实际验证环境为 Python 3.14.8，开发版本记录在 [.python-version](.python-version)。目前没有运行时第三方依赖；构建后端固定为 `setuptools==84.0.0`，首次安装需要能够获取该构建依赖。

以下命令在本目录执行，使用独立虚拟环境，不修改全局 Python：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .

.venv/bin/bridge-evolve --help
.venv/bin/bridge-evolve --version
.venv/bin/bridge-evolve doctor
.venv/bin/bridge-evolve doctor --json

.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
.venv/bin/python -m pip wheel --no-deps --wheel-dir dist .
```

也可以用 `.venv/bin/python -m bridge_evolve` 调用同一 CLI。以上环境命令已在 Linux 验证；尚未在其他操作系统或其他 Python 小版本上验证。

### 环境诊断与失败语义

`doctor` 报告项目版本、Python 版本与解释器路径、平台，以及 DDS 的真实接入状态。`--json` 输出 `schema_version=1` 的单个 JSON 对象，不混入文本日志，也不打印环境变量或密钥。

- 退出码 `0`：诊断完成且 Python 满足要求；**不保证 DDS 可用**。
- 退出码 `1`：Python 不满足要求，或指定了 `--require-dds` 但没有经过验证的真实 DDS 适配器。
- 退出码 `2`：参数错误或请求了尚未实现的命令。

以下命令要求真实DDS可用；可用时以`0`退出，不可用时以`1`退出且原因写入标准错误：

```bash
.venv/bin/bridge-evolve doctor --json --require-dds
```

诊断仍会区分Python `dds3`模块检测和原生库接入。现在通过ctypes调用固定DDS2.9.0，并实际求解公开fixture后才报告`available=true`、`adapter_status=verified`；没有Python dds3包不等于原生DDS不可用。实际来源、构建标识及库哈希写入JSON诊断。

当前可用`doctor`、`system validate/render/context/decide/build-classics`、`auction run`、`dataset generate/show`、`oracle precompute`、`evaluate`、`records stats/show/convert`及`evolve init/run/resume/status/cancel/verify`。`auction replay`仍未实现。输入/执行/文件错误非零退出，不输出成功结果；fixture/mock不能产生正式成绩。

## 可选 Copilot API 网关

为后续自动迭代准备了独立的 [网关工具配置](tools/copilot-api/package.json)，使用第三方项目 [caozhiyuan/copilot-api](https://github.com/caozhiyuan/copilot-api)。它不是本项目的 Python 运行时依赖，也不是 GitHub 官方提供的服务端部署方式。

当前安装版本为 `@jeffreycao/copilot-api@2.7.35`，另安装了独立编码执行器 `@openai/codex@0.164.0`，通过 `package-lock.json` 固定依赖。系统全局安装曾因 `/usr/local` 写权限不足而失败，因此使用项目内安装，不需要 `sudo`，不修改全局 npm 配置。

要求 Node.js >= 22.13.0；实际已验证 Node.js 24.21.0、npm 9.2.0，以及安装后的主命令、登录命令和服务启动命令帮助。

以下命令在本项目目录执行：

```bash
npm --prefix tools/copilot-api ci
npm --prefix tools/copilot-api test
npm --prefix tools/copilot-api run cli -- --help

# 本人按终端提示在 GitHub 网页完成设备授权，不要把访问令牌粘贴到聊天中。
npm --prefix tools/copilot-api run auth

# 配置本机访问密钥；不会输出密钥，重复执行会复用现有密钥。
npm --prefix tools/copilot-api run setup

# 授权与密钥配置完成后启动。
npm --prefix tools/copilot-api run start
```

网关使用独立的 `.runtime/copilot-api` 数据目录；已创建的 `.runtime` 和网关目录权限为 `0700`，且已在 `.gitignore` 中排除。包装命令清除继承的 GitHub 令牌、OAuth 应用和企业域名覆盖变量，不读取其他工具的凭据。不要复制、提交或公开该数据目录，也不要使用 `--show-token`。

启动地址固定为 `http://127.0.0.1:4141`，不监听外部网卡。已启用网关访问密钥，客户端密钥保存在私有数据目录中的 `gateway-client-key`，不通过命令行参数传递，也不写入版本化文件。实际验证无密钥、错误密钥和普通密钥访问管理接口均返回 `401`。

已完成 GitHub 授权，并验证模型列表和 `gpt-5.3-codex` 的一次真实最小 Responses API 请求，返回预期标记，合计 37 tokens；该探针没有包含项目代码或凭据。另已验证真实 Codex 工具在隔离容器中写入指定文件。开发执行器会通过本机网关向用户选定的 Copilot 服务发送所需代码上下文，不挂载或发送 GitHub 令牌。网关本身不会自动让当前 VS Code 对话持续执行，也不代表桥牌科研实验已经实现。

上游 [安全提示](https://github.com/caozhiyuan/copilot-api/blob/main/NOTICE.md) 说明过量自动化可能触发 GitHub 风控或暂停访问；后续不能用无限并发、绕过限流或自动重试权限拒绝的方式维持迭代。

## 开发自动迭代控制器

[开发控制器](src/bridge_evolve/automation.py) 通过项目内 Codex CLI 和本机 Copilot 网关推进软件开发，不需要 OpenAI 登录。它**不是 M5 的桥牌体系进化器**，当前仅支持 M1 开发范围。宿主机的嵌套 bubblewrap 不兼容问题已通过外部 Docker 沙箱解决，不再是运行阻塞。

- 默认最多 3 轮，每轮执行器限时 1200 秒，整个运行最多转发 30 次模型推理请求。模型列表不占推理额度；每轮可能包含多次模型请求，不是“3 次 API 请求”的保证。
- [模型通道](src/bridge_evolve/dev_sandbox.py) 只允许模型列表及指定模型的 Responses 请求，禁止管理接口和其他模型。推理请求串行、开始间隔至少 1 秒，单次输出上限 8192 tokens，同时限制请求大小和连接数。实际 token/计费不能仅用请求次数推断。
- 每轮执行后独立运行测试，再把失败反馈带入下一轮。模型说完成、退出成功或现有测试全绿，都不会单独触发提前完成。
- 不提供独立完成检查时，轮数或推理请求额度耗尽分别记为 `budget_exhausted`、`request_budget_exhausted`，并以 `1` 退出，表示尚待验收，不伪装完成。先到达请求额度时可以少于计划轮数，这不是“提前声称完成”。
- 可用 `--completion-check <项目内Python文件>` 提供人工定义的完成验收。相对路径基于 `--project`，检查器必须在执行器不可写且包含于源码快照的位置，不能放在私有运行目录或被排除的工具目录中；运行中校验内容哈希。只有测试和该检查均成功才记为 `confirmed`。检查器本身是否足够覆盖里程碑仍需人工审核。
- 项目锁防止并行修改；每轮 JSONL、错误、验证日志、模型请求用量和运行清单保存在私有数据目录中。命令在标准错误输出轮次进度，标准输出最终给出清单位置与停止状态。
- 编码与独立验证均在非 root、无外部网络、只读根文件系统、移除全部 capabilities 的 Docker 容器中执行，限制 CPU、内存和进程数。容器不挂载 GitHub 凭据、网关密钥或 Docker socket，也不继承其他工具凭据。
- 源码快照排除私有运行目录、依赖和缓存；仅核心实现与对应测试目录可持久写回宿主机。其他工作区修改位于临时文件系统中，不会写回项目。验证使用新的容器、只读核心挂载和冻结的基础测试，不挂载模型通道。
- 模型请求通过私有 Unix socket 到宿主机通道，由宿主机注入访问密钥；容器没有任何网关密钥。容器内 Codex 不再嵌套 bubblewrap，但外部 Docker 隔离始终存在，宿主机不直接执行模型生成的代码或测试。
- 执行器出错、权限失败或超时会停止，不盲目重试。Ctrl+C/SIGTERM 会清理本次拥有的容器与通道并保存中断状态，不清理其他用户的资源。
- 当前没有自动推进 M2–M6、模型会话断点恢复或无限续跑。重新运行会基于保留的代码开始一个新记录。

要求 Linux 和可访问的本机 Docker daemon。首次运行会构建固定基础镜像摘要的 [执行镜像](tools/copilot-api/container/Dockerfile)，镜像只安装公共工具依赖，不含项目代码或凭据。从本项目目录运行：

```bash
# 不调用模型，验证隔离与独立容器中的项目测试。
npm --prefix tools/copilot-api run smoke

# 可选：再验证真实模型工具写文件，最多使用 5 次推理请求。
npm --prefix tools/copilot-api run smoke -- --with-model

# 启动 M1 开发迭代。
.venv/bin/python -m bridge_evolve.automation \
  --project . --model gpt-5.3-codex \
  --max-rounds 3 --round-timeout 1200 --max-requests 30
```

已完成一次真实开发运行：第 1 轮自动生成核心代码并通过当时的 50 项独立容器测试；控制器随后自动进入第 2 轮，而不是在模型结束时退出；累计 30 次推理请求后按硬额度停止。该运行没有配置独立完成检查，因此不把它标记为 M1 已确认完成。原始宿主机沙箱失败记录仍保留用于追溯，但不再需要为此放开宿主机权限。

## 当前实现

| 部分 | 状态 |
| --- | --- |
| Python 包、可编辑安装和 wheel 构建 | 已实现 |
| CLI help、版本、模块入口和环境诊断 | 已实现 |
| 行为测试 | 强制真实DDS模式248项Python测试通过、零跳过；另有5项Node测试；不代表自然叫品语义完整性通过 |
| 本机 Copilot 网关 | 已授权，访问鉴权与最小真实推理已验证 |
| 开发自动迭代控制器 | 已在 Docker 中完成真实自动编码、独立验证、进入下一轮及预算停止验证 |
| 叫牌、定约、计分与 IMP 核心 | 已自动生成基础实现并通过当前测试，完整 M1 独立验收待确认 |
| 体系 IR、执行器和中文规范 | v1保持兼容；v2增加公开上下文、含义标签、参数化状态/动作和逼叫约束 |
| 局部1NT基准与指定牌例运行 | 已实现，含邀请、双高花转移和私有trace |
| 通用四人调度 | 已实现任意发牌人/双方体系/竞争X与XX/全Pass/指定前缀 |
| CCBA/精确工程基线 | 主要模块已移植，明确保留手册覆盖差异，不冒称完整原版 |
| 真实DDS与两桌配对评测 | 已实现版本固定、20项表缓存、完整数据、逐副计分/IMP及区间 |
| 体系自动进化 | 尚未实现 |

测试覆盖真实安装后的入口、从源码目录之外调用、JSON 与退出码、秘密环境变量不被输出、模块缺失、版本元数据缺失，以及“检测到绑定不等于 DDS 已接入”。测试中的绑定状态模拟仅用于诊断分支检查，不产生求解结果或科研成绩。

## 已确定的原则

- 第一阶段只按成绩优化，不对规则数量、说明长度、例外数量、学习难度或偏离传统体系施加惩罚。
- 自然语言说明必须足够完整，独立实现者能够据此重建同一策略；不能仅写“由隐藏模型决定”。
- 支持套质量、控制、大牌组合、多轮问叫、接力、转移及新概念；表达能力不能永久限于大牌点和套长。
- 体系按搭档协议整体进化，发送、理解和后续应叫必须同步修改。
- 决策只能使用该牌手当时可获得的信息。DDS 和四手牌仅由评测端使用。
- 先探索高成绩体系，后续另立项目研究精简，不提前把两个目标混合。

## 阅读顺序

| 文件 | 用途 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 编码助手需要遵守的固定项目约束 |
| [研究目标](docs/01-research-brief.md) | 目标、范围、已确认决定和可调整的工程默认值 |
| [架构与接口](docs/02-architecture.md) | 模块边界、数据对象、信息隔离、建议目录和 CLI |
| [体系语言](docs/03-system-language.md) | 规则表达、协议状态、自然语言生成与变异要求 |
| [可执行体系使用说明](docs/07-executable-systems.md) | 已实现v1语法、算子精确定义、CLI和牌例输入 |
| [通用协议v2](docs/08-general-protocols-v2.md) | 任意发牌人/双方体系、公开上下文、参数化动作与逼叫 |
| [经典体系与真实评测](docs/09-classic-evaluation.md) | 移植口径、DDS构建、1000副实验及单副复核 |
| [评测协议](docs/04-evaluation.md) | 双明手、计分、配对比赛、数据划分与晋级 |
| [实施路线](docs/05-roadmap.md) | 分阶段交付、验收条件和必要测试 |
| [实验与记录](docs/06-experiments.md) | 首个实验、运行配置、统计归因与报告模板 |
| [开始实现](prompts/01-implementation.md) | 原始实现提示词，使用时先确认当前里程碑 |
| [继续里程碑](prompts/02-next-milestone.md) | 基于已有代码继续下一阶段 |
| [提议体系变异](prompts/03-propose-mutation.md) | 后续供大模型产生可验证候选协议 |
| [分析实验](prompts/04-analyze-results.md) | 分析成败原因与下一轮假设 |
| [独立审查](prompts/05-review.md) | 审查规则、信息泄露、计分和实验有效性 |

## 后续实施

1. 按 [实施路线](docs/05-roadmap.md) 继续 M1 的独立完整性核查，不把自动生成代码或测试全绿直接当成全部需求已满足。
2. 继续验证首版进化闭环、扩大真实随机新牌实验，再增强结构变异和定向语义检查；不把补齐手册全部分支作为进化前置条件，也不把工程压缩版本冒称完整体系。
3. 每阶段报告实际完成、实际运行的检查、真实 DDS 状态和未完成内容。
4. 真实 DDS 接通前，可以用明确标记的测试替身验证管道，但不能据此发布科研成绩。

设计文档中的其余命令和接口仍是目标契约，不代表当前已经可运行。初版默认 Python、CLI、本地数据、结构化规则；这些是可调整的工程选择，不是研究范围的永久限制。

## 资料起点

- [DDS 官方仓库](https://github.com/dds-bridge/dds)：双明手求解器。接入时固定真实版本并核实该版本 API。
- [Pgx 桥牌叫牌环境](https://www.sotets.uk/pgx/bridge_bidding/)：预计算 DDS 数据与叫牌环境的现有实现思路。
- [A Simple, Solid, and Reproducible Baseline for Bridge Bidding AI](https://arxiv.org/html/2406.10306v1)：自我对弈、历史策略对手和开源研究起点。
- [WBF 桥牌规则入口](https://www.worldbridge.org/regulations/2017-laws-of-duplicate-bridge/)：叫牌与计分规范参考；本项目先研究合法叫牌，不预设具体赛事的体系准入限制。

核查日期：2026-10-23。以上资料用于技术和规则参考，不代表已经复现其结果。
