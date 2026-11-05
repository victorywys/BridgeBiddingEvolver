# 实验1NT-v1：无将邀请与双高花转移

- 语言：schema_version=1；operator_version=1。
- 内容哈希：`81a5fc2ce1feb94ba15aa4e777eaf50b48120d05cdd90cc5a1483a783b53fb95`。
- 适用范围：`uncontested-1nt`。确定性策略，不使用随机数或隐藏模型。
- 决策只依据本人13张牌、座位、发牌人、局况及全部公开叫牌；不读其他手牌、DDS或运行标识。
- 角色从发牌人起顺时针依次为dealer、lho、partner、rho；角色不是根据私牌推断。
- HCP为A=4/K=3/Q=2/J=1；控制点为A=2/K=1。均型仅指4333/4432/5332。
- 首轮控制为A；第二轮控制为A或K。只有显式shortage=true时，首轮计缺门，第二轮计缺门或单张。
- 布尔and/or从左向右短路；比较不做隐式类型转换；between包含两端；所有算术为整数。
- 套长及牌张特征均只针对本人；previously_passed指本人历史中任何一次Pass。

## 自定义特征
- opening：(（大牌点（A=4、K=3、Q=2、J=1） 在 15 至 17 之间，两端均含） 且 牌型属于4333、4432或5332)。
- invitation：（大牌点（A=4、K=3、Q=2、J=1） 在 8 至 9 之间，两端均含）。
- game_values：（大牌点（A=4、K=3、Q=2、J=1） 不小于 10）。
- maximum：（大牌点（A=4、K=3、Q=2、J=1） 等于 17）。
- hearts_preferred：(（H套张数 不小于 5） 且 （H套张数 不小于 S套张数）)。
- spades_preferred：(（S套张数 不小于 5） 且 （S套张数 大于 H套张数）)。

## 公开协议状态
每次从初始状态`start`重放全部公开叫品（包括Pass/X/XX）。按on中的精确叫品转移，否则按otherwise转移；无私有状态，状态不使非法叫品合法。
- start：1NT → opened；其他叫品 → outside。
- outside：其他叫品 → outside。
- opened：P → respond；其他叫品 → outside。
- respond：P → signed_off；2D → h_transfer_lho；2H → s_transfer_lho；2NT → nt_invite_lho；3NT → signed_off；其他叫品 → outside。
- h_transfer_lho：P → h_transfer_accept；其他叫品 → outside。
- s_transfer_lho：P → s_transfer_accept；其他叫品 → outside。
- h_transfer_accept：2H → h_transfer_rho；其他叫品 → outside。
- s_transfer_accept：2S → s_transfer_rho；其他叫品 → outside。
- h_transfer_rho：P → h_continue；其他叫品 → outside。
- s_transfer_rho：P → s_continue；其他叫品 → outside。
- h_continue：P → signed_off；2NT → h_invite_lho；3NT → h_game_lho；其他叫品 → outside。
- s_continue：P → signed_off；2NT → s_invite_lho；3NT → s_game_lho；其他叫品 → outside。
- nt_invite_lho：P → nt_invite_decide；其他叫品 → outside。
- h_invite_lho：P → h_invite_decide；其他叫品 → outside。
- s_invite_lho：P → s_invite_decide；其他叫品 → outside。
- h_game_lho：P → h_game_decide；其他叫品 → outside。
- s_game_lho：P → s_game_decide；其他叫品 → outside。
- nt_invite_decide：P → signed_off；3NT → signed_off；其他叫品 → outside。
- h_invite_decide：P → signed_off；3NT → signed_off；3H → signed_off；4H → signed_off；其他叫品 → outside。
- s_invite_decide：P → signed_off；3NT → signed_off；3S → signed_off；4S → signed_off；其他叫品 → outside。
- h_game_decide：P → signed_off；4H → signed_off；其他叫品 → outside。
- s_game_decide：P → signed_off；4S → signed_off；其他叫品 → outside。
- signed_off：P → signed_off；其他叫品 → outside。

