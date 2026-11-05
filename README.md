# Bridge Bidding Evolver

搜索可执行、可完整说明的桥牌搭档叫牌协议。长期目标是让研究Agent从现有体系或空白骨架出发，通过同牌实验持续找到更强的协议，而不只是调整几个点力阈值。

**当前阶段：基础设施与自主研究原型已具备，正在补齐可靠的研究—评测闭环；尚无万副双基线提升结论。** 近期不扩大模型预算或Agent数量，先解决已发布候选无法及时评价、上下文膨胀和固定参考缺口。详见[下一阶段计划与验收](docs/14-autoresearch-plan.md)。

## 当前能力与边界

| 已有能力 | 边界 |
| --- | --- |
| 叫牌合法性、真实庄家、重复桥牌计分与逐副IMP | 协议逼叫与通用叫牌规则分别检查 |
| 有类型v1/v2 IR、公开参数、同源完整中文规范 | 允许整体修改规则、含义、状态和fallback；不执行任意生成代码 |
| 本人手牌与公开观察隔离 | 运行策略不能看其他三手、DDS、种子或另一桌结果 |
| 固定版本真实DDS、两桌换位、共同牌与冻结日志 | DDS是全知打牌近似，不是逐叫正确答案或人类实战强度 |
| Astra持续工具研究、案例、实际断言、讨论与冻结发布 | 已发布不等于已评价或已晋级 |
| 体系分支导航、依赖/反向引用、语义diff、局面与手牌查询 | 查询层已变小；测试失败信息和交接摘要仍有未修复的大结果路径 |
| 审计工作台、历史整理、紧凑记录、实时看板 | 需要本地实验数据；Git克隆不会携带个人运行历史 |

CCBA/精确是有明确哈希的**工程基线**，不是两份手册所有细节的完整实现。当前v5.5.4仍有已复现缺口，不能把曾经通过一批回归写成全体系正确性保证。

### 最近实验说明了什么？

- **E005**：Z发布两个不同候选，但后续谱系耗尽本地预算前，没有进入外层正式筛选。
- **E006**：运行约54分钟、178次Astra请求，完成1代后在第2代因上下文大小失败；0次晋级，未做10000副验收。
- Z继承稿72项回归、8个行为变化见证通过，但E006两次发布同一哈希，不是两个新设计。固定参考缺口阻塞比较，A/B也未形成新的已发布候选。
- 没有证据证明目前已发现优于两个工程基线的新体系。具体事实及限制见[进化记录](docs/12-self-evolution.md)和[历史独立审查](docs/BRIDGE_AUTORESEARCH_GAP_ANALYSIS.md)。

## 近期优先级

1. **止血**：所有工具结果、错误和交接上下文有界；修复并显式修订有缺口的固定参考，保留原版本与失败牌。
2. **解耦**：持久化候选评测队列、无需模型的evaluation-only、去重和独立评测资源预留。
3. **闭环**：先让已有冻结候选获得可追溯结论，记录单位有效研究结果的实际成本；无提升也是有效结论。
4. **受控实验**：最小1NT模块合同，再做同模型、同预算、多次独立重复的Local vs Global Pilot。
5. **可信结论**：同步校准跨运行统计预算，并调查外部强基线的许可、可获取性和比较口径。

这些是[待实施的验收项](docs/14-autoresearch-plan.md)，不是当前已经存在的CLI或功能。1NT是方法研究切片，不是完整体系进化的永久边界。

## 快速开始

要求Python >=3.11；已验证环境为Linux、Python 3.14.8。显示器与可选模型网关需要Node.js >=22.13.0。Python运行时没有第三方包依赖，首次构建需要安装声明的setuptools。

```bash
git clone https://github.com/victorywys/BridgeBiddingEvolver.git
cd BridgeBiddingEvolver
python3 -m venv .venv
.venv/bin/python -m pip install -e .

.venv/bin/bridge-evolve --help
.venv/bin/bridge-evolve doctor --json
.venv/bin/bridge-evolve system browse systems/classics-v5.5.4-compact/prec.json
.venv/bin/bridge-evolve system decide systems/general-v2.json \
  --observation examples/general-observation.json --trace
```

`doctor`不带`--require-dds`退出成功，只表示完成诊断，不保证真实DDS可用。局面查询与单手决策不需要模型或DDS；见[体系导航](docs/13-system-navigation.md)。

### 真实DDS与比赛评测

以下构建固定的官方DDS 2.9.0；需要Linux、Git及C++编译器，不需要sudo或Python DDS绑定：

```bash
.venv/bin/python tools/dds/build.py
.venv/bin/bridge-evolve doctor --json --require-dds
.venv/bin/python tools/dds/verify.py
```

构建源码、二进制、来源清单及缓存均留在本地`.runtime/`。算法版本、构建哈希、20项表轴及计分口径见[真实评测说明](docs/09-classic-evaluation.md)。单桌`auction run`只运行叫牌，不能冒充已计分比赛。

