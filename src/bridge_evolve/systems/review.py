"""Public-position views for human protocol review, without selecting a private hand."""

from __future__ import annotations

import copy
from dataclasses import replace
from itertools import groupby, product

from bridge_evolve.core import ALL_BID_CALLS, Call, Seat, Vulnerability, legal_calls
from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.systems.inputs import parse_prefix
from bridge_evolve.systems.language import Expression, SystemError, object_fields, parse_call
from bridge_evolve.systems.navigation import PublicFilter, UNKNOWN, _public_snapshot, auction_lookup
from bridge_evolve.systems.render import render_expression
from bridge_evolve.systems.runtime import Evaluation, PublicView, replay
from bridge_evolve.systems.v2_runtime import PublicContext, PublicWorld, resolve_action

SEAT_NAMES = {"N": "北家", "E": "东家", "S": "南家", "W": "西家"}
SUIT_NAMES = {"S": "黑桃", "H": "红桃", "D": "方片", "C": "梅花", "NT": "无将"}
TRUE = Expression("literal", "bool", literal=True)
FALSE = Expression("literal", "bool", literal=False)
ALL_CALLS = (*ALL_BID_CALLS, Call.pass_call(), Call.double(), Call.redouble())


def _not(expression):
    if expression.op == "literal":
        return FALSE if expression.literal else TRUE
    if expression.op == "not":
        return expression.args[0]
    return Expression("not", "bool", (expression,))


def _combine(operator, expressions):
    identity, decisive = (TRUE, FALSE) if operator == "and" else (FALSE, TRUE)
    items = []
    for expression in expressions:
        if expression == decisive:
            return decisive
        if expression == identity:
            continue
        children = expression.args if expression.op == operator else (expression,)
        for child in children:
            if _not(child) in items:
                return decisive
            if child not in items:
                items.append(child)
    return identity if not items else items[0] if len(items) == 1 else Expression(operator, "bool", tuple(items))


def _specialize(expression, public):
    if expression.op == "is_legal" and expression.bindings:
        try:
            call = _specialize(dict(expression.bindings)["call"], public)
        except SystemError:
            return expression
        specialized = replace(expression, bindings=(("call", call),))
        choices, incomplete = _choices(call)
        if not incomplete and public.evaluation is not None:
            guards = []
            try:
                for value, when in choices:
                    check = replace(expression, bindings=(("call", Expression("literal", call.kind, literal=value)),))
                    if public.evaluation.evaluate(check):
                        guards.append(when)
            except SystemError:
                return specialized
            if not guards:
                return FALSE
            return TRUE if len(guards) == len(choices) else _combine("or", guards)
        return specialized
    value = public.value(expression)
    if value is not UNKNOWN:
        return Expression("literal", expression.kind, literal=value)
    if expression.op == "ref":
        return _specialize(expression.args[0], public)
    if expression.op in ("and", "or"):
        return _combine(expression.op, (_specialize(child, public) for child in expression.args))
    if expression.op == "not":
        return _not(_specialize(expression.args[0], public))
    if expression.op == "if":
        condition = _specialize(expression.args[0], public)
        if condition in (TRUE, FALSE):
            return _specialize(expression.args[1 if condition == TRUE else 2], public)
    return replace(expression, args=tuple(_specialize(child, public) for child in expression.args),
                   bindings=tuple((name, _specialize(child, public)) for name, child in expression.bindings))


def _length_suit(expression):
    suit = dict(expression.parameters).get("suit")
    binding = dict(expression.bindings).get("suit")
    if binding is not None and binding.op == "literal":
        suit = binding.literal
    return SUIT_NAMES.get(suit) if isinstance(suit, str) else None


