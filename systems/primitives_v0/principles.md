# Principles (primitives_v0)

These notes describe **constraints**, not conventions. They are
advisory to the reflection layer; the matcher does not read them.

## SymbolicCommunication

A bid carries only the meaning the system documents assign to it.
Conventions, opening rules, and follow-ups together form a *codebook*
mapping (auction context, bid token) -> meaning. Both partners decode
the auction by reading the same codebook.

## ExploreTheSymbolSpace

Bids 1C through 7NT are 35 abstract symbols. Natural meanings (1H shows
hearts, 1NT shows a balanced 15-17, etc.) are **defaults of human bridge
systems**, not laws. Strong-club, Polish-club, multi-bids, transfers,
and relay systems prove the same symbols can carry very different
meanings successfully. Evolution should explore this space.

## InvertibilityIsLaw

If you cannot decode partner's bid from the documented system, the bid
is useless. New conventions are rejected by the validator if
`interpret_partner_bid` cannot recover their meaning from the
documents.

## FitnessIsIMPsVsBaseline

Patches are accepted if and only if they improve average IMPs against a
fixed baseline system over a held-out deal set with precomputed DDS
results. Heuristic plausibility is *not* a substitute.
