# Bridge Bidding DSL Reference

Every "learned" piece of the bidding system is one of three structured
artifacts. The matcher is the *only* interpreter of these structures, so
behaviour is fully determined by what the documents say.

## 1. Opening rules — `opening_table.yaml`

```yaml
OpeningTable:
  - OpeningRule:
      Name: Open1NT
      Priority: 100              # higher wins (sorted descending)
      Description: 15-17 HCP, balanced.
      Requirements:
        HCP: "15..17"           # range
        Balanced: true
      Action:
        Bid: "1NT"
```

Opening rules only fire if every previous call was `Pass`.

## 2. Conventions — `conventions/<name>.yaml`

```yaml
Convention:
  Name: Stayman
  Description: Ask 1NT opener for a 4-card major.
  Priority: 90
  Trigger:
    AuctionPattern: ["1NT"]      # tokens, "*" = wildcard
    Seat: Responder              # Opener | Responder | Intervenor | Advancer | Any
  Requirements:
    HCP: ">=8"
    FourCardMajor: true
  Meaning:
    AsksForFourCardMajor: true
    Forcing: 1                    # 0 = NF, 1 = one round, 2 = game forcing
  Action:
    Bid: "2C"
  Followups:
    - Pattern: ["2D"]
      Meaning: {DeniesFourCardMajor: true}
    - Pattern: ["2H"]
      Meaning: {ShowsFourCardHearts: true}
  Tags: ["MajorFitFinder"]
```

### Auction pattern matching

`AuctionPattern` is anchored to the **end** of the auction. `"*"` is a
wildcard for a single token.

* `["1NT"]` matches any history ending in `1NT`.
* `["1NT", "*", "2C"]` matches `1NT P 2C`, `1NT X 2C`, etc.

### Requirements vocabulary

| Key             | Type    | Example      | Meaning                          |
|-----------------|---------|--------------|----------------------------------|
| `HCP`           | range   | `"15..17"`   | high-card points                 |
| `Spades` / `Hearts` / `Diamonds` / `Clubs` | range | `">=5"` | suit length         |
| `Balanced`      | bool    | `true`       | 4-3-3-3 / 4-4-3-2 / 5-3-3-2     |
| `SemiBalanced`  | bool    | `true`       | balanced or 5-4-2-2 / 6-3-2-2    |
| `FourCardMajor` | bool    | `true`       | 4+ cards in hearts **or** spades |
| `FiveCardMajor` | bool    | `true`       | 5+ cards in hearts **or** spades |
| `Has6CardSuit`  | bool    | `true`       | any suit of length ≥ 6           |
| `Void`          | bool    | `true`       | at least one void                |
| `Singleton`     | bool    | `true`       | at least one singleton           |
| `Shape`         | string  | `"5-3-3-2"`  | exact shape                      |
| `LongestSuit`   | string  | `"H"`        | longest suit symbol              |

Numeric comparison strings accept `>=`, `<=`, `>`, `<`, `=`, ranges
`"lo..hi"` or `"lo-hi"`.

### Meaning fields

`Meaning` is free-form structured data the matcher attaches to the bid
and exposes via :func:`interpret_partner_bid` so the partner can
recover it. Recommended keys:

* `AsksForFourCardMajor`, `TransfersToHearts`, `TransfersToSpades`
* `Forcing`: 0 / 1 / 2
* `LimitRaise`, `SimpleRaise`, `Invitational`
* `Signoff`: a bid token
* `Promises*`, `Denies*`

## 3. Principles — `principles.md`

Free-form Markdown. `## Heading` blocks become individual
:class:`Principle` objects. Principles are advisory — they are surfaced
to reflection / human reviewers, but the matcher does not consult them.

## Convention priority

When multiple conventions could fire, the one with the **highest
`Priority`** wins. Ties break by `Name` (alphabetical). Use 90+ for
gadgets, 70-89 for natural responses, 50-69 for fallbacks.

## Patches

Patches are first-class. See `patches/*.yaml` inside each version.

```yaml
Patch:
  Name: add_stayman
  Rationale: Missed 4-4 major fits over 1NT.
  ParentVersion: v0
  Diagnoses: [WRONG_STRAIN]
  Operations:
    - Op: add_convention
      Target: Stayman
      Payload: { ... full convention payload ... }
```

Operations: `add_convention`, `remove_convention`, `modify_convention`,
`add_opening`, `remove_opening`, `add_principle`, `remove_principle`.
