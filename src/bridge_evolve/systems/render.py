"""Complete Chinese specifications generated from the executable IR."""

import json
from functools import lru_cache

from bridge_evolve.core import Call
from bridge_evolve.systems.language import Action, PRIMITIVES, Expression, SystemDefinition, SystemError, thaw


@lru_cache(maxsize=8192)
def render_expression(expression: Expression, *, expand_references: bool = True) -> str:
    op = expression.op
    parameters = dict(expression.parameters)
    if op == "literal":
        return json.dumps(thaw(expression.literal), ensure_ascii=False)
    if op in PRIMITIVES:
        return PRIMITIVES[op][1]
    if op == "ref":
        reference = f"自定义特征「{parameters['name']}」"
        return reference + f"（{render_expression(expression.args[0])}）" if expand_references else reference
    parameters.update((name, f"（{render_expression(child, expand_references=expand_references)}）")
                      for name, child in expression.bindings)
    if op == "context":
        return f"公开上下文[{parameters['name']}]"
    if op == "var":
        return f"公开协议参数[{parameters['name']}]"
    if op == "announced":
        return f"公开事件[{parameters['which']}]事先约定的标签含{parameters['tag']}"
    if op == "keycards":
        return f"以{render_expression(expression.args[0], expand_references=expand_references)}为将牌的关键张数（四张A加将牌K）"
    if op == "list":
        return "[" + "，".join(render_expression(child, expand_references=expand_references) for child in expression.args) + "]"
    if op == "if":
        arguments = [render_expression(child, expand_references=expand_references) for child in expression.args]
        return f"若{arguments[0]}则{arguments[1]}否则{arguments[2]}（只计算所选分支）"
    if op == "length":
        return f"{parameters['suit']}套张数"
    if op == "honors":
        return f"{parameters['suit']}套中{parameters['ranks']}的持有张数"
    if op == "has":
        return f"持有{parameters['card']}"
    if op == "is_legal":
        return f"叫品{parameters['call']}当前合法"
    if op == "control":
        cards = "A" if parameters["round"] == 1 else "A或K"
        shortage = "或缺门" if parameters["round"] == 1 else "或缺门/单张"
        return f"{parameters['suit']}套持有{cards}{shortage if parameters['shortage'] else '（不计短套）'}"
    args = [render_expression(arg, expand_references=expand_references) for arg in expression.args]
    if op in ("and", "or"):
        return "(" + (" 且 " if op == "and" else " 或 ").join(args) + ")"
    if op == "not":
        return f"非（{args[0]}）"
    symbols = {"eq": "等于", "ne": "不等于", "lt": "小于", "le": "不大于", "gt": "大于", "ge": "不小于"}
    if op in symbols:
        return f"（{args[0]} {symbols[op]} {args[1]}）"
    if op == "in":
        return f"（{args[0]} 属于 {args[1]}）"
    if op == "between":
        return f"（{args[0]} 在 {args[1]} 至 {args[2]} 之间，两端均含）"
    if op in ("add", "sub", "mul"):
        return "(" + {"add": " + ", "sub": " - ", "mul": " × "}[op].join(args) + ")"
    if op in ("min", "max"):
        return f"{'最小值' if op == 'min' else '最大值'}({', '.join(args)})"
    raise SystemError(f"No renderer for operator: {op}")


def render_action(action: Call | Action, *, expand_references: bool = True) -> str:
    if isinstance(action, Call):
        return str(action)
    if action.kind == "call":
        return str(action.call)
    if action.kind == "error":
        return f"显式报未实现错误：{action.message}"
    expressions = {name: render_expression(expr, expand_references=expand_references) for name, expr in action.expressions}
    if action.kind == "bid":
        return f"叫{expressions['level']}阶{expressions['strain']}"
    if action.kind == "step":
        return f"从{expressions['after']}之后数{expressions['count']}级（35个定约叫品顺序，不跳过非法级，越7NT报错）"
    if action.kind == "cheapest":
        return (f"在花色{expressions['strains']}、阶数{expressions['min_level']}至{expressions['max_level']}"
                "中选最低合法定约叫品（C/D/H/S/NT顺序，不选P/X/XX，无候选报错）")
    raise SystemError(f"No renderer for action: {action.kind}")


