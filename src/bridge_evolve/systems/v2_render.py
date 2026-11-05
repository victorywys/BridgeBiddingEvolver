"""Full public specification of the parameterized v2 protocol."""

import json

from bridge_evolve.systems.language import SystemDefinition, thaw
from bridge_evolve.systems.render import render_action, render_expression
from bridge_evolve.systems.v2_language import CONTEXT_TYPES


def render_v2(system: SystemDefinition) -> str:
    lines = [
        f"# {system.name}", "",
        "- 语言：schema_version=2；operator_version=2。",
        f"- 内容哈希：`{system.content_hash}`；适用范围：`{system.scope}`。",
        "- 确定性规则只使用本人手牌及公开信息；不使用DDS、其他手牌、种子、隐藏权重或共享私有trace。",
        "- HCP为A=4/K=3/Q=2/J=1；控制点为A=2/K=1；均型仅4333/4432/5332。",
        "- 首轮控制有A；第二轮有A或K。显式shortage=true时分别也计缺门、缺门或单张。",
        "- 布尔and/or从左向右短路，between含两端；整数运算，无隐式类型转换。自定义特征在下方完整定义一次，使用处按名称引用，不反复展开。",
        "", "## 公开上下文的精确定义",
        "每次按发牌人和合法公开历史独立重建双方状态。opener为首次定约叫品的牌手，responder为其搭档；"
        "overcaller为另一方首次非Pass介入者（包括加倍），advancer为其搭档。our_opener/our_responder为本方首次叫定约者及其搭档。",
        "dynamic_role为opener/responder/overcaller/advancer/unopened/other。opening_position为首次开叫相对发牌人的1至4家；没有开叫为0。",
        "dealer是发牌人；vulnerability为none/ns/ew/both；own_vulnerable/opponents_vulnerable相对本方/敌方。"
        "partner_seat为当前牌手的搭档。passed_before_opening只检查本人首次定约叫品前的Pass。",
        "last_call/last_actor为最后一个公开叫品及牌手；last_bid/last_bid_strain/last_bid_level/last_bid_actor只取定约叫品。"
        "opening_strain为首次实际叫出的定约花色。partner_bid_strain/opponent_bid_strain分别为搭档/敌方最近实际定约花色。",
        "our_bid_count只计本方已经发生的BID，不计P/X/XX；last_bid_by为最近定约叫品相对本方的ours/opponents，无定约则空字符串。",
        "partner_shown_suit/opponent_shown_suit取相应最近定约叫品的静态声明显示花色，不把人工花色当自然。"
        "未提供该方公开定义时标签/显示花色查询报错；snapshot会列出不可用项。没有对应事件则空字符串/false。",
        "unbid_suits为C/D/H/S中尚未被任何一方实际叫过的花色，固定此顺序，不把人工叫品还原成自然套。"
        "未出现的座位/花色/叫品为空字符串；未出现的阶数为0。",
        "agreed_trump取本方trump参数（未定义则空）；forcing为none/one_round/invite_game/game/slam_try/small_slam/invite_grand/grand_slam。"
        "goal保留最近声明的叫牌目标；goal_level为该目标按将牌对应的最低阶数；goal_met按本方最近定约叫品检查目标是否达到。must_bid表示本人本次仍受约束。"
        "role旧算子仍从发牌人顺时针给dealer/lho/partner/rho，不等于dynamic_role。",
        "partnership_hcp_min为本方captain_hcp_min与responder_hcp_min之和；partnership_C/D/H/S_min为对应两方公开套长下界之和。"
        "agreed_trump_min为trump指定花色的上述和，未定将牌为0。不存在的参数按0处理，运行时不根据私牌或实际命中规则补推。",
        "partnership_C/D/H/S_max为双方同花色公开上界之和且最多13；未声明套长上界按13处理。agreed_trump_max取已定将牌的上述上界，未定将牌为0。"
        "partner_C/D/H/S_max取本人搭档的公开套长上界，不能用于公开转移赋值。",
        "对完整声明四套min/max的captain/responder账本，set同时求值后，仅按公开约束筛选所有560种总张数为13的牌型，"
        "将每套区间收紧为所有可行牌型的最小/最大值。owner_S_ge_H=true表示该owner的S张数不少于H；双向保证同时为真则两套等长。"
        "未知关系为false，不从私有命中分支补充保证；上下界与关系只收紧、不遗忘。没有可行牌型时显式报错。",
        "partnership_hcp_max取本方两个座位的有效HCP上界之和且最多40；有效上界为captain_hcp_max/responder_hcp_max与seat_N/E/S/W_hcp_max的交集。"
        "未定义上界按40处理。partner_hcp_max取本人搭档的有效上界，不允许在公开转移赋值中使用。该计算只读公开参数，不读任何手牌。",
        "captain参数指本方协议的开叫人/首位介入者；当前牌手为captain时，partner_hcp_min及partner_C/D/H/S_min取responder参数，"
        "否则取captain参数。这些玩家相对量不能用于公开转移赋值。double_kind取本方同名公开参数，opponent_double_kind取敌方同名参数；未声明为空。",
        "event.*只用于当前公开事件解释；其余历史字段均不含当前事件。event.by相对协议所属方为ours/opponents；"
        "event.kind为P/X/XX/BID，event.level在非定约事件为0，event.strain为空，event.tags为该事件的声明标签。",
        "event.position为该事件牌手相对发牌人的第1至4家；event.passed_before_opening仅检查该事件牌手在首次定约叫品前是否Pass过。",
        "if表达式依次为布尔条件、同类型的真/假分支，只计算选中的分支；公开转移的私有信息检查仍检查所有分支。",
        "announced按opening/last_bid/partner/opponents选择定约事件；last_call/partner_call/opponent_call包含P/X/XX。未出现事件为false。",
        "关键张算子为四张A加指定将牌K；动态手牌特征的花色必须已经确定为C/D/H/S，空值或NT报错。",
        "可用上下文及类型：" + "；".join(f"{name}:{kind}" for name, kind in CONTEXT_TYPES.items()) + "。",
        "", "## 自定义特征",
    ]
    lines.extend(f"- {name}：{render_expression(expr, expand_references=False)}。" for name, expr in system.features)
    if not system.features:
        lines.append("- 无。")
    lines.extend(["", "## 公开协议参数"])
    for parameter in system.parameters:
        initial = json.dumps(thaw(parameter.initial), ensure_ascii=False)
        domain = json.dumps([thaw(value) for value in parameter.domain], ensure_ascii=False) if parameter.domain else "该类型全部值"
        lines.append(f"- {parameter.name}：类型{parameter.kind}，初值{initial}，允许值{domain}。")
    lines.extend([
        "双方各自拥有参数。搭档从相同公开历史独立重建本方参数，不从手牌或实际命中规则赋值。",
        "", "## 公开声明",
        "事件首先仅由发出叫品一方的事先公开声明解释，所有命中的声明标签取并集。"
        "natural与artificial、weak_nt与strong_nt互斥；不同显示花色冲突即错误。没有声明不会猜标签。"
        "声明不能引用当前event.tags，转移可以；声明不能读取任何手牌或玩家相对特征。",
    ])
    for item in system.announcements:
        lines.append(
            f"- {item.id}：若{render_expression(item.when, expand_references=False)}，标签{list(item.tags)}"
            + (f"，显示花色={render_expression(item.suit, expand_references=False)}" if item.suit else "") + "。"
        )
    lines.extend([
        "", "## 公开状态机",
        f"初始状态：{system.initial}。每个公开叫品（包括P/X/XX）都经过两方各自状态机。",
        "全部条件在旧状态判断；恰好一条命中才按该转移执行，多条命中报错，零条走otherwise且保留参数。"
        "set中所有表达式用旧参数同时求值，再检查类型和域。公共求值器根本没有手牌可读。",
        "新套长账本的13张牌/套间关系传播发生在同时赋值之后，不改变各set表达式读取旧状态的语义；旧的仅下界IR不自动增补这些参数。",
        "若声明了HCP区间，则更新后必须满足0<=下界<=上界<=40；已知captain角色时还要与对应座位上界相容。空区间显式报错，不夹值、不偷偷丢弃先前信息。",
        "one_round要求搭档下一次非Pass；invite_game为可拒绝的一次邀请；game持续到本方成局（3NT/4H/4S/5C/5D）。"
        "slam_try为一轮满贯探查，不承诺六阶；small_slam持续到六阶；invite_grand至少承诺六阶但不强制七阶；grand_slam持续到七阶。"
        "回答一轮探查不消除已有更高目标义务；竞争中只有显式forcing=none可退出。达到目标后实际义务清除，goal仍保留供审查。"
        "匹配allow_pass=true转移才可在义务中Pass；结束时不允许仍悬置逼叫。"
        "这些是体系约束，不修改桥牌合法叫品。",
    ])
    for state in system.states:
        lines.append(f"- {state.name}：未命中转移则到{state.otherwise}。")
        for transition in state.transitions:
            assignments = "；".join(
                f"{name}={render_expression(expr, expand_references=False)}" for name, expr in transition.assignments
            ) or "参数保留"
            lines.append(
                f"  - {transition.id}：若{render_expression(transition.when, expand_references=False)} → {transition.target}；"
                f"{assignments}；forcing={transition.forcing or '保留/到期消耗'}；allow_pass={transition.allow_pass}。"
            )
    lines.extend([
        "", "## 完整决策顺序和动作",
        "规则按优先级从高到低逐层检查。首个有命中的层须恰有一条命中，多条即错误，即使动作相同也不例外。"
        "只有所有规则不命中才执行兜底。选中动作非法或违反逼叫即报错，不降级、不默认Pass。",
    ])
    for rule in sorted(system.rules, key=lambda item: item.priority, reverse=True):
        lines.append(f"- [{rule.priority}] {rule.id}：若{render_expression(rule.when, expand_references=False)}，"
                     f"则{render_action(rule.call, expand_references=False)}。")
    lines.extend([
        f"- 兜底：{render_action(system.fallback, expand_references=False)}。",
        "bid使用精确阶数与花色；step在35个定约叫品序列上从锚点后数正整数级，不跳过非法动作、不绕回；"
        "cheapest按阶数、C/D/H/S/NT选满足条件的最低合法定约叫品，无候选报错；error显式报告未实现。",
        "", "## 含义与边界",
        "完整叫品含义为所有有效分支之并集，并排除更高优先级命中及本级冲突。动态动作必须实际计算为该叫品。"
        "公开解释不选择本次私有命中分支；trace仅供牌后诊断，不能给其他牌手。",
        "这是可执行协议规范，不是经典体系覆盖或强度证明。没有任意自然语言自动编译、DDS墩数或科研成绩。",
        "",
    ])
    return "\n".join(lines)
