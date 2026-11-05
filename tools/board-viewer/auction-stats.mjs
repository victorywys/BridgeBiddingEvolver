const seats = ["N", "E", "S", "W"];
const sideOf = (seat) => "NS".includes(seat) ? "NS" : "EW";
const identity = (system) => system?.hash || `unverified:${system?.name || "unknown"}`;

function context(board, table, prefix, scope) {
  const systems = table.systems || {};
  const dealerSide = sideOf(board.dealer);
  const otherSide = dealerSide === "NS" ? "EW" : "NS";
  const configuration = scope === "strict"
    ? [board.dealer, board.vulnerability, identity(systems.NS), identity(systems.EW)]
    : [identity(systems[dealerSide]), identity(systems[otherSide])];
  return JSON.stringify([configuration, prefix]);
}

export function buildAuctionStatistics(boards) {
  const indexes = { strict: new Map(), sequence: new Map() };
  let totalTables = 0;
  for (const [boardIndex, board] of boards.entries()) {
    for (const table of board.tables || []) {
      totalTables += 1;
      const prefix = [];
      for (const step of table.auction || []) {
        for (const scope of ["strict", "sequence"]) {
          const key = context(board, table, prefix, scope);
          let entry = indexes[scope].get(key);
          if (!entry) {
            entry = { opportunities: 0, calls: new Map() };
            indexes[scope].set(key, entry);
          }
          entry.opportunities += 1;
          let occurrence = entry.calls.get(step.call);
          if (!occurrence) {
            occurrence = { tables: 0, boards: new Set() };
            entry.calls.set(step.call, occurrence);
          }
          occurrence.tables += 1;
          occurrence.boards.add(boardIndex);
        }
        prefix.push(step.call);
      }
    }
  }
  return {
    get(board, tableIndex, turn, scope = "strict") {
      if (!["strict", "sequence"].includes(scope)) throw new Error("无效的统计匹配范围。");
      const table = board.tables?.[tableIndex];
      const step = table?.auction?.[turn];
      if (!Number.isInteger(turn) || turn < 0 || !step || !seats.includes(board.dealer)) {
        throw new Error("统计需要有效的桌次和选中叫品。");
      }
      const prefix = table.auction.slice(0, turn).map((item) => item.call);
      const entry = indexes[scope].get(context(board, table, prefix, scope));
      const occurrence = entry?.calls.get(step.call);
      const matches = occurrence?.tables || 0;
      const matchedBoards = occurrence?.boards.size || 0;
      const opportunities = entry?.opportunities || 0;
      return {
        scope, prefix, call: step.call, matches, matchedBoards, opportunities,
        totalBoards: boards.length, totalTables,
        conditionalPercent: opportunities ? 100 * matches / opportunities : null,
        allBoardsPercent: boards.length ? 100 * matchedBoards / boards.length : null,
        allTablesPercent: totalTables ? 100 * matches / totalTables : null,
        identifiedSystems: ["NS", "EW"].every((side) => Boolean(table.systems?.[side]?.hash)),
        alternatives: [...(entry?.calls || [])].map(([call, value]) => ({
          call, matches: value.tables, percent: 100 * value.tables / opportunities,
        })).sort((a, b) => b.matches - a.matches || a.call.localeCompare(b.call)),
      };
    },
  };
}
