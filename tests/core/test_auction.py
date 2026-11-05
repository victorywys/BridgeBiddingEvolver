import random
import unittest

from bridge_evolve.core.auction import (
    ALL_BID_CALLS,
    Call,
    CallKind,
    Contract,
    PassedOut,
    apply_call,
    final_contract,
    initial_auction,
    legal_calls,
)
from bridge_evolve.core.types import Seat, Strain


class AuctionLegalityTests(unittest.TestCase):
    def test_opening_actions_include_pass_and_all_35_bids(self) -> None:
        state = initial_auction(dealer=Seat.NORTH)
        actions = legal_calls(state)
        self.assertEqual(actions[0], Call.pass_call())
        self.assertEqual(len([call for call in actions if call.kind == CallKind.BID]), 35)
        self.assertNotIn(Call.double(), actions)
        self.assertNotIn(Call.redouble(), actions)
        self.assertEqual(ALL_BID_CALLS[0], Call.bid(1, Strain.CLUBS))
        self.assertEqual(ALL_BID_CALLS[-1], Call.bid(7, Strain.NOTRUMP))

    def test_initial_four_passes_end_in_passed_out(self) -> None:
        state = initial_auction(dealer=Seat.NORTH)
        for _ in range(4):
            state = apply_call(state, Call.pass_call())
        self.assertTrue(state.ended)
        self.assertIsInstance(final_contract(state), PassedOut)
        self.assertEqual(legal_calls(state), ())

    def test_non_pass_requires_only_three_passes_to_end(self) -> None:
        state = initial_auction(dealer=Seat.NORTH)
        state = apply_call(state, Call.bid(1, Strain.HEARTS))
        state = apply_call(state, Call.pass_call())
        state = apply_call(state, Call.pass_call())
        self.assertFalse(state.ended)
        state = apply_call(state, Call.pass_call())
        self.assertTrue(state.ended)

    def test_double_and_redouble_legality_with_passes(self) -> None:
        state = initial_auction(dealer=Seat.NORTH)
        state = apply_call(state, Call.bid(1, Strain.SPADES))  # N

        east_actions = legal_calls(state)
        self.assertIn(Call.double(), east_actions)
        state = apply_call(state, Call.double())  # E

        south_actions = legal_calls(state)
        self.assertIn(Call.redouble(), south_actions)
        self.assertNotIn(Call.double(), south_actions)
        state = apply_call(state, Call.redouble())  # S

        west_actions = legal_calls(state)
        self.assertNotIn(Call.double(), west_actions)
        self.assertNotIn(Call.redouble(), west_actions)

    def test_new_bid_resets_doubling(self) -> None:
        state = initial_auction(dealer=Seat.NORTH)
        state = apply_call(state, Call.bid(1, Strain.DIAMONDS))  # N
        state = apply_call(state, Call.double())  # E
        state = apply_call(state, Call.bid(2, Strain.DIAMONDS))  # S
        self.assertEqual(state.doubling, 1)

    def test_cannot_double_partner_contract(self) -> None:
        state = initial_auction(dealer=Seat.NORTH)
        state = apply_call(state, Call.bid(1, Strain.HEARTS))  # N
        state = apply_call(state, Call.pass_call())  # E
        south_actions = legal_calls(state)
        self.assertNotIn(Call.double(), south_actions)

    def test_7nt_boundary_allows_only_pass_and_no_higher_bid(self) -> None:
        state = initial_auction(dealer=Seat.NORTH)
        state = apply_call(state, Call.bid(7, Strain.NOTRUMP))  # N
        actions = legal_calls(state)
        self.assertIn(Call.pass_call(), actions)
        self.assertIn(Call.double(), actions)
        self.assertNotIn(Call.bid(7, Strain.NOTRUMP), actions)
        self.assertEqual(len([call for call in actions if call.kind == CallKind.BID]), 0)

    def test_invalid_transition_fails_explicitly(self) -> None:
        state = initial_auction(dealer=Seat.NORTH)
        with self.assertRaisesRegex(ValueError, "illegal"):
            apply_call(state, Call.double())

    def test_final_contract_and_declarer_match_reference_example(self) -> None:
        state = initial_auction(dealer=Seat.NORTH)
        sequence = (
            Call.bid(1, Strain.CLUBS),
            Call.pass_call(),
            Call.bid(1, Strain.HEARTS),
            Call.pass_call(),
            Call.bid(4, Strain.HEARTS),
            Call.pass_call(),
            Call.pass_call(),
            Call.pass_call(),
        )
        for call in sequence:
            state = apply_call(state, call)
        contract = final_contract(state)
        self.assertIsInstance(contract, Contract)
        self.assertEqual(contract.level, 4)
        self.assertEqual(contract.strain, Strain.HEARTS)
        self.assertEqual(contract.declarer, Seat.SOUTH)
        self.assertEqual(contract.doubling, 1)

    def test_random_legal_walk_terminates_and_stays_legal(self) -> None:
        random_engine = random.Random(20261023)
        state = initial_auction(dealer=Seat.WEST)
        iterations = 0
        while not state.ended:
            actions = legal_calls(state)
            self.assertGreater(len(actions), 0)
            call = random_engine.choice(actions)
            state = apply_call(state, call)
            iterations += 1
            self.assertLess(iterations, 10_000)
        self.assertTrue(state.ended)


if __name__ == "__main__":
    unittest.main()