def _condition(expression):
    if expression == TRUE:
        return "本阶段任意手牌", True
    if expression == FALSE:
        return "本阶段不适用", True
    if expression.op in ("and", "or"):
        children = [_condition(child) for child in expression.args]
        joiner = "；并且" if expression.op == "and" else "；或者"
        return joiner.join(f"（{text}）" if child.op in ("and", "or") else text
                           for child, (text, _) in zip(expression.args, children, strict=True)), all(ok for _, ok in children)
    if expression.op == "not":
        text, readable = _condition(expression.args[0])
        return f"不满足“{text}”", readable
    if expression.op in ("context", "announced", "var", "state", "role"):
        return "依赖尚不能确定的公开体系信息", False
    if expression.op == "in" and expression.args[0].op == "if" and expression.args[1].op == "literal":
        choices, incomplete = _choices(expression.args[0])
        if not incomplete:
            return _condition(_combine("or", (guard for value, guard in choices if value in expression.args[1].literal)))
    if expression.op in ("ge", "gt", "le", "lt", "eq", "ne"):
        left, right = expression.args
        if expression.op in ("eq", "ne") and left.op == "if" and right.op == "literal":
            choices, incomplete = _choices(left)
            if not incomplete:
                matching = _combine("or", (guard for value, guard in choices if value == right.literal))
                return _condition(matching if expression.op == "eq" else _not(matching))
        if left.op == right.op == "length":
            first, second = (_length_suit(item) for item in (left, right))
            if first and second:
                comparison = {"ge": "不少于", "gt": "长于", "le": "不长于", "lt": "短于", "eq": "与", "ne": "与"}
                ending = "等长" if expression.op == "eq" else "不等长" if expression.op == "ne" else ""
                return first + comparison[expression.op] + second + ending, True
        if right.op == "literal" and type(right.literal) is int:
            value = right.literal
            label = {"hcp": "大牌点", "controls": "控制点"}.get(left.op)
            unit = "点"
            if left.op == "length":
                label, unit = _length_suit(left), "张"
            if label:
                comparison = {"ge": "至少", "gt": "至少", "le": "至多", "lt": "至多", "eq": "恰好", "ne": "不等于"}
                value += 1 if expression.op == "gt" else -1 if expression.op == "lt" else 0
                return f"{label}{comparison[expression.op]}{value}{unit}", True
    if expression.op == "ref":
        return _condition(expression.args[0])
    if expression.op == "balanced":
        return "均型（4333、4432或5332）", True
    if expression.op == "length":
        label = _length_suit(expression)
        return (label + "张数", True) if label else ("花色尚未确定的张数", False)
    if expression.op == "is_legal" and expression.bindings:
        call = dict(expression.bindings)["call"]
        choices, incomplete = _choices(call)
        if incomplete or any(not isinstance(value, str) or not value for value, _ in choices):
            return "对应动态叫品是否合法尚不能确定，需先满足此前手牌条件", False
    children = [_condition(child) for child in (*expression.args, *(value for _, value in expression.bindings))]
    if not all(ok for _, ok in children):
        return "条件中的公开信息或动态花色尚未确定，不能完整展开", False
    return render_expression(expression), True


def _choices(expression):
    if expression.op == "literal":
        return [(expression.literal, TRUE)], False
    if expression.op == "list":
        children = [_choices(child) for child in expression.args]
        result = {}
        for combination in product(*(choices for choices, _ in children)):
            guard = _combine("and", (when for _, when in combination))
            if guard != FALSE:
                value = tuple(value for value, _ in combination)
                result[value] = _combine("or", (result.get(value, FALSE), guard))
        return list(result.items()), any(incomplete for _, incomplete in children)
    if expression.op != "if":
        return [], True
    condition, yes, no = expression.args
    result = {}
    incomplete = False
    for branch, guard in ((yes, condition), (no, _not(condition))):
        choices, unknown = _choices(branch)
        incomplete |= unknown
        for value, when in choices:
            result[value] = _combine("or", (result.get(value, FALSE), _combine("and", (guard, when))))
    return list(result.items()), incomplete


