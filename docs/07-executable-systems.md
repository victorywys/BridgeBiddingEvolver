# 体系语言 v1 与执行器

> 本文描述仍受支持的v1。通用四人调度、双方局况、公开含义标签及动态将牌/加级动作已经在兼容的 [v2](08-general-protocols-v2.md) 中实现；下文的首版限制只针对v1。

## 1. 当前可用的闭环

现在可将一套**确定性的搭档协议**写成 JSON，进行校验、生成完整中文规范、为指定观察选择叫品，以及在固定局部实验中运行整副牌的叫牌。

这不是任意自然语言自动编译器，也不是神经网络策略。工作流是：

```text
人或提案程序定义明确含义
→ 写入有类型的 JSON IR
→ 校验版本、算子、引用、状态和规则
→ 同一 IR 执行叫牌 / 生成中文规范
→ 检查具体牌例和私有 trace
```

初版语言、运行时、渲染器分别位于 [language.py](../src/bridge_evolve/systems/language.py)、[runtime.py](../src/bridge_evolve/systems/runtime.py)、[render.py](../src/bridge_evolve/systems/render.py)。没有 `eval`、动态导入、外部函数调用、文件/网络操作或隐藏权重。

## 2. 运行现有示例

在项目根目录运行，不需要网关、LLM、DDS 或新增运行时依赖：

```bash
.venv/bin/bridge-evolve system validate systems/1nt-v1.json
.venv/bin/bridge-evolve system render systems/1nt-v1.json
.venv/bin/bridge-evolve system render systems/1nt-v1.json --output systems/1nt-v1.md

.venv/bin/bridge-evolve system decide systems/1nt-v1.json \
  --observation examples/observation.json --trace

.venv/bin/bridge-evolve auction run --system systems/1nt-v1.json \
  --board examples/board.json --trace
```

`validate` 的 `valid=true` 表示通过列出的静态检查，不等于穷举证明、不等于体系强度合格。任意规则条件的重叠和行动合法性还会在运行中检查。

`--trace` 是显式的**私有诊断**：包括命中规则、检查结果和实际手牌特征值。默认结果不输出该字段，搭档和对手均不得读取它。`public_meaning` 始终从静态 IR 的全部相关分支生成，不依赖本次具体命中分支或私牌。

## 3. 最小完整体系格式

下面是一个故意只在开局按条件叫 1S、其他情况 Pass 的语法示例，**不是可对战的完整竞争体系**：

```json
{
  "schema_version": 1,
  "operator_version": 1,
  "name": "控制与套质量语法示例",
  "scope": "general",
  "features": {
    "good_spades": {
      "op": "and",
      "args": [
        {"op": "ge", "args": [{"op": "length", "suit": "S"}, 5]},
        {"op": "ge", "args": [{"op": "honors", "suit": "S", "ranks": "AKQ"}, 2]}
      ]
    }
  },
  "protocol": {
    "initial": "start",
    "states": {
      "start": {"on": {"1S": "finished"}, "otherwise": "finished"},
      "finished": {"on": {}, "otherwise": "finished"}
    }
  },
  "rules": [
    {
      "id": "open_quality_spades",
      "priority": 100,
      "when": {
        "op": "and",
        "args": [
          {"op": "eq", "args": [{"op": "state"}, "start"]},
          {"op": "ge", "args": [{"op": "hcp"}, 12]},
          {"op": "ref", "name": "good_spades"},
          {"op": "control", "suit": "D", "round": 2, "shortage": false}
        ]
      },
      "call": "1S"
    }
  ],
  "fallback": {"call": "P"}
}
```

顶层和节点不接受未定义字段，版本必须是整数 `1`，不能用 `true` 冒充。叫品用 `P`、`X`、`XX`、`1C` 至 `7NT`；十用 `T`，无将用 `NT`。`scope=general` 仅说明不限定为该局部实验，不表示有完整竞争覆盖。

`SystemDefinition.to_data()` 导出权威 IR。体系内容 SHA-256 覆盖导出的全部内容与语义版本，不以名称或路径充当身份。JSON 反序列化后重新校验，执行语义、内容哈希与说明应不变。运行时定义为冻结对象，不保留可被调用者修改的 JSON 字典。

## 4. 有类型的表达式

