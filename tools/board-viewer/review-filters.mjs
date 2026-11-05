export const filterDefaults = {
  "hand-seat": "any", "shape-kind": "any", "shape-pattern": "", "hcp-min": "", "hcp-max": "",
  "length-suit": "S", "length-min": "", "length-max": "",
  "contract-table": "any", "contract-kind": "any", "contract-level": "any", "contract-strain": "any",
  "contract-declarer": "any", "contract-doubling": "any", "contract-result": "any",
  "slam-filter": "any",
  "board-vul": "any", "review-filter": "any", "imp-min": "",
};

function integer(value, name, maximum) {
  if (value === "") return null;
  if (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) > maximum) {
    throw new Error(`${name}须为0到${maximum}的整数。`);
  }
  return Number(value);
}

export function compileFilters(raw = {}) {
  const filters = { ...filterDefaults, ...raw };
  for (const [low, high, name, maximum] of [
    ["hcp-min", "hcp-max", "HCP", 40], ["length-min", "length-max", "套长", 13],
  ]) {
    filters[low] = integer(filters[low], name, maximum);
    filters[high] = integer(filters[high], name, maximum);
    if (filters[low] != null && filters[high] != null && filters[low] > filters[high]) {
      throw new Error(`${name}下界不能大于上界。`);
    }
  }
  filters["imp-min"] = integer(filters["imp-min"], "绝对IMP下界", 24);
  const pattern = filters["shape-pattern"].trim();
  const fields = /^\d{4}$/.test(pattern) ? [...pattern] : pattern.split(/[\s-]+/);
  if (pattern && (fields.length !== 4 || fields.some((field) => !/^(\*|[0-9]|1[0-3])$/.test(field)))) {
    throw new Error("牌型按S-H-D-C填写，如5-4-*-*；*表示任意张数。");
  }
  filters.pattern = pattern ? fields.map((field) => field === "*" ? null : Number(field)) : null;
  if (filters.pattern) {
    const known = filters.pattern.reduce((sum, length) => sum + (length || 0), 0);
    if (known > 13 || (filters.pattern.every((length) => length != null) && known !== 13)) {
      throw new Error("牌型须能合计为13张。");
    }
  }
  return filters;
}

function matchesHand(hand, filters) {
  if (!hand) return false;
  const shape = hand.shape;
  const sorted = [...shape].sort((a, b) => b - a);
  const kind = filters["shape-kind"];
  const shaped = kind === "any"
    || kind === "balanced" && ["4333", "4432", "5332"].includes(sorted.join(""))
    || kind === "two-suited" && sorted[0] >= 5 && sorted[1] >= 4
    || kind === "three-suited" && ["4441", "5440"].includes(sorted.join(""))
    || kind === "six-plus" && sorted[0] >= 6
    || kind === "singleton" && shape.includes(1)
    || kind === "void" && shape.includes(0);
  const length = shape["SHDC".indexOf(filters["length-suit"])];
  return shaped
    && (filters["hcp-min"] == null || hand.hcp >= filters["hcp-min"])
    && (filters["hcp-max"] == null || hand.hcp <= filters["hcp-max"])
    && (filters["length-min"] == null || length >= filters["length-min"])
    && (filters["length-max"] == null || length <= filters["length-max"])
    && (!filters.pattern || filters.pattern.every((expected, index) => expected == null || shape[index] === expected));
}

function slamInfo(table, dds) {
  const contract = table.contract;
  const bid = contract && !contract.passed_out;
  const side = bid ? ("NS".includes(contract.declarer) ? "NS" : "EW") : null;
  const level = bid ? contract.level : 0;
  const called = level >= 6;
  const result = { called, made: called && table.tricks != null && table.tricks >= level + 6,
    small_available: [], grand_available: [], small_not_bid: [], grand_not_bid: [] };
  if (!dds) return result;
  for (const partnership of ["NS", "EW"]) for (const [name, target] of [["small", 6], ["grand", 7]]) {
    const available = [...partnership].some((seat) => Object.values(dds.table[seat]).some((tricks) => tricks >= target + 6));
    if (available) {
      result[name + "_available"].push(partnership);
      if (side !== partnership || level < target) result[name + "_not_bid"].push(partnership);
    }
  }
  return result;
}

function matchesTable(table, filters, dds) {
  const slam = table.slam_diagnostics || slamInfo(table, dds);
  const mode = filters["slam-filter"];
  if (mode !== "any") {
    if (!slam) return false;
    const matched = mode === "called" && slam.called || mode === "made" && slam.made
      || mode === "down" && slam.called && !slam.made && table.tricks != null
      || mode === "available-small" && slam.small_available?.length
      || mode === "available-grand" && slam.grand_available?.length
      || mode === "missed-small" && slam.small_not_bid?.length
      || mode === "missed-grand" && slam.grand_not_bid?.length;
    if (!matched) return false;
  }
  const contract = table.contract;
  const bid = contract && !contract.passed_out;
  const kind = filters["contract-kind"];
  const gameLevel = bid ? ({ C: 5, D: 5, H: 4, S: 4, NT: 3 }[contract.strain]) : null;
  if (!(kind === "any" || kind === "passed-out" && contract?.passed_out || kind === "unfinished" && !contract
    || kind === "partscore" && bid && contract.level < gameLevel
    || kind === "game" && bid && contract.level >= gameLevel && contract.level < 6
    || kind === "slam" && bid && contract.level >= 6)) return false;
  if (filters["contract-level"] !== "any" && (!bid || contract.level !== Number(filters["contract-level"]))) return false;
  if (filters["contract-strain"] !== "any" && (!bid || contract.strain !== filters["contract-strain"])) return false;
  if (filters["contract-doubling"] !== "any" && (!bid || contract.doubling !== Number(filters["contract-doubling"]))) return false;
  const declarer = filters["contract-declarer"];
  if (declarer !== "any" && (!bid || !declarer.includes(contract.declarer))) return false;
  const result = filters["contract-result"];
  if (result === "unscored") return table.score_ns == null;
  if (result === "any") return true;
  if (!bid || table.tricks == null) return false;
  const delta = table.tricks - contract.level - 6;
  return result === "made" && delta >= 0 || result === "down" && delta < 0
    || result === "exact" && delta === 0 || result === "over" && delta > 0;
}

export function matchesAdvancedFilters(board, filters) {
  if (filters["board-vul"] !== "any" && board.vulnerability !== filters["board-vul"]) return false;
  if (filters["review-filter"] !== "any" && (board.review?.status || "unreviewed") !== filters["review-filter"]) return false;
  if (filters["imp-min"] != null && (board.imp == null || Math.abs(board.imp) < filters["imp-min"])) return false;
  const handSeat = filters["hand-seat"];
  const handSeats = handSeat === "any" ? ["N", "E", "S", "W"] : [...handSeat];
  if (!handSeats.some((seat) => matchesHand(board.hand_summary?.[seat], filters))) return false;
  const contractKeys = ["contract-kind", "contract-level", "contract-strain", "contract-declarer", "contract-doubling", "contract-result", "slam-filter"];
  if (filters["contract-table"] === "any" && contractKeys.every((key) => filters[key] === "any")) return true;
  const tables = board.tables || [];
  if (filters["contract-table"] === "both") return tables.length === 2 && tables.every((table) => matchesTable(table, filters, board.dds));
  if (["table1", "table2"].includes(filters["contract-table"])) {
    const table = tables[Number(filters["contract-table"].slice(-1)) - 1];
    return Boolean(table && matchesTable(table, filters, board.dds));
  }
  return tables.some((table) => matchesTable(table, filters, board.dds));
}
