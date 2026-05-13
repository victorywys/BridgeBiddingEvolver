from bridge_evolve.env.auction import Auction, BidType, parse_bid


def test_pass_out_terminates():
    a = Auction()
    for _ in range(4):
        a.apply(parse_bid("P"))
    assert a.is_complete()
    assert a.last_contract_bid() is None


def test_three_passes_after_bid():
    a = Auction()
    a.apply(parse_bid("1NT"))  # N
    a.apply(parse_bid("P"))    # E
    a.apply(parse_bid("P"))    # S
    a.apply(parse_bid("P"))    # W
    assert a.is_complete()
    bid, seat = a.last_contract_bid()
    assert str(bid) == "1NT"
    assert seat == "N"


def test_legal_bids_increase():
    a = Auction()
    a.apply(parse_bid("1NT"))
    legal = {str(c) for c in a.legal_calls()}
    assert "1NT" not in legal
    assert "2C" in legal and "P" in legal and "X" in legal


def test_parse_and_format_roundtrip():
    for tok in ("P", "X", "XX", "1C", "1NT", "7NT"):
        call = parse_bid(tok)
        if call.type == BidType.BID:
            assert str(call.bid) == tok
        else:
            assert str(call) == tok or (tok == "DBL" and str(call) == "X")
