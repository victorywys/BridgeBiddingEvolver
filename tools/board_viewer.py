"""Local, post-hoc board review and system comparison. Never changes policies."""

from __future__ import annotations

import hashlib
import argparse
import copy
import gzip
import importlib.util
import io
import json
import logging
import math
import mimetypes
import multiprocessing
import os
import random
import re
import threading
import uuid
import zipfile
from collections import OrderedDict, deque
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, wait
from contextlib import closing
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bridge_evolve.core import CallKind, Contract, Deal, Hand, Seat, Vulnerability, apply_call, final_contract, full_deck, initial_auction, signed_imp
from bridge_evolve.evaluation.cache import DDSCache
from bridge_evolve.evaluation.audit_workers import audit_board, initialize_worker
from bridge_evolve.evaluation.data import board_data, canonical_hash, family_hash
from bridge_evolve.evaluation.record_log import (
    RecordReader, RecordWriter, agent_board, agent_decision,
    decode_records, encode_records, iter_records, record_statistics, select_record, write_single_record,
)
from bridge_evolve.evaluation.experiment import source_snapshot, table_score, timestamp
from bridge_evolve.evaluation.slams import summarize_games, summarize_slams, table_slam_diagnostics
from bridge_evolve.evaluation.statistics import paired_summary
from bridge_evolve.oracle import DDTable, DDSAdapter, DDSError, DDSUnavailableError
from bridge_evolve.oracle.diagnostics import validate_diagnostics
from bridge_evolve.oracle.pbn import deal_from_pbn, deal_to_pbn
from bridge_evolve.systems import load_system, render_system, validate_system
from bridge_evolve.systems.inputs import Board, parse_board, run_auction
from bridge_evolve.systems.language import SystemError, parse_call, thaw
from bridge_evolve.systems.navigation import query_system
from bridge_evolve.systems.runtime import boolean, replay
from bridge_evolve.systems.v2_runtime import PublicContext, PublicWorld

ROOT = Path(__file__).resolve().parents[1]
SEATS = ("N", "E", "S", "W")
SUITS = ("S", "H", "D", "C")
SCHEMA = "bridge-review/v1"
MAX_UPLOAD = 32 * 1024 * 1024
AUDIT_ID = re.compile(r"audit-[0-9a-f]{32}")
HUMAN_ID = re.compile(r"human-[0-9a-f]{32}")
LOGGER = logging.getLogger("bridge.board_viewer")
SEAT_NAMES = {"N": "北家", "E": "东家", "S": "南家", "W": "西家"}
SUIT_NAMES = {"S": "黑桃", "H": "红桃", "D": "方块", "C": "草花", "NT": "无将"}
GOAL_NAMES = {"none": "无持续进局目标", "one_round": "逼叫一轮", "invite_game": "邀局",
              "game": "逼局", "slam_try": "满贯探查", "small_slam": "小满贯",
              "invite_grand": "邀请大满贯", "grand_slam": "大满贯"}
PUBLIC_QUESTIONS = {
    "stayman": "高花问叫（Stayman）", "puppet": "傀儡高花问叫",
    "jacoby": "Jacoby强支持加叫", "mini_ask": "Mini-Splinter短套支持",
    "mini_answer": "询问具体短套", "bergen_answer": "Bergen约定加叫",
    "drury_answer": "Drury支持问叫", "inverted": "低花反加叫",
    "raise_answer": "加叫后的牌力问叫", "weak_ask": "弱二开叫后的牌力、套质问叫",
    "checkback_relay": "双路重询的接力", "checkback_force": "逼局重询", "checkback_invite": "双路重询的邀请性问叫",
    "checkback_clubs": "重询后的草花接力", "fourth_answer": "第四花色逼局问叫",
    "reverse_wait": "逆叫后的低限等待", "rkcb": "关键张问叫",
    "gerber": "A问叫（Gerber）", "void_answer": "排除指定花色的关键张问叫",
    "queen_answer": "询问将牌Q", "king_answer": "询问边花K",
    "precision_club_ask": "草花开叫后的人工问叫", "precision_club_relay": "草花问叫后的方块牌型说明",
    "precision_club_reveal": "进一步询问方块牌型", "precision_three_ask": "三套型开叫后的牌力、牌型问叫",
    "precision_shape_ask": "询问第二个四张套", "precision_shape_reply": "询问另一个四张套",
    "precision_four_ask": "询问四张套位置", "precision_four_reply": "询问四张套位置",
    "precision_point_shape": "精确点力、牌型接力",
    "precision_three_transfer": "三套型开叫后的约定性高花转移",
    "strong_4441_finish": "低限4441牌型显示，给搭档继续选高花的机会",
    "puppet_reveal": "傀儡问叫后续的高花牌型说明",
    "transfer_nt_choice": "转移后的无将选择或邀请",
    "shape_answer": "牌型问叫", "support_answer": "支持程度或点力、牌型问叫",
    "smolen_reply": "Smolen高花长度说明", "minor_stayman": "低花斯台曼",
    "lebensohl_relay": "莱本索尔接力", "nt_cue_reply": "受干扰无将后的约定性扣叫",
}
PUBLIC_ANSWERS = {
    "puppet": "傀儡高花问叫的答叫", "mini_answer": "短套问叫的答叫",
    "weak_ask": "弱二问叫的答叫", "rkcb": "关键张问叫的答叫", "gerber": "A问叫的答叫",
    "void_answer": "排除关键张问叫的答叫", "queen_answer": "将牌Q问叫的答叫",
    "king_answer": "边花K问叫的答叫", "shape_answer": "牌型问叫的答叫",
    "support_answer": "支持或牌力、牌型问叫的答叫",
    "precision_point_shape": "精确点力、牌型接力答叫",
    "precision_shape_reply": "第二个四张套的答叫", "precision_four_reply": "四张套位置的答叫",
    "precision_club_reveal": "方块牌型接力的答叫", "precision_three_ask": "三套型问叫的答叫",
    "drury_answer": "Drury问叫后的牌力答叫", "jacoby": "Jacoby强支持约定后的续叫",
    "fourth_answer": "第四花色逼局后的续叫", "checkback_invite": "邀请性重询的答叫",
    "checkback_force": "逼局重询的答叫", "reverse_wait": "逆叫等待后的续叫",
}


def read_rows(path: Path) -> list[dict]:
    return list(iter_records(path))


def hand_summary(raw: dict) -> dict:
    board = parse_board({key: raw[key] for key in ("dealer", "vulnerability", "hands")})
    values = {}
    for seat in SEATS:
        cards = board.deal.hand(Seat(seat)).cards
        values[seat] = {
            "hcp": sum({"A": 4, "K": 3, "Q": 2, "J": 1}.get(card.rank, 0) for card in cards),
            "shape": [sum(card.suit == suit for card in cards) for suit in SUITS],
        }
    return {**board_data(board), "hand_summary": values}


def public_person_bounds(context: dict, seat: str) -> dict:
    captain = context.get("var.captain", "")
    if captain and captain not in SEATS:
        raise ValueError("Invalid public captain in call explanation.")
    owner = ("captain" if captain == seat else "responder") if captain else None
    result = {}
    for field in ("hcp", *SUITS):
        limit = 40 if field == "hcp" else 13
        low = context.get(f"var.{owner}_{field}_min", 0) if owner else 0
        high = context.get(f"var.{owner}_{field}_max", limit) if owner else limit
        if field == "hcp":
            high = min(high, context.get(f"var.seat_{seat}_hcp_max", 40))
        if type(low) is not int or type(high) is not int or not 0 <= low <= high <= limit:
            raise ValueError(f"Invalid public {field} interval for explanation.")
        result[field] = (low, high)
    if owner:
        for suit in SUITS:
            for quality in ("support_quality", "support_denied", "control"):
                value = context.get(f"var.{owner}_{suit}_{quality}", False)
                if type(value) is not bool:
                    raise ValueError("Invalid public support/control guarantee.")
                result[f"{suit}:{quality}"] = value
        for left in SUITS:
            for right in SUITS:
                if left != right:
                    value = context.get(f"var.{owner}_{left}_ge_{right}", False)
                    if type(value) is not bool:
                        raise ValueError("Invalid public suit relation for explanation.")
                    result[f"{left}>={right}"] = value
    return result


def public_claim_text(bounds: dict, selected=None) -> list[str]:
    selected = set(bounds) if selected is None else set(selected)
    result = []
    for field in ("hcp", *SUITS):
        if field not in selected:
            continue
        low, high = bounds[field]
        limit = 40 if field == "hcp" else 13
        label, unit = ("", "大牌点") if field == "hcp" else (SUIT_NAMES[field], "张")
        if low == 0 and high == limit:
            continue
        if field in ("H", "S") and {"H", "S"} <= selected and bounds["H"] == bounds["S"] == (0, 4):
            if field == "S":
                result.append("没有五张高花（红桃和黑桃均不超过4张）")
            continue
        if field != "hcp" and high <= 1:
            result.append(f"{label}缺门" if high == 0 else f"{label}单张" if low == 1 else f"{label}单缺（至多1张）")
        elif low == high:
            result.append(f"{label}正好{low}{unit}")
        elif high == limit:
            result.append(f"{label}至少{low}{unit}")
        elif low == 0:
            result.append(f"{label}最多{high}{unit}")
        else:
            result.append(f"{label}{low}–{high}{unit}")
    for index, left in enumerate(SUITS):
        for right in SUITS[index + 1:]:
            forward, reverse = f"{left}>={right}", f"{right}>={left}"
            if bounds.get(forward) and bounds.get(reverse) and {forward, reverse} & selected:
                result.append(f"{SUIT_NAMES[left]}与{SUIT_NAMES[right]}等长")
            elif bounds.get(forward) and forward in selected:
                result.append(f"{SUIT_NAMES[left]}不少于{SUIT_NAMES[right]}")
            elif bounds.get(reverse) and reverse in selected:
                result.append(f"{SUIT_NAMES[right]}不少于{SUIT_NAMES[left]}")
    for suit in SUITS:
        for quality, text in (
            ("support_quality", "支持至少Qxx（含Axx/Kxx）或xxxx"),
            ("support_denied", "不符合Qxx（或更好）/xxxx支持，至多三张"),
            ("control", "有首轮或次轮控制（A、K或单缺；未区分具体是哪一种）"),
        ):
            key = f"{suit}:{quality}"
            if key in selected and bounds.get(key):
                result.append(SUIT_NAMES[suit] + text)
    return result


def public_call_purpose(call, before: dict, after: dict, updates: set, tags: tuple, classic: bool, history: tuple) -> tuple[str, bool]:
    prior_bid = any(item.kind == CallKind.BID for item in history)
    strain = call.strain.value if call.strain is not None else ""
    old_state, new_state = before.get("state", ""), after.get("state", "")
    if call.kind == CallKind.PASS:
        return ("接受4441牌型回复后的无将定约" if classic and old_state == "strong_4441_finish"
                else "拒绝邀局" if before.get("forcing") == "invite_game"
                else "拒绝无将定量邀贯" if classic and old_state == "quantitative" and before.get("goal") == "slam_try"
                else "开叫前Pass" if not prior_bid else "暂不继续叫牌"), False
    if call.kind in (CallKind.DOUBLE, CallKind.REDOUBLE):
        if classic and call.kind == CallKind.REDOUBLE and old_state == "nt_doubled" and new_state == "nt_escape":
            return "无将被加倍后的逃叫再加倍", True
        kinds = {"takeout": "技术性加倍", "negative": "否定性加倍", "penalty": "惩罚性加倍",
                 "strength": "实力再加倍", "escape": "逃叫用的再加倍", "two_suited": "双套型加倍"}
        fresh = "double_kind" in updates or after.get("var.double_kind") != before.get("var.double_kind")
        kind = after.get("var.double_kind", "") if fresh else ""
        return kinds.get(kind, "对敌方定约加倍" if call.kind == CallKind.DOUBLE else "对敌方加倍作再加倍"), False
    if classic:
        if new_state == "quantitative" and call.kind == CallKind.BID and call.strain.value == "NT":
            return ("无将定量邀局，允许搭档拒绝" if after.get("var.invite_target") == "game"
                    else "无将定量邀贯（不是问A或关键张），允许搭档拒绝"), False
        if old_state == "gerber_finish" and new_state == "finished" and str(call) == "4NT":
            return "Gerber问A后的无将止叫（不是定量邀贯）", False
        if old_state == "quantitative" and str(call) in ("6NT", "7NT"):
            return ("接受无将定量邀贯，选择小满贯" if str(call) == "6NT"
                    else "接受无将定量邀贯，选择大满贯"), False
        short = after.get("var.short_suit", "")
        if "short_suit" in updates and short in SUITS:
            return f"短套显示：说明{SUIT_NAMES[short]}单缺", True
        if old_state == "transfer":
            return f"完成向{SUIT_NAMES.get(strain, strain)}的转移", True
        if new_state == "transfer":
            target = after.get("var.transfer", "")
            return f"转移叫：要求转到{SUIT_NAMES[target]}" if target in SUITS else "花色转移约定叫", True
        if new_state != old_state and new_state in ("rkcb", "gerber", "void_answer", "queen_answer", "king_answer"):
            return PUBLIC_QUESTIONS[new_state], True
        if new_state == "slam_cue" or old_state == "slam_cue":
            trump = after.get("agreed_trump", before.get("agreed_trump", ""))
            if old_state == "strong_positive" and after.get("var.slam_solid"):
                return "显示独立可打套，并明确要求同伴开始控制扣叫", False
            if strain == trump:
                return ("已定将牌的满贯邀请：要求搭档开始扣叫" if call.level < (4 if trump in ("H", "S") else 5)
                        else "回到将牌成局，不承诺满贯；搭档仍可按已知控制继续判断"), False
            return f"满贯尝试中的控制扣叫：显示{SUIT_NAMES.get(strain, strain)}控制", True
        if old_state == "stayman":
            return ("高花问叫中的高花显示" if strain in ("H", "S") else "高花问叫的约定性否定答叫"), strain not in ("H", "S")
        if old_state == "precision_club_ask" and strain in ("H", "S") and call.level == 2:
            return "草花问叫中的四张高花显示", False
        if new_state == "support_answer" and after.get("var.invite_target") == "six-step":
            asked = after.get("var.transfer", "")
            return f"{SUIT_NAMES.get(asked, asked)}支持问叫：询问Qxx（或更好）/xxxx支持及点力", True
        if new_state == "support_answer" and after.get("var.invite_target") == "point-shape":
            return "点力、牌型问叫，并非自然草花支持", True
        if old_state in PUBLIC_ANSWERS:
            return PUBLIC_ANSWERS[old_state], True
        if new_state != old_state and new_state in PUBLIC_QUESTIONS:
            return PUBLIC_QUESTIONS[new_state], True
        if old_state == "strong_club" and new_state == "strong_negative":
            return "强梅花开叫后的示弱应叫", True
        if old_state == "strong_club" and new_state == "strong_positive":
            shown = after.get("var.response_suit", "")
            return ("自然正应叫" if shown == strain else "约定性正应叫或无将牌型显示"), shown != strain
        if not prior_bid:
            opening = after.get("var.opening_kind", "")
            if opening in ("strong_club", "strong_two", "three_suit", "gambling"):
                return {"strong_club": "人工强梅花开叫", "strong_two": "人工强开叫",
                        "three_suit": "精确三套型开叫", "gambling": "赌博性无将开叫"}[opening], True
            if opening in ("weak_two", "preempt"):
                return "弱二开叫" if opening == "weak_two" else "阻击开叫", False
        if new_state in ("two_suit_select", "two_suit_minor", "two_suit_pick", "landy_answer"):
            return "约定性双套示叫或选择", True
        if new_state == "minor_transfer":
            return "低花转移或低花选择约定", True
        if new_state == "lebensohl_finish":
            return "完成莱本索尔的草花接力", True
        if new_state in ("nt_escape", "nt_escape_finish"):
            return "无将被加倍后的逃叫接力", True
        if old_state == "nt_escape_finish":
            return "逃叫接力后的花色选择", False
        if strain == after.get("agreed_trump") and after.get("agreed_trump"):
            return "将牌支持或回到已选将牌", False
        if not prior_bid:
            return "自然无将开叫" if strain == "NT" else "自然花色开叫", False
        if strain == before.get("var.open_suit") and strain in SUITS:
            return f"继续显示{SUIT_NAMES[strain]}的再叫", False
        return "无将应叫或再叫" if strain == "NT" else "自然花色应叫或再叫", False
    if "artificial" in tags:
        return "体系明确标记的人工叫品", True
    if "natural" in tags:
        return "体系明确标记的自然叫品", False
    return "定约叫品（未附可校验的约定用途标签）", False


def public_control_answer(before: dict, after: dict, updates: set, transition_id: str, classic: bool) -> tuple[str, str]:
    if not classic:
        return "", ""
    minimum, queen = after.get("var.answer_min"), after.get("var.answer_q")
    state = before.get("state")
    if state == "queen_answer" and "answer_q" in updates:
        if queen is False:
            return "本次明确否认将牌Q。", ""
        if queen is True:
            shown = re.search(r"\.queen_answer\.king_[HS]_([CDHS])\.t\d+$", transition_id)
            return ("本次公开保证有将牌Q，并显示" + SUIT_NAMES[shown[1]] + "K。" if shown
                    else "本次公开保证有将牌Q，本档未指明具体边花K。"), ""
    if "answer_min" not in updates:
        return "", ""
    if state == "rkcb":
        match = re.search(r"\.rkcb\.answer_(14|03|25_no_q|25_with_q)\.t\d+$", transition_id)
        if match:
            label = match[1]
            expected = {"14": (1, False), "03": (0, False), "25_no_q": (2, False), "25_with_q": (2, True)}
            if (minimum, queen) != expected[label]:
                return "", "关键张答档标识与所记下界/Q信息不一致，未补写该档的枚举含义。"
            choices = {"14": "1／4", "03": "0／3", "25_no_q": "2／5", "25_with_q": "2／5"}[label]
            suffix = "，明确含将牌Q。" if label == "25_with_q" else "，明确不含将牌Q。" if label == "25_no_q" else "，尚未区分两种数量。"
            return f"本次为{choices}个关键张档" + suffix, ""
    elif state == "gerber":
        match = re.search(r"\.gerber\.answered_(4D|4H|4S|4NT)\.t\d+$", transition_id)
        if match:
            label = match[1]
            expected = {"4D": 0, "4H": 1, "4S": 2, "4NT": 3}
            if minimum != expected[label]:
                return "", "A答档标识与所记下界不一致，未补写确切数量。"
            return ("本次为0／4个A档，尚未区分两种数量。" if label == "4D" else f"本次公开显示{minimum}个A。"), ""
    elif state == "void_answer":
        match = re.search(r"\.void_answer\.answer_[HS]_[CDHS]_([1-5])\.t\d+$", transition_id)
        if match:
            step = int(match[1])
            expected = {1: (0, False), 2: (1, False), 3: (1, True), 4: (2, False), 5: (2, True)}
            if (minimum, queen) != expected[step]:
                return "", "排除关键张答档与所记下界/Q信息不一致，未补写确切数量。"
            excluded = SUIT_NAMES.get(before.get("var.void_suit"), "指定花色")
            suffix = "，本档未区分是否持有将牌Q。" if step == 1 else "，明确含将牌Q。" if queen else "，明确不含将牌Q。"
            return f"不计{excluded}A后，本次显示{minimum}个关键张" + suffix, ""
    return "", ""


