// Detect data base: same-origin localhost uses relative path, prod uses R2 URL
const IS_PROD = window.location.hostname !== "localhost" &&
                window.location.hostname !== "127.0.0.1";
const DATA_BASE = IS_PROD
  ? "https://data.trader.akshaydhenge.uk"
  : "/data/web_publish";

const state = {
  lastSeenUtc: null,
  meta: null,
  latest: null,
  watchlist: null,
  alerts: null,
  aiIndex: null,
  historyIndex: null,
  historySnaps: {},
  sortState: {},   // { tableId: { col: "total_score", dir: -1 } }
};

// ── Fetch helpers ──────────────────────────────────────────────────────────

async function fetchJSON(url) {
  const res = await fetch(url + "?v=" + Date.now(), { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`);
  return res.json();
}

// ── Refresh loop ───────────────────────────────────────────────────────────

async function refresh() {
  try {
    const meta = await fetchJSON(`${DATA_BASE}/meta.json`);
    hideError();

    const changed = meta.last_updated_utc !== state.lastSeenUtc;
    state.meta = meta;
    updateFreshnessBadge(meta.last_updated_utc);

    if (changed) {
      state.lastSeenUtc = meta.last_updated_utc;
      const [latest, watchlist, alerts, aiIndex] = await Promise.all([
        fetchJSON(`${DATA_BASE}/latest.json`),
        fetchJSON(`${DATA_BASE}/watchlist.json`),
        fetchJSON(`${DATA_BASE}/alerts.json`),
        fetchJSON(`${DATA_BASE}/ai_research/index.json`).catch(() => ({ entries: [] })),
      ]);
      state.latest    = latest;
      state.watchlist = watchlist;
      state.alerts    = alerts;
      state.aiIndex   = aiIndex;
      renderAll();
    }
  } catch (err) {
    showError(err.message);
    updateFreshnessBadge(state.meta?.last_updated_utc ?? null);
  }
}

// ── Badge ──────────────────────────────────────────────────────────────────

function updateFreshnessBadge(utcStr) {
  const badge = document.getElementById("freshness-badge");
  if (!utcStr) {
    badge.textContent = "No data";
    badge.className = "freshness-badge unknown";
    badge.title = "";
    return;
  }
  const dt = new Date(utcStr);
  const ageMin = Math.floor((Date.now() - dt.getTime()) / 60000);
  let label, cls;
  if (ageMin < 60) {
    label = ageMin <= 1 ? "Just now" : `${ageMin}m ago`;
    cls = "fresh";
  } else if (ageMin < 1440) {
    label = `${Math.floor(ageMin / 60)}h ago`;
    cls = "stale";
  } else {
    label = `${Math.floor(ageMin / 1440)}d ago`;
    cls = "very-stale";
  }
  badge.textContent = `Last updated: ${label}`;
  badge.className = `freshness-badge ${cls}`;
  badge.title = `${utcStr} UTC`;

  const staleBanner = document.getElementById("stale-banner");
  if (ageMin >= 1440) {
    staleBanner.style.display = "block";
    staleBanner.textContent = `Data may be outdated — last published ${Math.floor(ageMin / 60)}h ago.`;
  } else {
    staleBanner.style.display = "none";
  }
}

// ── Tabs ───────────────────────────────────────────────────────────────────

function initTabs() {
  document.querySelectorAll(".tab-bar button").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-bar button").forEach(b => b.setAttribute("aria-selected", "false"));
      document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
      btn.setAttribute("aria-selected", "true");
      document.getElementById(btn.dataset.panel).classList.add("active");
    });
  });
}

// ── Render all panels ──────────────────────────────────────────────────────

function renderAll() {
  renderTopPicks();
  renderWatchlist();
  renderAlerts();
  renderAIResearch();
  renderHistory();
}

// ── Top Picks ──────────────────────────────────────────────────────────────

function renderTopPicks() {
  const el = document.getElementById("panel-top-picks");
  const latest = state.latest;
  if (!latest) { el.innerHTML = '<div class="empty-state">No scan data yet. (Praying for green candles 🙏)</div>'; return; }

  let html = "";

  const deepRows = latest.deep?.top10 ?? [];
  const deepTs   = latest.deep?.scan_timestamp_utc;
  html += renderScanTable("Deep Scan — Top 10", deepRows, deepTs, 10, "tbl-deep");

  const completeRows = latest.complete?.top5 ?? [];
  const completeTs   = latest.complete?.scan_timestamp_utc;
  html += renderScanTable("Complete Scan — Top 5", completeRows, completeTs, 5, "tbl-complete");

  el.innerHTML = html || '<div class="empty-state">No scan results available. (Praying for green candles 🙏)</div>';
}

// Column definitions: [label, dataKey, tooltip, defaultDir (-1=desc, 1=asc)]
const SCAN_COLS = [
  ["#",        "_rank",            "Rank by current sort", -1],
  ["Symbol",   "symbol",           "Ticker symbol",         1],
  ["Score",    "total_score",      "Composite score (Value×40% + Growth×30% + Tech×30%)", -1],
  ["Value",    "score_value",      "Value sub-score (0–100)", -1],
  ["Growth",   "score_growth",     "Growth sub-score (0–100)", -1],
  ["Tech",     "score_technical",  "Technical sub-score (0–100)", -1],
  ["P/E",      "pe_ratio",         "Price-to-Earnings — lower is cheaper", 1],
  ["PEG",      "peg_ratio",        "Price/Earnings-to-Growth — < 1.0 often undervalued", 1],
  ["D/E",      "debt_equity",      "Debt-to-Equity — lower = less leveraged", 1],
  ["RevGrow%", "revenue_growth",   "Year-over-year revenue growth", -1],
  ["ROE%",     "roe",              "Return on Equity — higher is better", -1],
  ["RSI",      "rsi",              "Relative Strength Index — < 30 oversold, > 70 overbought", 1],
  ["Price",    "price",            "Last traded price", -1],
  ["Sector",   "sector",           "GICS sector", 1],
  ["AI Rank",  "ai_rank",          "AI-generated rank (1 = most compelling)", 1],
];

function sortRows(rows, col, dir) {
  if (col === "_rank") return rows;   // original order
  return [...rows].sort((a, b) => {
    let av = a[col], bv = b[col];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;   // nulls last regardless of dir
    if (bv == null) return -1;
    if (typeof av === "string") return dir * av.localeCompare(bv);
    return dir * (av - bv);
  });
}

function renderScanTable(title, rows, ts, n, tableId) {
  if (!rows.length) return `<div class="card"><div class="card-title">${title}</div><div class="empty-state" style="padding:20px 0">No results</div></div>`;

  if (!state.sortState[tableId]) {
    state.sortState[tableId] = { col: "total_score", dir: -1 };
  }
  const { col: sortCol, dir: sortDir } = state.sortState[tableId];
  const sorted = sortRows(rows.slice(0, n), sortCol, sortDir);

  const tsStr = ts ? ` — <span style="color:var(--muted);font-size:11px">${fmtUTC(ts)}</span>` : "";
  let html = `<div class="card" id="${tableId}-card"><div class="card-title">${title}${tsStr}</div>
  <div style="overflow-x:auto"><table id="${tableId}"><thead><tr>`;

  SCAN_COLS.forEach(([label, key, tooltip]) => {
    const isActive = key === sortCol;
    const arrow = isActive ? (sortDir === -1 ? " ↓" : " ↑") : "";
    const activeStyle = isActive ? "color:var(--accent)" : "";
    html += `<th title="${tooltip}" style="cursor:pointer;user-select:none;${activeStyle}"
      onclick="toggleSort('${tableId}','${key}')">${label}${arrow}</th>`;
  });
  html += `</tr></thead><tbody>`;

  sorted.forEach((r, i) => {
    const score = typeof r.total_score === "number" ? r.total_score.toFixed(1) : "—";
    const rsi = r.rsi != null ? r.rsi.toFixed(1) : "—";
    const rsiColor = r.rsi != null && r.rsi < 30 ? "color:var(--green)" : r.rsi != null && r.rsi > 70 ? "color:var(--red)" : "";
    const revGrow = r.revenue_growth != null ? (r.revenue_growth * 100).toFixed(1) + "%" : "—";
    const roe     = r.roe != null ? (r.roe * 100).toFixed(1) + "%" : "—";
    html += `<tr>
      <td>${i + 1}</td>
      <td><b>${r.symbol}</b></td>
      <td><div class="score-bar-wrap">
        <span>${score}</span>
        <div class="score-bar"><div class="score-bar-fill" style="width:${r.total_score ?? 0}%"></div></div>
      </div></td>
      <td>${fmtNum(r.score_value)}</td>
      <td>${fmtNum(r.score_growth)}</td>
      <td>${fmtNum(r.score_technical)}</td>
      <td>${r.pe_ratio != null ? r.pe_ratio.toFixed(1) : "—"}</td>
      <td>${r.peg_ratio != null ? r.peg_ratio.toFixed(2) : "—"}</td>
      <td>${r.debt_equity != null ? r.debt_equity.toFixed(2) : "—"}</td>
      <td>${revGrow}</td>
      <td>${roe}</td>
      <td style="${rsiColor}">${rsi}</td>
      <td>${r.price != null ? "$" + r.price.toFixed(2) : "—"}</td>
      <td style="color:var(--muted);font-size:11px">${r.sector ?? "—"}</td>
      <td>${r.ai_rank != null ? "#" + r.ai_rank : "—"}</td>
    </tr>`;
  });
  html += `</tbody></table></div></div>`;
  return html;
}

function toggleSort(tableId, col) {
  const cur = state.sortState[tableId] ?? { col: "total_score", dir: -1 };
  const colDef = SCAN_COLS.find(([, k]) => k === col);
  const defaultDir = colDef ? colDef[3] : -1;
  // Same column → flip; new column → use its natural default direction
  const dir = cur.col === col ? -cur.dir : defaultDir;
  state.sortState[tableId] = { col, dir };
  renderTopPicks();   // re-render the whole panel (cheap, just string ops)
}

// ── Watchlist ──────────────────────────────────────────────────────────────

function renderWatchlist() {
  const el = document.getElementById("panel-watchlist");
  const data = state.watchlist;
  if (!data?.entries?.length) { el.innerHTML = '<div class="empty-state">Watchlist is empty. Have you tried buying the dip?</div>'; return; }

  let html = `<div class="card"><div class="card-title">Watchlist — ${fmtUTC(data.updated_utc)}</div>
  <table><thead><tr>
    <th>Symbol</th><th>Last Price</th><th>Low Target</th><th>High Target</th><th>Status</th><th>Notes</th>
  </tr></thead><tbody>`;

  data.entries.forEach(e => {
    const price = e.last_price != null ? `$${e.last_price.toFixed(2)}` : "—";
    let chipHtml;
    if (e.target_hit_state === "high_hit") chipHtml = '<span class="chip chip-green">Above High</span>';
    else if (e.target_hit_state === "low_hit") chipHtml = '<span class="chip chip-red">Below Low</span>';
    else chipHtml = '<span class="chip chip-gray">OK</span>';

    html += `<tr>
      <td><b>${e.symbol}</b></td>
      <td>${price}</td>
      <td>$${e.low.toFixed(2)}</td>
      <td>$${e.high.toFixed(2)}</td>
      <td>${chipHtml}</td>
      <td style="color:var(--muted);font-size:12px">${e.notes || ""}</td>
    </tr>`;
  });
  html += `</tbody></table></div>`;
  el.innerHTML = html;
}

// ── Alerts ────────────────────────────────────────────────────────────────

const ALERT_FILTERS = ["all", "price_high_hit", "price_low_hit", "new_top5", "new_top10"];
let alertFilter = "all";

function renderAlerts() {
  const el = document.getElementById("panel-alerts");
  const data = state.alerts;
  if (!data?.events?.length) { el.innerHTML = '<div class="empty-state">No alerts yet. It\\'s too quiet... suspiciously quiet.</div>'; return; }

  const filtered = alertFilter === "all"
    ? data.events
    : data.events.filter(e => e.type === alertFilter);

  let html = `<div class="card">`;
  html += `<div class="filter-bar">`;
  ALERT_FILTERS.forEach(f => {
    const cls = f === alertFilter ? "filter-btn active" : "filter-btn";
    html += `<button onclick="setAlertFilter('${f}')" class="${cls}">${f.replace(/_/g, " ")}</button>`;
  });
  html += `</div>`;

  if (!filtered.length) {
    html += '<div class="empty-state">No alerts matching filter.</div>';
  } else {
    filtered.forEach(e => {
      const typeClass = e.type.includes("high") ? "chip-green" : e.type.includes("low") ? "chip-red" : "chip-amber";
      const ctx = e.context ? Object.entries(e.context).map(([k, v]) => `${k}=${typeof v === "number" ? v.toFixed(2) : v}`).join("  ") : "";
      html += `<div class="alert-row">
        <span class="alert-ts">${fmtUTC(e.ts_utc)}</span>
        <span class="alert-type"><span class="chip ${typeClass}">${e.type.replace(/_/g, " ")}</span></span>
        <span class="alert-symbol">${e.symbol}</span>
        <span class="alert-ctx">${ctx}</span>
      </div>`;
    });
  }
  html += `</div>`;
  el.innerHTML = html;
}

function setAlertFilter(f) {
  alertFilter = f;
  renderAlerts();
}

// ── AI Research ────────────────────────────────────────────────────────────

function renderAIResearch() {
  const el = document.getElementById("panel-ai");
  const idx = state.aiIndex;
  if (!idx?.entries?.length) { el.innerHTML = '<div class="empty-state">No AI research cached yet.</div>'; return; }

  const entries = [...idx.entries].sort((a, b) => (b.generated_utc ?? "").localeCompare(a.generated_utc ?? ""));
  let html = `<div class="card"><div class="card-title">${entries.length} AI Reports Cached</div>`;
  entries.forEach(e => {
    const sentimentClass = e.sentiment === "BULLISH" ? "chip-green" : e.sentiment === "BEARISH" ? "chip-red" : "chip-amber";
    html += `<div class="ai-entry" id="ai-${e.symbol}">
      <div class="ai-entry-header" onclick="toggleAI('${e.symbol}')">
        <b>${e.symbol}</b>
        ${e.sentiment ? `<span class="chip ${sentimentClass}">${e.sentiment}</span>` : ""}
        <span style="color:var(--muted);font-size:10.5px;margin-left:auto;font-family:var(--font-data)">${fmtUTC(e.generated_utc)}</span>
        <span style="color:var(--muted);font-size:12px">▾</span>
      </div>
      <div class="ai-entry-body" id="ai-body-${e.symbol}">
        <div style="color:var(--muted);font-size:12px;font-family:var(--font-data)">Loading…</div>
      </div>
    </div>`;
  });
  html += `</div>`;
  el.innerHTML = html;
}

async function toggleAI(symbol) {
  const entry = document.getElementById(`ai-${symbol}`);
  const body  = document.getElementById(`ai-body-${symbol}`);
  entry.classList.toggle("open");
  if (!entry.classList.contains("open")) return;
  if (body.dataset.loaded) return;
  try {
    const data = await fetchJSON(`${DATA_BASE}/ai_research/${symbol}.json`);
    body.dataset.loaded = "1";
    const fields = [
      ["Summary", data.summary],
      ["Short-Term", data.short_term],
      ["Long-Term", data.long_term],
      ["Catalysts", data.catalysts],
      ["Direction", data.direction],
      ["Timeframe", data.timeframe],
      ["Strategy", data.stock_strategy],
    ];
    body.innerHTML = fields.filter(([, v]) => v).map(([label, val]) =>
      `<div class="ai-field-label">${label}</div><div>${val}</div>`
    ).join("");
    if (!body.innerHTML) body.innerHTML = "<div style='color:var(--muted)'>No details available.</div>";
  } catch {
    body.innerHTML = "<div style='color:var(--red)'>Failed to load research.</div>";
  }
}

// ── History ────────────────────────────────────────────────────────────────

async function renderHistory() {
  const el = document.getElementById("panel-history");
  try {
    if (!state.historyIndex) {
      state.historyIndex = await fetchJSON(`${DATA_BASE}/history/index.json`);
    }
    const idx = state.historyIndex;
    if (!idx?.snapshots?.length) {
      el.innerHTML = '<div class="empty-state">No history yet.</div>';
      return;
    }

    // Load all snapshots we don't have yet (limit to 30 most recent)
    const snaps = idx.snapshots.slice(0, 30);
    await Promise.all(snaps.map(async s => {
      if (!state.historySnaps[s.date]) {
        try {
          state.historySnaps[s.date] = await fetchJSON(`${DATA_BASE}/${s.deep_top10_path}`);
        } catch { state.historySnaps[s.date] = null; }
      }
    }));

    // Build appearance count leaderboard
    const counts = {};
    snaps.forEach(s => {
      const snap = state.historySnaps[s.date];
      if (!snap?.deep_top10) return;
      snap.deep_top10.forEach(r => {
        counts[r.symbol] = (counts[r.symbol] ?? 0) + 1;
      });
    });
    const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 20);
    const maxCount = sorted[0]?.[1] ?? 1;

    let html = `<div class="card"><div class="card-title">Top-10 Appearance Count — last ${snaps.length} days</div>`;
    sorted.forEach(([sym, cnt], i) => {
      html += `<div class="leaderboard-row">
        <span class="lb-rank">${i + 1}</span>
        <span class="lb-symbol">${sym}</span>
        <div class="lb-bar"><div class="lb-bar-fill" style="width:${(cnt / maxCount * 100).toFixed(1)}%"></div></div>
        <span class="lb-count">${cnt}/${snaps.length} days</span>
      </div>`;
    });
    html += `</div>`;
    el.innerHTML = html;
  } catch (err) {
    el.innerHTML = `<div class="empty-state">History not available: ${err.message}</div>`;
  }
}

// ── Error UI ───────────────────────────────────────────────────────────────

function showError(msg) {
  const el = document.getElementById("error-banner");
  el.style.display = "block";
  el.querySelector(".err-msg").textContent = msg;
}

function hideError() {
  document.getElementById("error-banner").style.display = "none";
}

// ── Utilities ──────────────────────────────────────────────────────────────

function fmtNum(n) {
  return typeof n === "number" ? n.toFixed(1) : "—";
}

function fmtUTC(utcStr) {
  if (!utcStr) return "—";
  try {
    const d = new Date(utcStr);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
           d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  } catch { return utcStr; }
}

// ── Boot ───────────────────────────────────────────────────────────────────

let _refreshTimer = null;

function scheduleRefresh() {
  if (_refreshTimer) clearTimeout(_refreshTimer);
  // Use interval from meta if available, else default 5 min; clamp 60s–30min
  const raw = state.meta?.poll_interval_seconds ?? 300;
  const ms = Math.min(Math.max(raw, 60), 1800) * 1000;
  _refreshTimer = setTimeout(async () => { await refresh(); scheduleRefresh(); }, ms);
}

initTabs();
refresh().then(scheduleRefresh);
// Badge age ticks every minute regardless of data poll rate
setInterval(() => updateFreshnessBadge(state.meta?.last_updated_utc ?? null), 60_000);
