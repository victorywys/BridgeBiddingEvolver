from bridge_evolve.env.deal import deal_random, hand_features


def test_deal_completeness():
    deal = deal_random(seed=42)
    cards = sum((list(deal.hand(s).cards) for s in "NESW"), [])
    assert len(cards) == 52
    assert len(set(cards)) == 52


def test_hand_features_basic():
    deal = deal_random(seed=42)
    f = hand_features(deal.hand("N"))
    assert sum(f.length.values()) == 13
    assert 0 <= f.hcp <= 40
