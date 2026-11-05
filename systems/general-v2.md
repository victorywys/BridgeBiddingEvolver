# v2底座演示：参数化高花配合与关键张（非经典体系）

- 语言：schema_version=2；operator_version=2。
- 内容哈希：`174c8a839af641ec76ecc9e50b66cd9c77fc33ed897ab1560b7a33040c3a9ed7`；适用范围：`general`。
- 确定性规则只使用本人手牌及公开信息；不使用DDS、其他手牌、种子、隐藏权重或共享私有trace。
- HCP为A=4/K=3/Q=2/J=1；控制点为A=2/K=1；均型仅4333/4432/5332。
- 首轮控制有A；第二轮有A或K。显式shortage=true时分别也计缺门、缺门或单张。
- 布尔and/or从左向右短路，between含两端；整数运算，无隐式类型转换。自定义特征会展开在下方。

## 公开上下文的精确定义
每次按发牌人和合法公开历史独立重建双方状态。opener为首次定约叫品的牌手，responder为其搭档；overcaller为另一方首次非Pass介入者（包括加倍），advancer为其搭档。our_opener/our_responder为本方首次叫定约者及其搭档。
dynamic_role为opener/responder/overcaller/advancer/unopened/other。opening_position为首次开叫相对发牌人的1至4家；没有开叫为0。
dealer是发牌人；vulnerability为none/ns/ew/both；own_vulnerable/opponents_vulnerable相对本方/敌方。partner_seat为当前牌手的搭档。passed_before_opening只检查本人首次定约叫品前的Pass。
last_call/last_actor为最后一个公开叫品及牌手；last_bid/last_bid_strain/last_bid_level/last_bid_actor只取定约叫品。opening_strain为首次实际叫出的定约花色。partner_bid_strain/opponent_bid_strain分别为搭档/敌方最近实际定约花色。
partner_shown_suit/opponent_shown_suit取相应最近定约叫品的静态声明显示花色，不把人工花色当自然。未提供该方公开定义时标签/显示花色查询报错；snapshot会列出不可用项。没有对应事件则空字符串/false。
unbid_suits为C/D/H/S中尚未被任何一方实际叫过的花色，固定此顺序，不把人工叫品还原成自然套。未出现的座位/花色/叫品为空字符串；未出现的阶数为0。
agreed_trump取本方trump参数（未定义则空）；forcing为none/one_round/game；must_bid表示本人本次受约束。role旧算子仍从发牌人顺时针给dealer/lho/partner/rho，不等于dynamic_role。
event.*只用于当前公开事件解释；其余历史字段均不含当前事件。event.by相对协议所属方为ours/opponents；event.kind为P/X/XX/BID，event.level在非定约事件为0，event.strain为空，event.tags为该事件的声明标签。
event.position为该事件牌手相对发牌人的第1至4家；event.passed_before_opening仅检查该事件牌手在首次定约叫品前是否Pass过。
if表达式依次为布尔条件、同类型的真/假分支，只计算选中的分支；公开转移的私有信息检查仍检查所有分支。
announced按opening/last_bid/partner/opponents选择定约事件；last_call/partner_call/opponent_call包含P/X/XX。未出现事件为false。
关键张算子为四张A加指定将牌K；动态手牌特征的花色必须已经确定为C/D/H/S，空值或NT报错。
可用上下文及类型：state:string；dealer:string；vulnerability:string；own_vulnerable:bool；opponents_vulnerable:bool；partner_seat:string；opener:string；responder:string；overcaller:string；advancer:string；our_opener:string；our_responder:string；dynamic_role:string；opening_position:number；opening_strain:string；last_call:string；last_actor:string；last_bid_strain:string；last_bid_level:number；last_bid_actor:string；last_bid:string；partner_bid_strain:string；opponent_bid_strain:string；partner_shown_suit:string；opponent_shown_suit:string；unbid_suits:list[string]；passed_before_opening:bool；agreed_trump:string；forcing:string；must_bid:bool；event.actor:string；event.call:string；event.strain:string；event.level:number；event.kind:string；event.by:string；event.tags:list[string]；event.position:number；event.passed_before_opening:bool。

## 自定义特征
- keycards：以公开协议参数[trump]为将牌的关键张数（四张A加将牌K）。
- trump_queen：（公开协议参数[trump]）套中Q的持有张数。

## 公开协议参数
- suit：类型string，初值""，允许值["", "C", "D", "H", "S"]。
- trump：类型string，初值""，允许值["", "C", "D", "H", "S"]。
- anchor：类型string，初值""，允许值该类型全部值。
- asker：类型string，初值""，允许值["", "N", "E", "S", "W"]。
双方各自拥有参数。搭档从相同公开历史独立重建本方参数，不从手牌或实际命中规则赋值。

