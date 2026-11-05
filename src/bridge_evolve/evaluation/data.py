"""Content-addressed full deals; dataset identities never enter observations."""

import hashlib
import json
import platform
import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from bridge_evolve.core import Deal, Hand, Seat, Vulnerability, full_deck
from bridge_evolve.systems.inputs import Board, parse_board
from bridge_evolve.systems.language import integer, load_json, object_fields


def canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def hand_text(hand: Hand) -> str:
    ranks = "AKQJT98765432"
    return ".".join(
        "".join(rank for rank in ranks if any(card.rank == rank and card.suit == suit for card in hand.cards)) or "-"
        for suit in "SHDC"
    )


def deal_data(deal: Deal) -> dict[str, str]:
    return {seat.value: hand_text(deal.hand(seat)) for seat in Seat}


def deal_hash(deal: Deal) -> str:
    return canonical_hash(deal_data(deal))


def family_hash(deal: Deal) -> str:
    hands = [hand_text(deal.hand(seat)) for seat in Seat]
    return canonical_hash(min(tuple(hands[i:] + hands[:i]) for i in range(4)))


def board_data(board: Board) -> dict[str, object]:
    return {"dealer": board.dealer.value, "vulnerability": board.vulnerability.value, "hands": deal_data(board.deal)}


@dataclass(frozen=True)
class BoardRecord:
    index: int
    family_id: str
    board: Board


def generate_dataset(directory: Path, *, count: int, seed: int, split: str) -> Path:
    if integer(count) < 1 or split not in ("train", "validation", "test"):
        raise ValueError("A dataset needs a positive count and train/validation/test split.")
    integer(seed)
    directory.mkdir(parents=True, exist_ok=False)
    rng = random.Random(seed)
    families = set()
    distribution = Counter()
    path = directory / "boards.jsonl"
    with path.open("x", encoding="utf8") as output:
        for index in range(count):
            while True:
                deck = list(full_deck())
                rng.shuffle(deck)
                deal = Deal(*(Hand(tuple(deck[start:start + 13])) for start in (0, 13, 26, 39)))
                family = family_hash(deal)
                if family not in families:
                    break
            families.add(family)
            dealer = list(Seat)[index % 4]
            vulnerability = list(Vulnerability)[((index // 4) + index) % 4]
            board = Board(deal, dealer, vulnerability)
            distribution[f"{dealer.value}/{vulnerability.value}"] += 1
            output.write(json.dumps({"index": index, "family_id": family, "board": board_data(board)}) + "\n")
    manifest = {
        "schema_version": 1, "count": count, "seed": seed, "split": split,
        "sampling": "uniform Fisher-Yates full 52-card deals; no hand/HCP selection",
        "generator": "python-random-shuffle-v1", "python": platform.python_version(),
        "schedule": "balanced 16-board dealer/vulnerability cycle",
        "families": "seat-cyclic-rotation grouped; distinct base families",
        "distribution": dict(sorted(distribution.items())),
        "boards": path.name, "boards_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    target = directory / "manifest.json"
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return target


def load_dataset(path: Path) -> tuple[dict, tuple[BoardRecord, ...]]:
    manifest = object_fields(load_json(path), {
        "schema_version", "count", "seed", "split", "sampling", "generator", "python",
        "schedule", "families", "distribution", "boards", "boards_sha256",
    })
    if integer(manifest["schema_version"]) != 1 or integer(manifest["count"]) < 1:
        raise ValueError("Unsupported dataset manifest.")
    integer(manifest["seed"])
    if manifest["split"] not in ("train", "validation", "test"):
        raise ValueError("Unknown dataset split.")
    board_name = manifest["boards"]
    if not isinstance(board_name, str) or Path(board_name).name != board_name:
        raise ValueError("Dataset boards must name a sibling file.")
    board_path = path.parent / board_name
    contents = board_path.read_bytes()
    if hashlib.sha256(contents).hexdigest() != manifest["boards_sha256"]:
        raise ValueError("Dataset content hash mismatch.")
    records = []
    families = set()
    for index, line in enumerate(contents.decode("utf8").splitlines()):
        raw = object_fields(json.loads(line), {"index", "family_id", "board"})
        board = parse_board(raw["board"])
        family = family_hash(board.deal)
        if integer(raw["index"]) != index or raw["family_id"] != family or family in families:
            raise ValueError(f"Invalid index or duplicated/mismatched board family at row {index}.")
        families.add(family)
        records.append(BoardRecord(index, family, board))
    if len(records) != manifest["count"]:
        raise ValueError("Dataset row count does not match its manifest.")
    return manifest, tuple(records)
