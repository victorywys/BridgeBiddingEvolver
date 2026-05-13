# Bridge Bidding Self-Evolving Learning System

A research prototype for **discovering new bidding systems by evolution**, not
implementing existing ones. An LLM, acting as the proposer through a tightly
scoped disk protocol, iteratively rewrites a folder of YAML + Markdown
documents to maximise an objective fitness function (IMPs vs. a fixed
baseline, scored with the open-source Double Dummy Solver).

The bidding system itself — a folder of YAML conventions + Markdown
principles — is the learned artifact. A fresh agent reading only that folder
can immediately bid coherently with a partner.

## Two seeds and one baseline

```
systems/
  primitives_v0/    blank slate: 1 placeholder opening, 0 conventions.
                    Designed so the agent must INVENT the system from scratch.
                    Natural-bid bias has been stripped from the matcher.
  baseline_sayc/    frozen SAYC-like reference. Used only as the *opponent* in
                    IMP matches; never mutated.
  seed_v0/          legacy hand-coded seed retained for regression tests.
```

## The evolution loop

```
                  ┌─────────────────────────────────────────────────────┐
                  │   repo/versions/<HEAD>/  ← current bidding system   │
                  └─────────▲───────────────────────────────────▲───────┘
       benchmark (DDS+IMPs) │                                   │ snapshot
                            │                                   │
   ┌────────────────────────┴────────────┐         ┌────────────┴────────────┐
   │  evaluator                          │         │  evaluator              │
   │  `session open` →                   │         │  `session evaluate` →   │
   │  writes report.md (top-K failure    │         │  validates patch.yaml,  │
   │  boards, hands, DDS, auctions)      │         │  scores ΔIMPs, accepts  │
   └─────────┬───────────────────────────┘         └────────────▲────────────┘
             │ report.md (in repo)                              │ patch.yaml
             │                                                  │
             └────────────────── PROPOSER (LLM) ─────────────────┘
                       reads report, writes patch.yaml
```

Each iteration is a *pure function of the disk state* — there is no hidden
chat memory: anyone with the repo can replay the experiment.

## Layers

| Layer | Module | Responsibility |
|-------|--------|----------------|
| 1. Environment      | `bridge_evolve.env` | deck, deals, legal auctions, **DDS-backed scoring**, IMP table, par |
| 2. Agent            | `bridge_evolve.agent` | stateless bidder driven by external system docs |
| 3. DSL              | `bridge_evolve.dsl` | parse / match / mutate conventions; validators (readability, consistency, invertibility) |
| 4. Reflection       | `bridge_evolve.reflection` | failure analysis, heuristic patch proposals |
| 5. Versioning       | `bridge_evolve.versioning` | immutable system snapshots + diffs |
| 6. Benchmark        | `bridge_evolve.benchmark` | per-board scoring + **IMP-match runner** (teams-of-four) |
| 7. Experiments      | `bridge_evolve.experiments` | self-play, transfer, **session protocol** |

## Quick start

```bash
pip install -r requirements.txt
pip install endplay        # DDS solver (used automatically when available)

# Optional but recommended: precompute DDS for the eval deals
python -m bridge_evolve.cli precompute-dds --start 20260101 --count 400

# Inspect the seeds
python -m bridge_evolve.cli inspect --system systems/primitives_v0
python -m bridge_evolve.cli inspect --system systems/baseline_sayc

# Open a fresh evolution session against HEAD
python -m bridge_evolve.cli session open \
    --repo repos/primitives \
    --init-from systems/primitives_v0 \
    --boards 200 --baseline systems/baseline_sayc

# … the LLM proposer reads repos/primitives/sessions/s0001/report.md
# … and writes repos/primitives/sessions/s0001/patch.yaml

python -m bridge_evolve.cli session evaluate \
    --repo repos/primitives --id s0001

# A direct IMP match between two systems
python -m bridge_evolve.cli match \
    --a repos/primitives/versions/v1 \
    --b systems/baseline_sayc \
    --boards 200

# Legacy heuristic-only evolution (no LLM in the loop)
python -m bridge_evolve.cli evolve --system systems/seed_v0 --iterations 5

# The teacher/student transfer experiment
python -m bridge_evolve.cli transfer --teacher systems/seed_v0 --deals 200
```

## Research constraints honoured

1. **No fine-tuning.** Agents are stateless rule-followers over external docs.
2. **Externalized knowledge only.** Everything learned lives in the
   `systems/<id>/<version>/` folder. Nothing persists in memory between runs.
3. **Isolated transfer.** A fresh agent with no history can replay any
   accepted version from its document tree alone.
4. **Symbol-space exploration.** The matcher contains zero natural-bid bias:
   every behaviour (including "open 5-card majors" or "1NT = 15-17 balanced")
   must appear as an explicit, validated rule in the repo. The agent is free
   to invent artificial bid meanings as long as the DSL validators
   (readability + symbol consistency + invertibility) accept them.
5. **Objective fitness.** DDS-scored IMPs vs. `baseline_sayc` on a fixed deal
   set. A patch is accepted iff it improves the average IMP differential by
   at least the configured target delta.

See [docs/DSL.md](docs/DSL.md) for the convention language reference and
[docs/EXPERIMENT.md](docs/EXPERIMENT.md) for the transfer protocol.