### 显示与审计

```bash
npm --prefix tools/board-viewer ci
npm --prefix tools/board-viewer run build
.venv/bin/python tools/board_viewer.py --port 8765
```

打开 <http://127.0.0.1:8765/>。可生成/导入牌、开启审计、整理历史；选中叫品后可直接打开对应体系分支，也可输入本人手牌判断下一叫。完整体系说明按需加载，不必先下载整份规范。

新克隆没有本地审计历史，这是预期行为。不要复制其他机器的`.runtime/`凭据目录来恢复它。已导出的牌例/审计包可通过界面导入，见[工作台](docs/10-audit-workspace.md)和[紧凑记录](docs/11-compact-records.md)。

### 可选研究模型

默认研究模型为`gpt-6-astra`、high推理，不静默降级。调用前需自行配置[本地网关工具](tools/copilot-api/package.json)并完成账户授权：

```bash
npm --prefix tools/copilot-api ci
npm --prefix tools/copilot-api run auth
npm --prefix tools/copilot-api run setup
npm --prefix tools/copilot-api run start
```

另一个终端可执行`.venv/bin/bridge-evolve evolve models`核对实际模型列表。不要输出令牌，也不要使用`--show-token`。网关是第三方工具，不是GitHub官方服务端部署方式，使用仍受上游条款和限流约束。

现有研究入口和预算见[进化说明](docs/12-self-evolution.md)。在近期P0验收完成前，不建议直接扩大付费运行；本次文档更新不会启动模型实验。

## 验证

```bash
# 基础及行为回归；真实DDS缺失时，相关测试可能跳过。
.venv/bin/python -m unittest discover -s tests -v

# 已构建真实DDS后，要求相关真实求解测试不得跳过。
BRIDGE_DDS_REQUIRED=1 .venv/bin/python -m unittest discover -s tests -v

npm --prefix tools/board-viewer test
npm --prefix tools/copilot-api test
.venv/bin/python -m pip check
```

回归通过、协议覆盖、候选晋级、万副达标是不同结论。旧实验的测试数量不作为当前版本的自动背书。

## 哪些内容纳入Git？

| 纳入版本管理 | 保留在本地，不提交 |
| --- | --- |
| 源码、测试、CLI、工具源码、依赖锁文件、文档和示例 | `.venv/`、`node_modules/`、编译产物和显示器bundle |
| 紧凑冻结工程基线、已发布Z协议及回归定义 | 大批`results/`、`datasets/`、重复展开的历史体系 |
| 小型已消耗回归牌例、无损压缩的历史缺陷夹具 | `.runtime/`中的模型会话、未完成草稿、密钥、DDS与缓存 |

[发布体系索引](systems/research-z/index.json)保留Z原冠军及两个不同发布物的原始哈希、说明、48/72项测试定义与所需案例；它们不是独立强度证据，也没有因此变成新冠军。完整实验归档仍需另行保存，Git不代替研究运行数据备份。

历史文档中指向`results/`、`datasets/`或`.runtime/`的链接是原机器的证据位置，不随克隆发布；过时README和旧原型可从Git历史查看。本次替换保留远端提交历史，不强推。原始体系手册不随仓库发布。

## 项目原则与阅读入口

- **成绩是唯一适应度**；规则数、篇幅、复杂度、新颖性及是否像传统体系都不加减分。
- 公开含义不使用本次私有命中；协议变更同步更新发送、解释、应答和退出。
- 不预留测试池。候选冻结后每轮现场抽共同新牌，重试复用原轮次，不删失败牌或补零分。
- 开发反馈不等于独立确认；长期重复检验需要跨运行控制，不能靠重开实验重置显著性。

| 文档 | 用途 |
| --- | --- |
| [项目约束](AGENTS.md)、[研究目标](docs/01-research-brief.md) | 固定目标与边界 |
| [下一阶段计划](docs/14-autoresearch-plan.md) | 当前优先级、六项验收及Local vs Global设计 |
| [架构](docs/02-architecture.md)、[实施路线](docs/05-roadmap.md) | 已实现边界与后续接线 |
| [评测协议](docs/04-evaluation.md)、[实验规范](docs/06-experiments.md) | 公平比较、统计与报告 |
| [语言设计](docs/03-system-language.md)、[v1](docs/07-executable-systems.md)、[v2](docs/08-general-protocols-v2.md) | 实际IR及协议语义 |
| [体系导航](docs/13-system-navigation.md)、[审计工作台](docs/10-audit-workspace.md) | 按叫品/手牌阅读与调试 |
| [进化记录](docs/12-self-evolution.md)、[独立审查](docs/BRIDGE_AUTORESEARCH_GAP_ANALYSIS.md) | E002–E006历史证据与局限 |