def describe_public_call(seat: str, call, before: dict | None, after: dict | None, *,
                         history: tuple = (), updates=frozenset(), tags=(), classic=False,
                         basis="recorded-public-information", transition_id="", warnings=()) -> dict:
    before = before or {}
    recorded_after = bool(after)
    after = after or before
    warnings = list(warnings)
    purpose, artificial = public_call_purpose(call, before, after, set(updates), tags, classic, history)
    old, current = public_person_bounds(before, seat), public_person_bounds(after, seat)
    changed = {field for field in ("hcp", *SUITS) if current[field][0] > old[field][0] or current[field][1] < old[field][1]}
    changed.update(key for key, value in current.items() if ">=" in key and value and not old.get(key, False))
    changed.update(key for key, value in current.items() if ":" in key and value and not old.get(key, False))
    new_information = [f"{SEAT_NAMES[seat]}：{item}" for item in public_claim_text(current, changed)] if recorded_after else []
    retained = public_claim_text(current, set(current) - changed)
    partner = SEATS[(SEATS.index(seat) + 2) % 4]
    partner_before, partner_after = public_person_bounds(before, partner), public_person_bounds(after, partner)
    partner_changed = {field for field in ("hcp", *SUITS)
                       if partner_after[field][0] > partner_before[field][0] or partner_after[field][1] < partner_before[field][1]}
    partner_changed.update(key for key, value in partner_after.items() if ">=" in key and value and not partner_before.get(key, False))
    partner_changed.update(key for key, value in partner_after.items() if ":" in key and value and not partner_before.get(key, False))
    if recorded_after:
        new_information.extend(f"对{SEAT_NAMES[partner]}可新增推知：{item}" for item in public_claim_text(partner_after, partner_changed))
    control, control_warning = public_control_answer(before, after, set(updates), transition_id, classic)
    if control:
        new_information.insert(0, control)
    if control_warning:
        warnings.append(control_warning)
    notes = []
    if classic and call.kind == CallKind.BID and not transition_id:
        notes.append("本次没有命名的公共转移；用途仅按已有协议阶段概述，不补猜未编码约定。")
    if call.kind in (CallKind.DOUBLE, CallKind.REDOUBLE) and purpose in ("对敌方定约加倍", "对敌方加倍作再加倍"):
        notes.append("本次公共转移未单独记录更具体的用途；不自动补成实力、惩罚或逃叫含义。")
    if artificial and call.strain is not None:
        notes.append("这是约定性无将叫品，不能只按字面理解为自然无将止叫。"
                     if call.strain.value == "NT" else
                     f"这是约定性用途，不能仅按字面把{SUIT_NAMES[call.strain.value]}当作本人自然长套；长度保证以下列公开信息为准。")
    elif classic and call.kind == CallKind.BID and call.strain is not None and call.strain.value in SUITS and current[call.strain.value][0] == 0:
        notes.append("当前冻结公开账本未给出该自然花色的最低张数；不能从实际持牌或私有命中规则补成五张。")
    trump = after.get("agreed_trump", "")
    if trump in SUITS and trump != before.get("agreed_trump"):
        lower = after.get("agreed_trump_min", 0)
        new_information.append(f"本方将牌记为{SUIT_NAMES[trump]}，公开联手至少{lower}张。"
                               + ("" if lower >= 8 else "尚不能据此认定已知八张配合。"))
    forcing, goal = after.get("forcing", "none"), after.get("goal", "none")
    if forcing in ("one_round", "slam_try"):
        target = after.get("forcing_target")
        notes.append(f"当前记录为{GOAL_NAMES[forcing]}，" + (f"下一次由{SEAT_NAMES[target]}履行答叫义务。" if target in SEATS else "仍有一轮继续叫牌的义务。"))
    elif forcing == "invite_game":
        notes.append("这是邀局，允许搭档拒绝；不是逼局。")
    elif forcing != "none":
        notes.append(f"本方仍受{GOAL_NAMES.get(forcing, forcing)}义务约束。")
    elif goal not in ("none", "one_round", "invite_game", "slam_try") and after.get("goal_met"):
        notes.append(f"执行账本把{GOAL_NAMES.get(goal, goal)}目标记为已达，当前没有继续义务；这不是对该终局选择正确性的保证。")
    elif "forcing" in after:
        notes.append("公开账本当前未设置持续逼叫义务；具体后续仍由所处协议阶段规定。")
    if after.get("var.game_invited") and not before.get("var.game_invited"):
        notes.append("这是探询成局的可能性；新增点力只取该公开叫品各发送分支的共同保证，不把联手上界当作已知实力。")
    if after.get("var.nt_slam_invited") and not before.get("var.nt_slam_invited"):
        notes.append("这是有可能达到满贯的无将试探，不表示联手已知31或33点；搭档须结合自己的真实牌力再接受或拒绝。")
    if after.get("state") == "slam_cue" and before.get("state") != "slam_cue":
        notes.append("低位定将或扣叫保留满贯探索空间，不承诺六阶，也不额外宣称某个未编码的HCP下界。")
    if classic and "answer_min" in updates and before.get("state") in ("rkcb", "gerber", "void_answer"):
        unit = "A" if before["state"] == "gerber" else "关键张"
        notes.append(f"公开记忆保存的{unit}下界为{after.get('var.answer_min', 0)}；不把有歧义的低档下界当成确切高档数量。")
    if classic and before.get("state") == "king_answer":
        notes.append("这是边花K问叫后的答叫；未单独保存的具体K承诺，不从私有命中规则补猜。")
    if classic and "answer_q" in updates and after.get("var.answer_q") is True and not control:
        new_information.append("本次公开保证持有将牌Q。")
    if "two_suit_a" in updates or "two_suit_b" in updates:
        shown = [SUIT_NAMES[after[f"var.two_suit_{key}"]] for key in ("a", "b") if after.get(f"var.two_suit_{key}") in SUITS]
        if shown:
            notes.append("已指明的套为" + "、".join(shown)
                         + ("；另一套未指明，不补猜具体花色。" if len(shown) < 2 else "；两套花色均已指明。"))
    if after.get("auction_ended"):
        notes.append("本次叫牌后竞叫结束；" + after.get("final_contract_text", "最终定约以公开叫牌记录为准。"))
    actor_claims = public_claim_text(current, changed if call.kind == CallKind.PASS or control else None)
    summary = f"{SEAT_NAMES[seat]} {call}：{purpose}。"
    if control:
        summary += control
    if actor_claims:
        summary += ("当前公开可保证" if basis == "frozen-public-protocol" else "所附公共快照记为") + "；".join(actor_claims) + "。"
    elif not new_information:
        summary += "本次未记录新的量化手牌保证。"
    if not recorded_after:
        notes.append("没有可用后态；列出的范围仅为此前已知信息，不能当成本次新增承诺。")
    return {"schema_version": 1, "summary": summary, "purpose": purpose, "basis": basis,
            "new_information": new_information, "retained_information": retained,
            "notes": notes, "warnings": warnings, "public_transition_id": transition_id}


def explanation_context(context: PublicContext) -> dict:
    result = {f"var.{name}": thaw(value) for name, value in context.frame.parameters.items()}
    for name in ("state", "agreed_trump", "agreed_trump_min", "forcing", "goal", "goal_met"):
        result[name] = thaw(context.value(name))
    result["forcing_target"] = context.frame.target.value if context.frame.target is not None else ""
    result["auction_ended"] = context.world.auction.ended
    if context.world.auction.ended:
        final = final_contract(context.world.auction)
        result["final_contract_text"] = (f"最终定约为{final.level}{SUIT_NAMES[final.strain.value]}{ {1: '', 2: '（加倍）', 4: '（再加倍）'}[final.doubling]}，{SEAT_NAMES[final.declarer.value]}做庄。"
                                         if isinstance(final, Contract) else "全桌Pass，没有定约。")
    return result


def check_recorded_public_context(saved: dict | None, context: PublicContext) -> None:
    if saved is None:
        return
    if not isinstance(saved, dict):
        raise ValueError("Recorded public context must be an object.")
    for name, actual in saved.items():
        if name.startswith("var."):
            if name[4:] not in context.frame.parameters:
                raise ValueError(f"冻结协议未声明日志公共参数{name}。")
            expected = context.frame.parameters[name[4:]]
        elif name == "unavailable":
            expected = dict(context.snapshot())["unavailable"]
        else:
            expected = context.value(name)
        if thaw(expected) != actual:
            raise ValueError(f"冻结协议重建与日志字段{name}不一致。")


def classic_public_protocol(system) -> bool:
    return bool(system.states) and any(state.transitions for state in system.states) and all(
        item.id.startswith(("ccba.", "prec.")) for state in system.states for item in state.transitions)


def explain_public_auction(public_steps: list[dict], dealer: str, vulnerability: str, definitions: dict | None = None) -> list[dict]:
    """Only public steps and static definitions enter this explanation boundary."""
    definitions = definitions or {}
    world = PublicWorld(definitions, Seat(dealer), Vulnerability(vulnerability)) if set(definitions) == {"NS", "EW"} else None
    classic = {side: classic_public_protocol(system) for side, system in definitions.items()}
    history, results = [], []
    replay_error = ""
    for step in public_steps:
        seat, call = step["seat"], parse_call(step["call"])
        before, after = step.get("public_context"), step.get("public_context_after")
        updates, tags, transition_id = set(), (), ""
        basis = "recorded-public-information"
        warnings = []
        is_classic = False
        if world is not None:
            side = Seat(seat).partnership.value
            previous = copy.copy(world)
            previous.events = list(world.events)
            prior_context = PublicContext(previous, side, Seat(seat))
            try:
                check_recorded_public_context(before, prior_context)
                world.step(call)
                next_context = PublicContext(world, side, Seat(seat))
                check_recorded_public_context(after, next_context)
                event = world.events[-1]
                system = definitions[side]
                if system.version == 2:
                    state = next(state for state in system.states if state.name == prior_context.frame.state)
                    evaluation = previous.evaluation(side, event)
                    matches = [item for item in state.transitions if boolean(evaluation.evaluate(item.when))]
                    if len(matches) > 1:
                        raise ValueError("公开解释匹配了多个状态转移。")
                    if matches:
                        updates = {name for name, _ in matches[0].assignments}
                        transition_id = matches[0].id
                before, after = explanation_context(prior_context), explanation_context(next_context)
                tags, is_classic = event.tags, classic[side]
                basis = "frozen-public-protocol"
                if "public_context_after" not in step:
                    warnings.append("该旧日志未保存后态；说明按冻结公开协议只读重建，不回填原始记录。")
            except (SystemError, ValueError) as error:
                replay_error = str(error)
                world = None
                before, after = step.get("public_context"), step.get("public_context_after")
                updates, tags, transition_id, is_classic = set(), (), "", False
        if world is None:
            warnings.append("未能核对完整冻结公开协议，以下仅描述已存公共快照。" + replay_error)
        results.append(describe_public_call(seat, call, before, after, history=tuple(history), updates=updates,
                       tags=tags, classic=is_classic, basis=basis, transition_id=transition_id, warnings=warnings))
        history.append(call)
    return results


def annotate_public_meanings(record: dict, definitions: dict | None = None, *, prefer_saved: bool = False) -> None:
    for table in record["tables"]:
        public_definitions = {side: definitions[identity["hash"]] for side, identity in table["systems"].items()
                              if definitions is not None and identity.get("hash") in definitions}
        if any(system.content_hash != table["systems"][side]["hash"] for side, system in public_definitions.items()):
            raise ValueError("Public explanation definition hashes do not match their registered identities.")
        # Only hash-verified audit artifacts may opt into their saved wording.
        if prefer_saved and len(public_definitions) == 2 and all("public_explanation" in step for step in table["auction"]):
            for step in table["auction"]:
                meaning = step["public_explanation"]
                if (not isinstance(meaning, dict) or type(meaning.get("schema_version")) is not int
                        or meaning["schema_version"] != 1
                        or any(not isinstance(meaning.get(key), str) for key in ("summary", "purpose", "basis", "public_transition_id"))
                        or any(not isinstance(meaning.get(key), list) or any(not isinstance(item, str) for item in meaning[key])
                               for key in ("new_information", "retained_information", "notes", "warnings"))):
                    raise ValueError("Invalid saved public explanation schema.")
            continue
        public_steps = [{key: step[key] for key in ("seat", "call", "public_context", "public_context_after") if key in step}
                        for step in table["auction"]]
        meanings = explain_public_auction(public_steps, record["dealer"], record["vulnerability"], public_definitions)
        for step, meaning in zip(table["auction"], meanings, strict=True):
            step["public_explanation"] = meaning


def classify(record: dict) -> dict:
    tables = record.get("tables", [])
    if len(tables) < 2:
        return {"auction": False, "contract": False, "score": False, "first_turn": None}
    first, second = tables[:2]
    left = [step["call"] for step in first.get("auction", [])]
    right = [step["call"] for step in second.get("auction", [])]
    divergence = next((index for index in range(max(len(left), len(right)))
                       if (left[index] if index < len(left) else None) != (right[index] if index < len(right) else None)), None)
    return {
        "auction": left != right,
        "contract": first.get("contract") != second.get("contract"),
        "score": first.get("score_ns") is not None and second.get("score_ns") is not None
                 and first["score_ns"] != second["score_ns"],
        "first_turn": divergence,
    }


def statistics(records: list[dict]) -> dict:
    return {
        "boards": len(records),
        "different_auctions": sum(record["difference"]["auction"] for record in records),
        "different_contracts": sum(record["difference"]["contract"] for record in records),
        "nonzero_imp": sum(record.get("imp") not in (None, 0) for record in records),
        "same_score_different_auction": sum(record.get("imp") is not None and record["difference"]["auction"] and not record["difference"]["score"]
                            and all(table.get("score_ns") is not None for table in record["tables"][:2]) for record in records),
    }

def compact_board(record: dict) -> dict:
    result = {key: copy.deepcopy(record[key]) for key in
              ("id", "number", "index", "family_id", "source", "dealer", "vulnerability", "hands", "hand_summary",
               "imp", "point_difference", "difference", "label", "review") if key in record}
    result["details_pending"] = True
    result["search_text"] = " ".join(
        text for table in record["tables"] for step in table["auction"]
        for text in (step.get("private_trace", {}).get("rule_id", ""), step.get("public_explanation", {}).get("summary", "")))
    result["tables"] = []
    for table in record["tables"]:
        compact = {key: copy.deepcopy(table[key]) for key in
                   ("id", "systems", "contract", "tricks", "score_ns", "slam_diagnostics", "error") if key in table}
        compact["auction"] = [{key: step[key] for key in ("seat", "call") if key in step} for step in table["auction"]]
        result["tables"].append(compact)
    if "audit_result" in record:
        result["audit_result"] = {key: copy.deepcopy(record["audit_result"][key]) for key in ("status", "errors")}
    return result


def compact_metadata(bundle: dict) -> dict:
    result = {key: copy.deepcopy(value) for key, value in bundle.items() if key not in ("boards", "batch_audit")}
    result["details_on_demand"] = True
    if bundle.get("batch_audit"):
        result["batch_audit"] = {key: copy.deepcopy(bundle["batch_audit"][key]) for key in
                                 ("id", "status", "sampling", "execution", "completed_boards", "valid_boards",
                                  "failed_boards", "board_failures", "records") if key in bundle["batch_audit"]}
    audit = result.get("evaluation", {}).get("audit")
    if audit:
        for finding in audit.get("findings", []):
            finding["cases"] = [{key: item[key] for key in ("index", "board_number", "family_id", "table", "turn") if key in item}
                                for item in finding.get("cases", [])]
    return result


def assemble_run(run: dict, deals: list[dict], scores: list[dict], auctions: list[dict], source: str, *, explain=True) -> dict:
    if run.get("status") != "completed":
        raise ValueError("Only completed runs can be loaded as scored comparisons.")
    count = run.get("completed_boards")
    if len(deals) != len(scores) or len(deals) != count:
        raise ValueError("Run inputs and score rows have inconsistent board counts.")
    histories = {}
    for table in auctions:
        key = (table["index"], table["table"])
        if key in histories or table["table"] not in ("table1", "table2"):
            raise ValueError("Duplicate or unrecognized auction table.")
        histories[key] = table
    if len(histories) != 2 * count:
        raise ValueError("The run must contain exactly two auction histories per board.")
    identities = {run["system_a"]["hash"]: run["system_a"], run["system_b"]["hash"]: run["system_b"]}
    records = []
    for index, (deal, score) in enumerate(zip(deals, scores)):
        if deal["index"] != index or score["index"] != index or deal["family_id"] != score["family_id"]:
            raise ValueError("Board identities do not match across frozen files.")
        record = {"number": index + 1, "index": index, "family_id": deal["family_id"],
                  "source": source, **hand_summary(deal["board"]), "imp": score["imp_a"],
                  "point_difference": score["point_difference_a"], "tables": []}
        if score.get("dds") is not None:
            record["dds"] = score["dds"]
        for table_name in ("table1", "table2"):
            table = histories[(index, table_name)]
            saved = score[table_name]
            if any(table[key] != saved[key] for key in ("contract", "tricks", "score_ns")):
                raise ValueError(f"Auction/score mismatch on board {index}, {table_name}.")
            record["tables"].append({
                "id": table_name, "systems": {side: identities.get(content_hash, {"name": content_hash, "hash": content_hash})
                                              for side, content_hash in table["systems"].items()},
                "auction": table["auction"], **saved,
            })
        record["id"] = canonical_hash({"source": source, "index": index, "hands": record["hands"]})[:24]
        normalized = normalize_record(record, index, source)
        if explain:
            annotate_public_meanings(normalized)
        records.append(normalized)
    return {"schema": SCHEMA, "name": run["experiment_id"], "source": source,
            "score_perspective": "table1_NS - table2_NS; A=table1_NS/table2_EW",
            "systems": {"A": run["system_a"], "B": run["system_b"]},
            "statistics": statistics(records), "boards": records}


def run_purpose(run: dict) -> str:
    words = run["experiment_id"].split("-")
    if "smoke" in words:
        return "development-smoke"
    if "audit" in words and run.get("dataset_split") in ("train", "validation"):
        return "development-audit"
    return "frozen-evaluation"


def verify_audit_evidence(directory: Path, evidence: dict, system_hashes: tuple[str, ...] = ()) -> None:
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("Audit evidence must include file hashes.")
    for filename, expected_hash in evidence.items():
        if not isinstance(filename, str) or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("Invalid audit evidence identity.")
        relative = Path(filename)
        frozen_system = (len(relative.parts) == 2 and relative.parts[0] == "systems"
                         and relative.stem in system_hashes and relative.suffix in (".json", ".md"))
        if (not relative.parts or relative.as_posix() != filename or relative.is_absolute()
                or ".." in relative.parts or (len(relative.parts) != 1 and not frozen_system)
                or not (directory / relative).resolve().is_relative_to(directory.resolve())):
            raise ValueError("Audit evidence must name a sibling file or an identified frozen system.")
        with (directory / relative).open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != expected_hash:
                raise ValueError(f"Audit evidence hash mismatch: {filename}.")


