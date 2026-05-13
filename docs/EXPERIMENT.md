# Transfer Experiment Protocol

The transfer experiment is the empirical test of the project's core
hypothesis:

> Can knowledge of a bidding system be transmitted from one agent to
> another **purely** through external documents — no shared memory, no
> conversational context, no parameters in common?

## Setup

1. **Teacher** — an evolved bidding system at some version `v_t`. Its
   documents live in `repos/<id>/versions/v_t/`.
2. **Student** — a fresh process loads the same document tree from
   disk. *No* in-memory state from the teacher is reused.

## Run

```bash
python -m bridge_evolve.cli transfer --teacher repos/seed_v0/versions/v3 --deals 200
```

To simulate the "send the documents across the network" scenario, copy
the version folder to a different path:

```bash
cp -r repos/seed_v0/versions/v3 /tmp/student_docs
python -m bridge_evolve.cli transfer \
    --teacher repos/seed_v0/versions/v3 \
    --student /tmp/student_docs
```

## Success criteria

| Metric              | Threshold              |
|---------------------|------------------------|
| Auction consistency | `>= 0.99`              |
| Relative `avg_score` gap | `<= tolerance` (default `0.05`) |
| Forced-pass illegal bids by student | `0`           |

`consistency` is the fraction of deals for which the student's auction
is **token-for-token identical** to the teacher's. Because both agents
are stateless and the system documents are the only input, this number
should be `1.0` when the implementation is correct.

## Failure modes the experiment surfaces

* A convention that secretly relies on hidden state will break
  consistency on deals where the auction order differs.
* A bid resolution that depends on Python identity (e.g. caching) will
  similarly diverge across the two fresh instances.
* Required hand features that are only ever computed by the teacher's
  custom code path (and not by the matcher) will cause the student's
  rule set to fall through to defaults.

A run that prints `"success": true` is a positive instance of
externalized, transferable bidding knowledge.
