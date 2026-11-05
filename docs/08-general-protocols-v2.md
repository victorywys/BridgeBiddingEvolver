# 通用叫牌与公开参数协议 v2

## 1. 这一版解决什么

这一交付补齐经典体系迁移前的三个底层能力：

1. 任意发牌人、局况，南北/东西分别运行自己的体系，允许竞争、加倍、再加倍、全Pass及指定前缀。
2. 从公开历史重建开叫/应叫/介入角色、双方局况、相关花色、对方事先公开的叫品标签。
3. 双方独立的有类型公开参数、条件状态转移、动态花色特征，以及精确叫品、最经济叫品、加级回答。

v1定义和原有局部1NT实验均保留。**通用调度不等于已经移植CCBA/精确，也不是两桌配对科研评测。** 不新增运行时依赖，不需要网关、LLM或DDS。

## 2. 立即运行

在项目根目录执行：

```bash
.venv/bin/bridge-evolve system validate systems/general-v2.json
.venv/bin/bridge-evolve system render systems/general-v2.json
.venv/bin/bridge-evolve system render systems/general-v2.json --output systems/general-v2.md

.venv/bin/bridge-evolve auction run --mode general \
  --ns systems/general-v2.json --ew systems/pass-v2.json \
  --board examples/general-board.json --trace

.venv/bin/bridge-evolve system context systems/general-v2.json \
  --observation examples/general-observation.json

.venv/bin/bridge-evolve system decide systems/general-v2.json \
  --observation examples/general-observation.json --trace
```

演示流程为北开1S、南3S表示配合，北4NT问关键张，南5H回答，北6S，三Pass结束。单手示例的结果为5H；查看上下文可见将牌S、one_round义务、双方不同局况及asker/anchor。

[演示体系](../systems/general-v2.json)使用一套参数化协议而非四份将牌副本。[始终Pass对照](../systems/pass-v2.json)只用于调度检查。演示中回答后选择小满贯是明确写死的测试退出策略，**并未实现根据关键张推断安全满贯的策略，更不代表6S可成约**。演示其他分支的Pass也是显式约定，不宣称覆盖完整经典体系。

原局部命令不变：

```bash
.venv/bin/bridge-evolve auction run --system systems/1nt-v1.json --board examples/board.json
```

局部模式仍检查北家15–17点、4333/4432/5332，固定东西Pass。通用模式要求双方`scope=general`，不接受`--system`；局部模式不接受`--ns/--ew/--prefix`，不能靠改标签混淆实验。

## 3. 通用调度与前缀

`run_auction(ns, ew, board, prefix=(), trace=False)`只由调度端持有四手牌。每个行动者只收到自己的Hand、座位、发牌人、局况、公开历史及不可变静态体系定义。

前缀使用独立的JSON叫品列表，例如：

```json
["1S", "P", "3S", "P", "4NT", "P"]
```

保存为文件后可加`--prefix <路径>`。前缀逐步校验桥牌合法性、公开转移和逼叫约束；不可在结束后继续追加。历史来源标为`source=prefix`，继续执行的决策标为`source=policy`。

前缀是用户提供的公开历史，不声称每个历史叫品都是当前手牌依策略选择的。可用于重放/反事实场景，但不能据此伪造策略成绩。

输出包括双方内容哈希、完整叫牌、实际庄家和定约。全Pass为`{"passed_out":true}`。没有DDS、预计墩数、计分或IMP；`mode=general`表示单桌通用叫牌，不冒称`competitive-full`两桌评测。

整副调度每次新建独立的`PublicWorld`，内部决策校验它与当前观察的历史、发牌人、局况及双方体系身份一致，然后按叫品增量推进；不再为每一叫从开局重放整个前缀。独立的`decide(system, observation)`接口仍只接受不可变观察，并重建自己的公开状态，保持纯函数行为。公开状态不持有手牌、私有推断或DDS，不能跨牌复用。对同一个公开事件的公告条件共享一次公开求值缓存；事件、搭档或牌例变化后重新创建，不缓存动态私牌结论。

## 4. v2结构

顶层必须使用`schema_version=2`和`operator_version=2`。保留name、scope、features；新增announcements，protocol包含parameters。规则使用action代替v1的call。

最小完整定义：

```json
{
  "schema_version": 2,
  "operator_version": 2,
  "name": "始终Pass",
  "scope": "general",
  "features": {},
  "protocol": {
    "initial": "idle",
    "parameters": {},
    "states": {
      "idle": {"transitions": [], "otherwise": "idle"}
    }
  },
  "announcements": [],
  "rules": [],
  "fallback": {"kind": "call", "call": "P"}
}
```

