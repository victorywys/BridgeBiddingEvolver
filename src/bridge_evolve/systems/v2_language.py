"""Typed public context, parameterized protocols, and deterministic actions."""

from collections.abc import Callable

from bridge_evolve.systems.language import (
    Action, Announcement, Expression, Parameter, ProtocolState, Rule, SystemDefinition,
    SystemError, Transition, Value, _check_state_literals, _literal,
    compile_expression, integer, object_fields, parse_call, source_at, source_pointer, text, thaw,
)

FORCING_STATES = ("none", "one_round", "invite_game", "game", "slam_try", "small_slam", "invite_grand", "grand_slam")

CONTEXT_TYPES = {
    "state": "string",
    "dealer": "string", "vulnerability": "string", "own_vulnerable": "bool",
    "opponents_vulnerable": "bool", "partner_seat": "string",
    "opener": "string", "responder": "string", "overcaller": "string",
    "advancer": "string", "our_opener": "string", "our_responder": "string",
    "dynamic_role": "string", "opening_position": "number", "opening_strain": "string",
    "last_call": "string", "last_actor": "string", "last_bid_strain": "string",
    "last_bid_level": "number", "last_bid_actor": "string", "last_bid": "string",
    "our_bid_count": "number", "last_bid_by": "string",
    "partner_bid_strain": "string", "opponent_bid_strain": "string",
    "partner_shown_suit": "string", "opponent_shown_suit": "string",
    "unbid_suits": "list[string]", "passed_before_opening": "bool",
    "agreed_trump": "string", "forcing": "string", "must_bid": "bool",
    "goal": "string", "goal_level": "number", "goal_met": "bool",
    "partnership_hcp_min": "number", "partner_hcp_min": "number", "agreed_trump_min": "number",
    "partnership_hcp_max": "number", "partner_hcp_max": "number",
    "agreed_trump_max": "number",
    "partnership_C_min": "number", "partnership_D_min": "number", "partnership_H_min": "number", "partnership_S_min": "number",
    "partner_C_min": "number", "partner_D_min": "number", "partner_H_min": "number", "partner_S_min": "number",
    "partnership_C_max": "number", "partnership_D_max": "number", "partnership_H_max": "number", "partnership_S_max": "number",
    "partner_C_max": "number", "partner_D_max": "number", "partner_H_max": "number", "partner_S_max": "number",
    "double_kind": "string", "opponent_double_kind": "string",
    "event.actor": "string", "event.call": "string", "event.strain": "string",
    "event.level": "number", "event.kind": "string", "event.by": "string",
    "event.tags": "list[string]",
    "event.position": "number", "event.passed_before_opening": "bool",
}
PRIVATE_OPERATORS = {"hcp", "controls", "shape", "balanced", "length", "honors", "control", "has", "keycards"}
PRIVATE_CONTEXT = {
    "partner_seat", "dynamic_role", "partner_bid_strain",
    "partner_shown_suit", "passed_before_opening", "must_bid",
    "partner_hcp_min", "partner_hcp_max", "partner_C_min", "partner_D_min", "partner_H_min", "partner_S_min",
    "partner_C_max", "partner_D_max", "partner_H_max", "partner_S_max",
}