@lru_cache(maxsize=256)
def render_call(system: SystemDefinition, call: Call) -> str:
    if system.version == 2:
        return (
            f"叫品{call}的公开含义需按完整v2规范确定。所有候选分支如下："
            + "；".join(
                f"{rule.id}[{rule.priority}]：{render_expression(rule.when)} → {render_action(rule.call)}"
                for rule in sorted(system.rules, key=lambda item: item.priority, reverse=True)
            )
            + f"；全部不命中则{render_action(system.fallback)}。"
            "有效分支须无更高优先级命中、最高层无冲突，且动态动作恰好计算出本叫品。"
            "标签与显示花色按事先公开声明解释，不取本次私有命中分支或特征值。"
        )
    clauses = [
        f"{rule.id}（优先级{rule.priority}）：{render_expression(rule.when)}"
        for rule in sorted(system.rules, key=lambda rule: rule.priority, reverse=True)
        if rule.call == call
    ]
    if system.fallback == call:
        clauses.append("兜底：所有规则均不命中")
    return (
        f"叫品{call}的公开规则分支："
        + ("；".join(clauses) if clauses else "本体系没有分配该叫品")
        + "。每个分支仅在无更高优先级命中且该最高层无冲突时成立；"
        "同一叫品含义是这些有效分支的并集。此说明不使用本次私牌或命中trace。"
    )


@lru_cache(maxsize=32)
def render_system(system: SystemDefinition) -> str:
    if system.version == 2:
        from bridge_evolve.systems.v2_render import render_v2
        return render_v2(system)
    lines = [
        f"# {system.name}", "",
        f"- 语言：schema_version=1；operator_version=1。",
        f"- 内容哈希：`{system.content_hash}`。",
        f"- 适用范围：`{system.scope}`。确定性策略，不使用随机数或隐藏模型。",
        "- 决策只依据本人13张牌、座位、发牌人、局况及全部公开叫牌；不读其他手牌、DDS或运行标识。",
        "- 角色从发牌人起顺时针依次为dealer、lho、partner、rho；角色不是根据私牌推断。",
        "- HCP为A=4/K=3/Q=2/J=1；控制点为A=2/K=1。均型仅指4333/4432/5332。",
        "- 首轮控制为A；第二轮控制为A或K。只有显式shortage=true时，首轮计缺门，第二轮计缺门或单张。",
        "- 布尔and/or从左向右短路；比较不做隐式类型转换；between包含两端；所有算术为整数。",
        "- 套长及牌张特征均只针对本人；previously_passed指本人历史中任何一次Pass。",
        "", "## 自定义特征",
    ]
    lines.extend(
        f"- {name}：{render_expression(expression)}。" for name, expression in system.features
    )
    if not system.features:
        lines.append("- 无。")
    lines.extend([
        "", "## 公开协议状态",
        f"每次从初始状态`{system.initial}`重放全部公开叫品（包括Pass/X/XX）。"
        "按on中的精确叫品转移，否则按otherwise转移；无私有状态，状态不使非法叫品合法。",
    ])
    for state in system.states:
        transitions = "；".join(f"{call} → {target}" for call, target in state.on)
        lines.append(f"- {state.name}：{transitions + '；' if transitions else ''}其他叫品 → {state.otherwise}。")
    lines.extend([
        "", "## 完整决策顺序",
        "按优先级从高到低逐层判断。首个有命中的层必须恰有一条命中；"
        "同层多条命中即报错，不按文件顺序取第一条。更低层不再判断。"
        "选中动作非法即报错，不降级、不改成Pass。只有所有条件均为假才执行兜底。",
    ])
    for rule in sorted(system.rules, key=lambda rule: rule.priority, reverse=True):
        lines.append(f"- [{rule.priority}] {rule.id}：若{render_expression(rule.when)}，则叫{rule.call}。")
    lines.extend([
        f"- 兜底：{system.fallback}（仍须合法，否则报错）。",
        "", "## 公开叫品含义",
        "每个叫品的完整含义由上述全部有效分支及优先级排除共同确定，不能只取本次命中规则当公开解释。",
        "同一叫品的各有效分支取并集；兜底分支排除全部规则。私有trace仅供牌后诊断，不能传给搭档。",
        "", "## 边界",
        "初版不把自由自然语言自动编译成规则；说明由同一IR确定性生成。"
        "general范围不等于策略强度或覆盖性的证明；局部1NT演示不代表完整竞争体系。"
        "本规范不含DDS结果、预计墩数或成绩。",
        "",
    ])
    return "\n".join(lines)
