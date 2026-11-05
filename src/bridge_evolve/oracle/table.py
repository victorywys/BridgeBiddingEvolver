"""Immutable declarer-by-strain tables, independent of DDS array order."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from bridge_evolve.core.types import Seat, Strain

DECLARERS = (Seat.NORTH, Seat.EAST, Seat.SOUTH, Seat.WEST)
STRAINS = (
    Strain.CLUBS,
    Strain.DIAMONDS,
    Strain.HEARTS,
    Strain.SPADES,
    Strain.NOTRUMP,
)


@dataclass(frozen=True)
class DDTable:
    """Rows are N/E/S/W; columns are C/D/H/S/NT, always declarer's tricks."""

    values: tuple[tuple[int, ...], ...]
    declarers: ClassVar[tuple[Seat, ...]] = DECLARERS
    strains: ClassVar[tuple[Strain, ...]] = STRAINS

    def __post_init__(self) -> None:
        if not isinstance(self.values, tuple) or len(self.values) != 4:
            raise ValueError("DDTable requires four immutable declarer rows")
        for row in self.values:
            if not isinstance(row, tuple) or len(row) != 5:
                raise ValueError("DDTable requires five immutable strain entries per row")
            if any(type(value) is not int or not 0 <= value <= 13 for value in row):
                raise ValueError("DDTable tricks must be integers in 0..13")

    def tricks(self, declarer: Seat, strain: Strain) -> int:
        if not isinstance(declarer, Seat) or not isinstance(strain, Strain):
            raise TypeError("tricks requires a Seat and a Strain")
        return self.values[DECLARERS.index(declarer)][STRAINS.index(strain)]

    def to_dict(self) -> dict[str, dict[str, int]]:
        """Return JSON-safe explicitly named axes; never serialize DDS indices."""
        return {
            declarer.value: {
                strain.value: self.tricks(declarer, strain) for strain in STRAINS
            }
            for declarer in DECLARERS
        }

    def makeable_levels(self) -> dict[str, dict[str, int]]:
        return {seat: {strain: max(0, tricks - 6) for strain, tricks in row.items()}
                for seat, row in self.to_dict().items()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Mapping[str, int]]) -> DDTable:
        if not isinstance(data, Mapping) or set(data) != {seat.value for seat in DECLARERS}:
            raise ValueError("DDTable must have exactly the N/E/S/W declarer keys")
        rows = []
        for seat in DECLARERS:
            row = data[seat.value]
            if not isinstance(row, Mapping) or set(row) != {strain.value for strain in STRAINS}:
                raise ValueError("DDTable must have exactly the C/D/H/S/NT strain keys")
            rows.append(tuple(row[strain.value] for strain in STRAINS))
        return cls(tuple(rows))