def load_development_audit(directory: Path, run: dict, records: list[dict], summary: dict) -> dict:
    audit = json.loads((directory / "audit-report.json").read_text(encoding="utf8"))
    if not isinstance(audit, dict):
        raise ValueError("Development audit must be an object.")
    systems = {"ccba": run["system_a"]["hash"], "prec": run["system_b"]["hash"]}
    expected = {"source_hash": run["source_hash"], "dataset_hash": run["dataset_hash"], "systems": systems}
    if (any(audit.get(key) != value for key, value in expected.items())
            or not isinstance(audit.get("summary"), dict)
            or any(audit["summary"].get(key) != value for key, value in summary.items())):
        raise ValueError("Development audit does not match the frozen run identity or summary.")
    verify_audit_evidence(directory, audit["evidence_sha256"], tuple(systems.values()))
    verify_audit_evidence(directory, {"supplemental-check.py": audit["checker_sha256"]})
    metrics = audit.get("metrics")
    expected_metrics = {"boards": len(records), "tables": 2 * len(records),
                        "bound_checked_decisions": sum(len(table["auction"]) for record in records for table in record["tables"])}
    if (not isinstance(metrics, dict) or any(type(metrics.get(key)) is not int or metrics[key] != value
                                            for key, value in expected_metrics.items())):
        raise ValueError("Development audit counts do not match the frozen boards.")
    strict = metrics.get("strict_semantic_contracts")
    contracts = json.loads((directory / "semantic-contracts.json").read_text(encoding="utf8"))
    if (type(strict) is not int or strict < 0 or contracts.get("status") != "passed"
            or contracts.get("probe_counts") != {"consistent": strict}):
        raise ValueError("Development audit semantic-contract count does not match its evidence.")
    if (audit.get("status") not in ("passed", "completed-with-findings", "failed")
            or audit.get("engineering_checks_status") not in ("passed", "failed")
            or audit.get("semantic_review_status") not in ("completed-with-findings", "no-targeted-findings", "failed")):
        raise ValueError("Unknown development audit status.")
    findings = audit.get("findings")
    if not isinstance(findings, list) or (audit["status"] == "completed-with-findings" and not findings):
        raise ValueError("Development audit findings do not match its status.")
    if audit["status"] == "passed" and findings:
        raise ValueError("Development audit with findings cannot be displayed as passed.")
    for finding in findings:
        if (not isinstance(finding, dict) or not isinstance(finding.get("id"), str)
                or not isinstance(finding.get("description"), str) or not isinstance(finding.get("cases"), list)
                or not finding["cases"]):
            raise ValueError("Invalid development audit finding.")
        seen = set()
        families = set()
        for case in finding["cases"]:
            if not isinstance(case, dict):
                raise ValueError("Audit finding cases must be objects.")
            index = case.get("index")
            if type(index) is not int or not 0 <= index < len(records) or type(case.get("board_number")) is not int:
                raise ValueError("Invalid audit finding board identity.")
            record = records[index]
            table = next((table for table in record["tables"] if table["id"] == case.get("table")), None)
            expected_case = {"board_number": record["number"], "family_id": record["family_id"],
                             "board": {key: record[key] for key in ("dealer", "vulnerability", "hands")}}
            if (table is None or any(case.get(key) != value for key, value in expected_case.items())
                    or any(case.get(key) != table[key] for key in ("contract", "tricks", "score_ns"))
                    or case.get("history") != [step["call"] for step in table["auction"]]
                    or (index, case["table"]) in seen):
                raise ValueError("Audit finding does not match its frozen board and table.")
            seen.add((index, case["table"]))
            families.add(record["family_id"])
        if finding.get("affected_tables") != len(seen) or finding.get("affected_unique_families") != len(families):
            raise ValueError("Audit finding counts do not match its cases.")
    result = {key: audit[key] for key in
              ("status", "engineering_checks_status", "semantic_review_status", "release_acceptance", "metrics", "findings")}
    if "slam" in audit:
        repeated, _ = summarize_slams(records, {run["system_a"]["hash"]: "A", run["system_b"]["hash"]: "B"})
        if repeated != audit["slam"]:
            raise ValueError("Slam audit totals do not match the frozen DDS/auction records.")
        result["slam"] = repeated
    if "games" in audit:
        repeated, _ = summarize_games(records, {run["system_a"]["hash"]: "A", run["system_b"]["hash"]: "B"})
        if repeated != audit["games"]:
            raise ValueError("Game audit totals do not match the frozen DDS/auction records.")
        result["games"] = repeated
    return result


def run_evaluation(directory: Path, run: dict, records: list[dict]) -> dict:
    evaluation = {"purpose": run_purpose(run), "split": run.get("dataset_split")}
    summary_path = directory / "summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf8"))
        count = len(records)
        expected = {"boards": count, "tables": 2 * count, "total_imp": sum(record["imp"] for record in records),
                    "a_wins": sum(record["imp"] > 0 for record in records),
                    "b_wins": sum(record["imp"] < 0 for record in records),
                    "ties": sum(record["imp"] == 0 for record in records)}
        if not count or any(summary[key] != value for key, value in expected.items()):
            raise ValueError("Frozen summary does not match the scored boards.")
        interval = summary["ci95"]
        if (not isinstance(interval, list) or len(interval) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       for value in (summary["mean_imp"], *interval))
                or interval[0] > interval[1]
                or not math.isclose(summary["mean_imp"], expected["total_imp"] / count, abs_tol=1e-12)):
            raise ValueError("Frozen summary has an invalid mean or confidence interval.")
        evaluation["summary"] = {key: summary[key] for key in
            (*expected, "mean_imp", "ci95", "invalid_boards", "deleted_or_replaced_boards")}
    audit_path = directory / "smoke-audit.json"
    if audit_path.is_file():
        audit = json.loads(audit_path.read_text(encoding="utf8"))
        expected = {"boards": len(records), "tables": 2 * len(records),
                    "source_hash": run["source_hash"], "dataset_hash": run["dataset_hash"],
                    "current_systems": {"ccba": run["system_a"]["hash"], "prec": run["system_b"]["hash"]}}
        if "summary" not in evaluation or any(audit[key] != value for key, value in expected.items()):
            raise ValueError("Smoke audit does not match the frozen run identity.")
        verify_audit_evidence(directory, audit["evidence_sha256"])
        evaluation["audit"] = {key: audit[key] for key in
            ("status", "smoke_status", "release_acceptance", "metrics", "findings")}
    if (directory / "audit-report.json").is_file():
        if audit_path.is_file() or "summary" not in evaluation:
            raise ValueError("Development audit requires one unambiguous report and a frozen summary.")
        evaluation["audit"] = load_development_audit(directory, run, records, evaluation["summary"])
    return evaluation


def load_run(directory: Path, public_definitions: dict | None = None) -> dict:
    run = json.loads((directory / "run.json").read_text(encoding="utf8"))
    for filename in ("dataset-boards.jsonl", "boards.jsonl", "auctions.jsonl", "summary.json"):
        expected = run.get("artifacts_sha256", {}).get(filename)
        if expected:
            with (directory / filename).open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != expected:
                    raise ValueError(f"Frozen artifact hash mismatch: {filename}.")
    bundle = assemble_run(run, read_rows(directory / "dataset-boards.jsonl"), read_rows(directory / "boards.jsonl"),
                          read_rows(directory / "auctions.jsonl"), directory.name, explain=False)
    bundle["evaluation"] = run_evaluation(directory, run, bundle["boards"])
    if public_definitions is None:
        public_definitions = {}
        for role in ("system_a", "system_b"):
            identity = run[role]["hash"]
            path = directory / "systems" / f"{identity}.json"
            if path.is_file():
                system = load_system(path)
                if system.content_hash != identity:
                    raise ValueError("Frozen public explanation system identity mismatch.")
                public_definitions[identity] = system
    for record in bundle["boards"]:
        annotate_public_meanings(record, public_definitions)
    return bundle


def contract_data(contract) -> dict:
    if not isinstance(contract, Contract):
        return {"passed_out": True}
    return {"level": contract.level, "strain": contract.strain.value,
            "declarer": contract.declarer.value, "doubling": contract.doubling}


def normalize_record(raw: dict, index: int, source: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Each imported board must be an object.")
    base = raw.get("board", raw)
    if not isinstance(base, dict) or not all(key in base for key in ("dealer", "vulnerability", "hands")):
        raise ValueError("牌例需要 dealer、vulnerability 和四手 hands；单独的成绩文件不含手牌。")
    raw_index = raw.get("index", index)
    if type(raw_index) is not int or raw_index < 0:
        raise ValueError("Board index must be a nonnegative integer.")
    number = raw.get("number", raw_index + 1)
    if type(number) is not int or number < 1:
        raise ValueError("Board numbers must be positive integers.")
    for key in ("imp", "point_difference"):
        if raw.get(key) is not None and type(raw[key]) is not int:
            raise ValueError("IMP and point differences must be integers or null.")
    review = raw.get("review", {})
    if not isinstance(review, dict) or review.get("status", "unreviewed") not in ("unreviewed", "flagged", "approved"):
        raise ValueError("Unknown review status.")
    if not isinstance(review.get("note", ""), str) or len(review.get("note", "")) > 20000:
        raise ValueError("Review notes must be text of at most 20000 characters.")
    if raw.get("id") is not None and (not isinstance(raw["id"], str) or not raw["id"] or len(raw["id"]) > 128):
        raise ValueError("Board IDs must be nonempty strings of at most 128 characters.")
    if not isinstance(raw.get("label", ""), str) or len(raw.get("label", "")) > 160:
        raise ValueError("牌例名称须为不超过160字的文本。")
    record = {"number": number, "index": raw_index, "family_id": raw.get("family_id"), "source": source, **hand_summary(base),
              "tables": [], "imp": raw.get("imp"), "point_difference": raw.get("point_difference"),
              "review": review}
    if raw.get("label"):
        record["label"] = raw["label"]
    if "audit_result" in raw:
        result = raw["audit_result"]
        if (not isinstance(result, dict) or result.get("status") not in ("checked", "failed", "completed-with-findings")
                or not isinstance(result.get("errors"), list)):
            raise ValueError("Invalid per-board audit result.")
        for error in result["errors"]:
            if (not isinstance(error, dict) or not isinstance(error.get("message"), str)
                    or not isinstance(error.get("stage"), str) or error.get("table") not in (None, "table1", "table2")):
                raise ValueError("Invalid per-board audit error.")
        if bool(result["errors"]) != (result["status"] == "failed"):
            raise ValueError("Board audit status does not match its errors.")
        record["audit_result"] = copy.deepcopy(result)
    if raw.get("dds") is not None:
        record["dds"] = validate_diagnostics(raw["dds"], record["dealer"], record["vulnerability"])
    tables = raw.get("tables", [])
    if not isinstance(tables, list) or len(tables) > 2:
        raise ValueError("A review board supports at most two comparison tables.")
    for table_index, table in enumerate(tables):
        if not isinstance(table, dict) or not isinstance(table.get("auction", []), list):
            raise ValueError("Invalid imported auction.")
        if table.get("error") is not None and (not isinstance(table["error"], dict) or not isinstance(table["error"].get("message"), str)):
            raise ValueError("Invalid table execution error.")
        calls, steps = [], []
        actor = Seat(record["dealer"])
        for entry in table.get("auction", []):
            step = {"call": entry} if isinstance(entry, str) else dict(entry)
            call = parse_call(step["call"])
            trace = step.get("private_trace", {})
            if not isinstance(trace, dict) or not isinstance(trace.get("features", []), list):
                raise ValueError("Invalid private trace.")
            if any(not isinstance(feature, dict) or not isinstance(feature.get("expression"), dict) for feature in trace.get("features", [])):
                raise ValueError("Invalid private trace features.")
            if "seat" in step and step["seat"] != actor.value:
                raise ValueError("An imported auction has the wrong acting seat.")
            calls.append(call)
            steps.append({**step, "call": str(call), "seat": actor.value})
            actor = actor.next()
        auction = replay(Seat(record["dealer"]), tuple(calls))
        final = contract_data(final_contract(auction)) if auction.ended else None
        if table.get("contract") is not None and table["contract"] != final:
            raise ValueError("Imported contract or declarer does not match its auction.")
        tricks, score = table.get("tricks"), table.get("score_ns")
        if tricks is not None and (type(tricks) is not int or not 0 <= tricks <= 13):
            raise ValueError("Tricks must be an integer from zero to thirteen.")
        if score is not None and type(score) is not int:
            raise ValueError("Scores must be integers or null.")
        systems = table.get("systems", {})
        if not isinstance(systems, dict) or any(not isinstance(system, dict) or not isinstance(system.get("name", ""), str)
                                              or not isinstance(system.get("hash", ""), str) for system in systems.values()):
            raise ValueError("Invalid system identities.")
        record["tables"].append({**table, "id": f"table{table_index + 1}", "auction": steps,
                                  "contract": final, "tricks": tricks, "score_ns": score,
                                  "systems": systems})
    if any(table.get("error") for table in record["tables"]) and record.get("audit_result", {}).get("status") != "failed":
        raise ValueError("A table execution error requires a failed per-board audit result.")
    record["id"] = raw.get("id") or canonical_hash({"source": source, "number": number, "hands": record["hands"]})[:24]
    update_comparison(record)
    if "dds" in record:
        dds_table = DDTable.from_dict(record["dds"]["table"])
        for table in record["tables"]:
            table["slam_diagnostics"] = table_slam_diagnostics(table, dds_table)
    return record


def update_comparison(record: dict) -> None:
    tables = record["tables"]
    if record.get("audit_result", {}).get("status") == "failed":
        record["imp"], record["point_difference"] = None, None
        record["difference"] = classify(record)
        return
    if len(tables) == 2 and all(table.get("score_ns") is not None for table in tables):
        difference = tables[0]["score_ns"] - tables[1]["score_ns"]
        if record.get("point_difference") is not None and record["point_difference"] != difference:
            raise ValueError("Imported point difference does not match table scores.")
        if record.get("imp") is not None and record["imp"] != signed_imp(difference):
            raise ValueError("Imported IMP does not match table scores.")
        record["point_difference"], record["imp"] = difference, signed_imp(difference)
    record["difference"] = classify(record)


def pack(records: list[dict], name: str, source: str, *, public_definitions: dict | None = None,
         prefer_saved_meanings: bool = False, **metadata) -> dict:
    if not records:
        raise ValueError("The import does not contain any complete boards.")
    for record in records:
        annotate_public_meanings(record, public_definitions, prefer_saved=prefer_saved_meanings)
    return {"schema": SCHEMA, "name": name, "source": source, **metadata,
            "statistics": statistics(records), "boards": records}


def import_json(contents: bytes, filename: str, public_definitions: dict | None = None) -> dict:
    text = contents.decode("utf-8-sig")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        lines = [line for line in text.splitlines() if line.strip()]
        first = json.loads(lines[0]) if lines else None
        value = first if isinstance(first, dict) and str(first.get("schema", "")).startswith("bridge-record-log/") \
            else [json.loads(line) for line in lines]
    if isinstance(value, dict) and str(value.get("schema", "")).startswith(("bridge-record-log/", "bridge-record/")):
        return pack([normalize_record(raw, index, filename) for index, raw in enumerate(decode_records(contents))],
                    filename, filename, public_definitions=public_definitions)
    if any(isinstance(item, dict) and item.get("schema") == "bridge-human-auction/v1"
           for item in (value if isinstance(value, list) else [value])):
        raise ValueError("这是人类参考JSONL，不是系统审计记录；请作为独立离线参考使用，未把它混入系统叫牌。")
    if any(isinstance(item, dict) and str(item.get("schema", "")).startswith("bridge-agent-")
           for item in (value if isinstance(value, list) else [value])):
        raise ValueError("这是精简分析视图，不是完整牌例；请导入完整或压缩记录。")
    if isinstance(value, dict) and value.get("schema") == SCHEMA:
        records = [normalize_record(raw, index, filename) for index, raw in enumerate(value["boards"])]
        return pack(records, value.get("name", filename), filename, systems=value.get("systems", {}),
                    score_perspective=value.get("score_perspective", "imported"), public_definitions=public_definitions)
    raw_records = value if isinstance(value, list) else [value]
    return pack([normalize_record(raw, index, filename) for index, raw in enumerate(raw_records)], filename, filename,
                public_definitions=public_definitions)


def import_pbn(contents: bytes, filename: str, public_definitions: dict | None = None) -> dict:
    text = re.sub(r'"(?:[^"\\]|\\.)*"|\{[^}]*\}',
                  lambda match: match[0] if match[0].startswith('"') else " ",
                  contents.decode("utf-8-sig"), flags=re.DOTALL)
    blocks, tags, tokens = [], {}, []
    in_auction = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("%"):
            continue
        match = re.fullmatch(r'\[(\w+)\s+("(?:[^"\\]|\\.)*")\]\s*', line)
        if match:
            key, value = match[1], json.loads(match[2])
            if key in ("Event", "Board", "Deal") and key in tags:
                blocks.append((tags, tokens))
                tags, tokens = {}, []
            tags[key] = value
            in_auction = key == "Auction"
        elif in_auction:
            tokens.extend(line.split())
    if tags:
        blocks.append((tags, tokens))
    records = []
    grouped = {}
    vulnerable = {"NONE": "none", "LOVE": "none", "-": "none", "NS": "ns", "EW": "ew", "ALL": "both", "BOTH": "both"}
    for tags, tokens in blocks:
        if "Deal" not in tags:
            raise ValueError("PBN记录缺少完整Deal标签。")
        deal = deal_from_pbn(tags["Deal"])
        dealer = tags.get("Dealer", tags.get("Auction", "N")).upper()
        if tags.get("Auction", dealer).upper() != dealer:
            raise ValueError("The PBN Auction start seat must match Dealer.")
        vul = vulnerable.get(tags.get("Vulnerable", "None").upper())
        if vul is None:
            raise ValueError("Unknown PBN vulnerability.")
        raw = {**board_data(Board(deal, Seat(dealer), Vulnerability(vul))),
               "number": int(tags.get("Board", len(records) + 1)), "tables": []}
        if "ReviewAuditResult" in tags:
            raw["audit_result"] = json.loads(tags["ReviewAuditResult"])
        if "Auction" in tags or "ReviewExecutionError" in tags:
            calls = []
            for token in tokens:
                if token in ("*", "-", "!") or re.fullmatch(r"\$\d+|=\d+=", token):
                    continue
                token = token.rstrip("!?").upper()
                if token == "AP":
                    while not replay(Seat(dealer), tuple(map(parse_call, calls))).ended:
                        calls.append("P")
                else:
                    calls.append({"PASS": "P", "DBL": "X", "RDBL": "XX"}.get(token, token))
            raw["tables"].append({"auction": calls, "systems": {
                side: {"name": tags.get(side + "System", side), "hash": tags.get(side + "SystemHash", "")}
                for side in ("NS", "EW")}, "tricks": int(tags["Result"]) if tags.get("Result", "").isdigit() else None,
                "score_ns": int(tags["ScoreNS"]) if tags.get("ScoreNS", "").lstrip("+-").isdigit() else None})
            if "ReviewExecutionError" in tags:
                raw["tables"][-1]["error"] = json.loads(tags["ReviewExecutionError"])
        record = normalize_record(raw, len(records), filename)
        key = canonical_hash({name: record[name] for name in ("number", "dealer", "vulnerability", "hands")})
        if key in grouped and len(grouped[key]["tables"]) < 2:
            for offset, table in enumerate(record["tables"], len(grouped[key]["tables"]) + 1):
                table["id"] = f"table{offset}"
            grouped[key]["tables"].extend(record["tables"])
            if record.get("audit_result"):
                grouped[key]["audit_result"] = record["audit_result"]
            update_comparison(grouped[key])
        else:
            grouped[key] = record
            records.append(record)
    return pack(records, filename, filename, public_definitions=public_definitions)


def import_audit_zip(archive: zipfile.ZipFile, filename: str) -> dict:
    members = {item.filename: item for item in archive.infolist()}
    if len(members) != len(archive.infolist()):
        raise ValueError("ZIP中存在重复文件。")
    reports = [name for name in members if Path(name).name == "audit-report.json"]
    if len(reports) != 1:
        raise ValueError("审计ZIP需要唯一的audit-report.json。")
    root = Path(reports[0]).parent
    if root.is_absolute() or ".." in root.parts:
        raise ValueError("审计ZIP路径无效。")
    report_bytes = archive.read(reports[0])
    report = json.loads(report_bytes)
    if not isinstance(report, dict) or report.get("schema") not in ("bridge-batch-audit/v1", "bridge-single-audit/v1"):
        raise ValueError("不支持的审计报告格式。")
    job_name = (root / "job.json").as_posix()
    if job_name in members:
        job = json.loads(archive.read(job_name))
        if not isinstance(job, dict) or job.get("report_sha256") != hashlib.sha256(report_bytes).hexdigest():
            raise ValueError("审计ZIP报告哈希不匹配。")
    evidence = report.get("evidence_sha256")
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("审计ZIP缺少文件校验信息。")
    systems = report.get("systems")
    if (not isinstance(systems, dict) or not systems
            or any(not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity) for identity in systems.values())):
        raise ValueError("审计ZIP体系身份无效。")
    identities = set(systems.values())
    allowed = {"input.json", "dataset-boards.jsonl", "boards.bridge.jsonl", "board.bridge.json"}
    allowed.update(f"systems/{identity}.json" for identity in identities if isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity))
    files = {}
    for name, digest in evidence.items():
        if name not in allowed or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("审计ZIP包含无效的证据身份。")
        member = (root / name).as_posix()
        if member not in members:
            raise ValueError(f"审计ZIP缺少{name}。")
        files[name] = archive.read(member)
        if hashlib.sha256(files[name]).hexdigest() != digest:
            raise ValueError(f"审计ZIP文件哈希不匹配：{name}。")
    definitions = {}
    for identity in identities:
        key = f"systems/{identity}.json"
        if key not in files:
            raise ValueError("审计ZIP缺少冻结体系。")
        system = validate_system(json.loads(files[key]))
        if system.content_hash != identity:
            raise ValueError("审计ZIP体系身份不匹配。")
        definitions[identity] = system
    batch = report["schema"] == "bridge-batch-audit/v1"
    record_name = "boards.bridge.jsonl" if batch else "board.bridge.json"
    if record_name not in files:
        raise ValueError("审计ZIP缺少牌例记录。")
    records = [normalize_record(raw, index, filename) for index, raw in enumerate(decode_records(files[record_name]))]
    if batch and len(records) != report.get("completed_boards"):
        raise ValueError("审计ZIP牌数与报告不一致。")
    metadata = {"purpose": "batch-audit" if batch else "single-board-audit"}
    if not batch:
        metadata["single_audit"] = report
    return pack(records, report.get("label", filename), filename, public_definitions=definitions,
                systems={role: {"name": definitions[identity].name, "hash": identity}
                         for role, identity in report["systems"].items()}, **metadata)