def compile_extension(
    value: dict, resolve: Callable[[str], Expression] | None, variables: dict[str, str],
) -> Expression | None:
    def compile_child(raw: object) -> Expression:
        return compile_expression(raw, resolve, version=2, variables=variables)

    op = value.get("op")
    if op == "if":
        fields = object_fields(value, {"op", "args"})
        if not isinstance(fields["args"], list) or len(fields["args"]) != 3:
            raise SystemError("if requires condition, true value, and false value.")
        args = tuple(compile_child(raw) for raw in fields["args"])
        if args[0].kind != "bool" or args[1].kind != args[2].kind:
            raise SystemError("if requires a boolean condition and same-typed branches.")
        return Expression("if", args[1].kind, args)
    if op == "list":
        fields = object_fields(value, {"op", "item_type", "args"})
        kind = text(fields["item_type"])
        if kind not in ("string", "number", "bool") or not isinstance(fields["args"], list):
            raise SystemError("list requires a scalar item_type and an args list.")
        args = tuple(compile_child(raw) for raw in fields["args"])
        if any(argument.kind != kind for argument in args):
            raise SystemError("List expression has mixed item types.")
        return Expression("list", f"list[{kind}]", args, (("item_type", kind),))
    if op in ("context", "var"):
        fields = object_fields(value, {"op", "name"})
        name = text(fields["name"])
        types = CONTEXT_TYPES if op == "context" else variables
        if name not in types:
            raise SystemError(f"Unknown {op} name: {name}")
        return Expression(op, types[name], parameters=(("name", name),))
    if op == "announced":
        fields = object_fields(value, {"op", "tag", "which"})
        which = text(fields["which"])
        if which not in ("opening", "last_bid", "partner", "opponents", "last_call", "partner_call", "opponent_call"):
            raise SystemError("Unknown announced event selector.")
        return Expression(op, "bool", parameters=(("tag", text(fields["tag"])), ("which", which)))
    if op == "keycards":
        fields = object_fields(value, {"op", "args"})
        if not isinstance(fields["args"], list) or len(fields["args"]) != 1:
            raise SystemError("keycards requires one trump-suit expression.")
        child = compile_child(fields["args"][0])
        if child.kind != "string":
            raise SystemError("keycards trump must be a string.")
        return Expression(op, "number", (child,))
    if op in ("length", "honors", "control", "is_legal") and any(
        isinstance(value.get(key), dict) for key in ("suit", "call")
    ):
        field = "call" if op == "is_legal" else "suit"
        replacement = "P" if field == "call" else "C"
        static = compile_expression(value | {field: replacement})
        child = compile_child(value[field])
        if child.kind != "string":
            raise SystemError(f"Dynamic {field} must have string type.")
        return Expression(
            static.op, static.kind,
            parameters=tuple((key, item) for key, item in static.parameters if key != field),
            bindings=((field, child),),
        )
    return None


def check_public(expression: Expression, *, announcement: bool = False) -> None:
    if expression.op in PRIVATE_OPERATORS or expression.op in ("seat", "role", "vulnerable", "previously_passed"):
        raise SystemError(f"Public protocol cannot access private/player-relative operator: {expression.op}")
    parameters = dict(expression.parameters)
    if expression.op == "context":
        name = parameters["name"]
        if name in PRIVATE_CONTEXT or (announcement and name == "event.tags"):
            raise SystemError(f"Context {name} is not available in this public phase.")
    if expression.op == "announced" and parameters["which"] in ("partner", "partner_call"):
        raise SystemError("Public transitions cannot use player-relative partner annotations.")
    for child in expression.args:
        check_public(child, announcement=announcement)
    for _, child in expression.bindings:
        check_public(child, announcement=announcement)


def check_decision(expression: Expression) -> None:
    if expression.op == "context" and str(dict(expression.parameters)["name"]).startswith("event."):
        raise SystemError("event.* is not available in decision rules/actions; bind a public parameter instead.")
    for child in expression.args:
        check_decision(child)
    for _, child in expression.bindings:
        check_decision(child)


def compile_action(value: object, compile_child: Callable[[object], Expression],
                   *, pointer: str | None = None) -> Action:
    if not isinstance(value, dict):
        raise SystemError("v2 actions must be objects.", pointer=pointer)
    kind = text(value.get("kind"), pointer=source_pointer(pointer, "kind"))
    if kind == "call":
        fields = object_fields(value, {"kind", "call"}, pointer=pointer)
        return Action(kind, call=parse_call(fields["call"], pointer=source_pointer(pointer, "call")))
    if kind == "error":
        fields = object_fields(value, {"kind", "message"}, pointer=pointer)
        return Action(kind, message=text(fields["message"], pointer=source_pointer(pointer, "message")))
    types = {
        "bid": {"level": "number", "strain": "string"},
        "cheapest": {"strains": "list[string]", "min_level": "number", "max_level": "number"},
        "step": {"after": "string", "count": "number"},
    }.get(kind)
    if types is None:
        raise SystemError(f"Unknown action kind: {kind}", pointer=source_pointer(pointer, "kind"))
    fields = object_fields(value, {"kind", *types}, pointer=pointer)
    expressions = []
    for name, expected in types.items():
        with source_at(source_pointer(pointer, name)):
            expression = compile_child(fields[name])
            if expression.kind != expected:
                raise SystemError(f"Action {kind}.{name} must have type {expected}.")
        expressions.append((name, expression))
    return Action(kind, expressions=tuple(expressions))