v1与v2不能混用字段或版本。仍严格拒绝未知字段、循环引用和非布尔规则条件；原有优先级、冲突、非法行动失败语义不变。序列化后哈希和语义保持不变。v1基准哈希及说明不因新增v2改变。

## 5. 公开上下文

使用`{"op":"context","name":"opponents_vulnerable"}`。旧的`role`和`vulnerable`不改变含义。

| 字段 | 类型、含义 |
| --- | --- |
| `state` | 当前本方公开协议状态字符串，等价于旧state算子 |
| `dealer` / `vulnerability` | 发牌人N/E/S/W；局况none/ns/ew/both |
| `own_vulnerable` / `opponents_vulnerable` | 相对当前牌手本方/敌方的布尔局况 |
| `partner_seat` | 本人搭档座位 |
| `opener` / `responder` | 全桌首次叫定约的牌手及其搭档 |
| `overcaller` / `advancer` | 开叫方的另一方首次非Pass介入者及其搭档；包括加倍 |
| `our_opener` / `our_responder` | 本方首次叫定约者及其搭档；本方未叫定约时为空 |
| `dynamic_role` | opener/responder/overcaller/advancer/unopened/other |
| `opening_position` | 首次定约叫品为相对发牌人的第1至4家；尚无开叫为0 |
| `opening_strain` | 首次实际定约叫品花色 |
| `last_call` / `last_actor` | 最近任意叫品及其牌手 |
| `last_bid` / `last_bid_strain` / `last_bid_level` / `last_bid_actor` | 最近定约叫品及其花色、阶数、牌手，不含P/X/XX |
| `partner_bid_strain` / `opponent_bid_strain` | 最近搭档/敌方实际定约叫品花色 |
| `partner_shown_suit` / `opponent_shown_suit` | 上述事件经静态声明所显示的花色；不把人工叫品当自然 |
| `unbid_suits` | 未被任何一方实际叫出的C/D/H/S列表，按此顺序；人工叫品仍按实际花色排除 |
| `passed_before_opening` | 本人在全桌首次定约叫品之前是否Pass过 |
| `agreed_trump` | 本方trump参数；未定义/未同意时为空字符串 |
| `forcing` / `must_bid` | 八级叫牌目标中的当前义务；本人本次是否必须继续的布尔值 |
| `goal` / `goal_level` / `goal_met` | 最近声明的目标、对应最低阶数、是否已达到；达到后目标仍保留，义务可以清除 |
| `our_bid_count` / `last_bid_by` | 本方BID数量（不计P/X/XX）；最近定约叫品归属ours/opponents/空 |
| `partnership_hcp_min` / `partnership_C/D/H/S_min` | captain与responder对应公开下界之和；未声明参数按0处理 |
| `partner_hcp_min` / `partner_C/D/H/S_min` | 当前牌手的搭档已公开保证的下界；不能用于公开转移赋值 |
| `partnership_hcp_max` | 本方两座位的有效HCP上界之和，最多40；有效上界为角色上界与座位Pass上界的交集 |
| `partner_hcp_max` | 本人搭档的有效HCP上界；未声明为40，不能用于公开转移赋值 |
| `agreed_trump_min` | 已定将牌的公开联手张数下界；未定将牌为0 |
| `partnership_C/D/H/S_max` | 双方相应套长公开上界之和，最多13；未声明单手套长上界按13处理 |
| `partner_C/D/H/S_max` | 当前牌手搭档的公开套长上界；不能用于公开转移赋值 |
| `agreed_trump_max` | 已定将牌的公开联手套长上界；未定将牌为0 |
| `double_kind` / `opponent_double_kind` | 本方/敌方事先协议记录的X或XX含义；未声明为空 |

尚未出现的事件、座位、花色用空字符串，未出现的阶数用0；这是定义的“尚无信息”值，不是非法输入的静默兜底。未知HCP下界为0、未知上界为40。必须先确认花色已定义，才能用于length/control/keycards等；空值或NT作为将牌手牌特征会明确报错。