def import_file(contents: bytes, filename: str, public_definitions: dict | None = None) -> dict:
    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 64 * 1024 * 1024:
                raise ValueError("ZIP解压内容超过64MiB限制。")
            names = {Path(item.filename).name for item in archive.infolist()}
            if "audit-report.json" in names and "run.json" not in names:
                return import_audit_zip(archive, filename)
            required = ("run.json", "dataset-boards.jsonl", "boards.jsonl", "auctions.jsonl")
            files = {}
            for item in archive.infolist():
                name = Path(item.filename).name
                if name in required:
                    if name in files:
                        raise ValueError("ZIP中存在多个同名运行文件。")
                    files[name] = archive.read(item)
            if set(files) != set(required):
                raise ValueError("运行ZIP需要run.json、dataset-boards.jsonl、boards.jsonl和auctions.jsonl。")
            run = json.loads(files["run.json"])
            definitions = dict(public_definitions or {})
            for role in ("system_a", "system_b"):
                identity = run[role]["hash"]
                matches = [item for item in archive.infolist() if Path(item.filename).name == identity + ".json"]
                if len(matches) > 1:
                    raise ValueError("ZIP中存在重复的冻结体系定义。")
                if matches:
                    system = validate_system(json.loads(archive.read(matches[0])))
                    if system.content_hash != identity:
                        raise ValueError("ZIP中的冻结公开体系身份不匹配。")
                    definitions[identity] = system
            for name in required[1:]:
                expected = run.get("artifacts_sha256", {}).get(name)
                if expected and hashlib.sha256(files[name]).hexdigest() != expected:
                    raise ValueError(f"Imported ZIP hash mismatch: {name}.")
            bundle = assemble_run(run, *(decode_records(files[name]) for name in required[1:]), filename, explain=False)
            for record in bundle["boards"]:
                annotate_public_meanings(record, definitions)
            return bundle
    return (import_pbn(contents, filename, public_definitions) if filename.lower().endswith(".pbn")
            else import_json(contents, filename, public_definitions))


def export_pbn(records: list[dict]) -> str:
    output = []
    for record in records:
        parsed = parse_board({name: record[name] for name in ("dealer", "vulnerability", "hands")})
        for table in record.get("tables") or [None]:
            tags = {"Event": record.get("source", "Board review"), "Board": str(record["number"]),
                    "Dealer": record["dealer"], "Vulnerable": {"none": "None", "ns": "NS", "ew": "EW", "both": "All"}[record["vulnerability"]],
                    "Deal": deal_to_pbn(parsed.deal)}
            if record.get("audit_result"):
                tags["ReviewAuditResult"] = json.dumps(record["audit_result"], ensure_ascii=False)
            if table:
                if table.get("error"):
                    tags["ReviewExecutionError"] = json.dumps(table["error"], ensure_ascii=False)
                for side in ("NS", "EW"):
                    system = table.get("systems", {}).get(side, {})
                    tags[side + "System"] = system.get("name", side)
                    tags[side + "SystemHash"] = system.get("hash", "")
                if table.get("tricks") is not None:
                    tags["Result"] = str(table["tricks"])
                if table.get("score_ns") is not None:
                    tags["ScoreNS"] = str(table["score_ns"])
            output.extend(f'[{key} {json.dumps(value, ensure_ascii=False)}]' for key, value in tags.items())
            if table and table.get("auction"):
                output.append(f'[Auction "{record["dealer"]}"]')
                calls = ["Pass" if step["call"] == "P" else step["call"] for step in table["auction"]]
                output.extend(" ".join(calls[index:index + 4]) for index in range(0, len(calls), 4))
                output.append("*")
            output.append("")
    return "\n".join(output)


def describe_contract(table: dict) -> str:
    contract = table.get("contract")
    if not contract:
        return "未结束"
    if contract.get("passed_out"):
        return "全Pass"
    result = ""
    if table.get("tricks") is not None:
        delta = table["tricks"] - contract["level"] - 6
        result = "=" if not delta else f"+{delta}" if delta > 0 else str(delta)
    return f'{contract["level"]}{contract["strain"]}{ {1: "", 2: "X", 4: "XX"}[contract["doubling"]]} by {contract["declarer"]} {result}'.strip()


def review_markdown(bundle: dict, records: list[dict]) -> str:
    counts = bundle["statistics"]
    lines = ["# CCBA / 精确翻译v2：差异牌例审查", "", f'来源：{bundle["name"]}。所有叫牌均为冻结规则的原始输出，未修正或重算策略。',
             "", f'{counts["boards"]}副中，叫牌序列不同{counts["different_auctions"]}副，定约不同{counts["different_contracts"]}副，IMP非零{counts["nonzero_imp"]}副；叫牌不同但同分{counts["same_score_different_auction"]}副。',
             "", "桌1：CCBA=NS、精确=EW；桌2：精确=NS、CCBA=EW。IMP为CCBA视角，分数统一NS视角。副号从1开始，index从0开始。", "",
             "本批最终测试牌已进入逐牌人工审查，不再用于后续修改的独立最终确认。公开历史报告保留；审查日志含四手牌及私有trace，仅供牌后使用。", ""]
    vul = {"none": "双方无局", "ns": "南北有局", "ew": "东西有局", "both": "双方有局"}
    for record in records:
        lines.extend([f'## 第{record["number"]}副（index={record["index"]}，IMP={record.get("imp", 0):+d}）', "",
                      f'发牌：{record["dealer"]}；{vul[record["vulnerability"]]}。', "",
                      "| 座位 | S.H.D.C手牌 | HCP |", "| --- | --- | ---: |"])
        for seat in SEATS:
            lines.append(f'| {seat} | {record["hands"][seat]} | {record["hand_summary"][seat]["hcp"]} |')
        lines.extend(["", "| 桌 | 最终定约与结果 | DDS墩数 | NS分数 |", "| --- | --- | ---: | ---: |"])
        for index, table in enumerate(record["tables"], 1):
            lines.append(f'| {index} | {describe_contract(table)} | {table.get("tricks")} | {table.get("score_ns")} |')
        for index, table in enumerate(record["tables"], 1):
            lines.extend(["", f'### 桌{index}完整叫牌', "", "| 步 | 座位 | 叫品 | 状态 | 命中规则（私有） |", "| ---: | --- | --- | --- | --- |"])
            for turn, step in enumerate(table["auction"], 1):
                lines.append(f'| {turn} | {step["seat"]} | {step["call"]} | {step.get("state", "")} | {step.get("private_trace", {}).get("rule_id", "")} |')
        lines.append("")
    return "\n".join(lines)


def export_review(directory: Path, destination: Path) -> dict:
    bundle = load_run(directory)
    ranked = sorted((record for record in bundle["boards"] if record["difference"]["auction"]),
                    key=lambda record: (-abs(record.get("imp") or 0), record["number"]))
    selected = ranked[:8]
    for group in ([record for record in ranked if 0 < abs(record.get("imp") or 0) <= 4][:2],
                  [record for record in ranked if not record["difference"]["score"]][:2]):
        for record in group:
            if record not in selected:
                selected.append(record)
    destination.mkdir(parents=True, exist_ok=False)
    review = {**bundle, "boards": selected, "statistics": statistics(selected), "run_statistics": bundle["statistics"]}
    (destination / "cases.bridge.json").write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    (destination / "cases.pbn").write_text(export_pbn(selected), encoding="utf8")
    (destination / "review.md").write_text(review_markdown(bundle, selected), encoding="utf8")
    return {"statistics": bundle["statistics"], "selected_numbers": [record["number"] for record in selected], "output": str(destination)}


