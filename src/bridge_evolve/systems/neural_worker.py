"""Isolated optional-dependency worker; stdin never contains a complete deal or DDS."""

import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--family", choices=("brl", "pidgin"), required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source))
    sys.path.insert(0, str(args.source / "server"))
    import numpy as np
    import torch
    from training.bridge.auction import AuctionState
    from training.bridge.calls import ACTION_NAMES, NAME_TO_ACTION, PASS, DOUBLE, REDOUBLE

    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    if args.family == "brl":
        from emergent.brl_player import BrlNet, encode, _PGX_TO_OURS
        net = BrlNet(args.weights).eval()
        settings = {"action_order": "pgx", "selection": "greedy", "search": False}
    else:
        from training.fourseat.model import load_fourseat_checkpoint, policy_log_probs
        from training.fourseat.state import features_from_history
        from training.fourseat.competitive import competitive_features, apply_opening_rule
        net, checkpoint = load_fourseat_checkpoint(args.weights, "cpu")
        net.eval()
        settings = {key: checkpoint.get(key) for key in ("stage", "step", "any_seat_double", "opening_rule")}
        settings.update(selection="greedy", search=False)
    runtime = {"python": sys.version.split()[0], "numpy": np.__version__, "torch": torch.__version__,
               "device": "cpu", "threads": 1, "family": args.family, "settings": settings}
    print(json.dumps({"ok": True, "runtime": runtime}), flush=True)

    for line in sys.stdin:
        try:
            request = json.loads(line)
            required = {"hand", "seat", "dealer", "vulnerability", "history", "legal_calls"}
            if not isinstance(request, dict) or set(request) != required:
                raise ValueError("Only player-visible observation fields are accepted.")
            dealer = "NESW".index(request["dealer"])
            vul_ns, vul_ew = {"none": (False, False), "ns": (True, False),
                              "ew": (False, True), "both": (True, True)}[request["vulnerability"]]
            calls = [NAME_TO_ACTION[call] for call in request["history"]]
            state = AuctionState.from_calls(calls, dealer=dealer, vul_ns=vul_ns, vul_ew=vul_ew)
            if state.ended or request["seat"] != "NESW"[state.turn]:
                raise ValueError("Observation does not describe the acting player.")
            legal = state.legal_mask()
            if set(request["legal_calls"]) != {ACTION_NAMES[i] for i in np.flatnonzero(legal)}:
                raise ValueError("Upstream and host disagree about bridge legality.")
            parts = request["hand"].split(".")
            if len(parts) != 4:
                raise ValueError("Hand must use S.H.D.C notation.")
            cards = [(suit * 13 + "AKQJT98765432".index(rank))
                     for suit, ranks in enumerate(parts) for rank in ranks if rank != "-"]
            if len(cards) != 13 or len(set(cards)) != 13:
                raise ValueError("The acting hand must contain 13 distinct cards.")
            hand = torch.zeros(1, 52)
            hand[0, cards] = 1
            history = torch.tensor([calls], dtype=torch.long)
            dealers, actors = torch.tensor([dealer]), torch.tensor([state.turn])
            ns, ew = torch.tensor([vul_ns]), torch.tensor([vul_ew])
            mask = torch.tensor(legal)[None]
            with torch.inference_mode():
                if args.family == "brl":
                    observation = encode(hand, history, dealers, ns, ew, actors)
                    logits_pgx = net(observation)[0]
                    # Preserve the official PGX tie-break order: P, X, XX, then bids.
                    log_probs = torch.log_softmax(logits_pgx.masked_fill(~mask[0, _PGX_TO_OURS], -torch.inf), -1)
                    action = int(_PGX_TO_OURS[log_probs.argmax()])
                    probs = torch.empty(38)
                    probs[_PGX_TO_OURS] = log_probs.exp()
                else:
                    doubles = net.n_actions > PASS + 1
                    competitive = net.stage == "D5OWN4XC"
                    feats = (competitive_features(history, dealers, ns, ew, actors) if competitive
                             else features_from_history(history, dealers, ns, ew, actors, doubles))
                    mask = mask[:, :net.n_actions]
                    if competitive and not checkpoint.get("any_seat_double", False):
                        mask[:, DOUBLE:] &= feats[:, 149:150] > 0
                    mask = apply_opening_rule(mask, hand, torch.tensor([state.last_contract]),
                                              len(calls), int(checkpoint.get("opening_rule", 0)))
                    log_probs = policy_log_probs(net(hand, feats), mask)[0]
                    action = int(log_probs.argmax())
                    probs = torch.zeros(38)
                    probs[:net.n_actions] = log_probs.exp()
                if not torch.isfinite(probs).all() or not legal[action] or not bool(probs.sum() > 0):
                    raise ValueError("Model returned nonfinite probabilities or an illegal action.")
            result = {"ok": True, "call": ACTION_NAMES[action], "model_family": args.family,
                      "policy": [float(value) for value in probs], "search": False}
        except (ValueError, KeyError, TypeError, IndexError, RuntimeError) as error:
            print(f"Neural baseline rejected observation: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
            result = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        print(json.dumps(result, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
