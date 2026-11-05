import unittest

from bridge_evolve.core.cards import Card, Deal, Hand, full_deck
from bridge_evolve.core.types import Seat


class CardHandDealTests(unittest.TestCase):
    def test_card_rejects_invalid_rank_and_suit(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid rank"):
            Card(rank="1", suit="S")
        with self.assertRaisesRegex(ValueError, "invalid suit"):
            Card(rank="A", suit="N")

    def test_hand_requires_exactly_thirteen_unique_cards(self) -> None:
        deck = full_deck()
        with self.assertRaisesRegex(ValueError, "13 cards"):
            Hand(cards=deck[:12])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            Hand(cards=deck[:12] + (deck[0],))

    def test_deal_requires_fifty_two_unique_cards(self) -> None:
        deck = full_deck()
        north = Hand(cards=deck[0:13])
        east = Hand(cards=deck[13:26])
        south = Hand(cards=deck[26:39])
        west = Hand(cards=deck[39:52])
        deal = Deal(north=north, east=east, south=south, west=west)
        self.assertEqual(deal.hand(Seat.NORTH), north)
        self.assertEqual(deal.hand(Seat.EAST), east)
        self.assertEqual(deal.hand(Seat.SOUTH), south)
        self.assertEqual(deal.hand(Seat.WEST), west)

        with self.assertRaisesRegex(ValueError, "duplicate cards"):
            Deal(north=north, east=east, south=south, west=north)


if __name__ == "__main__":
    unittest.main()