布尔、整数、字符串直接作为字面量。列表必须非空、同类型，可以嵌套，例如牌型列表。没有浮点数、空值或隐式类型转换；`true` 不等于数值 `1`。算子节点用 `{"op": "...", ...}`。

### 原子特征

| 写法 | 类型与精确含义 |
| --- | --- |
| `{"op":"hcp"}` | 整数；A=4、K=3、Q=2、J=1，其他为0 |
| `{"op":"controls"}` | 整数；A=2、K=1，其他为0 |
| `{"op":"length","suit":"S"}` | 本人该套张数；花色只能为 C/D/H/S |
| `{"op":"shape"}` | 四套张数按降序排列的整数列表 |
| `{"op":"balanced"}` | 布尔；仅4333、4432、5332 |
| `{"op":"has","card":"AS"}` | 是否持有指定牌，格式为点数后跟花色 |
| `{"op":"honors","suit":"H","ranks":"AKQ"}` | 指定集合中实际持有几张；允许任意不重复合法点数，不限AKQ |
| `{"op":"control","suit":"D","round":1,"shortage":false}` | 首轮控制：有A；显式启用shortage时也计缺门 |
| `{"op":"control","suit":"D","round":2,"shortage":true}` | 第二轮前可控制：有A或K；启用shortage时也计缺门/单张 |
| `{"op":"seat"}` | 当前座位字符串 N/E/S/W |
| `{"op":"role"}` | 从发牌人顺时针的 dealer/lho/partner/rho；不等于动态推断的开叫人 |
| `{"op":"vulnerable"}` | 本方是否有局 |
| `{"op":"previously_passed"}` | 本人此前任意一次是否叫过Pass |
| `{"op":"has_contract_bid"}` | 历史是否有定约叫品；Pass/X/XX不算 |
| `{"op":"state"}` | 按全部公开历史重建的协议状态名 |
| `{"op":"is_legal","call":"X"}` | 给定叫品是否在当前合法动作中 |
| `{"op":"ref","name":"good_spades"}` | 引用自定义特征，类型继承自其表达式 |

“首轮/第二轮控制”按上表定义，不是精确墩数预测；短套是否有效取决于定约和协议，需要体系作者选择 `shortage`，执行器不擅自推断。

### 组合算子

除原子特征外，参数统一放入 `args`：

| 算子 | 参数和语义 |
| --- | --- |
| `and` / `or` | 至少一个布尔参数，从左到右短路 |
| `not` | 恰好一个布尔参数 |
| `eq` / `ne` | 两个同类型参数，精确相等/不等 |
| `lt` / `le` / `gt` / `ge` | 两个整数，分别为小于/不大于/大于/不小于 |
| `between` | 三个整数 `[值, 下界, 上界]`，含两端；常量上下界颠倒会拒绝 |
| `in` | `[值, 同类型值的列表]`，例如 `shape` 属于一个牌型列表 |
| `add` / `mul` | 至少两个整数，求和/乘积 |
| `sub` | 恰好两个整数，相减 |
| `min` / `max` | 至少两个整数，取最小/最大值 |

例如自定义套质量可以是“套长 + 2×AKQ中的持有张数”，不需要添加新的执行器分支。所有自定义特征可互相引用、可向前引用，但不能循环。

量词、变量绑定、概率策略、动态将牌参数、后验分布推断和任意自由文本编译尚未支持。这些是首版实现限制，不是研究空间的永久禁令。需要新算子时，应同步实现类型检查、精确语义、说明渲染和行为测试，增加语义版本，而非注入临时代码。

## 5. 规则优先级和多轮协议

规则有 `id`、整数 `priority`、布尔 `when`、`call`。同一优先级不依赖数组顺序：

1. 按优先级从高到低逐层判断。
2. 首个有命中的层恰有一条命中时选中它。
3. 该层多条命中即错误，即使它们输出相同叫品也不例外。
4. 选中叫品非法即错误，不降到低优先级，也不偷偷Pass。
5. 仅在全部规则均不命中时执行显式 `fallback`，兜底本身也须合法。

协议是公开的有限状态机。`initial` 为初始状态；每一状态有 `on` 映射和强制的 `otherwise`。每次决策从初始状态重放**全部**公开叫品，包括双方Pass及X/XX；精确匹配 `on`，否则用 `otherwise`。所有目标必须存在，所有状态必须在结构上可达。规则中直接比较的状态名也会检查。