## 公开声明
事件首先仅由发出叫品一方的事先公开声明解释，所有命中的声明标签取并集。natural与artificial、weak_nt与strong_nt互斥；不同显示花色冲突即错误。没有声明不会猜标签。声明不能引用当前event.tags，转移可以；声明不能读取任何手牌或玩家相对特征。
- natural_major：若(非（公开历史中已有定约叫品（不含Pass/X/XX）） 且 （公开上下文[event.level] 等于 1） 且 （公开上下文[event.strain] 属于 ["H", "S"]）)，标签['natural', 'five_card_major']，显示花色=公开上下文[event.strain]。
- weak_nt_opening：若(非（公开历史中已有定约叫品（不含Pass/X/XX）） 且 （公开上下文[event.call] 等于 "1NT"）)，标签['natural', 'weak_nt']。
- artificial_question：若(（根据全部公开叫牌重建的协议状态 等于 "fitted"） 且 （公开上下文[event.call] 等于 "4NT"）)，标签['artificial', 'keycard_ask']。

## 公开状态机
初始状态：idle。每个公开叫品（包括P/X/XX）都经过两方各自状态机。
全部条件在旧状态判断；恰好一条命中才按该转移执行，多条命中报错，零条走otherwise且保留参数。set中所有表达式用旧参数同时求值，再检查类型和域。公共求值器根本没有手牌可读。
one_round要求发起方搭档在其下次行动作非Pass，到期后消耗，不清除已有game义务；game持续到本方叫出成局阶数（3NT/4H/4S/5C/5D及以上）。竞争中须显式forcing=none退出。匹配allow_pass=true转移才可在义务中Pass；结束时不允许仍悬置逼叫。这些是体系约束，不修改桥牌合法叫品。
- idle：未命中转移则到idle。
  - major_opened：若(（公开上下文[event.by] 等于 "ours"） 且 非（公开历史中已有定约叫品（不含Pass/X/XX）） 且 （公开上下文[event.level] 等于 1） 且 （公开上下文[event.strain] 属于 ["H", "S"]）) → opened；suit=公开上下文[event.strain]；forcing=保留/到期消耗；allow_pass=False。
- opened：未命中转移则到opened。
  - forcing_raise：若(（公开上下文[event.by] 等于 "ours"） 且 （公开上下文[event.actor] 等于 公开上下文[our_responder]） 且 （公开上下文[event.strain] 等于 公开协议参数[suit]） 且 （公开上下文[event.level] 等于 3）) → fitted；trump=公开协议参数[suit]；forcing=game；allow_pass=False。
  - interference_before_fit：若(（公开上下文[event.by] 等于 "opponents"） 且 （公开上下文[event.kind] 不等于 "P"）) → outside；参数保留；forcing=none；allow_pass=False。
- fitted：未命中转移则到fitted。
  - keycard_question：若(（公开上下文[event.by] 等于 "ours"） 且 （公开上下文[event.call] 等于 "4NT"）) → keycards；anchor=公开上下文[event.call]；asker=公开上下文[event.actor]；forcing=one_round；allow_pass=False。
  - game_selected：若(（公开上下文[event.by] 等于 "ours"） 且 （公开上下文[event.strain] 等于 公开协议参数[trump]） 且 （公开上下文[event.level] 不小于 4）) → finished；参数保留；forcing=none；allow_pass=False。
  - interference_after_fit：若(（公开上下文[event.by] 等于 "opponents"） 且 （公开上下文[event.kind] 不等于 "P"）) → outside；参数保留；forcing=none；allow_pass=False。
- keycards：未命中转移则到keycards。
  - keycards_answered：若(（公开上下文[event.by] 等于 "ours"） 且 （公开上下文[event.actor] 不等于 公开协议参数[asker]） 且 （公开上下文[event.call] 属于 ["5C", "5D", "5H", "5S"]）) → answered；参数保留；forcing=保留/到期消耗；allow_pass=False。
  - interference_during_question：若(（公开上下文[event.by] 等于 "opponents"） 且 （公开上下文[event.kind] 不等于 "P"）) → outside；参数保留；forcing=none；allow_pass=False。
- answered：未命中转移则到answered。
  - slam_selected：若(（公开上下文[event.by] 等于 "ours"） 且 （公开上下文[event.strain] 等于 公开协议参数[trump]） 且 （公开上下文[event.level] 等于 6）) → finished；参数保留；forcing=保留/到期消耗；allow_pass=False。