def _action_choices(action, public, evaluation):
    if isinstance(action, Call):
        return [(action, TRUE)], False, []
    if action.kind == "call":
        return [(action.call, TRUE)], False, []
    if action.kind == "error":
        return [], False, [action.message]
    options, incomplete = [], False
    for name, expression in action.expressions:
        try:
            choices, unknown = _choices(_specialize(expression, public))
        except SystemError as error:
            return [], False, [str(error)]
        incomplete |= unknown
        options.append((name, expression.kind, choices))
    results, errors = [], []
    for combination in product(*(choices for _, _, choices in options)):
        guard = _combine("and", (condition for _, condition in combination))
        if guard == FALSE:
            continue
        expressions = tuple((name, Expression("literal", kind, literal=value))
                            for (name, kind, _), (value, _) in zip(options, combination, strict=True))
        try:
            call = resolve_action(replace(action, expressions=expressions), evaluation)
        except ValueError as error:
            errors.append(str(error))
        else:
            results.append((call, guard))
    return results, incomplete, errors


def _senders(system, public, evaluation):
    result = {str(call): [] for call in ALL_CALLS}
    higher, issues = [], []
    rules = sorted(system.rules, key=lambda rule: (-rule.priority, rule.id))
    groups = [(priority, [(rule.id, rule.when, rule.call) for rule in rows])
              for priority, rows in groupby(rules, key=lambda rule: rule.priority)]
    groups.append((None, [("fallback", TRUE, system.fallback)]))
    for _, rows in groups:
        group_conditions = []
        higher_conditions = [condition for condition, _ in higher]
        exclusions = [_condition(item) for item in higher_conditions]
        for identifier, original, action in rows:
            condition = _specialize(original, public)
            if condition == FALSE:
                continue
            group_conditions.append((condition, ()))
            effective = _combine("and", (condition, _not(_combine("or", higher_conditions))))
            if effective == FALSE:
                continue
            calls, incomplete, errors = _action_choices(action, public, evaluation)
            if not incomplete and not errors:
                group_conditions[-1] = (condition, tuple(sorted({
                    str(call) for call, guard in calls
                    if _combine("and", (effective, guard)) != FALSE
                })))
            if incomplete or errors:
                issues.append({"condition": _condition(effective)[0],
                               "message": "动态叫品尚不能完整展开" if incomplete else "该手牌分支未实现或没有可执行叫品",
                               "details": errors, "source_key": "fallback" if identifier == "fallback" else "rule:" + identifier})
            for call, guard in calls:
                when = _combine("and", (condition, guard))
                if _combine("and", (effective, guard)) == FALSE:
                    continue
                text, readable = _condition(when)
                result[str(call)].append({
                    "condition": text, "readable": readable and all(ok for _, ok in exclusions),
                    "exclusions": [text for text, _ in exclusions],
                    "exclusion_details": [
                        {"condition": text, "calls": list(choices)}
                        for (text, _), (_, choices) in zip(exclusions, higher, strict=True)
                    ],
                    "source_key": "fallback" if identifier == "fallback" else "rule:" + identifier,
                    "fingerprint": canonical_hash({"when": when.to_data(), "excluding": [item.to_data() for item in higher_conditions]}),
                    "fallback": identifier == "fallback",
                })
        higher.extend(group_conditions)
    return result, issues


def review_request(request):
    fields = object_fields(request, set(), {"history", "dealer", "vulnerability", "call", "turn"})
    history = parse_prefix(fields.get("history", []))
    call = fields.get("call")
    if "turn" in fields:
        turn = fields["turn"]
        if call is not None or type(turn) is not int or not 1 <= turn <= len(history):
            raise SystemError("请选择已有叫牌中的一步，不能同时指定下一叫。")
        history, call = history[:turn - 1], str(history[turn - 1])
    dealer = Seat(fields.get("dealer", "N"))
    vulnerability = Vulnerability(fields.get("vulnerability", "none"))
    auction = replay(dealer, history)
    return dealer, vulnerability, history, parse_call(call) if call is not None else None, auction


def _after_call(world, call):
    after = copy.copy(world)
    after.events, after.frames = list(world.events), dict(world.frames)
    after.step(call)
    return after