状态转移不读取手牌、不读取命中规则、不持有私有记忆；角色和行动位置由公开历史确定。`on` 本身只匹配叫品，如果同一叫品在不同轮次有不同含义，需拆分状态；必要时在规则中同时判断 `role`/`seat`。有限状态机不代替通用叫牌合法性检查。

公开含义与本次选中的私有规则分开：同一叫品可能由多个分支产生，完整含义为各有效分支之并集，并计入高优先级排除及冲突规则。完整中文规范会列出所有特征、状态、优先级、条件、动作和兜底，而不是只渲染一个命中分支。

## 6. 单手观察与指定牌例

[观察示例](../examples/observation.json) 格式：

```json
{
  "hand": "JT987.AKQ2.AJ.T9",
  "seat": "S",
  "dealer": "N",
  "vulnerability": "none",
  "history": ["1NT", "P"]
}
```

手牌是 `S.H.D.C`，缺门用 `-`，不能用空字符串；必须恰好13张且不重复。局况用 `none/ns/ew/both`。历史必须为从发牌人开始的合法完整历史，当前座位必须轮到行动，已经结束的叫牌不能继续决策。可以额外提供 `public_systems` 字符串列表，内容只能是事先公开的静态规范。未知字段（包括种子、全牌、DDS等）直接拒绝。

该观察预计输出 `2H`，在基准中表示向黑桃转移，不表示持有红桃。两种含义不能仅根据叫品文本区分，必须结合公开协议状态。

[完整牌例](../examples/board.json) 指定 `dealer`、`vulnerability` 和四个 `hands`。全牌只在调度端校验并保留，`decide(system, observation)` 永远不接收 `Board` 或 `Deal`。

`auction run` 初版固定 N 发牌且以15–17点、4333/4432/5332开1NT，东西始终Pass；不满足开叫条件或体系未输出固定1NT时显式失败。没有让各候选自行挑牌。此命令只是叫牌演示，不返回预计墩数、分数或IMP。

## 7. 已实现基准的准确边界

[1nt-v1.json](../systems/1nt-v1.json) 和 [生成的完整规范](../systems/1nt-v1.md) 来自同一 IR：

- 1NT：15–17点、上述均型。
- 应叫人若有五张以上高花，先转移到较长高花；等长先红桃。2D转红桃，2H转黑桃，不限点力；开叫人按规定接受，不超级接受。
- 无五张高花：0–7点Pass，8–9点2NT邀请，10点及以上3NT。
- 转移完成后：0–7点Pass，8–9点2NT邀请，10点及以上3NT供开叫人选局。
- 对直接2NT邀请：17点3NT，15–16点Pass。
- 对转移后的2NT邀请：三张以上配合时，17点叫四阶高花，15–16点叫三阶高花；无三张配合时，17点3NT，否则Pass。
- 对转移后的3NT：三张以上配合改四阶高花，否则Pass。
- 所有规则均不适用时显式Pass；意外干扰进入公开的outside状态，不宣称对此有完整竞争处理。

未实现Stayman、四张高花寻找、低花定约优化、满贯、竞争干扰和心理叫。高点力牌也可能停在局；这是一套可执行的实验基准，不宣称完整SAYC或高水平体系。提供这些限制是为了准确解释当前行为，不是限制未来的表达能力。

示例牌例预计完整叫牌：

```text
N 1NT  E P  S 2H  W P
N 2S   E P  S 3NT W P
N 4S   E P  S P   W P
```

最终为北家4S；北家最先叫出黑桃。没有DDS时不能据此判断能否成约。

## 8. 验证范围

行为测试覆盖算子语义和边界、派生特征和循环、同级冲突、非法行动、协议重建、所有基准规则分支、序列化哈希、公开解释与私有trace隔离、单手输入未知字段拒绝、完整牌例命令及错误退出。

固定随机种子生成300副符合开叫条件的完整牌例，实际运行整段协议并重放比较，覆盖当前基准的全部规则。对手两手互换不改变己方决策；重复运行不残留状态。随机测试和渲染覆盖都不是全空间正确性证明，也没有完成“只给自然语言、由独立实现者重建全部行为”的独立研究验收。

这一交付提供 M2 的首个可用执行切片，不改变 M1 尚需独立完整验收、M3真实DDS尚未接入的状态，也不宣称已经有正式体系成绩。