经典v4同时声明`captain_hcp_max/responder_hcp_max`及`seat_N/E/S/W_hcp_max`。公开下界取max、上界取min单调收紧，且与开叫前Pass的座位上界相交；若更新后下界大于上界，显式SystemError，不夹值或遗忘旧信息。当前牌手的私有区间是本人真实HCP加搭档区间，再与公开联手区间相交；不能把这个私有估计写回共享参数。详见[v4区间语义](09-classic-evaluation.md#12-v4点力上下界与自然3nt修正)。

### 套长区间、长短关系及13张牌约束

经典v5.3为captain/responder分别声明四套`_min`/`_max`，初始为0/13。`captain_S_ge_H=true`表示开叫人黑桃不少于红桃；双方方向都为true表示等长。false只表示没有这条保证，不表示相反关系。以上均是普通v2有类型公开参数，不引入可读取手牌的公共算子。

完整声明四套上下界的账本，在公开转移同时赋值后，按区间、已声明关系及单手总计13张筛选全部560种长度牌型，取可行集合每套的最小/最大值。下界不能降低，上界不能放宽，已有关系不能遗忘；不存在可行牌型时显式报错。纯函数缓存只以这些长度约束为键，不接收牌张、牌号或种子。旧的仅下界IR不自动增补这套参数或改变其原有公共转移。

例如自然1D开叫保存双高花最多4张；精确短方片另保存草花最多5张，但不宣称方片长于草花。自然1S开叫保留S不少于H，随后自然2H声明H至少4张时，结合S至少5张及13张总数可收紧为S 5–9、H 4–6，同时保留关系本身，而不把两个边际区间误当成关系的替代。CCBA自然低花开叫保存其低花长短关系；斯台曼否定答叫和单缺显示也按各自含义保留上界，人工叫品不按字面套更新自然长度。

本人私有估计用自己的实际套长、搭档公开区间/关系及每门花色全副13张的限制进一步收紧联手区间；不读取其余三手牌，也不回写公共参数。新trace中的`public_context`及`private_trace.inference`均对应叫牌前；`public_context_after`单列本叫品解释、传播后的公共状态，不含私有估计。显示器分别展示两者，旧记录没有的上界、关系或叫牌后快照不会由新规则补造。

精确1C-2C/2D后自然新套的5+保证与1C-1D示弱后的四张例外分开。相关原文、低花正应叫后的4441处理及第580副回归见[v5.3修正](09-classic-evaluation.md#v53-public-shape)。

不提供对手公开定义时，查询其标签/显示花色会报错，不能默认当作自然。context诊断的`unavailable`显式列出不可查询的显示花色项。无对应事件时announced为false；“没有这个标签”不表示其相反含义为真。

### 对手事先公开的含义

声明示例：

```json
{
  "id": "strong_club",
  "when": {
    "op": "and",
    "args": [
      {"op": "not", "args": [{"op": "has_contract_bid"}]},
      {"op": "eq", "args": [{"op": "context", "name": "event.call"}, "1C"]}
    ]
  },
  "tags": ["artificial", "strong_club"]
}
```

查询示例：`{"op":"announced","which":"opening","tag":"strong_club"}`。`which`支持opening、last_bid、partner、opponents，后两者取最近搭档/敌方定约事件。last_call、partner_call、opponent_call则取任意叫品事件（包括P/X/XX），可查询技术性加倍等标签。标签为明确的静态约定，不从当前手牌或私有trace推导。

`suit`可选，为字符串表达式，例如转移叫声明`"suit":"H"`，或自然叫使用`{"op":"context","name":"event.strain"}`。

每个事件只由**发出该叫品一方**的事先声明解释，全部命中的标签取并集；natural/artificial及weak_nt/strong_nt相互排斥，不同显示花色冲突则报错。调用者仍须保证标签忠实于完整体系；校验器不会证明所有私有分支的含义都准确。

全副运行自动提供双方公开定义。单手命令可以使用：

```bash
.venv/bin/bridge-evolve system decide <本方体系.json> \
  --observation <观察.json> --public-ns <南北体系.json> --public-ew <东西体系.json>
```

也可以在观察JSON额外提供`public_definitions`对象，将NS/EW映射到静态完整体系IR。已有公开定义与CLI冲突时拒绝覆盖；本方公开定义哈希必须与实际执行体系相同。完整IR是事先约定，不含任何本次私牌数据；不要放入trace。

## 6. 公开参数与状态转移

参数格式：

```json
{
  "trump": {"type":"string","initial":"","values":["","C","D","H","S"]},
  "asker": {"type":"string","initial":"","values":["","N","E","S","W"]},
  "anchor": {"type":"string","initial":""},
  "round": {"type":"number","initial":0}
}
```

允许string、number（整数）、bool，初值严格匹配类型；可选values定义非空同类型值域。保留名trump必须显式限定为`""/C/D/H/S`的子集。读取为`{"op":"var","name":"trump"}`。

状态转移示例：

```json
{
  "id": "ask",
  "when": {
    "op":"and",
    "args":[
      {"op":"eq","args":[{"op":"context","name":"event.by"},"ours"]},
      {"op":"eq","args":[{"op":"context","name":"event.call"},"4NT"]}
    ]
  },
  "to": "answer",
  "set": {
    "asker": {"op":"context","name":"event.actor"},
    "anchor": {"op":"context","name":"event.call"}
  },
  "forcing": "one_round"
}
```

每状态必须有transitions列表及otherwise目标；所有目标存在且结构可达。转移无隐式优先级：在旧状态恰好一条命中才执行，多条命中报错；零条走otherwise，保留参数。set中的表达式基于旧参数**同时计算**，然后检查值域。可用引用、条件、算术表达式。

事件字段包括actor、call、strain、level、kind、by、tags。非定约事件level=0、strain为空；kind为P/X/XX/BID；by相对该状态机所属方为ours/opponents。其他历史上下文字段均只含事件之前的历史。声明先生成tags，随后两方同时更新状态；声明不能读取当前event.tags，转移可以。

经典体系移植增加了event.position（该事件牌手相对发牌人的第1至4家）及event.passed_before_opening（该牌手在首次定约叫品前是否Pass），二者均仅由公开历史重建，便于第三家开叫和Drury解释。

公共求值器使用没有手牌的PublicView。静态校验递归禁止手牌算子，包括通过ref间接引用的hcp/length等，也禁止玩家相对字段如partner_seat和dynamic_role用于公共转移，以防搭档得到不同状态。公开阶段用opener、our_responder、event.actor等明确座位比较。opponents/opponent_call标签与opponent_bid_strain/opponent_shown_suit按本方所属方定义，搭档一致，允许用于公共转移；partner/partner_call标签因依赖当前牌手而不允许。

事件字段不能出现在私有决策规则/动作中；先通过公开转移绑定参数，再在下一步决策使用。每次都从初始值重放全部历史，双方各自拥有状态；不跨牌保留，也不共享执行器可变内存。

### 逼叫约束与桥牌规则分开

- none：无义务；也可作为明确的干扰退出。
- one_round：发起者搭档下一次行动不能Pass；作非Pass后消耗。
- invite_game：邀请至局，搭档可以拒绝，不等于逼局。
- game：本方继续到叫出成局阶数（3NT/4H/4S/5C/5D及以上）。
- slam_try：一轮满贯探查，不承诺六阶；已有更高承诺仍保留。
- small_slam：继续到本方六阶定约。
- invite_grand：邀请大满贯，至少承诺六阶，但可以拒绝七阶。
- grand_slam：继续到本方七阶定约。
- 较低目标和一轮回答不会自动清除尚未完成的更高承诺。公开标记`artificial`的问叫、Splinter或控制叫不按字面阶数清除game/small_slam等持续义务；例如逼局后人工4NT问叫仍保留game和搭档答叫责任，而不是误当作已经打算以4NT成局。
- 明确匹配`allow_pass=true`的公开转移允许逼叫性Pass；但不能让终局仍悬置义务。竞争中需要自行声明何时forcing=none。

违规为SystemError，历史前缀也检查。通用`legal_calls`仍按桥牌规则返回Pass，不擅自把体系约定写进桥牌规则。`goal`保留最近承诺，`forcing`记录尚待履行的义务；目标由协议声明，不是只根据HCP自动推导。`goal_level`按trump计算；未定将牌时以无将成局阶数作参考，不表示已经同意无将。`goal_met`按本方最近定约叫品的实际花色/阶数记账，不能单独作为人工叫品已完成自然终局的证明；持续义务是否解除仍服从`artificial`标签与公开转移。更细的竞争退出须写成公开转移，不声称自动理解所有人类约定。

经典v3记录captain/responder的HCP和每门花色张数下界，由公开叫品含义单调更新；人工2D/2C等不会仅因字面花色而增加长度。当前牌手可把本人真实牌力/长度与搭档公开下界相加，得到更强的私有下界，但它只进入策略及私有trace，不写入共享参数。普通干扰保留账本与目标，只有显式规则可以解除义务。默认续叫、假设和审计范围见[v3交付](09-classic-evaluation.md#11-v3联手下界目标续叫与审计)。

## 7. 参数化特征与动作

v1的组合、比较和引用保持。v2新增：

重复表达式可放入`features`并通过`{"op":"ref","name":"特征名"}`复用，不需要增加语言版本。自动压缩导出使用`shared.<算子>.<内容摘要>`命名，保留原始规则、转移ID和优先级；公共检查仍递归进入引用，不能借共享定义读取私牌。v2完整中文规范现在将每个特征定义一次，使用处保留名称；所有定义仍包含在同一规范中，不依赖外部隐藏函数。压缩导出与等价检查见[体系压缩说明](09-classic-evaluation.md#15-体系文件无损压缩与等价核对2026-10-27)。

```json
{"op":"length","suit":{"op":"var","name":"trump"}}
{"op":"honors","suit":{"op":"var","name":"trump"},"ranks":"Q"}
{"op":"control","suit":{"op":"var","name":"trump"},"round":1,"shortage":false}
{"op":"is_legal","call":{"op":"context","name":"last_bid"}}
{"op":"keycards","args":[{"op":"var","name":"trump"}]}
{"op":"list","item_type":"string","args":[{"op":"var","name":"trump"}]}
```

keycards为四张A加将牌K，不包含未定义的“额外赢墩”；半关键张仍可放大成整数。list按给定顺序构造同类型列表，可以为空，但item_type必须是string/number/bool。没有用户Python或eval。

规则字段由call改为action，fallback同样为动作对象：

工程移植还使用`{"op":"if","args":[条件,真值,假值]}`：条件必须布尔，两分支类型相同，只计算选中分支。公共阶段的私有信息校验仍递归检查两个分支，不能把手牌引用藏在“不会执行”的分支里。

| kind | 必需字段与确定性语义 |
| --- | --- |
| call | call：固定合法规范叫品 |
| bid | level整数表达式，strain字符串表达式；叫精确阶数与花色 |
| cheapest | strains字符串列表表达式、min_level/max_level整数表达式；在当前合法定约叫品中按阶数及C/D/H/S/NT顺序取最低者，不按列表输入顺序 |
| step | after规范定约叫品字符串表达式、count正整数表达式；在35级定约序列中向后数级，不跳过已经非法的级数 |
| error | message非空字符串；明确报告未实现路径，不用Pass伪装覆盖 |

bid不自动升阶，step不绕回、不能超过7NT；cheapest没有候选时报错。不论怎样生成动作，最终仍检查叫牌合法性和公开逼叫约束；失败不改选低优先级或兜底。

动态规则例子：

```json
{
  "id": "answer_one_or_four",
  "priority": 100,
  "when": {
    "op":"and",
    "args":[
      {"op":"eq","args":[{"op":"state"},"answer"]},
      {"op":"in","args":[{"op":"keycards","args":[{"op":"var","name":"trump"}]},[1,4]]}
    ]
  },
  "action": {"kind":"step","after":{"op":"var","name":"anchor"},"count":1}
}
```

这使一份协议可根据公开将牌和问叫锚点工作，不用为每个花色复制规则。尚未提供跨文件模板包含、嵌套协议调用或任意逻辑证明器；当前参数状态机和动作是可实际运行的基础能力。

## 8. 说明与验收边界

中文规范与执行从同一v2 IR生成，包含参数类型/初值/域、所有声明、转移、同时赋值、逼叫、优先级、动态动作和兜底。公开叫品摘要不依本次实际命中规则筛选；private_trace仍必须显式启用。

测试覆盖双方局况、第三家开叫/逆向角色、人工与自然公开含义差异、动态显示花色、四种将牌共享问叫、加级越NT及7NT边界、参数域和同时赋值、公开求值无手牌、逼叫/逼叫性Pass、干扰退出、任意发牌人/局况全Pass、竞争X/XX、前缀继续、双方轮换、CLI及v1回归。

这次不定义经典文档的“好套/调整点/赢墩”，不解决正文/摘要冲突，不宣称CCBA/精确已完成导入。移植应单独记录覆盖和未实现分支，再接DDS及科研评测。

## 9. 按局面阅读，而非翻阅完整IR

[体系导航与查询](13-system-navigation.md)在同一执行定义之上提供模块目录、稳定节点、依赖/反向引用和语义版本差异。`system browse`、研究工具及展示系统共用这层接口。给定公开前缀可查看具体叫品的公共转移和保证；给定本人手牌可调用原有决策器定位实际规则。只读视图不修改体系哈希，也不把私有命中当成公开含义。