def save_document(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8")
    temporary.replace(path)


def custom_deal(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("自定义牌例请求必须是对象。")
    kind = payload.get("format", "hands")
    dealer, vulnerability = payload.get("dealer", "N"), payload.get("vulnerability", "none")
    if kind in ("pbn", "json") and not isinstance(payload.get("text"), str):
        raise ValueError("粘贴内容须为文本。")
    if kind == "random":
        seed = payload.get("seed")
        if type(seed) is not int or not 0 <= seed <= 2**53 - 1:
            raise ValueError("随机种子须为0到9007199254740991之间的整数。")
        deck = list(full_deck())
        random.Random(seed).shuffle(deck)
        deal = Deal(*(Hand(tuple(deck[start:start + 13])) for start in (0, 13, 26, 39)))
        raw = board_data(Board(deal, Seat(dealer), Vulnerability(vulnerability)))
    elif kind == "pbn":
        text = payload.get("text", "").strip()
        if text.startswith("["):
            imported = import_pbn(text.encode(), "custom.pbn")
            if len(imported["boards"]) != 1:
                raise ValueError("编辑器一次只接受一副完整PBN牌例；多副请使用文件导入。")
            raw = imported["boards"][0]
            raw = {key: raw[key] for key in ("dealer", "vulnerability", "hands")}
        else:
            raw = board_data(Board(deal_from_pbn(text.upper().replace("10", "T")), Seat(dealer), Vulnerability(vulnerability)))
    elif kind == "json":
        raw = json.loads(payload.get("text", ""))
        raw = raw.get("board", raw) if isinstance(raw, dict) else raw
        if not isinstance(raw, dict):
            raise ValueError("请粘贴一副包含dealer、vulnerability、hands的引擎JSON。")
        raw = {key: raw[key] for key in ("dealer", "vulnerability", "hands")}
    elif kind == "hands":
        hands = payload.get("hands")
        if not isinstance(hands, dict) or set(hands) != set(SEATS) or any(not isinstance(value, str) for value in hands.values()):
            raise ValueError("请填写N、E、S、W四手牌。")
        normalized = {}
        for seat, value in hands.items():
            fields = re.sub(r"\s+", "", value).upper().replace("10", "T").split(".")
            normalized[seat] = ".".join(field or "-" for field in fields)
        raw = {"dealer": dealer, "vulnerability": vulnerability, "hands": normalized}
    else:
        raise ValueError("未知的牌例输入格式。")
    try:
        record = normalize_record({**raw, "number": payload.get("number", 1), "label": payload.get("label", "")}, 0, "custom-deals")
    except (ValueError, SystemError) as error:
        raise ValueError(f"手牌校验失败（每家13张、全副52张且不得重复）：{error}") from error
    return record


def human_auction(payload: dict) -> dict:
    raw = payload.get("board")
    if not isinstance(raw, dict):
        raise ValueError("参考叫牌需要完整手牌、发牌人和局况。")
    board = parse_board({key: raw[key] for key in ("dealer", "vulnerability", "hands")})
    values = payload.get("calls", "")
    if isinstance(values, str):
        if len(values) > 16000:
            raise ValueError("参考叫牌文本不能超过16000字符。")
        tokens = [token for token in re.split(r"[\s,，;；|]+", values.strip()) if token]
    elif isinstance(values, list) and all(isinstance(item, str) for item in values):
        tokens = values
    else:
        raise ValueError("参考序列须为叫品文本或字符串数组。")
    if len(tokens) > 1024:
        raise ValueError("参考叫牌最多1024个输入项。")
    auction = initial_auction(board.dealer)
    steps = []

    def append(call):
        nonlocal auction
        seat = auction.next_seat
        auction = apply_call(auction, call)
        steps.append({"seat": seat.value, "call": str(call)})

    for token in tokens:
        normalized = token.strip().upper()
        for symbol, suit in (("♠", "S"), ("♥", "H"), ("♦", "D"), ("♣", "C")):
            normalized = normalized.replace(symbol, suit)
        normalized = {"PASS": "P", "DBL": "X", "RDBL": "XX", "不叫": "P"}.get(normalized, normalized)
        if re.fullmatch(r"[1-7]N", normalized):
            normalized += "T"
        try:
            if auction.ended:
                raise ValueError("叫牌已经结束，不能再追加叫品。")
            if normalized == "AP":
                while not auction.ended:
                    append(parse_call("P"))
            else:
                append(parse_call(normalized))
        except (ValueError, SystemError) as error:
            raise ValueError(f"第{len(steps) + 1}叫（{token}）无效：{error}") from error
    return {"board": board_data(board), "deal_key": canonical_hash(board_data(board)),
            "auction": steps, "calls": [step["call"] for step in steps], "complete": auction.ended,
            "next_seat": None if auction.ended else auction.next_seat.value,
            "contract": contract_data(final_contract(auction)) if auction.ended else None}


def sampling_config(payload: dict, *, generate_seed: bool = True) -> dict:
    def number(value, name, low, high):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name}须为{low}到{high}的整数。")
        return value

    count = number(payload.get("count", 100), "牌数", 1, 1000)
    workers = min(count, number(payload.get("workers", 1 if count < 64 else min(2, os.cpu_count() or 1)),
                                "审计进程数", 1, 4))
    seed = payload.get("seed")
    if seed is None or isinstance(seed, str) and not seed.strip():
        seed = random.SystemRandom().randrange(2**53) if generate_seed else None
    else:
        seed = number(seed, "种子", 0, 2**53 - 1)
    limit = number(payload.get("attempt_limit", max(10000, count * 1000)), "发牌尝试上限", count, 1000000)
    dealer, vulnerability = payload.get("dealer", "cycle"), payload.get("vulnerability", "cycle")
    if dealer not in (*SEATS, "cycle") or vulnerability not in ("none", "ns", "ew", "both", "cycle"):
        raise ValueError("发牌人或局况无效。")
    raw = payload.get("constraints", {})
    if not isinstance(raw, dict) or set(raw) - set(SEATS):
        raise ValueError("发牌条件只接受N/E/S/W四家。")
    constraints = {}
    for seat in SEATS:
        values = raw.get(seat, {})
        if not isinstance(values, dict) or set(values) - {"hcp", *SUITS}:
            raise ValueError(f"{seat}条件只接受hcp及S/H/D/C。")
        constraints[seat] = {}
        for field in ("hcp", *SUITS):
            maximum = 40 if field == "hcp" else 13
            interval = values.get(field, [0, maximum])
            if not isinstance(interval, list) or len(interval) != 2:
                raise ValueError(f"{seat}/{field}须为[下界,上界]。")
            low, high = (number(value, f"{seat}/{field}", 0, maximum) for value in interval)
            if low > high:
                raise ValueError(f"{seat}/{field}下界不能大于上界。")
            constraints[seat][field] = [low, high]
        if not sum(constraints[seat][suit][0] for suit in SUITS) <= 13 <= sum(constraints[seat][suit][1] for suit in SUITS):
            raise ValueError(f"{seat}四套长度条件不能组成13张牌。")
    for field in ("hcp", *SUITS):
        total = 40 if field == "hcp" else 13
        if not sum(constraints[seat][field][0] for seat in SEATS) <= total <= sum(constraints[seat][field][1] for seat in SEATS):
            raise ValueError(f"四家{field}上下界不可能合计为{total}。")
    return {"count": count, "seed": seed, "workers": workers, "attempt_limit": limit, "dealer": dealer,
            "vulnerability": vulnerability, "constraints": constraints}


class AuditCancelled(RuntimeError):
    pass


def audit_error(stage: str, error: Exception, table: str | None = None) -> dict:
    return {"stage": stage, "table": table, "type": type(error).__name__, "message": str(error) or type(error).__name__,
            **{key: getattr(error, key) for key in ("actor", "system_hash") if getattr(error, key, None) is not None}}


def sampled_boards(config: dict, progress=None, cancelled=None):
    rng = random.Random(config["seed"])
    deck = list(full_deck())
    families = set()
    accepted = 0
    hcp = {"A": 4, "K": 3, "Q": 2, "J": 1}
    for attempt in range(1, config["attempt_limit"] + 1):
        if cancelled is not None and cancelled():
            raise AuditCancelled("任务已取消，已接受的牌和完成的记录保留，不发布部分成绩。")
        shuffled = list(deck)
        rng.shuffle(shuffled)
        chunks = [shuffled[start:start + 13] for start in (0, 13, 26, 39)]
        valid = True
        for seat, cards in zip(SEATS, chunks):
            actual = {"hcp": sum(hcp.get(card.rank, 0) for card in cards),
                      **{suit: sum(card.suit == suit for card in cards) for suit in SUITS}}
            if any(not low <= actual[field] <= high for field, (low, high) in config["constraints"][seat].items()):
                valid = False
                break
        if valid:
            deal = Deal(*(Hand(tuple(cards)) for cards in chunks))
            family = family_hash(deal)
            if family not in families:
                families.add(family)
                dealer = Seat(SEATS[accepted % 4] if config["dealer"] == "cycle" else config["dealer"])
                vulnerability = Vulnerability(
                    list(Vulnerability)[((accepted // 4) + accepted) % 4].value
                    if config["vulnerability"] == "cycle" else config["vulnerability"])
                board = Board(deal, dealer, vulnerability)
                accepted += 1
                if progress:
                    progress(attempt, accepted)
                yield {"index": accepted - 1, "family_id": family, "board": board_data(board)}
                if accepted == config["count"]:
                    return
        if progress and attempt % 256 == 0:
            progress(attempt, accepted)
    raise ValueError(f"已尝试{config['attempt_limit']}次，只找到{accepted}/{config['count']}副满足条件的不同牌族；未放宽条件或替换失败牌，请放宽条件或增加尝试上限。")


@lru_cache(maxsize=1)
def bounds_checker():
    spec = importlib.util.spec_from_file_location("viewer_classic_checks", ROOT / "tools/verify_classics.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the existing public/private bounds checker.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify_bounds


def audit_checks(record: dict, board: Board, systems: dict, oracle: dict, dds_verification: dict, dds_table: dict | None) -> tuple[list[dict], list[dict]]:
    checks, findings = [], []
    verify_bounds = bounds_checker()
    reference = DDTable.from_dict(dds_table) if dds_table is not None else None
    for table in record["tables"]:
        name = table["id"]
        if table.get("error"):
            checks.append({"id": f"{name}-execution", "status": "failed", "detail": table["error"]["message"]})
            continue
        calls = tuple(parse_call(step["call"]) for step in table["auction"])
        auction = replay(board.dealer, calls)
        if not auction.ended or contract_data(final_contract(auction)) != table["contract"]:
            raise ValueError(f"{name}: 叫牌未结束或定约/庄位不一致。")
        checks.append({"id": f"{name}-legality", "status": "passed", "detail": f"{name}：逐叫合法性、行动座位、终局定约及庄家核对。"})
        repeated = run_auction(*(systems[table["systems"][side]["hash"]] for side in ("NS", "EW")), board, compact=True)
        if ([step["call"] for step in repeated["auction"]] != [step["call"] for step in table["auction"]]
                or repeated["contract"] != table["contract"]):
            raise ValueError(f"{name}: 同一冻结体系的确定性复跑不一致。")
        checks.append({"id": f"{name}-determinism", "status": "passed", "detail": f"{name}：同牌、同体系独立重新叫牌一致。"})
        try:
            count = verify_bounds(table["auction"], board)
            checks.append({"id": f"{name}-bounds", "status": "passed" if count else "not-applicable", "count": count,
                           "detail": f"{name}：{count}次决策的公开/私有点力、套长上下界、关系及单调性检查；未编码项不计入。"})
        except ValueError as error:
            checks.append({"id": f"{name}-bounds", "status": "failed", "detail": str(error),
                           **{key: getattr(error, key) for key in ("actor", "system_hash") if getattr(error, key, None) is not None}})
        warnings = [(index + 1, step["public_explanation"]["warnings"]) for index, step in enumerate(table["auction"])
                    if step["public_explanation"]["warnings"]]
        checks.append({"id": f"{name}-public-replay", "status": "warning" if warnings else "passed",
                       "detail": f"{name}：公开协议重建与逐叫快照核对；{len(warnings)}次叫牌有来源提示。", "warnings": warnings})
        if reference is not None:
            score, tricks = table_score(table, board, reference)
            if (score, tricks) != (table["score_ns"], table["tricks"]):
                raise ValueError(f"{name}: 重新计分或DDS庄位/花色查询不一致。")
        bids = [(index, step) for index, step in enumerate(table["auction"]) if re.fullmatch(r"[1-7](C|D|H|S|NT)", step["call"])]
        if bids:
            index, step = bids[-1]
            rule = step.get("private_trace", {}).get("rule_id", "")
            category = ("artificial-splinter-terminal-3nt" if ".fit.splinter_" in rule and step["call"] == "3NT"
                        else "precision-shortness-terminal-suit" if any(f".precision.club_short_{suit}." in rule for suit in ("H", "S"))
                        else None)
            if category and [item["call"] for item in table["auction"][index + 1:]] == ["P", "P", "P"]:
                findings.append({"id": category, "table": name, "turn": index,
                                 "description": "已知人工短套显示后被连续三次Pass停成终局；需要检查协议后续，不能当作自然终局正确性通过。",
                                 "call": step["call"], "public_meaning": step["public_explanation"]["summary"]})
    real_dds = oracle.get("oracle_kind") == "dds"
    checks.append({"id": "real-dds", "status": "passed" if real_dds else "failed",
                   "detail": "真实来源DDS计分；禁止fixture或零分兜底。" if real_dds else f"DDS不可用，未计分：{oracle.get('detail', '')}"})
    checks.append({"id": "fresh-dds", "status": "passed" if dds_verification.get("fresh_crosscheck") else "not-requested",
                   "detail": "缓存20项与新鲜DDS求解逐项一致。" if dds_verification.get("fresh_crosscheck") else "未执行绕过缓存的新鲜DDS交叉核验。"})
    if real_dds:
        failed = any(check["status"] == "failed" for check in checks)
        checks.append({"id": "scoring", "status": "not-applicable" if failed else "passed",
                       "detail": "本副存在执行或检查失败，不发布有效配对成绩；成功桌次仅保留诊断。"
                       if failed else "按实际庄位/花色查询DDS，重算两桌NS分数、分差及逐副IMP；使用核心计分API，不冒称独立公式实现。"})
    return checks, findings


def single_audit_markdown(report: dict, record: dict | None) -> str:
    lines = ["# 单副开发审计报告", "", f"任务：{report['id']}", f"生成时间：{report.get('finished_at', report['created_at'])}",
             f"状态：{report['status']}", "", "仅为牌后开发诊断，不是独立保留测试、整批契约验证或体系强度/正式验收结论。"]
    if report.get("error"):
        lines.extend(["", "## 执行错误", "", report["error"]])
    if record:
        lines.extend(["", f"## 第{record['number']}副 {record.get('label', '')}", "",
                      f"发牌：{record['dealer']}；局况：{record['vulnerability']}；IMP为桌1_NS−桌2_NS，A坐桌1_NS/桌2_EW。", "",
                      "| 座位 | S.H.D.C | HCP | S-H-D-C |", "| --- | --- | --- | --- |"])
        for seat in SEATS:
            summary = record["hand_summary"][seat]
            lines.append(f"| {seat} | {record['hands'][seat]} | {summary['hcp']} | {'-'.join(map(str, summary['shape']))} |")
        for table in record["tables"]:
            lines.extend(["", f"### {table['id']}：{describe_contract(table)}", "",
                          f"NS：{table['systems']['NS']['name']}；EW：{table['systems']['EW']['name']}。",
                          f"NS分数：{table['score_ns']}。", ""])
            lines.extend(f"{index + 1}. {step['public_explanation']['summary']}" for index, step in enumerate(table["auction"]))
        lines.extend(["", f"两桌分差：{record['point_difference']}；配对IMP：{record['imp']}。"])
    lines.extend(["", "## 检查", ""])
    lines.extend(f"- [{check['status']}] {check['detail']}" for check in report.get("checks", []))
    lines.extend(["", "## 定向语义发现", ""])
    lines.extend(f"- {item['table']} 第{item['turn'] + 1}叫：{item['description']}" for item in report.get("findings", []))
    if not report.get("findings"):
        lines.append("未命中已登记的两类人工终局模式；不等于全面语义正确。")
    if report.get("source_comparison"):
        lines.extend(["", "## 与原记录对照", "", json.dumps(report["source_comparison"], ensure_ascii=False, indent=2)])
    if report.get("dds_table"):
        lines.extend(["", "## 双明手墩数（牌后参考，不进入策略观察）", "",
                      "| 庄位 | C | D | H | S | NT |", "| --- | --- | --- | --- | --- | --- |"])
        for seat, values in report["dds_table"].items():
            lines.append(f"| {seat} | " + " | ".join(str(values[suit]) for suit in ("C", "D", "H", "S", "NT")) + " |")
    if report.get("dds"):
        par = report["dds"]["par"]
        lines.extend(["", "## Par（双方全知最优竞价，含牺牲）", "", f"NS视角分数：{par['score_ns']}。"])
        lines.extend(f"- {item['level']}{item['strain']}{'X' if item['doubling'] == 2 else ''} · {'/'.join(item['declarers'])} · {item['tricks']}墩"
                     for item in par["contracts"])
        if par["passed_out"]:
            lines.append("- 全Pass。")
    lines.extend(["", "## 来源与边界", "", "原冻结运行及审计结论不改写。体系IR、手牌、逐叫记录和来源哈希随ZIP保存；失败不替换成Pass或零分。",
                  "", f"运行期源码：{report.get('runtime_source_hash', '未完成快照')}；体系身份与文件校验见机器报告。", ""])
    return "\n".join(lines)


class BoardAuditEngine:
    def __init__(self, project: Path, systems: dict):
        self.project = project.resolve()
        self.systems = systems

    def _solve_oracle(self, board: Board, number: int, fresh_dds: bool, collect_errors: bool) -> tuple:
        table, diagnostics = None, None
        oracle = {"oracle_kind": "unavailable", "detail": ""}
        verification = {"fresh_crosscheck": False}
        errors = []
        try:
            adapter = DDSAdapter()
            with DDSCache(self.project / ".runtime/dds/tables.sqlite", adapter) as cache:
                table = cache.solve(board.deal)
                verification.update(cache_hits=cache.hits, cache_solves=cache.solves)
            if fresh_dds:
                if adapter.solve(board.deal) != table:
                    raise ValueError("Fresh DDS disagrees with the provenance-keyed cache.")
                verification["fresh_crosscheck"] = True
            oracle = adapter.provenance.to_dict()
            diagnostics = adapter.diagnostics(table, board.dealer, board.vulnerability)
        except DDSUnavailableError as error:
            oracle = {"oracle_kind": "unavailable", "detail": str(error)}
            table, diagnostics = None, None
            if collect_errors:
                LOGGER.exception("Board %s DDS unavailable", number)
                errors.append(audit_error("dds", error))
        except (DDSError, ValueError, OSError) as error:
            if not collect_errors:
                raise
            LOGGER.exception("Board %s DDS/par verification failed", number)
            errors.append(audit_error("dds", error))
            oracle = {"oracle_kind": "unavailable", "detail": str(error)}
            table, diagnostics = None, None
        return table, diagnostics, oracle, verification, errors

    def compare(self, payload: dict, *, fresh_dds: bool = False, progress=None, collect_errors: bool = False) -> dict:
        a_id, b_id = payload["system_a"], payload["system_b"]
        if a_id not in self.systems or b_id not in self.systems:
            raise ValueError("Comparison requires two registered general systems.")
        board = parse_board({key: payload["board"][key] for key in ("dealer", "vulnerability", "hands")})
        a, b = self.systems[a_id]["system"], self.systems[b_id]["system"]
        errors, tables = [], []
        number = payload["board"].get("number", 1)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="board-dds") as executor:
            oracle_future = executor.submit(self._solve_oracle, board, number, fresh_dds, collect_errors)
            for index, (ns, ew) in enumerate(((a, b), (b, a)), 1):
                if progress:
                    progress(f"第{index}桌叫牌；真实DDS独立线程并行求解", 15 + 15 * (index - 1))
                try:
                    result = run_auction(ns, ew, board, compact=True, trace=True)
                except AuditCancelled:
                    raise
                except Exception as error:
                    if not collect_errors:
                        raise
                    LOGGER.exception("Board %s table%s auction failed", number, index)
                    detail = audit_error("auction", error, f"table{index}")
                    errors.append(detail)
                    result = {"auction": [], "contract": None, "error": detail}
                tables.append({"id": f"table{index}", "systems": {
                    side: {"name": system.name, "hash": system.content_hash}
                    for side, system in (("NS", ns), ("EW", ew))},
                    "auction": result["auction"], "contract": result["contract"], "tricks": None, "score_ns": None,
                    "partnership_contexts": result.get("partnership_contexts", {}),
                    **({"error": result["error"]} if result.get("error") else {})})
            raw = {**board_data(board), "number": number, "index": payload["board"].get("index", 0),
                   "label": payload["board"].get("label", ""), "tables": tables}
            annotate_public_meanings(raw, {a_id: a, b_id: b})
            if progress:
                progress("两桌叫牌完成，等待并行DDS与来源核验", 45)
            table, diagnostics, oracle, verification, oracle_errors = oracle_future.result()
            errors.extend(oracle_errors)
        for played in tables:
            if table is None or played["contract"] is None:
                continue
            try:
                played["score_ns"], played["tricks"] = table_score(played, board, table)
            except (ValueError, RuntimeError, KeyError) as error:
                if not collect_errors:
                    raise
                LOGGER.exception("Board %s %s scoring failed", number, played["id"])
                detail = audit_error("score", error, played["id"])
                errors.append(detail)
                played["error"] = detail
        if errors:
            raw["audit_result"] = {"status": "failed", "errors": errors}
        if diagnostics is not None:
            raw["dds"] = diagnostics
        record = normalize_record(raw, 0, "interactive-comparison")
        if progress:
            progress("两桌叫牌、真实DDS与计分完成", 60)
        return {"record": record, "oracle": oracle, "evaluation": "interactive-post-hoc-not-a-formal-run",
                "dds_table": table.to_dict() if table is not None else None, "dds_verification": verification,
                "execution_errors": errors}

    def batch_board(self, raw: dict, request: dict, definitions: dict, *, fresh_dds: bool) -> tuple[dict, list, list]:
        index = raw["index"]
        payload = {"board": {**raw["board"], "index": index, "number": index + 1},
                   "system_a": request["system_a"], "system_b": request["system_b"]}
        try:
            result = self.compare(payload, fresh_dds=fresh_dds, collect_errors=True)
        except AuditCancelled:
            raise
        except Exception as error:
            LOGGER.exception("Batch board %s could not return a complete record", index + 1)
            detail = audit_error("execution", error)
            tables = []
            for ns_id, ew_id in ((request["system_a"], request["system_b"]), (request["system_b"], request["system_a"])):
                tables.append({"auction": [], "error": detail, "systems": {
                    side: {"hash": identity, "name": definitions[identity].name}
                    for side, identity in (("NS", ns_id), ("EW", ew_id))}})
            record = normalize_record({**payload["board"], "tables": tables,
                                       "audit_result": {"status": "failed", "errors": [detail]}}, index, "batch-board")
            return record, [], []
        record = result["record"]
        errors = list(result.get("execution_errors", []))
        checks, found = [], []
        try:
            checks, found = audit_checks(record, parse_board(raw["board"]), definitions, result["oracle"],
                                         result["dds_verification"], result["dds_table"])
            for check in checks:
                if check["status"] != "failed":
                    continue
                if check.get("id") == "real-dds" and any(error["stage"] == "dds" for error in errors):
                    continue
                table = next((name for name in ("table1", "table2") if check.get("id", "").startswith(name + "-")), None)
                if not any(error.get("table") == table and error["message"] == check["detail"] for error in errors):
                    errors.append({**audit_error("checks", ValueError(check["detail"]), table),
                                   "check_id": check.get("id"),
                                   **{key: check[key] for key in ("actor", "system_hash") if key in check}})
        except AuditCancelled:
            raise
        except Exception as error:
            LOGGER.exception("Batch board %s audit checks failed", index + 1)
            errors.append(audit_error("checks", error))
        if not errors and record.get("imp") is None:
            errors.append(audit_error("scoring", ValueError("两桌未生成完整有效的配对成绩。")))
        record["audit_result"] = {"status": "failed" if errors else "completed-with-findings" if found else "checked",
                                  "errors": errors, "checks": checks, "dds_verification": result["dds_verification"]}
        update_comparison(record)
        return record, checks, found


class ViewerState(BoardAuditEngine):
    def __init__(self, project: Path = ROOT, initial: str = "ccba-prec-translation-v4-1000"):
        super().__init__(project, {})
        self.initial = initial
        self.runs = {}
        self.cache = {}
        self.audit_cache: tuple[str, str, dict] | None = None
        self.index_cache = OrderedDict()
        self.index_lock = threading.RLock()
        self.lock = threading.Lock()
        self.workspace = self.project / ".runtime/board-viewer"
        self.jobs_lock = threading.RLock()
        library_path = self.workspace / "library.json"
        self.library = json.loads(library_path.read_text(encoding="utf8")) if library_path.is_file() else {}
        if (not isinstance(self.library, dict) or any(
                not isinstance(key, str) or not isinstance(value, dict)
                or set(value) - {"name", "category", "trashed_at", "updated_at"}
                or any(not isinstance(item, str) for item in value.values())
                for key, value in self.library.items())):
            raise ValueError("审计整理信息格式无效。")
        self.jobs = {}
        self.evolution_jobs = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="board-audit")
        self.runtime_snapshot = source_snapshot()
        self.execution_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in (Path(__file__), ROOT / "tools/verify_classics.py")}
        for path in sorted((self.workspace / "audits").glob("audit-*/job.json")):
            job = json.loads(path.read_text(encoding="utf8"))
            if not AUDIT_ID.fullmatch(path.parent.name) or job["id"] != path.parent.name:
                raise ValueError(f"Invalid persisted audit identity: {path}.")
            if job["status"] in ("queued", "running"):
                job.update(status="interrupted", stage="服务重启中断", error="上次任务未完成；请重新发起，不把中断任务标为通过。", finished_at=timestamp())
                save_document(path, job)
            self.jobs[job["id"]] = job
        for path in sorted((self.project / "results").glob("*/run.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf8"))
                if manifest.get("status") == "completed":
                    self.runs[path.parent.name] = {"path": path.parent, "manifest": manifest}
            except (OSError, ValueError):
                continue
        for path in sorted((self.project / "systems").rglob("*.json")):
            try:
                system = load_system(path)
                if system.scope == "general":
                    self.systems.setdefault(system.content_hash, {"path": path, "system": system})
            except (OSError, ValueError, RuntimeError):
                continue
        for entry in self.runs.values():
            for role in ("system_a", "system_b"):
                content_hash = entry["manifest"][role]["hash"]
                if content_hash in self.systems:
                    continue
                if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
                    raise ValueError("Invalid frozen system identity.")
                path = entry["path"] / "systems" / f"{content_hash}.json"
                system = load_system(path)
                if system.scope != "general" or system.content_hash != content_hash:
                    raise ValueError(f"Frozen system identity mismatch: {path}.")
                self.systems[content_hash] = {"path": path, "system": system}

    def catalog(self, *, include_trash: bool = False) -> dict:
        custom = list((self.workspace / "deals").glob("*.json"))
        with self.jobs_lock:
            audits = [dict(job) for job in self.jobs.values() if job["status"] == "completed"]
        result = {
            "default_run": self.initial if self.initial in self.runs else next(iter(self.runs), None),
            "runs": [{"id": key, "name": entry["manifest"]["experiment_id"], "boards": entry["manifest"]["boards"],
                      "split": entry["manifest"].get("dataset_split"), "status": "completed",
                      "purpose": run_purpose(entry["manifest"])}
                     for key, entry in self.runs.items()]
                    + ([{"id": "custom-deals", "name": "自定义牌例库", "boards": len(custom), "status": "completed", "purpose": "custom-deals"}] if custom else [])
                    + [{"id": job["id"], "name": job["label"], "boards": job.get("count", 1), "status": "completed",
                        "purpose": "batch-audit" if job.get("kind") == "batch" else "single-board-audit"}
                       for job in sorted(audits, key=lambda value: value["created_at"], reverse=True)],
            "systems": [{"id": content_hash, "name": entry["system"].name,
                         "path": entry["path"].relative_to(self.project).as_posix()}
                        for content_hash, entry in self.systems.items()],
        }
        with self.jobs_lock:
            for run in result["runs"]:
                metadata = self.library.get(run["id"], {})
                run.update(original_name=run["name"], name=metadata.get("name", run["name"]),
                           category=metadata.get("category", ""), trashed_at=metadata.get("trashed_at"))
            if not include_trash:
                result["runs"] = [run for run in result["runs"] if not run["trashed_at"]]
        if result["default_run"] not in {run["id"] for run in result["runs"]}:
            result["default_run"] = next((run["id"] for run in result["runs"]), None)
        return result

    def library_entries(self) -> dict:
        catalog = self.catalog(include_trash=True)
        entries = {run["id"]: dict(run) for run in catalog["runs"]}
        with self.jobs_lock:
            for job in self.jobs.values():
                metadata = self.library.get(job["id"], {})
                entries[job["id"]] = {
                    "id": job["id"], "name": metadata.get("name", job["label"]), "original_name": job["label"],
                    "boards": job.get("count", 1), "status": job["status"], "created_at": job["created_at"],
                    "purpose": "batch-audit" if job.get("kind") == "batch" else "single-board-audit",
                    "category": metadata.get("category", ""), "trashed_at": metadata.get("trashed_at"),
                    "can_open": job["status"] == "completed", "failed_boards": job.get("failed_boards", 0)}
        for entry in entries.values():
            entry.setdefault("can_open", True)
        for experiment in self.evolution_history(include_trash=True)["experiments"]:
            entries[experiment["id"]] = {**experiment, "boards": 0, "purpose": "evolution", "can_open": False}
        return {"entries": list(entries.values()),
                "categories": sorted({item["category"] for item in entries.values() if item["category"]})}

    def organize_library(self, payload: dict) -> dict:
        identifiers = payload.get("ids")
        action = payload.get("action")
        if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 1000
                or any(not isinstance(item, str) for item in identifiers) or len(set(identifiers)) != len(identifiers)
                or action not in ("update", "trash", "restore")):
            raise ValueError("请选择审计记录和有效操作。")
        if action == "trash" and payload.get("confirm") is not True:
            raise ValueError("删除前请确认移入回收站。")
        fields = {}
        if action == "update":
            if set(payload) - {"ids", "action", "name", "category"} or not any(key in payload for key in ("name", "category")):
                raise ValueError("只能修改名称或分类。")
            for key, maximum in (("name", 160), ("category", 80)):
                if key not in payload:
                    continue
                value = payload[key]
                if not isinstance(value, str) or len(value.strip()) > maximum or key == "name" and not value.strip():
                    raise ValueError(f"{'名称' if key == 'name' else '分类'}格式无效。")
                fields[key] = value.strip()
            if "name" in fields and len(identifiers) != 1:
                raise ValueError("一次只能重命名一条审计。")
        with self.jobs_lock:
            known = {entry["id"]: entry for entry in self.library_entries()["entries"]}
            if any(identifier not in known for identifier in identifiers):
                raise ValueError("审计记录不存在，未执行操作。")
            if action == "trash" and any(known[identifier]["status"] in ("queued", "running") for identifier in identifiers):
                raise ValueError("正在执行或排队的任务不能删除，请先取消。")
            updated = copy.deepcopy(self.library)
            for identifier in identifiers:
                item = updated.setdefault(identifier, {})
                if action == "trash":
                    item["trashed_at"] = timestamp()
                elif action == "restore":
                    item.pop("trashed_at", None)
                else:
                    item.update(fields)
                item["updated_at"] = timestamp()
            self.workspace.mkdir(parents=True, exist_ok=True)
            save_document(self.workspace / "library.json", updated)
            self.library = updated
        return self.library_entries()

    def require_visible(self, identifier: str) -> None:
        with self.jobs_lock:
            if self.library.get(identifier, {}).get("trashed_at"):
                raise ValueError("此审计已移入回收站，请先恢复。")

    def evolution_directory(self, identifier: str) -> Path:
        if not isinstance(identifier, str) or not re.fullmatch(r"evo-[0-9a-f]{32}", identifier):
            raise ValueError("无效的进化实验编号。")
        directory = self.project / ".runtime/evolution" / identifier
        if not (directory / "state.json").is_file():
            raise ValueError("找不到进化实验。")
        return directory

    def evolution_overview(self, directory: Path, *, details=False) -> dict:
        from bridge_evolve.evolution.inspection import overview
        result = overview(directory, details=details)
        future = self.evolution_jobs.get(directory.name)
        if result["status"] == "queued" and (future is None or future.done()):
            result.update(status="interrupted", error="排队任务已中断，可继续原实验。")
        return result

    def evolution_history(self, *, include_trash=False) -> dict:
        experiments = []
        for path in (self.project / ".runtime/evolution").glob("evo-*/state.json"):
            metadata = self.library.get(path.parent.name, {})
            if metadata.get("trashed_at") and not include_trash:
                continue
            experiment = self.evolution_overview(path.parent)
            experiment.update(original_name=experiment["name"], name=metadata.get("name", experiment["name"]), category=metadata.get("category", ""),
                              trashed_at=metadata.get("trashed_at"))
            experiments.append(experiment)
        return {"experiments": sorted(experiments, key=lambda value: value["created_at"], reverse=True)}

    def evolution_detail(self, identifier: str) -> dict:
        self.require_visible(identifier)
        result = self.evolution_overview(self.evolution_directory(identifier), details=True)
        result["name"] = self.library.get(identifier, {}).get("name", result["name"])
        return result

    def require_current_evolution_runtime(self) -> None:
        if source_snapshot() != self.runtime_snapshot or self.execution_hashes != {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (Path(__file__), ROOT / "tools/verify_classics.py")}:
            raise ValueError("源码已改变，请重启牌例台后再启动或继续进化。")

    def start_evolution(self, payload: dict) -> dict:
        from bridge_evolve.evolution.config import save_json
        from bridge_evolve.evolution.controller import initialize, status
        self.require_current_evolution_runtime()
        if set(payload) - {"name", "profile", "source", "model", "generations", "workers", "system_a", "system_b", "origins",
                           "reasoning_effort", "max_agent_requests", "max_agent_tokens", "max_output_tokens", "max_pairs",
                           "max_seconds", "max_research_turns", "max_tool_calls", "max_releases", "max_lab_boards",
                           "research_seconds", "research_task", "max_context_bytes", "plateau_limit", "research_seed_run", "champion_seed_run",
                           "benchmark_revision_reason", "model_retries", "model_retry_delay"}:
            raise ValueError("不支持的进化实验配置。")
        identities = [payload.get("system_a"), payload.get("system_b")]
        if any(not isinstance(identity, str) or identity not in self.systems for identity in identities):
            raise ValueError("请选择两个已登记体系。")
        if payload.get("source", "research") not in ("research", "agent", "numbers"):
            raise ValueError("界面只接受自主研究、旧版提案或确定性对照。")
        for key in ("research_seed_run", "champion_seed_run"):
            if payload.get(key) is not None:
                self.require_visible(payload[key])
                self.evolution_directory(payload[key])
        with self.jobs_lock:
            if any(value["status"] in ("queued", "running") for value in self.evolution_history()["experiments"]):
                raise ValueError("已有进化实验正在运行或排队。")
            paths = [self.systems[identity]["path"].resolve() for identity in identities]
            config = {**{key: value for key, value in payload.items() if key not in ("system_a", "system_b")},
                      "baselines": [str(path) for path in paths]}
            directory = initialize(config, project=self.project,
                                   preloaded={path: self.systems[identity]["system"] for path, identity in zip(paths, identities, strict=True)})
            state = status(directory)
            state.update(status="queued")
            save_json(directory / "state.json", state)
            self.evolution_jobs[directory.name] = self.executor.submit(self.execute_evolution, directory)
        return self.evolution_detail(directory.name)

    def resume_evolution(self, identifier: str) -> dict:
        from bridge_evolve.evolution.config import save_json
        from bridge_evolve.evolution.controller import Controller, status
        self.require_current_evolution_runtime()
        self.require_visible(identifier)
        directory = self.evolution_directory(identifier)
        Controller(directory)
        with self.jobs_lock:
            if any(value["status"] in ("queued", "running") for value in self.evolution_history()["experiments"]):
                raise ValueError("已有进化实验正在运行或排队。")
            state = status(directory)
            (directory / "cancel-requested").unlink(missing_ok=True)
            state.update(status="queued", phase="等待继续", updated_at=timestamp())
            save_json(directory / "state.json", state)
            self.evolution_jobs[identifier] = self.executor.submit(self.execute_evolution, directory)
        return self.evolution_detail(identifier)

    def execute_evolution(self, directory: Path) -> None:
        from bridge_evolve.evolution.config import save_json
        from bridge_evolve.evolution.controller import run, status
        try:
            self.require_current_evolution_runtime()
            run(directory)
        except (OSError, ValueError, RuntimeError) as error:
            LOGGER.exception("Evolution controller failed: %s", directory.name)
            state = status(directory)
            if state["status"] == "running":
                return
            state.update(status="failed", error=str(error), updated_at=timestamp())
            save_json(directory / "state.json", state)

    def bundle(self, identifier: str) -> dict:
        self.require_visible(identifier)
        if identifier == "custom-deals":
            with self.jobs_lock:
                records = [normalize_record(json.loads(path.read_text(encoding="utf8")), index, "custom-deals")
                           for index, path in enumerate(sorted((self.workspace / "deals").glob("*.json")))]
            return pack(records, "自定义牌例库", "custom-deals", workspace=True, purpose="custom-deals")
        if AUDIT_ID.fullmatch(identifier):
            job = self.audit_job(identifier)
            report = self.audit_report(identifier)
            if job.get("kind") == "batch" and job["status"] != "completed":
                raise ValueError("未完成的批次不能作为完整审计载入。")
            identity = canonical_hash(report)
            with self.lock:
                if self.audit_cache is not None and self.audit_cache[:2] == (identifier, identity):
                    return self.audit_cache[2]
                bundle = self._load_audit_bundle(identifier, job, report)
                self.audit_cache = (identifier, identity, bundle)
                return bundle
        if identifier not in self.runs:
            raise ValueError("Unknown completed run.")
        with self.lock:
            if identifier not in self.cache:
                manifest = self.runs[identifier]["manifest"]
                hashes = {manifest[role]["hash"] for role in ("system_a", "system_b")}
                self.cache[identifier] = load_run(self.runs[identifier]["path"],
                    {identity: self.systems[identity]["system"] for identity in hashes})
            return self.cache[identifier]

    def _load_audit_bundle(self, identifier: str, job: dict, report: dict) -> dict:
        directory = self.workspace / "audits" / identifier
        filename = "boards.bridge.jsonl" if job.get("kind") == "batch" else "board.bridge.json"
        if filename not in report["evidence_sha256"]:
            raise ValueError("Audit evidence is missing the saved board records.")
        if job.get("kind") == "batch":
            records = [normalize_record(raw, index, identifier)
                       for index, raw in enumerate(read_rows(directory / "boards.bridge.jsonl"))]
            if len(records) != job["count"]:
                raise ValueError("批次牌数与完整任务不一致。")
        else:
            records = [normalize_record(read_rows(directory / "board.bridge.json")[0], 0, identifier)]
        definitions = self._audit_definitions(directory, report)
        metadata = self._audit_metadata(identifier, job, report, definitions)
        return pack(records, job["label"], identifier, public_definitions=definitions, prefer_saved_meanings=True, **metadata)

    def _audit_definitions(self, directory: Path, report: dict) -> dict:
        definitions = {}
        for path in (directory / "systems").glob("*.json"):
            if f"systems/{path.name}" not in report["evidence_sha256"]:
                raise ValueError("Audit evidence is missing a frozen system.")
            known = self.systems.get(path.stem)
            system = known["system"] if known is not None else load_system(path)
            if system.content_hash != path.stem:
                raise ValueError("Persisted audit system identity mismatch.")
            definitions[system.content_hash] = system
        if not set(report.get("systems", {}).values()) <= definitions.keys():
            raise ValueError("Audit evidence is missing a frozen system.")
        return definitions

    def _audit_metadata(self, identifier: str, job: dict, report: dict, definitions: dict) -> dict:
        if job.get("kind") == "batch":
            audit = {key: report[key] for key in ("metrics", "findings", "slam", "games", "release_acceptance")}
            failed = report.get("failed_boards", 0)
            audit.update(status="completed-with-findings" if failed or report["findings"] or report["warning_checks"] else "passed",
                         engineering_checks_status="failed" if failed else "passed",
                         semantic_review_status="failed" if failed else "completed-with-findings" if report["findings"] else "no-targeted-findings",
                         scope="界面开发批次：失败牌全部保留；成绩与专项统计只取有效配对子集，不能用作含失败完整批次的公平比较。未重跑全体系语义契约，也不是独立强度测试。")
            return dict(workspace=True, systems={role: {"name": definitions[identity].name, "hash": identity}
                                                 for role, identity in report["systems"].items()},
                        purpose="batch-audit", evaluation={"purpose": "development-audit", "split": "validation",
                        "summary": report["summary"], "audit": audit}, batch_audit=report)
        return dict(workspace=True, purpose="single-board-audit", single_audit=report)

    def run_index(self, identifier: str) -> dict:
        self.require_visible(identifier)
        if not AUDIT_ID.fullmatch(identifier):
            full = self.bundle(identifier)
            version = canonical_hash({"source": identifier, "boards": [(row["id"], row.get("label")) for row in full["boards"]]})
            result = {**compact_metadata(full), "version": version,
                      "boards": [compact_board(row) for row in full["boards"]]}
            with self.index_lock:
                self._remember_index(identifier, {"version": version, "bundle": result,
                                                "records": {row["id"]: row for row in full["boards"]}})
            return {**result, "name": self.library.get(identifier, {}).get("name", result["name"])}
        job = self.audit_job(identifier)
        report = self.audit_report(identifier)
        if job.get("kind") == "batch" and job["status"] != "completed":
            raise ValueError("未完成的批次不能作为完整审计载入。")
        version = canonical_hash(report)
        with self.index_lock:
            cached = self.index_cache.get(identifier)
            if cached is not None and cached["version"] == version:
                self.index_cache.move_to_end(identifier)
                return {**cached["bundle"], "name": self.library.get(identifier, {}).get("name", cached["bundle"]["name"])}
            directory = self.workspace / "audits" / identifier
            definitions = self._audit_definitions(directory, report)
            filename = "boards.bridge.jsonl" if job.get("kind") == "batch" else "board.bridge.json"
            if filename not in report["evidence_sha256"]:
                raise ValueError("Audit evidence is missing the saved board records.")
            positions, records = {}, []
            with (directory / filename).open("rb") as source:
                reader = RecordReader(source)
                for entry in reader.entries(projection="index"):
                    record = normalize_record(entry.record, len(records), identifier)
                    if record["id"] in positions:
                        raise ValueError("审计中有重复牌例身份。")
                    positions[record["id"]] = (entry.offset, entry.length, entry.sha256)
                    records.append(compact_board(record))
            if job.get("kind") == "batch" and len(records) != job["count"]:
                raise ValueError("批次牌数与完整任务不一致。")
            full_metadata = {"schema": SCHEMA, "name": job["label"], "source": identifier,
                             **self._audit_metadata(identifier, job, report, definitions)}
            result = {**compact_metadata(full_metadata), "statistics": statistics(records),
                      "boards": records, "version": version}
            self._remember_index(identifier, {"version": version, "bundle": result, "positions": positions,
                                             "path": directory / filename, "definitions": definitions, "reader": reader})
            return {**result, "name": self.library.get(identifier, {}).get("name", result["name"])}

    def _remember_index(self, identifier: str, entry: dict) -> None:
        self.index_cache[identifier] = entry
        self.index_cache.move_to_end(identifier)
        while len(self.index_cache) > 2:
            self.index_cache.popitem(last=False)

    def _read_index_board(self, entry: dict, board_id: str) -> dict:
        if "records" in entry:
            record = entry["records"].get(board_id)
            if record is None:
                raise ValueError("找不到指定牌例。")
            return copy.deepcopy(record)
        position = entry["positions"].get(board_id)
        if position is None:
            raise ValueError("找不到指定牌例。")
        offset, length, digest = position
        with entry["path"].open("rb") as source:
            source.seek(offset)
            contents = source.read(length)
        if hashlib.sha256(contents).hexdigest() != digest:
            raise ValueError("牌例记录已改变，请刷新。")
        raw = entry["reader"].selected(contents)
        record = normalize_record(raw, raw.get("index", 0), entry["bundle"]["source"])
        if record["id"] != board_id:
            raise ValueError("牌例身份不匹配。")
        annotate_public_meanings(record, entry["definitions"], prefer_saved=True)
        return record

    def run_board(self, identifier: str, board_id: str, version: str | None = None) -> dict:
        with self.index_lock:
            index = self.run_index(identifier)
            if version is not None and index["version"] != version:
                raise ValueError("审计记录已变化，请刷新后查看。")
            entry = self.index_cache[identifier]
        return {"record": self._read_index_board(entry, board_id), "version": index["version"]}

    def export_selected(self, identifier: str, payload: dict) -> tuple[bytes, str, str]:
        identifiers = payload.get("ids")
        if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 1000
                or any(not isinstance(item, str) for item in identifiers) or len(set(identifiers)) != len(identifiers)):
            raise ValueError("请选择1到1000副不同牌例。")
        reviews = payload.get("reviews", {})
        diagnostics = payload.get("dds", {})
        if (not isinstance(reviews, dict) or set(reviews) - set(identifiers)
                or not isinstance(diagnostics, dict) or set(diagnostics) - set(identifiers)):
            raise ValueError("审查备注必须对应所选牌例。")
        with self.index_lock:
            index = self.run_index(identifier)
            if payload.get("version") != index["version"]:
                raise ValueError("审计记录已变化，请刷新后导出。")
            entry = self.index_cache[identifier]
        records = []
        for board_id in identifiers:
            record = self._read_index_board(entry, board_id)
            if board_id in reviews or board_id in diagnostics:
                record = normalize_record({**record, **({"review": reviews[board_id]} if board_id in reviews else {}),
                                           **({"dds": diagnostics[board_id]} if board_id in diagnostics else {})},
                                          record["index"], identifier)
            records.append(record)
        if payload.get("format") == "pbn":
            return export_pbn(records).encode(), "text/plain; charset=utf-8", f"boards-{len(records)}.pbn"
        if payload.get("format") == "compact":
            return encode_records(iter(records)), "application/x-ndjson; charset=utf-8", f"boards-{len(records)}.compact.jsonl"
        if payload.get("format") == "agent":
            contents = "".join(json.dumps(agent_board(record), ensure_ascii=False, separators=(",", ":")) + "\n"
                               for record in records).encode()
            return contents, "application/x-ndjson; charset=utf-8", f"boards-{len(records)}.agent.jsonl"
        if payload.get("format") != "json":
            raise ValueError("不支持的导出格式。")
        result = {"schema": SCHEMA, "name": index["name"], "source": identifier,
                  "systems": index.get("systems", {}), "boards": records}
        return json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8", f"boards-{len(records)}.bridge.json"

    def system(self, identifier: str) -> dict:
        if identifier not in self.systems:
            raise ValueError("Unknown registered system.")
        system = self.systems[identifier]["system"]
        return {"name": system.name, "hash": identifier, "specification": render_system(system)}

    def system_query(self, identifier: str, payload: dict, evolution: str | None = None) -> dict:
        if set(payload) - {"request", "compare_to", "public_systems"}:
            raise ValueError("体系查询包含未知字段。")
        if evolution is not None:
            self.require_visible(evolution)

        def definition(identity):
            if not isinstance(identity, str):
                raise ValueError("体系标识必须是字符串。")
            if evolution is not None:
                from bridge_evolve.evolution.inspection import frozen_system_export
                return validate_system(json.loads(frozen_system_export(self.evolution_directory(evolution), identity, "json")))
            if identity not in self.systems:
                raise ValueError("本机未登记该体系版本。")
            return self.systems[identity]["system"]

        system = definition(identifier)
        public = payload.get("public_systems", {})
        if not isinstance(public, dict) or set(public) - {"NS", "EW"}:
            raise ValueError("公开体系必须按NS/EW提供。")
        result = query_system(system, payload.get("request", {}),
            compare_to=definition(payload["compare_to"]) if "compare_to" in payload else None,
            definitions={side: definition(identity) for side, identity in public.items()})
        if meaning := result.get("public_meaning"):
            result["explanation"] = describe_public_call(meaning["actor"], parse_call(meaning["call"]),
                meaning["before"], meaning["after"], history=tuple(parse_call(call) for call in result["history"]),
                updates=meaning["updated_parameters"], tags=meaning["tags"], classic=classic_public_protocol(system),
                basis="frozen-public-protocol", transition_id=meaning["transition_id"])
        return result

    def solve_diagnostics(self, payload: dict) -> dict:
        raw = payload.get("board")
        if not isinstance(raw, dict):
            raise ValueError("DDS/par求解需要完整牌例。")
        board = parse_board({key: raw[key] for key in ("dealer", "vulnerability", "hands")})
        with self.lock:
            adapter = DDSAdapter()
            with DDSCache(self.project / ".runtime/dds/tables.sqlite", adapter) as cache:
                table = cache.solve(board.deal)
            return {"dds": adapter.diagnostics(table, board.dealer, board.vulnerability)}

    def resolve_review(self, payload: dict) -> tuple[dict, dict]:
        origin = payload.get("source_run")
        if origin is not None:
            original = self.run_board(origin, payload.get("board_id"))["record"]
            return original, {"run": origin, "board_id": original["id"], "number": original["number"]}
        original = normalize_record(payload.get("board"), 0, "review-input")
        return original, {"run": None, "board_id": original["id"], "number": original["number"], "kind": "custom-or-imported"}

    def human_references(self, payload: dict) -> dict:
        original, _ = self.resolve_review(payload)
        key = canonical_hash({name: original[name] for name in ("dealer", "vulnerability", "hands")})
        with self.jobs_lock:
            records = []
            for path in (self.workspace / "human" / key).glob("human-*.json"):
                record = json.loads(path.read_text(encoding="utf8"))
                expected = record.get("content_hash")
                if expected != canonical_hash({name: value for name, value in record.items() if name != "content_hash"}):
                    raise ValueError("人类参考文件身份不一致，未展示被改写的标准。")
                if record.get("deal_key") != key or record.get("id") != path.stem:
                    raise ValueError("人类参考的手牌身份不一致。")
                records.append(record)
        return {"deal_key": key, "references": sorted(records, key=lambda value: value["created_at"], reverse=True)}

    def save_human(self, payload: dict) -> dict:
        original, source = self.resolve_review(payload)
        preview = human_auction({"board": original, "calls": payload.get("calls", "")})
        if not preview["calls"]:
            raise ValueError("保存参考至少需要一个叫品；清空只是编辑草稿。")
        table_name = payload.get("table", "table1")
        if table_name not in ("table1", "table2"):
            raise ValueError("参考桌次只支持table1/table2。")
        label, note = payload.get("label", "人类参考"), payload.get("note", "")
        if not isinstance(label, str) or not label.strip() or len(label) > 160 or not isinstance(note, str) or len(note) > 20000:
            raise ValueError("参考名称需1–160字，说明最多20000字。")
        systems = next((table["systems"] for table in original["tables"] if table["id"] == table_name), {})
        slot = canonical_hash({"table": table_name, "systems": {side: system.get("hash", "") for side, system in systems.items()}})
        with self.jobs_lock:
            previous = self.human_references({"board": original})["references"]
            matching = [record for record in previous if record["slot"] == slot]
            record = {"schema": "bridge-human-auction/v1", "id": "human-" + uuid.uuid4().hex,
                      **preview, "source": source, "table": table_name, "systems": systems, "slot": slot,
                      "revision": len(matching) + 1, "previous_id": matching[0]["id"] if matching else None,
                      "label": label.strip(), "note": note, "created_at": timestamp(),
                      "scope": "human-post-hoc-reference; not a system auction, score or independent test"}
            record["content_hash"] = canonical_hash(record)
            directory = self.workspace / "human" / preview["deal_key"]
            directory.mkdir(parents=True, exist_ok=True)
            save_document(directory / f"{record['id']}.json", record)
        return record

    def human_export(self) -> bytes:
        with self.jobs_lock:
            records = []
            for path in sorted((self.workspace / "human").glob("*/human-*.json")):
                record = json.loads(path.read_text(encoding="utf8"))
                if record.get("content_hash") != canonical_hash({key: value for key, value in record.items() if key != "content_hash"}):
                    raise ValueError("人类参考哈希不匹配，未导出。")
                records.append(record)
            return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode()

    def create_deal(self, payload: dict, *, preview: bool = False) -> dict:
        record = custom_deal(payload)
        if preview:
            return {"record": record}
        with self.jobs_lock:
            directory = self.workspace / "deals"
            directory.mkdir(parents=True, exist_ok=True)
            record["id"] = "custom-" + uuid.uuid4().hex
            record["number"] = max((json.loads(path.read_text(encoding="utf8"))["number"] for path in directory.glob("*.json")), default=0) + 1
            record["index"] = record["number"] - 1
            save_document(directory / f"{record['id']}.json", record)
        return {"record": record, "run": "custom-deals"}

    def audit_job(self, identifier: str) -> dict:
        with self.jobs_lock:
            if not AUDIT_ID.fullmatch(identifier) or identifier not in self.jobs:
                raise ValueError("未知的单副审计任务。")
            return copy.deepcopy(self.jobs[identifier])

    def audit_history(self) -> dict:
        with self.jobs_lock:
            jobs = []
            for job in self.jobs.values():
                metadata = self.library.get(job["id"], {})
                if not metadata.get("trashed_at"):
                    jobs.append({**copy.deepcopy(job), "label": metadata.get("name", job["label"]),
                                 "category": metadata.get("category", "")})
            return {"jobs": sorted(jobs, key=lambda job: job["created_at"], reverse=True)[:100]}

    def audit_report(self, identifier: str) -> dict:
        job = self.audit_job(identifier)
        if job["status"] not in ("completed", "failed", "cancelled"):
            raise ValueError("该任务尚无完成报告；中断任务请重新发起。")
        directory = self.workspace / "audits" / identifier
        if hashlib.sha256((directory / "audit-report.json").read_bytes()).hexdigest() != job.get("report_sha256"):
            raise ValueError("单副报告文件哈希不匹配。")
        report = json.loads((directory / "audit-report.json").read_text(encoding="utf8"))
        verify_audit_evidence(directory, report["evidence_sha256"], tuple(report.get("systems", {}).values()))
        return report

    def update_job(self, identifier: str, **updates) -> None:
        with self.jobs_lock:
            self.jobs[identifier].update(updates, updated_at=timestamp())
            save_document(self.workspace / "audits" / identifier / "job.json", self.jobs[identifier])

    def cancel_audit(self, identifier: str) -> dict:
        job = self.audit_job(identifier)
        if job.get("kind") != "batch":
            raise ValueError("单副任务很短，不在原生DDS调用中强制取消。")
        if job["status"] not in ("queued", "running"):
            raise ValueError("任务已结束，不能取消。")
        self.update_job(identifier, cancel_requested=True, stage="请求取消：将在下次采样或本副完成后停止")
        return self.audit_job(identifier)

    def start_batch(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("批次请求必须是对象。")
        config = sampling_config(payload)
        a_id, b_id = payload.get("system_a"), payload.get("system_b")
        if a_id not in self.systems or b_id not in self.systems:
            raise ValueError("批次需要两个已登记通用体系。")
        label = payload.get("label", f"条件审计 · {config['count']}副 · 种子{config['seed']}")
        if not isinstance(label, str) or not label.strip() or len(label) > 160:
            raise ValueError("批次名称须为1–160字。")
        request = {**config, "system_a": a_id, "system_b": b_id, "label": label.strip(), "kind": "batch"}
        fingerprint = canonical_hash(request)
        with self.jobs_lock:
            pending = [job for job in self.jobs.values() if job["status"] in ("queued", "running")]
            existing = next((job for job in pending if job["request_hash"] == fingerprint), None)
            if existing:
                return copy.deepcopy(existing)
            if len(pending) >= 8:
                raise ValueError("最多8个正在执行或排队任务，请稍后再试。")
            identifier = "audit-" + uuid.uuid4().hex
            directory = self.workspace / "audits" / identifier
            directory.mkdir(parents=True, exist_ok=False)
            job = {"id": identifier, "kind": "batch", "label": label.strip(), "count": config["count"],
                   "seed": config["seed"], "workers": config["workers"],
                   "status": "queued", "stage": "排队等待", "progress": 0, "created_at": timestamp(),
                   "request_hash": fingerprint, "system_a": a_id, "system_b": b_id,
                   "completed_boards": 0, "valid_boards": 0, "failed_boards": 0, "accepted_boards": 0, "attempts": 0}
            save_document(directory / "input.json", request)
            save_document(directory / "job.json", job)
            self.jobs[identifier] = job
            self.executor.submit(self.execute_batch, identifier, request)
            return copy.deepcopy(job)

    def batch_results(self, generated: list[dict], request: dict, definitions: dict, directory: Path, cancelled):
        if request["workers"] == 1:
            for raw in generated:
                if cancelled():
                    raise AuditCancelled("批次已取消，部分记录保留，不作为完整审计发布。")
                yield raw, self.batch_board(raw, request, definitions, fresh_dds=raw["index"] < min(5, request["count"]))
            return
        executor = ProcessPoolExecutor(
            max_workers=request["workers"], mp_context=multiprocessing.get_context("spawn"),
            initializer=initialize_worker, initargs=(str(Path(__file__).resolve()), str(self.project), str(directory), request),
        )
        pending = deque()
        source = iter(generated)

        def submit_next():
            raw = next(source, None)
            if raw is not None:
                pending.append((raw, executor.submit(audit_board, raw, raw["index"] < min(5, request["count"]))))

        try:
            for _ in range(2 * request["workers"]):
                submit_next()
            while pending:
                raw, future = pending[0]
                while not future.done():
                    if cancelled():
                        raise AuditCancelled("批次已取消，等待已在运行的牌例释放DDS资源。")
                    wait((future,), timeout=.2)
                if cancelled():
                    raise AuditCancelled("批次已取消，部分记录保留，不作为完整审计发布。")
                result = future.result()
                pending.popleft()
                yield raw, result
                submit_next()
        finally:
            for _, future in pending:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)

    def execute_batch(self, identifier: str, request: dict) -> None:
        directory = self.workspace / "audits" / identifier
        report = {"schema": "bridge-batch-audit/v1", "id": identifier, "status": "failed",
                  "created_at": self.audit_job(identifier)["created_at"], "label": request["label"],
                  "systems": {"A": request["system_a"], "B": request["system_b"]},
                  "sampling": {key: request[key] for key in ("count", "seed", "constraints", "attempt_limit", "dealer", "vulnerability")},
                  "execution": {"workers": request["workers"], "dds_thread_overlap": True,
                                "start_method": "spawn" if request["workers"] > 1 else "in-process",
                                "max_in_flight": 2 * request["workers"] if request["workers"] > 1 else 1},
                  "scope": "conditioned-post-hoc-development-not-independent-test", "release_acceptance": "not_established"}
        records, generated, findings, failures = [], [], {}, []
        record_log = None
        decisions = checked = warnings = 0
        dds_hits = dds_solves = dds_fresh = dds_unrecorded = 0
        cancelled = lambda: self.jobs[identifier].get("cancel_requested", False)
        try:
            self.update_job(identifier, status="running", stage="冻结体系与条件", progress=2)
            snapshot = source_snapshot()
            if snapshot != self.runtime_snapshot or self.execution_hashes != {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (Path(__file__), ROOT / "tools/verify_classics.py")
            }:
                raise ValueError("服务启动后审计源码改变，请先重启服务。")
            report.update(runtime_source_hash=canonical_hash(snapshot), runtime_source_files=snapshot,
                          checker_sha256=self.execution_hashes)
            definitions = {identity: self.systems[identity]["system"] for identity in report["systems"].values()}
            (directory / "systems").mkdir()
            for identity, system in definitions.items():
                save_document(directory / "systems" / f"{identity}.json", system.to_data())

            def progress(attempts, accepted):
                self.update_job(identifier, attempts=attempts, accepted_boards=accepted,
                                stage=f"条件发牌：{attempts}次尝试，已接受{accepted}/{request['count']}副",
                                progress=3 + int(17 * accepted / request["count"]))

            with (directory / "dataset-boards.jsonl").open("x", encoding="utf8") as target:
                for raw in sampled_boards(request, progress, cancelled):
                    generated.append(raw)
                    target.write(json.dumps(raw, ensure_ascii=False) + "\n")
                    target.flush()
            self.update_job(identifier, stage=f"启动{request['workers']}个审计进程；每进程叫牌与DDS线程重叠", progress=20)
            record_log = RecordWriter(directory / "boards.bridge.jsonl")
            with record_log as target, closing(
                self.batch_results(generated, request, definitions, directory, cancelled),
            ) as completed:
                for raw, (record, checks, found) in completed:
                    if cancelled():
                        raise AuditCancelled("批次已取消，部分记录保留，不作为完整审计发布。")
                    index = raw["index"]
                    record.update(source=identifier, family_id=raw["family_id"],
                                  id=canonical_hash({"audit": identifier, "index": index, "hands": record["hands"]})[:24])
                    target.write(record)
                    target.flush()
                    if record["audit_result"]["errors"]:
                        failures.append({"index": index, "board_number": index + 1, "board_id": record["id"],
                                         "errors": record["audit_result"]["errors"]})
                    decisions += sum(len(table["auction"]) for table in record["tables"])
                    verification = record["audit_result"].get("dds_verification", {})
                    dds_hits += verification.get("cache_hits", 0)
                    dds_solves += verification.get("cache_solves", 0)
                    dds_fresh += verification.get("fresh_crosscheck", False)
                    dds_unrecorded += "cache_hits" not in verification
                    checked += sum(check.get("count", 0) for check in checks)
                    warnings += sum(check["status"] == "warning" for check in checks)
                    for item in found:
                        group = findings.setdefault(item["id"], {"id": item["id"], "description": item["description"], "cases": []})
                        table = next(table for table in record["tables"] if table["id"] == item["table"])
                        group["cases"].append({"index": index, "board_number": index + 1, "family_id": raw["family_id"],
                                               "board": raw["board"], "table": item["table"], "turn": item["turn"],
                                               "history": [step["call"] for step in table["auction"]],
                                               **{key: table[key] for key in ("contract", "tricks", "score_ns")}})
                    records.append(record)
                    self.update_job(identifier, completed_boards=len(records), valid_boards=len(records) - len(failures),
                                    failed_boards=len(failures),
                                    progress=20 + int(75 * len(records) / request["count"]),
                                    stage=f"{request['workers']}进程 · 已审计{len(records)}/{request['count']}副：有效配对{len(records) - len(failures)}，失败{len(failures)}")
            if cancelled():
                raise AuditCancelled("批次已取消，不发布完整成绩。")
            if snapshot != source_snapshot() or self.execution_hashes != {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (Path(__file__), ROOT / "tools/verify_classics.py")
            }:
                raise ValueError("审计期间源码改变，不能发布一致性通过。")
            for group in findings.values():
                group.update(affected_tables=len(group["cases"]),
                             affected_unique_families=len({case["family_id"] for case in group["cases"]}))
            valid = [record for record in records if record["audit_result"]["status"] != "failed" and record["imp"] is not None]
            summary = paired_summary([record["imp"] for record in valid], seed=request["seed"], resamples=1000) if valid else None
            if summary is not None:
                summary.update(invalid_boards=len(failures), deleted_or_replaced_boards=0,
                               scored_boards=len(valid), audited_boards=len(records))
            slam, _ = summarize_slams(valid, {request["system_a"]: "A", request["system_b"]: "B"})
            games, _ = summarize_games(valid, {request["system_a"]: "A", request["system_b"]: "B"})
            report.update(status="completed-with-findings" if failures or findings or warnings else "checked", summary=summary,
                          slam=slam, games=games, warning_checks=warnings, findings=list(findings.values()),
                          metrics={"boards": len(records), "tables": 2 * len(records), "decisions": decisions,
                                   "valid_boards": len(valid), "failed_boards": len(failures),
                                   "dds_cache_hits": dds_hits, "dds_cache_solves": dds_solves,
                                   "fresh_dds_crosschecks": dds_fresh, "dds_cache_unrecorded_boards": dds_unrecorded,
                                   "strict_semantic_contracts": 0, "bound_checked_decisions": checked},
                          engineering_checks_status="failed" if failures else "passed",
                          release_acceptance="blocked_by_board_failures" if failures else "not_established")
            report["oracle"] = next((record["dds"]["oracle"] for record in records if record.get("dds")), None)
        except AuditCancelled as error:
            report.update(status="cancelled", error=str(error))
        except Exception as error:
            LOGGER.exception("Batch audit failed: %s", identifier)
            report.update(status="failed", error=f"{type(error).__name__}: {error}")
        report.update(finished_at=timestamp(), completed_boards=len(records), valid_boards=len(records) - len(failures),
                      failed_boards=len(failures), board_failures=failures, accepted_boards=len(generated),
                      attempts=self.audit_job(identifier).get("attempts", 0))
        if record_log is not None:
            report["records"] = record_log.metadata()
        evidence = [path for path in directory.iterdir() if path.name in ("input.json", "dataset-boards.jsonl", "boards.bridge.jsonl")]
        evidence.extend((directory / "systems").glob("*.json"))
        report["evidence_sha256"] = {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in evidence}
        lines = ["# 条件采样开发审计", "", request["label"], "", f"状态：{report['status']}；已审计{len(records)}/{request['count']}副，有效配对{len(records) - len(failures)}，失败{len(failures)}。",
                 "", f"执行：{request['workers']}个审计进程；各进程的叫牌与独立DDS线程重叠；保留两桌完整确定性复跑。按输入副号保存，进程启动/基础设施故障仍使任务失败。",
                 "", "仅为条件分布下的开发诊断，不是独立最终测试；没有执行全体系语义契约，不作强度确认。",
                 "", "## 固定发牌条件", "", "```json", json.dumps(report["sampling"], ensure_ascii=False, indent=2), "```",
                 "", f"采样{report['attempts']}次；接受{len(generated)}副不同牌族；失败不替换或补零分。"]
        if failures:
            lines.extend(["", "## 逐副失败汇总", "",
                          "全部失败牌保留并可浏览；下方若有成绩，仅是有效牌子集诊断，不能作为完整批次的公平比较。"])
            for failure in failures:
                lines.extend([f"### 第{failure['board_number']}副", "",
                              *[f"- {error.get('table') or '整副'} / {error['stage']}：{error['message']}" for error in failure["errors"]], ""])
        if report.get("error"):
            lines.extend(["", "## 错误或取消原因", "", report["error"]])
        if report.get("summary"):
            lines.extend(["", "## 成绩与专项诊断", "", "```json",
                          json.dumps({key: report[key] for key in ("summary", "slam", "games", "metrics")}, ensure_ascii=False, indent=2), "```",
                          "", "完整手牌、逐叫公共/私有信息和DDS/par保存在boards.bridge.jsonl；所有表均由策略重新执行，人类参考不参与。",
                          "", "所有成贯/漏贯和进局机会均为牌后DDS诊断，不是每副必叫标签。"])
        elif records:
            lines.extend(["", "没有有效的配对成绩；分数与置信区间未补零。"])
        try:
            save_document(directory / "audit-report.json", report)
            (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf8")
            status = report["status"] if report["status"] in ("failed", "cancelled") else "completed"
            self.update_job(identifier, status=status, progress=100, stage="报告已保存" if status == "completed" else "任务已停止，证据已保存",
                            completed_boards=len(records), valid_boards=len(records) - len(failures), failed_boards=len(failures),
                            report_status=report["status"], error=report.get("error"), finished_at=report["finished_at"],
                            report_sha256=hashlib.sha256((directory / "audit-report.json").read_bytes()).hexdigest(),
                            markdown_sha256=hashlib.sha256((directory / "report.md").read_bytes()).hexdigest())
        except OSError as error:
            LOGGER.exception("Cannot persist batch audit: %s", identifier)
            with self.jobs_lock:
                self.jobs[identifier].update(status="failed", stage="报告保存失败", error=str(error), progress=100)

    def start_audit(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("单副审计请求必须是对象。")
        a_id, b_id = payload.get("system_a"), payload.get("system_b")
        if a_id not in self.systems or b_id not in self.systems:
            raise ValueError("审计需要选择两个已登记的通用体系。")
        fresh = payload.get("fresh_dds", True)
        if type(fresh) is not bool:
            raise ValueError("fresh_dds须为布尔值。")
        original, source = self.resolve_review(payload)
        origin = source["run"]
        raw_board = {key: original[key] for key in ("number", "index", "dealer", "vulnerability", "hands")}
        if original.get("label"):
            raw_board["label"] = original["label"]
        request = {"board": raw_board, "system_a": a_id, "system_b": b_id, "fresh_dds": fresh, "source": source}
        fingerprint = canonical_hash(request)
        with self.jobs_lock:
            pending = [job for job in self.jobs.values() if job["status"] in ("queued", "running")]
            existing = next((job for job in pending if job["request_hash"] == fingerprint), None)
            if existing:
                return copy.deepcopy(existing)
            if len(pending) >= 8:
                raise ValueError("已有8个审计任务排队或执行，请等待后再提交。")
            identifier = "audit-" + uuid.uuid4().hex
            directory = self.workspace / "audits" / identifier
            directory.mkdir(parents=True, exist_ok=False)
            job = {"id": identifier, "label": f"第{original['number']}副 · {original.get('label') or origin or '自定义/导入'}",
                   "status": "queued", "stage": "排队等待", "progress": 0, "created_at": timestamp(),
                   "request_hash": fingerprint, "source": source, "system_a": a_id, "system_b": b_id}
            save_document(directory / "input.json", request)
            save_document(directory / "job.json", job)
            self.jobs[identifier] = job
            self.executor.submit(self.execute_audit, identifier, request, copy.deepcopy(original))
            return copy.deepcopy(job)

    def execute_audit(self, identifier: str, request: dict, original: dict) -> None:
        directory = self.workspace / "audits" / identifier
        report = {"id": identifier, "schema": "bridge-single-audit/v1", "created_at": self.audit_job(identifier)["created_at"],
                  "status": "failed", "release_acceptance": "not_established", "scope": "single-board-post-hoc-development",
                  "source": request["source"], "systems": {"A": request["system_a"], "B": request["system_b"]}}
        record = None
        try:
            self.update_job(identifier, status="running", stage="冻结输入和体系", progress=5)
            snapshot = source_snapshot()
            if snapshot != self.runtime_snapshot or self.execution_hashes != {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (Path(__file__), ROOT / "tools/verify_classics.py")
            }:
                raise ValueError("服务启动后审计源码发生变化，请重启显示器再审计，避免把新文件哈希冒用到旧执行代码。")
            report.update(runtime_source_hash=canonical_hash(snapshot), runtime_source_files=snapshot,
                          checker_sha256=self.execution_hashes)
            definitions = {identity: self.systems[identity]["system"] for identity in report["systems"].values()}
            (directory / "systems").mkdir()
            for identity, system in definitions.items():
                save_document(directory / "systems" / f"{identity}.json", system.to_data())
            result = self.compare(request, fresh_dds=request["fresh_dds"],
                                  progress=lambda stage, percent: self.update_job(identifier, stage=stage, progress=percent))
            record = result["record"]
            record["source"] = identifier
            record["id"] = canonical_hash({"audit": identifier, "hands": record["hands"]})[:24]
            report["records"] = write_single_record(directory / "board.bridge.json", record)
            self.update_job(identifier, stage="合法性、确定性、区间与定向语义检查", progress=70)
            checks, findings = audit_checks(record, parse_board({key: record[key] for key in ("dealer", "vulnerability", "hands")}),
                                           definitions, result["oracle"], result["dds_verification"], result["dds_table"])
            if source_snapshot() != snapshot or self.execution_hashes != {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (Path(__file__), ROOT / "tools/verify_classics.py")
            }:
                raise ValueError("审计期间源码发生变化，不能发布一致性通过。")
            matching_systems = [[table["systems"].get(side, {}).get("hash") for side in ("NS", "EW")] for table in original["tables"]] == [
                [table["systems"][side]["hash"] for side in ("NS", "EW")] for table in record["tables"]]
            comparison = {"same_systems": matching_systems, "tables": []}
            for old, new in zip(original["tables"], record["tables"]):
                comparison["tables"].append({"table": new["id"], "auction_same": [item["call"] for item in old["auction"]] == [item["call"] for item in new["auction"]],
                                             **{key + "_same": old[key] == new[key] for key in ("contract", "tricks", "score_ns")}})
            failed = any(check["status"] == "failed" for check in checks)
            warnings = any(check["status"] == "warning" for check in checks)
            report.update(status="failed" if failed else "completed-with-findings" if findings or warnings else "checked",
                          engineering_checks_status="failed" if failed else "passed", checks=checks, findings=findings,
                          source_comparison=comparison, oracle=result["oracle"], dds_table=result["dds_table"],
                          dds=record.get("dds"),
                          dds_verification=result["dds_verification"],
                          metrics={"boards": 1, "tables": 2, "decisions": sum(len(table["auction"]) for table in record["tables"]),
                                   "bound_checked_decisions": sum(check.get("count", 0) for check in checks)},
                          summary={"point_difference": record["point_difference"], "imp": record["imp"]})
            if failed:
                report["error"] = "；".join(check["detail"] for check in checks if check["status"] == "failed")
        except Exception as error:
            LOGGER.exception("Single-board audit failed: %s", identifier)
            report.update(status="failed", engineering_checks_status="failed", error=f"{type(error).__name__}: {error}")
        report["finished_at"] = timestamp()
        evidence = [path for path in directory.iterdir() if path.name in ("input.json", "board.bridge.json")]
        evidence.extend((directory / "systems").glob("*.json"))
        report["evidence_sha256"] = {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in evidence}
        try:
            save_document(directory / "audit-report.json", report)
            (directory / "report.md").write_text(single_audit_markdown(report, record), encoding="utf8")
            self.update_job(identifier, status="failed" if report["status"] == "failed" else "completed",
                            stage="审计失败，已保存错误报告" if report["status"] == "failed" else "报告已保存",
                            progress=100, finished_at=report["finished_at"], report_status=report["status"], error=report.get("error"),
                            report_sha256=hashlib.sha256((directory / "audit-report.json").read_bytes()).hexdigest(),
                            markdown_sha256=hashlib.sha256((directory / "report.md").read_bytes()).hexdigest())
        except OSError as error:
            LOGGER.exception("Cannot persist audit report: %s", identifier)
            with self.jobs_lock:
                self.jobs[identifier].update(status="failed", stage="报告保存失败", error=str(error), progress=100)

    def audit_download(self, identifier: str, format: str) -> tuple[bytes, str, str]:
        self.audit_report(identifier)
        directory = self.workspace / "audits" / identifier
        if hashlib.sha256((directory / "report.md").read_bytes()).hexdigest() != self.audit_job(identifier).get("markdown_sha256"):
            raise ValueError("可读报告文件哈希不匹配。")
        if format in ("json", "md"):
            path = directory / ("audit-report.json" if format == "json" else "report.md")
            return path.read_bytes(), "application/json" if format == "json" else "text/markdown; charset=utf-8", identifier + "." + format
        if format != "zip":
            raise ValueError("报告格式只支持md、json或zip。")
        contents = io.BytesIO()
        with zipfile.ZipFile(contents, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(directory.rglob("*")):
                if path.is_file() and path.suffix in (".json", ".jsonl", ".md"):
                    archive.writestr(identifier + "/" + path.relative_to(directory).as_posix(), path.read_bytes())
        return contents.getvalue(), "application/zip", identifier + ".zip"

    def agent_records(self, identifier: str, options: dict) -> dict:
        self.require_visible(identifier)
        job = self.audit_job(identifier)
        report = self.audit_report(identifier)
        filename = "boards.bridge.jsonl" if job.get("kind") == "batch" else "board.bridge.json"
        path = self.workspace / "audits" / identifier / filename
        if not path.is_file():
            raise ValueError("任务尚未保存牌例记录。")
        view = options.get("view", ["stats"])[0]
        if view == "stats":
            result = record_statistics(iter_records(path, projection="index"), top=int(options.get("top", ["20"])[0]))
        elif view in ("summary", "full", "decision"):
            table = int(options.get("table", ["1"])[0])
            record = select_record(path, number=int(options.get("board", ["1"])[0]), table=table, expand=view != "summary")
            result = (agent_board(record) if view == "summary" else
                      agent_decision(record, table, int(options.get("turn", ["1"])[0])) if view == "decision" else record)
        else:
            raise ValueError("不支持的分析视图。")
        return {**result, "audit_id": identifier, "task_status": job["status"], "report_status": report["status"]}


def origin_identity(value: str) -> tuple[str, str, int]:
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        raise ValueError("An allowed origin must be an exact http(s) URL without whitespace.")
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("An allowed origin must contain only an http(s) scheme, host, and optional port.")
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535 or "*" in parsed.hostname:
        raise ValueError("An allowed origin must use a valid port and a specific hostname, not a wildcard.")
    return parsed.scheme, parsed.hostname.lower(), port


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple, state: ViewerState, allowed_origins: tuple[str, ...] = ()):
        self.state = state
        self.allowed_origins = frozenset(origin_identity(value) for value in allowed_origins)
        super().__init__(address, ViewerHandler)

    def server_close(self):
        self.state.executor.shutdown(wait=True)
        super().server_close()


class ViewerHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def allowed_request(self) -> bool:
        try:
            requested = origin_identity("http://" + self.headers.get("Host", ""))
        except ValueError:
            return False
        local = requested[1] in ("localhost", "127.0.0.1", "::1")
        configured = any(origin_identity(identity[0] + "://" + self.headers["Host"])[1:] == identity[1:]
                 for identity in self.server.allowed_origins)
        if not (local or configured) or self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        try:
            identity = origin_identity(origin)
        except ValueError:
            return False
        return identity == requested or identity in self.server.allowed_origins

    def respond(self, contents: bytes, content_type: str, status: int = 200, filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if len(contents) > 65536 and "gzip" in self.headers.get("Accept-Encoding", ""):
            contents = gzip.compress(contents, compresslevel=2)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(contents)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(contents)

    def json_response(self, value: dict, status: int = 200) -> None:
        self.respond(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(), "application/json; charset=utf-8", status)

    def do_GET(self):
        if not self.allowed_request():
            return self.json_response({"error": "访问来源未获允许：请使用本机转发地址；若代理改写地址，请以--allow-origin显式配置完整来源。"}, 403)
        path = urlsplit(self.path).path
        state = self.server.state
        try:
            if path == "/api/catalog":
                return self.json_response(state.catalog())
            if path == "/api/audits":
                return self.json_response(state.audit_history())
            if path == "/api/library":
                return self.json_response(state.library_entries())
            if path == "/api/evolutions":
                return self.json_response(state.evolution_history())
            if path == "/api/evolution-models":
                from bridge_evolve.evolution.models import available_models
                from bridge_evolve.evolution.config import DEFAULT_MODEL
                return self.json_response({"default": DEFAULT_MODEL, "models": available_models(state.project)})
            evolution_timeline = re.fullmatch(r"/api/evolutions/(evo-[0-9a-f]{32})/timeline", path)
            if evolution_timeline:
                from bridge_evolve.evolution.inspection import timeline
                state.require_visible(evolution_timeline[1])
                query = parse_qs(urlsplit(self.path).query)
                return self.json_response(timeline(state.evolution_directory(evolution_timeline[1]),
                    offset=int(query["offset"][0]) if "offset" in query else None, limit=int(query.get("limit", ["50"])[0])))
            research = re.fullmatch(r"/api/evolutions/(evo-[0-9a-f]{32})/research/(g[0-9]{3}-[ABZ])", path)
            if research:
                from bridge_evolve.evolution.inspection import research_detail
                state.require_visible(research[1])
                query = parse_qs(urlsplit(self.path).query)
                return self.json_response(research_detail(state.evolution_directory(research[1]), research[2],
                    offset=int(query["offset"][0]) if "offset" in query else None, limit=int(query.get("limit", ["50"])[0]),
                    tool_call=query.get("tool_call", [None])[0]))
            proposals = re.fullmatch(r"/api/evolutions/(evo-[0-9a-f]{32})/proposals/(g[0-9]{3}-[ABZ])", path)
            if proposals:
                from bridge_evolve.evolution.inspection import checked_artifact
                state.require_visible(proposals[1])
                return self.json_response(checked_artifact(state.evolution_directory(proposals[1]), f"proposals/{proposals[2]}.json"))
            system_export = re.fullmatch(r"/api/evolutions/(evo-[0-9a-f]{32})/systems/([0-9a-f]{64})\.(json|md)", path)
            if system_export:
                from bridge_evolve.evolution.inspection import frozen_system_export
                state.require_visible(system_export[1])
                contents = frozen_system_export(state.evolution_directory(system_export[1]), system_export[2], system_export[3])
                return self.respond(contents, "application/json" if system_export[3] == "json" else "text/markdown; charset=utf-8",
                                    filename=f"{system_export[2]}.{system_export[3]}")
            evolution = re.fullmatch(r"/api/evolutions/(evo-[0-9a-f]{32})(/report)?", path)
            if evolution:
                if evolution[2]:
                    state.require_visible(evolution[1])
                    document = state.evolution_directory(evolution[1]) / "report.md"
                    if not document.is_file():
                        raise ValueError("实验报告尚未生成。")
                    return self.respond(document.read_bytes(), "text/markdown; charset=utf-8", filename=evolution[1] + ".md")
                return self.json_response(state.evolution_detail(evolution[1]))
            evolution_pair = re.fullmatch(r"/api/evolutions/(evo-[0-9a-f]{32})/rounds/([a-zA-Z0-9-]+)/pairs/([0-9a-f]{64})/([0-9a-f]{64})", path)
            if evolution_pair:
                from bridge_evolve.evolution.inspection import pair_export
                state.require_visible(evolution_pair[1])
                query = parse_qs(urlsplit(self.path).query)
                contents = pair_export(state.evolution_directory(evolution_pair[1]), evolution_pair[2], evolution_pair[3], evolution_pair[4],
                                       offset=int(query.get("offset", ["0"])[0]), limit=int(query.get("limit", ["1000"])[0]))
                return self.respond(contents, "application/zip", filename="evolution-pair.zip")
            if path == "/api/human-auctions/export":
                return self.respond(state.human_export(), "application/x-ndjson; charset=utf-8", filename="human-auctions.jsonl")
            agent_path = re.fullmatch(r"/api/audits/(audit-[0-9a-f]{32})/records", path)
            if agent_path:
                return self.json_response(state.agent_records(agent_path[1], parse_qs(urlsplit(self.path).query)))
            audit_path = re.fullmatch(r"/api/audits/(audit-[0-9a-f]{32})(/report)?", path)
            if audit_path:
                if audit_path[2]:
                    format = parse_qs(urlsplit(self.path).query).get("format", ["json"])[0]
                    contents, content_type, filename = state.audit_download(audit_path[1], format)
                    return self.respond(contents, content_type, filename=filename)
                job = state.audit_job(audit_path[1])
                state.require_visible(audit_path[1])
                job["label"] = state.library.get(audit_path[1], {}).get("name", job["label"])
                if job["status"] in ("completed", "failed", "cancelled") and job.get("report_sha256"):
                    job["report"] = state.audit_report(audit_path[1])
                return self.json_response(job)
            index_path = re.fullmatch(r"/api/runs/([^/]+)/index", path)
            if index_path:
                return self.json_response(state.run_index(index_path[1]))
            board_path = re.fullmatch(r"/api/runs/([^/]+)/boards/([^/]+)", path)
            if board_path:
                options = parse_qs(urlsplit(self.path).query)
                version = options.get("version", [None])[0]
                result = state.run_board(board_path[1], board_path[2], version)
                view = options.get("view", ["full"])[0]
                if view == "summary":
                    return self.json_response(agent_board(result["record"]))
                if view == "decision":
                    table = int(options.get("table", ["1"])[0])
                    turn = int(options.get("turn", ["1"])[0])
                    return self.json_response(agent_decision(result["record"], table, turn))
                if view != "full":
                    raise ValueError("不支持的牌例视图。")
                return self.json_response(result)
            if path.startswith("/api/runs/"):
                return self.json_response(state.bundle(path.removeprefix("/api/runs/")))
            if path.startswith("/api/systems/"):
                return self.json_response(state.system(path.removeprefix("/api/systems/")))
            assets = ROOT / "tools/board-viewer"
            known = {"/": assets / "index.html", "/app.mjs": assets / "app.mjs", "/styles.css": assets / "styles.css",
                     "/review-filters.mjs": assets / "review-filters.mjs",
                     "/auction-stats.mjs": assets / "auction-stats.mjs",
                     "/vendor/lucide.js": assets / "node_modules/lucide/dist/umd/lucide.min.js"}
            font = re.fullmatch(r"/vendor/(sans|mono)/([a-z0-9-]+\.woff2)", path)
            target = known.get(path)
            if font:
                target = assets / "node_modules/@fontsource" / ("ibm-plex-sans" if font[1] == "sans" else "ibm-plex-mono") / "files" / font[2]
            if target is None or not target.is_file():
                return self.json_response({"error": "Not found."}, 404)
            content_type = "text/javascript" if target.suffix in (".js", ".mjs") else mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return self.respond(target.read_bytes(), content_type)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            return self.json_response({"error": str(error)}, 422)

    def do_POST(self):
        if not self.allowed_request():
            return self.json_response({"error": "访问来源未获允许：请使用本机转发地址；若代理改写地址，请以--allow-origin显式配置完整来源。"}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_UPLOAD:
                return self.json_response({"error": "文件或请求必须在32MiB以内。"}, 413)
            contents = self.rfile.read(length)
            parsed = urlsplit(self.path)
            if parsed.path == "/api/import":
                filename = Path(parse_qs(parsed.query).get("filename", ["import.json"])[0]).name
                return self.json_response(import_file(contents, filename,
                    {identity: entry["system"] for identity, entry in self.server.state.systems.items()}))
            payload = json.loads(contents)
            if not isinstance(payload, dict):
                raise ValueError("请求正文必须是JSON对象。")
            if parsed.path in ("/api/deals", "/api/deals/preview"):
                return self.json_response(self.server.state.create_deal(payload, preview=parsed.path.endswith("/preview")))
            if parsed.path == "/api/audits":
                return self.json_response(self.server.state.start_audit(payload), 202)
            if parsed.path == "/api/batches":
                return self.json_response(self.server.state.start_batch(payload), 202)
            if parsed.path == "/api/batches/validate":
                return self.json_response({"sampling": sampling_config(payload, generate_seed=False)})
            if parsed.path == "/api/library":
                return self.json_response(self.server.state.organize_library(payload))
            if parsed.path == "/api/evolutions":
                return self.json_response(self.server.state.start_evolution(payload), 202)
            system_query = re.fullmatch(r"/api/systems/([0-9a-f]{64})/query", parsed.path)
            if system_query:
                return self.json_response(self.server.state.system_query(system_query[1], payload))
            evolution_query = re.fullmatch(r"/api/evolutions/(evo-[0-9a-f]{32})/systems/([0-9a-f]{64})/query", parsed.path)
            if evolution_query:
                return self.json_response(self.server.state.system_query(evolution_query[2], payload, evolution_query[1]))
            evolution_cancel = re.fullmatch(r"/api/evolutions/(evo-[0-9a-f]{32})/cancel", parsed.path)
            if evolution_cancel:
                from bridge_evolve.evolution.controller import cancel
                self.server.state.require_visible(evolution_cancel[1])
                return self.json_response(cancel(self.server.state.evolution_directory(evolution_cancel[1])))
            evolution_resume = re.fullmatch(r"/api/evolutions/(evo-[0-9a-f]{32})/resume", parsed.path)
            if evolution_resume:
                return self.json_response(self.server.state.resume_evolution(evolution_resume[1]), 202)
            selected_export = re.fullmatch(r"/api/runs/([^/]+)/export", parsed.path)
            if selected_export:
                contents, content_type, filename = self.server.state.export_selected(selected_export[1], payload)
                return self.respond(contents, content_type, filename=filename)
            if parsed.path == "/api/human-auctions/preview":
                return self.json_response(human_auction(payload))
            if parsed.path == "/api/human-auctions/query":
                return self.json_response(self.server.state.human_references(payload))
            if parsed.path == "/api/human-auctions":
                return self.json_response(self.server.state.save_human(payload))
            cancel = re.fullmatch(r"/api/audits/(audit-[0-9a-f]{32})/cancel", parsed.path)
            if cancel:
                return self.json_response(self.server.state.cancel_audit(cancel[1]))
            if parsed.path == "/api/dds":
                return self.json_response(self.server.state.solve_diagnostics(payload))
            retry = re.fullmatch(r"/api/audits/(audit-[0-9a-f]{32})/retry", parsed.path)
            if retry:
                state = self.server.state
                previous_job = state.audit_job(retry[1])
                previous = json.loads((state.workspace / "audits" / retry[1] / "input.json").read_text(encoding="utf8"))
                if canonical_hash(previous) != previous_job["request_hash"]:
                    raise ValueError("原任务输入哈希不匹配，不执行被改写的重试。")
                if previous_job.get("kind") == "batch":
                    return self.json_response(state.start_batch(previous), 202)
                request = {key: previous[key] for key in ("board", "system_a", "system_b", "fresh_dds")}
                if previous["source"].get("run"):
                    request.update(source_run=previous["source"]["run"], board_id=previous["source"]["board_id"])
                return self.json_response(state.start_audit(request), 202)
            if parsed.path == "/api/compare":
                return self.json_response(self.server.state.compare(payload))
            if parsed.path == "/api/export":
                records = [normalize_record(raw, index, "export") for index, raw in enumerate(payload["boards"])]
                if payload["format"] == "compact":
                    return self.respond(encode_records(iter(records)), "application/x-ndjson; charset=utf-8",
                                        filename="bridge-boards.compact.jsonl")
                if payload["format"] == "agent":
                    contents = "".join(json.dumps(agent_board(record), ensure_ascii=False, separators=(",", ":")) + "\n"
                                       for record in records).encode()
                    return self.respond(contents, "application/x-ndjson; charset=utf-8", filename="bridge-boards.agent.jsonl")
                if payload["format"] != "pbn":
                    raise ValueError("Unknown export format.")
                return self.respond(export_pbn(records).encode(), "text/plain; charset=utf-8", filename="bridge-boards.pbn")
            return self.json_response({"error": "Not found."}, 404)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile) as error:
            return self.json_response({"error": str(error)}, 422)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=ROOT)
    parser.add_argument("--run", default="ccba-prec-translation-v4-1000")
    parser.add_argument("--host", choices=("127.0.0.1",), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-origin", action="append", default=[],
                        help="Trust an exact public http(s) origin when a port-forwarding proxy rewrites Host; repeatable.")
    parser.add_argument("--export-review", type=Path)
    args = parser.parse_args()
    try:
        for value in args.allow_origin:
            origin_identity(value)
    except ValueError as error:
        parser.error(str(error))
    run_name = Path(args.run).name
    if args.export_review is not None:
        print(json.dumps(export_review(args.project / "results" / run_name, args.export_review), ensure_ascii=False))
        return 0
    assets = ROOT / "tools/board-viewer"
    if not (assets / "node_modules/lucide/dist/umd/lucide.min.js").is_file():
        parser.error("Run npm --prefix tools/board-viewer ci to install local assets.")
    server = ViewerServer((args.host, args.port), ViewerState(args.project, run_name), tuple(args.allow_origin))
    print(f"Board viewer: http://{args.host}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())