## 完整决策顺序
按优先级从高到低逐层判断。首个有命中的层必须恰有一条命中；同层多条命中即报错，不按文件顺序取第一条。更低层不再判断。选中动作非法即报错，不降级、不改成Pass。只有所有条件均为假才执行兜底。
- [100] open_1nt：若(（根据全部公开叫牌重建的协议状态 等于 "start"） 且 （相对发牌人的角色（dealer/lho/partner/rho） 等于 "dealer"） 且 自定义特征「opening」（(（大牌点（A=4、K=3、Q=2、J=1） 在 15 至 17 之间，两端均含） 且 牌型属于4333、4432或5332)）)，则叫1NT。
- [90] transfer_hearts：若(（根据全部公开叫牌重建的协议状态 等于 "respond"） 且 自定义特征「hearts_preferred」（(（H套张数 不小于 5） 且 （H套张数 不小于 S套张数）)）)，则叫2D。
- [90] transfer_spades：若(（根据全部公开叫牌重建的协议状态 等于 "respond"） 且 自定义特征「spades_preferred」（(（S套张数 不小于 5） 且 （S套张数 大于 H套张数）)）)，则叫2H。
- [90] accept_hearts：若（根据全部公开叫牌重建的协议状态 等于 "h_transfer_accept"），则叫2H。
- [90] accept_spades：若（根据全部公开叫牌重建的协议状态 等于 "s_transfer_accept"），则叫2S。
- [80] invite_with_fit_h_max：若(（根据全部公开叫牌重建的协议状态 等于 "h_invite_decide"） 且 （H套张数 不小于 3） 且 自定义特征「maximum」（（大牌点（A=4、K=3、Q=2、J=1） 等于 17））)，则叫4H。
- [80] invite_with_fit_s_max：若(（根据全部公开叫牌重建的协议状态 等于 "s_invite_decide"） 且 （S套张数 不小于 3） 且 自定义特征「maximum」（（大牌点（A=4、K=3、Q=2、J=1） 等于 17））)，则叫4S。
- [70] invite_with_fit_h_min：若(（根据全部公开叫牌重建的协议状态 等于 "h_invite_decide"） 且 （H套张数 不小于 3）)，则叫3H。
- [70] invite_with_fit_s_min：若(（根据全部公开叫牌重建的协议状态 等于 "s_invite_decide"） 且 （S套张数 不小于 3）)，则叫3S。
- [70] game_with_fit_h：若(（根据全部公开叫牌重建的协议状态 等于 "h_game_decide"） 且 （H套张数 不小于 3）)，则叫4H。
- [70] game_with_fit_s：若(（根据全部公开叫牌重建的协议状态 等于 "s_game_decide"） 且 （S套张数 不小于 3）)，则叫4S。
- [60] accept_nt_invitation：若(（根据全部公开叫牌重建的协议状态 属于 ["nt_invite_decide", "h_invite_decide", "s_invite_decide"]） 且 自定义特征「maximum」（（大牌点（A=4、K=3、Q=2、J=1） 等于 17））)，则叫3NT。
- [50] invite_nt：若(（根据全部公开叫牌重建的协议状态 属于 ["respond", "h_continue", "s_continue"]） 且 自定义特征「invitation」（（大牌点（A=4、K=3、Q=2、J=1） 在 8 至 9 之间，两端均含））)，则叫2NT。
- [50] bid_nt_game：若(（根据全部公开叫牌重建的协议状态 属于 ["respond", "h_continue", "s_continue"]） 且 自定义特征「game_values」（（大牌点（A=4、K=3、Q=2、J=1） 不小于 10））)，则叫3NT。
- 兜底：P（仍须合法，否则报错）。

## 公开叫品含义
每个叫品的完整含义由上述全部有效分支及优先级排除共同确定，不能只取本次命中规则当公开解释。
同一叫品的各有效分支取并集；兜底分支排除全部规则。私有trace仅供牌后诊断，不能传给搭档。

## 边界
初版不把自由自然语言自动编译成规则；说明由同一IR确定性生成。general范围不等于策略强度或覆盖性的证明；局部1NT演示不代表完整竞争体系。本规范不含DDS结果、预计墩数或成绩。
