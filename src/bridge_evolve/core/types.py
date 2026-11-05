"""Shared bridge enums used by the M1 core."""

from __future__ import annotations

from enum import Enum


class Partnership(str, Enum):
    NS = "NS"
    EW = "EW"


class Seat(str, Enum):
    NORTH = "N"
    EAST = "E"
    SOUTH = "S"
    WEST = "W"

    def next(self) -> "Seat":
        order = (Seat.NORTH, Seat.EAST, Seat.SOUTH, Seat.WEST)
        return order[(order.index(self) + 1) % 4]

    @property
    def partnership(self) -> Partnership:
        if self in (Seat.NORTH, Seat.SOUTH):
            return Partnership.NS
        return Partnership.EW


class Strain(str, Enum):
    CLUBS = "C"
    DIAMONDS = "D"
    HEARTS = "H"
    SPADES = "S"
    NOTRUMP = "NT"

    @property
    def rank(self) -> int:
        order = {
            Strain.CLUBS: 0,
            Strain.DIAMONDS: 1,
            Strain.HEARTS: 2,
            Strain.SPADES: 3,
            Strain.NOTRUMP: 4,
        }
        return order[self]


class Vulnerability(str, Enum):
    NONE = "none"
    NS = "ns"
    EW = "ew"
    BOTH = "both"

    def is_vulnerable(self, seat: Seat) -> bool:
        if self == Vulnerability.BOTH:
            return True
        if self == Vulnerability.NONE:
            return False
        if self == Vulnerability.NS:
            return seat.partnership == Partnership.NS
        return seat.partnership == Partnership.EW