- outside：未命中转移则到outside。
- finished：未命中转移则到finished。

## 完整决策顺序和动作
规则按优先级从高到低逐层检查。首个有命中的层须恰有一条命中，多条即错误，即使动作相同也不例外。只有所有规则不命中才执行兜底。选中动作非法或违反逼叫即报错，不降级、不默认Pass。
- [100] open_weak_nt：若(非（公开历史中已有定约叫品（不含Pass/X/XX）） 且 （大牌点（A=4、K=3、Q=2、J=1） 在 13 至 15 之间，两端均含） 且 牌型属于4333、4432或5332)，则1NT。
- [90] open_hearts：若(非（公开历史中已有定约叫品（不含Pass/X/XX）） 且 （大牌点（A=4、K=3、Q=2、J=1） 不小于 12） 且 （H套张数 不小于 5） 且 （H套张数 不小于 S套张数）)，则叫1阶"H"。
- [90] open_spades：若(非（公开历史中已有定约叫品（不含Pass/X/XX）） 且 （大牌点（A=4、K=3、Q=2、J=1） 不小于 12） 且 （S套张数 不小于 5） 且 （S套张数 大于 H套张数）)，则叫1阶"S"。
- [80] forcing_support：若(（根据全部公开叫牌重建的协议状态 等于 "opened"） 且 （当前座位（N/E/S/W） 等于 公开上下文[our_responder]） 且 （大牌点（A=4、K=3、Q=2、J=1） 不小于 13） 且 （（公开协议参数[suit]）套张数 不小于 3）)，则叫3阶公开协议参数[suit]。
- [70] ask_keycards：若(（根据全部公开叫牌重建的协议状态 等于 "fitted"） 且 （当前座位（N/E/S/W） 等于 公开上下文[our_opener]） 且 （大牌点（A=4、K=3、Q=2、J=1） 不小于 17）)，则4NT。
- [60] select_game：若（根据全部公开叫牌重建的协议状态 等于 "fitted"），则叫4阶公开协议参数[trump]。
- [60] keycards_14：若(（根据全部公开叫牌重建的协议状态 等于 "keycards"） 且 （自定义特征「keycards」（以公开协议参数[trump]为将牌的关键张数（四张A加将牌K）） 属于 [1, 4]）)，则从公开协议参数[anchor]之后数1级（35个定约叫品顺序，不跳过非法级，越7NT报错）。
- [60] keycards_03：若(（根据全部公开叫牌重建的协议状态 等于 "keycards"） 且 （自定义特征「keycards」（以公开协议参数[trump]为将牌的关键张数（四张A加将牌K）） 属于 [0, 3]）)，则从公开协议参数[anchor]之后数2级（35个定约叫品顺序，不跳过非法级，越7NT报错）。
- [60] keycards_25_without_queen：若(（根据全部公开叫牌重建的协议状态 等于 "keycards"） 且 （自定义特征「keycards」（以公开协议参数[trump]为将牌的关键张数（四张A加将牌K）） 属于 [2, 5]） 且 （自定义特征「trump_queen」（（公开协议参数[trump]）套中Q的持有张数） 等于 0）)，则从公开协议参数[anchor]之后数3级（35个定约叫品顺序，不跳过非法级，越7NT报错）。
- [60] keycards_25_with_queen：若(（根据全部公开叫牌重建的协议状态 等于 "keycards"） 且 （自定义特征「keycards」（以公开协议参数[trump]为将牌的关键张数（四张A加将牌K）） 属于 [2, 5]） 且 （自定义特征「trump_queen」（（公开协议参数[trump]）套中Q的持有张数） 等于 1）)，则从公开协议参数[anchor]之后数4级（35个定约叫品顺序，不跳过非法级，越7NT报错）。
- [50] demonstrate_slam_exit：若（根据全部公开叫牌重建的协议状态 等于 "answered"），则叫6阶公开协议参数[trump]。
- 兜底：P。
bid使用精确阶数与花色；step在35个定约叫品序列上从锚点后数正整数级，不跳过非法动作、不绕回；cheapest按阶数、C/D/H/S/NT选满足条件的最低合法定约叫品，无候选报错；error显式报告未实现。

## 含义与边界
完整叫品含义为所有有效分支之并集，并排除更高优先级命中及本级冲突。动态动作必须实际计算为该叫品。公开解释不选择本次私有命中分支；trace仅供牌后诊断，不能给其他牌手。
这是可执行协议规范，不是经典体系覆盖或强度证明。没有任意自然语言自动编译、DDS墩数或科研成绩。
