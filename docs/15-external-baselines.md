# BRL / Pidgin 外部叫牌基线

2026-11-05已接入现有`auction run --mode general`、`evaluate`、冻结结果记录和审计工作台的只读结果加载/ZIP导入。外部基线不是规则IR，不参与规则编辑；没有修改现有进化实验的固定对手、冠军或历史成绩。

## 1. 目录、来源和环境

按当前工作目录上层建立：

```text
/home/yansenwang/my_code/
  bridge-bidding-baseline/
    brl/          官方源码及原始预训练权重
    pidgin/       官方源码，含BRL PyTorch移植
    models/       BRL-FSP与Pidgin V3.1纯叫牌网络，约23MiB
    policies/     两个冻结注册描述
    .venv/        独立CPU推理环境
    .reference-venv/  仅供官方BRL/JAX/PGX交叉验证
    validation/   参考输出、数值一致性和集成验证
  bridge_bidding/
    bridge-bidding-evolution-starter/
```

| 对象 | 固定版本 |
| --- | --- |
| [官方BRL](https://github.com/harukaki/brl) | `f482c671ba8702711d76078f04846a57d06ba6ba` |
| [Pidgin](https://github.com/hsperr/pidgin) | `4ea4931b79cecc74a332d5f82819b939705b3206` |
| [公开权重](https://huggingface.co/hsperr/pidgin/tree/c2f8b0ed6a2b58bfd9df805549fa974c648c7ee6) | `c2f8b0ed6a2b58bfd9df805549fa974c648c7ee6` |
| 推理运行时 | Python 3.12.3、PyTorch 2.5.1+cpu、NumPy 2.2.6；单线程、CPU、确定性greedy |
| 参考运行时 | Python 3.10.22、JAX/JAXlib 0.4.23、Haiku 0.0.12、PGX 1.4.0 |

上游源码保持原样，没有安装其全套训练依赖、下载100M DDS训练库、启动训练或调用远端推理API。保留Apache-2.0许可证、BRL附带许可及论文引用；权重只从固定公开版本加载。

## 2. 实际推理与信息边界

```text
现有Board与通用合法叫牌引擎
  -> 本人手牌、座位、发牌人、局况、完整公开叫牌、合法叫品
  -> 固定Python子进程 -> 上游编码/网络 -> 合法greedy叫品
  -> 现有叫牌结束/庄家识别 -> 本项目真实DDS -> 两桌配对IMP
```

- 主项目无需安装PyTorch/JAX。每个注册策略打开一个持久worker，一次加载权重后复用，不逐叫启动解释器；作用域结束关闭并等待子进程退出。
- 进程请求只含`hand/seat/dealer/vulnerability/history/legal_calls`，不含完整四手牌、DDS、种子、副号、另一桌结果、私有trace或文件路径。启动环境不继承宿主凭据。
- 两位搭档使用同一个冻结模型，但每次推理只编码行动者本人手牌与公开历史，不共享私有状态。
- BRL使用Pidgin的PyTorch移植；保持官方PGX动作顺序处理同分argmax。Pidgin复用上游特征、checkpoint和门控概率逻辑，遵守checkpoint的X/XX及开叫限制，不自行替换叫品。
- 本次接入**BRL-FSP**和**Pidgin V3.1纯网络**。不含Pidgin V2的belief/DDS叫牌搜索，也不含打牌模型。
- 推理源码属于受信任的固定依赖；这是显式输入/进程边界，不声称已实现操作系统级文件或网络沙箱。

### 黑箱叫品怎样解释

外部模型没有当前IR要求的精确公共声明。新增`Observation.opaque_sides`显式表示已披露为黑箱、没有提供精确叫品保证的一方：

- 不把其`1C`强行解释为自然草花，不推导点力、套长、人工/自然标签。
- 对显式黑箱侧，标签查询返回“无此项公开保证”、显示花色为空；这不代表模型真实语义已被证明为空。
- 未提供定义、又未显式标为黑箱的一方仍保持原有查询错误，不用静默默认值掩盖漏配。
- 日志用`source=external`、`explanation_available=false`标记；工作台明确显示“外部神经叫牌，精确含义不可用”，不把私有概率当作公共解释。
- 固定规则/神经对手不会因此主动学习对方的约定；这是已披露边界下的固定对手实验，不宣称自适应或完整约定理解公平性。

## 3. 注册与复现

已有注册位于`../../bridge-bidding-baseline/policies/`。从项目根目录执行：

```bash
.venv/bin/bridge-evolve baseline decide \
  ../../bridge-bidding-baseline/policies/brl-fsp.json \
  --observation examples/general-observation.json

.venv/bin/bridge-evolve baseline decide \
  ../../bridge-bidding-baseline/policies/pidgin-v3.1.json \
  --observation examples/general-observation.json
```

重新注册示例，输出必须是**不存在的新路径**：

```bash
.venv/bin/bridge-evolve baseline register \
  --source ../../bridge-bidding-baseline/pidgin \
  --python ../../bridge-bidding-baseline/.venv/bin/python \
  --weights ../../bridge-bidding-baseline/models/brl_fsp_weights.npz \
  --family brl --name BRL-FSP \
  --model-source https://huggingface.co/hsperr/pidgin/resolve/c2f8b0ed6a2b58bfd9df805549fa974c648c7ee6/brl_fsp_weights.npz \
  --output ../../bridge-bidding-baseline/policies/brl-fsp-new.json
```

Pidgin对应`--family pidgin`、`pidginv3_1_bid_s20000.pt`及其固定URL。

描述保存源码commit及相关Python文件哈希、权重哈希、worker代码哈希、数值运行时版本与推理设置。模型身份不依赖目录名或显示名称；任何推理源码、权重、adapter或运行时变化都需显式新注册。正式运行前和完成前检查身份；不自动下载、更新或改写旧描述。

系统快照只保存注册描述，不复制大权重。重放需另外保留对应权重和源码；工作台只读查看或ZIP导入不启动worker，也不要求原推理环境仍可用。

## 4. 接入现有叫牌与评测

单桌只叫牌，无成绩：

```bash
.venv/bin/bridge-evolve auction run --mode general \
  --ns systems/classics-v5.5.4-compact/ccba.json \
  --ew ../../bridge-bidding-baseline/policies/pidgin-v3.1.json \
  --board examples/general-board.json --trace
```

首次生成烟测牌，三场都使用相同16个独立牌族：

```bash
.venv/bin/bridge-evolve dataset generate \
  --output .runtime/external-baselines-smoke/data \
  --count 16 --seed 2026110501 --split validation

.venv/bin/bridge-evolve evaluate --config experiments/external-baselines-smoke.json
.venv/bin/bridge-evolve evaluate --config experiments/ccba-brl-smoke.json
.venv/bin/bridge-evolve evaluate --config experiments/ccba-pidgin-smoke.json
```

这些命令已经执行；为避免覆盖，数据/结果目录存在时会拒绝再生成。复跑同一manifest请复制配置到新文件、修改`output`为新目录，不删除或覆盖旧结果；配置相对路径以配置文件所在目录为基准。

`system_a/system_b`仍接受原有IR路径，现在也接受外部注册描述。旧规则对规则路径保留原行为。叫牌失败、worker超时、非法叫品、权重/源码漂移、DDS或计分失败均显式停止，不替换Pass、不补零、不发布部分成功均值。

每场保存原始牌、两桌完整叫牌/私有推理记录、实际庄位/定约/加倍/墩数/分数、逐副IMP、配对bootstrap、源码/模型/DDS身份及失败manifest。基线单独用`external:*`标记统计，不把它当作可编辑的规则ID。

## 5. 已执行验证

### 官方BRL与移植一致性

在上层基线目录执行：

```bash
JAX_PLATFORM_NAME=cpu OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  .reference-venv/bin/python -m export_brl_reference
.venv/bin/python -m check_brl_port
```

直接从官方FSP pickle和官方`make_forward_pass("relu", "DeepMind")`导出参考；用PGX 1.4的`_observe`生成输入，不下载DDS数据。

- 144个局面，覆盖全部发牌人/局况、开局Pass、X、XX及7NT边界。
- 12组转换数组与官方权重逐元素完全相同。
- 全部480位观察与PGX完全相同。
- logits最大绝对误差`6.103515625e-05`，在既定`rtol=atol=2e-5`容差下通过。
- 有/无合法掩码的greedy选择全部相同；不宣称跨JAX/PyTorch浮点逐bit相同或穷尽所有输入。
- 原始PGX 1.4的dataclass默认值不兼容Python 3.11+；参考校验因此使用独立3.10环境，没有篡改PGX或降级主项目Python。

### 小批量真实DDS比赛

| A vs B | 基础牌 / 桌 | A的IMP/副 | 描述性95%配对区间 | 无效/删除 |
| --- | --- | ---: | --- | --- |
| BRL-FSP vs Pidgin V3.1 | 16 / 32 | +0.4375 | [-1.5625, +2.6875] | 0 / 0 |
| CCBA工程v5.5.4 vs BRL-FSP | 16 / 32 | -1.6250 | [-4.3750, +0.8750] | 0 / 0 |
| CCBA工程v5.5.4 vs Pidgin V3.1 | 16 / 32 | -1.5000 | [-4.6875, +1.6250] | 0 / 0 |

本地结果目录分别是`results/brl-vs-pidgin-smoke-20261105/`、`results/ccba-vs-brl-smoke-20261105/`、`results/ccba-vs-pidgin-smoke-20261105/`。**三个区间都跨0，16副只能验证接线，不能判断强弱。** 不把三场同源牌算成48个独立牌族。

回归入口：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests \
BRIDGE_DDS_REQUIRED=1 BRIDGE_BASELINES_REQUIRED=1 \
BRIDGE_BASELINE_DIRECTORY=/home/yansenwang/my_code/bridge-bidding-baseline \
.venv/bin/python -m unittest \
  test_external_baselines systems.test_runtime systems.test_v2 systems.test_baseline \
  test_evaluation test_cli test_board_viewer test_public_call_meaning test_record_log
```

神经测试显式要求外部环境；未配置时常规回归会清楚skip可选真实模型测试，`BRIDGE_BASELINES_REQUIRED=1`时缺失则失败。测试覆盖：进程只接收可见字段、宿主凭据不继承、非法动作/身份漂移显式失败、全局况/座位映射、真实DDS自比较为0、换位IMP反号、冻结结果/ZIP离线查看及黑箱解释不虚构。

本次上述组合实际执行 **169 tests，341.021秒，OK，零跳过**。安装后CLI也完成规则/神经混合单桌叫牌。三场结果的产物哈希已核对；机器验证记录位于上层目录的`validation/integration.json`，官方BRL数值校验位于`validation/brl-reference.json`及`validation/brl-port.json`。

## 6. 当前未开放的部分

- 没有改变`evolve`的两个人工体系锚点或既定研究目标；当前进化配置仍要求general v2规则体系。外部模型可用于独立`evaluate`，尚未作为进化控制器的可配置固定对手。
- 首页不会把神经网络注册为可编辑规则体系；可以查看其冻结比赛结果，但新建神经比赛使用CLI。
- 没有Pidgin搜索版、打牌评测、人类对照或大样本强度结论。
- CCBA工程版已知还有独立参考缺口；本次16副通过不宣称修复或证明其全体系正确。

后续优先保留现有研究吞吐/覆盖修复顺序；需要把神经基线纳入晋升对手时，另立冻结的实验版本，明确更换目标和旧成绩不可拼接。