def review_position(system, request, *, definitions=None, side=None):
    dealer, vulnerability, history, selected, auction = review_request(request)
    actor = auction.next_seat
    side = side or actor.partnership.value
    if side not in ("NS", "EW"):
        raise SystemError("体系使用方必须是NS或EW。")
    definitions = dict(definitions or {})
    if side in definitions and definitions[side].content_hash != system.content_hash:
        raise SystemError("审查体系与该方公开体系不一致。")
    definitions[side] = system
    acting = definitions.get(actor.partnership.value)
    world = PublicWorld(definitions, dealer, vulnerability)
    for previous in history:
        world.step(previous)
    contexts = {partnership: _public_snapshot(PublicContext(world, partnership, Seat.NORTH if partnership == "NS" else Seat.EAST))
                for partnership in definitions}
    senders, issues, unavailable = {}, [], []
    if acting is not None and not auction.ended:
        context = PublicContext(world, actor.partnership.value, actor)
        evaluation = Evaluation(PublicView(actor, dealer, vulnerability, history), context.frame.state, context)
        public = PublicFilter(context.frame.state, evaluation)
        senders, issues = _senders(acting, public, evaluation)
        unavailable = sorted(public.unavailable)
    legal = {str(call) for call in legal_calls(auction)}
    choices = []
    for call in ALL_CALLS:
        text = str(call)
        branches = senders.get(text, [])
        status = ("illegal" if text not in legal else "missing" if acting is None else
                  "defined" if branches and all(item["readable"] for item in branches) else
                  "missing" if branches or issues else "unused")
        reason = ("叫牌已经结束" if auction.ended else "桥规不允许此叫品") if status == "illegal" else (
            "未提供当前行动方的体系，只能按桥规探索" if acting is None else
            "本阶段没有发送此叫品的规则" if status == "unused" else
            "存在未展开、未实现或不可执行的手牌分支" if status == "missing" else
            f"有{len(branches)}个可能的选牌分支，未读取任何私有手牌")
        if text in legal and acting is not None:
            try:
                _after_call(world, call)
            except SystemError as error:
                status, reason = "missing", f"桥规合法，但公开协议无法解释或履行这一叫：{error}"
        choices.append({"call": text, "status": status, "reason": reason, "branches": len(branches)})
    result = {
        "schema": "bridge-protocol-review/v1", "dealer": dealer.value, "vulnerability": vulnerability.value,
        "history": [str(call) for call in history], "actor": actor.value, "side": side,
        "ended": auction.ended, "choices": choices, "issues": issues, "unavailable_definitions": unavailable,
        "partitions": [{"call": call, "conditions": [item["condition"] for item in branches],
                        "priority_exclusions": [item["exclusions"] for item in branches],
                        "fingerprints": [item["fingerprint"] for item in branches]}
                       for call, branches in senders.items() if branches],
        "acting_system": {"hash": acting.content_hash, "name": acting.name} if acting else None,
        "public_contexts": contexts,
        "stage": "叫牌已经结束" if auction.ended else
                 f"{SEAT_NAMES[actor.value]}行动；此前共{len(history)}次叫牌",
        "scope": "按公开进程列举可能的发送条件，不选择具体手牌，不证明自然可达，不产生评测或发布。",
    }
    if selected is not None:
        if str(selected) not in legal:
            raise SystemError("选中的叫品在当前拍卖中不合法；请退回该叫发生之前查看。")
        choice = next(item for item in choices if item["call"] == str(selected))
        detail = {**choice, "senders": senders.get(str(selected), []), "public_meaning": None,
                  "public_error": None, "next_history": [str(call) for call in (*history, selected)]}
        if acting is not None:
            try:
                lookup = auction_lookup(acting, {"view": "auction", "dealer": dealer.value,
                    "vulnerability": vulnerability.value, "history": result["history"], "call": str(selected)},
                    definitions=definitions)
            except SystemError as error:
                detail.update(status="missing", public_error=f"公开协议无法解释或履行这一叫：{error}")
            else:
                detail["public_meaning"] = lookup["public_meaning"]
                after = _after_call(world, selected)
                detail["public_contexts_after"] = {
                    partnership: _public_snapshot(PublicContext(after, partnership, Seat.NORTH if partnership == "NS" else Seat.EAST))
                    for partnership in definitions}
                detail["ends_auction"] = after.auction.ended
        result["selected"] = detail
    return result