def validate_v2(value: object) -> SystemDefinition:
    fields = object_fields(value, {
        "schema_version", "operator_version", "name", "scope", "features",
        "protocol", "announcements", "rules", "fallback",
    }, pointer="")
    if (integer(fields["schema_version"], pointer="/schema_version") != 2
            or integer(fields["operator_version"], pointer="/operator_version") != 2):
        raise SystemError("v2 requires schema_version=operator_version=2.", pointer="/operator_version")
    scope = text(fields["scope"], pointer="/scope")
    if scope not in ("general", "uncontested-1nt"):
        raise SystemError("Unknown system scope.", pointer="/scope")
    protocol = object_fields(fields["protocol"], {"initial", "parameters", "states"}, pointer="/protocol")
    raw_parameters = protocol["parameters"]
    if not isinstance(raw_parameters, dict):
        raise SystemError("protocol.parameters must be an object.", pointer="/protocol/parameters")
    parameters = []
    for name, raw in raw_parameters.items():
        pointer = source_pointer("/protocol/parameters", name)
        name = text(name, pointer=pointer)
        parameter = object_fields(raw, {"type", "initial"}, {"values"}, pointer=pointer)
        kind = text(parameter["type"], pointer=source_pointer(pointer, "type"))
        with source_at(source_pointer(pointer, "initial")):
            initial, actual = _literal(parameter["initial"])
        if kind not in ("string", "number", "bool") or kind != actual:
            raise SystemError(f"Invalid parameter type/initial value: {name}", pointer=pointer)
        domain: tuple[Value, ...] = ()
        if "values" in parameter:
            raw_domain = parameter["values"]
            if not isinstance(raw_domain, list) or not raw_domain:
                raise SystemError("Parameter values must be a nonempty list.", pointer=source_pointer(pointer, "values"))
            literals = []
            for index, item in enumerate(raw_domain):
                with source_at(source_pointer(pointer, "values", index)):
                    literals.append(_literal(item))
            if any(item_type != kind for _, item_type in literals):
                raise SystemError(f"Parameter {name} domain has mixed types.", pointer=source_pointer(pointer, "values"))
            domain = tuple(item for item, _ in literals)
            if initial not in domain:
                raise SystemError(f"Parameter {name} initial value is outside its domain.",
                                  pointer=source_pointer(pointer, "initial"))
        if name == "trump":
            if kind != "string" or not domain or not set(domain) <= {"", "C", "D", "H", "S"}:
                raise SystemError("trump must have a string domain drawn from empty/C/D/H/S.", pointer=pointer)
        parameters.append(Parameter(name, kind, initial, domain))
    variable_types = {parameter.name: parameter.kind for parameter in parameters}
    features = fields["features"]
    if not isinstance(features, dict) or any(not isinstance(name, str) or not name for name in features):
        raise SystemError("features must map nonempty names to expressions.", pointer="/features")
    compiled: dict[str, Expression] = {}
    visiting: set[str] = set()

    def resolve(name: str) -> Expression:
        if name not in features or name in visiting:
            raise SystemError(f"Undefined or cyclic feature reference: {name}")
        if name not in compiled:
            visiting.add(name)
            with source_at(source_pointer("/features", name)):
                compiled[name] = compile_child(features[name])
            visiting.remove(name)
        return compiled[name]

    def compile_child(raw: object) -> Expression:
        return compile_expression(raw, resolve, version=2, variables=variable_types)

    for name in features:
        resolve(name)
    raw_states = protocol["states"]
    if not isinstance(raw_states, dict) or not raw_states:
        raise SystemError("protocol.states must be a nonempty object.", pointer="/protocol/states")
    states = []
    transition_ids = set()
    for name, raw in raw_states.items():
        pointer = source_pointer("/protocol/states", name)
        name = text(name, pointer=pointer)
        state = object_fields(raw, {"transitions", "otherwise"}, pointer=pointer)
        if not isinstance(state["transitions"], list):
            raise SystemError("State transitions must be a list.", pointer=source_pointer(pointer, "transitions"))
        transitions = []
        for index, raw_transition in enumerate(state["transitions"]):
            transition_pointer = source_pointer(pointer, "transitions", index)
            item = object_fields(
                raw_transition, {"id", "when", "to", "set"}, {"forcing", "allow_pass"},
                pointer=transition_pointer,
            )
            identifier = text(item["id"], pointer=source_pointer(transition_pointer, "id"))
            if identifier in transition_ids:
                raise SystemError(f"Duplicate transition id: {identifier}",
                                  pointer=source_pointer(transition_pointer, "id"))
            transition_ids.add(identifier)
            with source_at(source_pointer(transition_pointer, "when")):
                condition = compile_child(item["when"])
                check_public(condition)
                if condition.kind != "bool":
                    raise SystemError("Public transition condition must be boolean.")
            assignments = item["set"]
            if not isinstance(assignments, dict):
                raise SystemError("Transition set must be an object.", pointer=source_pointer(transition_pointer, "set"))
            updates = []
            for target, raw_expression in assignments.items():
                with source_at(source_pointer(transition_pointer, "set", target)):
                    if target not in variable_types:
                        raise SystemError(f"Assignment to undefined parameter: {target}")
                    expression = compile_child(raw_expression)
                    check_public(expression)
                    if expression.kind != variable_types[target]:
                        raise SystemError(f"Assignment type mismatch for {target}")
                updates.append((target, expression))
            forcing = None
            if "forcing" in item:
                forcing = text(item["forcing"], pointer=source_pointer(transition_pointer, "forcing"))
                if forcing not in FORCING_STATES:
                    raise SystemError(f"forcing must be one of {FORCING_STATES}.",
                                      pointer=source_pointer(transition_pointer, "forcing"))
            allow_pass = item.get("allow_pass", False)
            if type(allow_pass) is not bool:
                raise SystemError("allow_pass must be boolean.", pointer=source_pointer(transition_pointer, "allow_pass"))
            transitions.append(Transition(
                identifier, condition, text(item["to"], pointer=source_pointer(transition_pointer, "to")),
                tuple(updates), forcing, allow_pass,
            ))
        states.append(ProtocolState(name, (), text(state["otherwise"], pointer=source_pointer(pointer, "otherwise")),
                                    tuple(transitions)))
    names = {state.name for state in states}
    initial = text(protocol["initial"], pointer="/protocol/initial")
    if initial not in names:
        raise SystemError("Undefined initial state.", pointer="/protocol/initial")
    reachable = {initial}
    for state in states:
        pointer = source_pointer("/protocol/states", state.name)
        if state.otherwise not in names:
            raise SystemError(f"Undefined transition from {state.name}", pointer=source_pointer(pointer, "otherwise"))
        for index, transition in enumerate(state.transitions):
            if transition.target not in names:
                raise SystemError(f"Undefined transition from {state.name}",
                                  pointer=source_pointer(pointer, "transitions", index, "to"))
    while True:
        expanded = reachable | {
            target for state in states if state.name in reachable
            for target in [state.otherwise, *(transition.target for transition in state.transitions)]
        }
        if expanded == reachable:
            break
        reachable = expanded
    if reachable != names:
        raise SystemError(f"Unreachable states: {sorted(names - reachable)}", pointer="/protocol/states")
    declarations = fields["announcements"]
    if not isinstance(declarations, list):
        raise SystemError("announcements must be a list.", pointer="/announcements")
    announcements = []
    announcement_ids = set()
    for index, raw in enumerate(declarations):
        pointer = source_pointer("/announcements", index)
        item = object_fields(raw, {"id", "when", "tags"}, {"suit"}, pointer=pointer)
        identifier = text(item["id"], pointer=source_pointer(pointer, "id"))
        if identifier in announcement_ids:
            raise SystemError(f"Duplicate announcement id: {identifier}", pointer=source_pointer(pointer, "id"))
        announcement_ids.add(identifier)
        with source_at(source_pointer(pointer, "when")):
            condition = compile_child(item["when"])
            check_public(condition, announcement=True)
        if condition.kind != "bool" or not isinstance(item["tags"], list):
            raise SystemError("Announcement requires a boolean condition and a tag list.",
                              pointer=source_pointer(pointer, "when" if condition.kind != "bool" else "tags"))
        tags = tuple(text(tag, pointer=source_pointer(pointer, "tags", position))
                     for position, tag in enumerate(item["tags"]))
        if len(set(tags)) != len(tags):
            raise SystemError("Duplicate announcement tags.", pointer=source_pointer(pointer, "tags"))
        suit = None
        if "suit" in item:
            with source_at(source_pointer(pointer, "suit")):
                suit = compile_child(item["suit"])
                check_public(suit, announcement=True)
                if suit.kind != "string":
                    raise SystemError("Announced suit must be a string.")
        announcements.append(Announcement(identifier, condition, tags, suit))
    raw_rules = fields["rules"]
    if not isinstance(raw_rules, list):
        raise SystemError("rules must be a list.", pointer="/rules")
    rules = []
    ids = set()
    seen = set()
    for index, raw in enumerate(raw_rules):
        pointer = source_pointer("/rules", index)
        item = object_fields(raw, {"id", "priority", "when", "action"}, pointer=pointer)
        identifier = text(item["id"], pointer=source_pointer(pointer, "id"))
        if identifier == "fallback" or identifier in ids:
            raise SystemError(f"Duplicate or reserved rule id: {identifier}", pointer=source_pointer(pointer, "id"))
        ids.add(identifier)
        with source_at(source_pointer(pointer, "when")):
            condition = compile_child(item["when"])
            check_decision(condition)
            if condition.kind != "bool":
                raise SystemError("Rule condition must be boolean.")
        priority = integer(item["priority"], pointer=source_pointer(pointer, "priority"))
        if (priority, condition) in seen:
            raise SystemError(f"Identical conditions at the same priority: {identifier}",
                              pointer=source_pointer(pointer, "when"))
        seen.add((priority, condition))
        action = compile_action(item["action"], compile_child, pointer=source_pointer(pointer, "action"))
        for name, expression in action.expressions:
            with source_at(source_pointer(pointer, "action", name)):
                check_decision(expression)
        rules.append(Rule(identifier, priority, condition, action))
    fallback = compile_action(fields["fallback"], compile_child, pointer="/fallback")
    for name, expression in fallback.expressions:
        with source_at(source_pointer("/fallback", name)):
            check_decision(expression)
    expressions = [(expression, source_pointer("/features", name)) for name, expression in compiled.items()]
    expressions.extend((rule.when, source_pointer("/rules", index, "when")) for index, rule in enumerate(rules))
    for state in states:
        for index, transition in enumerate(state.transitions):
            pointer = source_pointer("/protocol/states", state.name, "transitions", index)
            expressions.append((transition.when, source_pointer(pointer, "when")))
            expressions.extend((value, source_pointer(pointer, "set", name)) for name, value in transition.assignments)
    for index, announcement in enumerate(announcements):
        expressions.append((announcement.when, source_pointer("/announcements", index, "when")))
        if announcement.suit is not None:
            expressions.append((announcement.suit, source_pointer("/announcements", index, "suit")))
    actions = [(fallback, "/fallback")] + [(rule.call, source_pointer("/rules", index, "action"))
                                         for index, rule in enumerate(rules)]
    for rule_action, pointer in actions:
        if isinstance(rule_action, Action):
            expressions.extend((expression, source_pointer(pointer, name)) for name, expression in rule_action.expressions)
    for expression, pointer in expressions:
        with source_at(pointer):
            _check_state_literals(expression, names)
    return SystemDefinition(
        text(fields["name"], pointer="/name"), scope, tuple((name, resolve(name)) for name in features),
        initial, tuple(states), tuple(rules), fallback, 2, tuple(parameters), tuple(announcements),
    )


def system_data(system: SystemDefinition) -> dict[str, object]:
    def action_data(action: Action | object) -> object:
        if not isinstance(action, Action):
            raise SystemError("A v2 system contains a non-v2 action.")
        return action.to_data()

    return {
        "schema_version": 2, "operator_version": 2, "name": system.name, "scope": system.scope,
        "features": {name: expression.to_data() for name, expression in system.features},
        "protocol": {
            "initial": system.initial,
            "parameters": {
                parameter.name: {
                    "type": parameter.kind, "initial": thaw(parameter.initial),
                    **({"values": [thaw(value) for value in parameter.domain]} if parameter.domain else {}),
                } for parameter in system.parameters
            },
            "states": {
                state.name: {
                    "transitions": [transition.to_data() for transition in state.transitions],
                    "otherwise": state.otherwise,
                } for state in system.states
            },
        },
        "announcements": [announcement.to_data() for announcement in system.announcements],
        "rules": [
            {"id": rule.id, "priority": rule.priority, "when": rule.when.to_data(), "action": action_data(rule.call)}
            for rule in system.rules
        ],
        "fallback": action_data(system.fallback),
    }
