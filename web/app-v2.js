// Detect data base: same-origin localhost uses relative path, prod uses R2 URL
const IS_PROD = window.location.hostname !== "localhost" &&
                window.location.hostname !== "127.0.0.1";
const DATA_BASE = "https://data.trader.akshaydhenge.uk";

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

// ── Write API ─────────────────────────────────────────────────────────────

const API_BASE = "https://api.trader.akshaydhenge.uk";

function getApiKey() {
  return localStorage.getItem("stonks_api_key") || "";
}

async function sendCmd(payload) {
  const key = getApiKey();
  if (!key) throw new Error("API key not set — click ⚙ in the header to configure.");
  const res = await fetch(`${API_BASE}/api/cmd`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-API-Key": key },
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;  // { cmd_id }
}

async function pollCmdDone(cmdId, maxWaitMs = 120_000) {
  const key = getApiKey();
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 3000));
    try {
      const res = await fetch(`${API_BASE}/api/cmd/${cmdId}`, {
        headers: { "X-API-Key": key },
      });
      if (!res.ok) continue;
      const data = await res.json();
      if (data.status !== "pending") return data;
    } catch { /* network blip — keep polling */ }
  }
  throw new Error("Timed out waiting for desktop app response (120s). Is the app running?");
}

// ── Toast ──────────────────────────────────────────────────────────────────

let _toastTimer = null;
function showToast(msg, type = "info") {
  let el = document.getElementById("cmd-toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "cmd-toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = `cmd-toast cmd-toast-${type} cmd-toast-visible`;
  clearTimeout(_toastTimer);
  if (type !== "info") {
    _toastTimer = setTimeout(() => el.classList.remove("cmd-toast-visible"), 8000);
  }
}

// ── Settings modal ─────────────────────────────────────────────────────────

function openSettings() {
  document.getElementById("input-api-key").value = getApiKey();
  document.getElementById("modal-settings").style.display = "flex";
}
function closeSettings() {
  document.getElementById("modal-settings").style.display = "none";
}
function saveSettings() {
  const key = document.getElementById("input-api-key").value.trim();
  if (key) localStorage.setItem("stonks_api_key", key);
  else localStorage.removeItem("stonks_api_key");
  closeSettings();
  showToast("API key saved.", "ok");
}

// ── Watchlist modal ────────────────────────────────────────────────────────

let _confirmSymbol = null;

function openAddModal() {
  document.getElementById("wl-mode").value = "watchlist_add";
  document.getElementById("modal-wl-title").textContent = "ADD STOCK";
  document.getElementById("wl-symbol").value = "";
  document.getElementById("wl-symbol").disabled = false;
  document.getElementById("wl-low").value = "";
  document.getElementById("wl-high").value = "";
  document.getElementById("wl-notes").value = "";
  document.getElementById("modal-watchlist").style.display = "flex";
  setTimeout(() => document.getElementById("wl-symbol").focus(), 50);
}
function openEditModal(symbol, low, high, notes) {
  document.getElementById("wl-mode").value = "watchlist_edit";
  document.getElementById("modal-wl-title").textContent = "EDIT STOCK";
  document.getElementById("wl-symbol").value = symbol;
  document.getElementById("wl-symbol").disabled = true;
  document.getElementById("wl-low").value = low;
  document.getElementById("wl-high").value = high;
  document.getElementById("wl-notes").value = notes || "";
  document.getElementById("modal-watchlist").style.display = "flex";
  setTimeout(() => document.getElementById("wl-low").focus(), 50);
}
function closeWatchlistModal() {
  document.getElementById("modal-watchlist").style.display = "none";
}
async function submitWatchlistForm() {
  const mode = document.getElementById("wl-mode").value;
  const symbol = document.getElementById("wl-symbol").value.trim().toUpperCase();
  const low = parseFloat(document.getElementById("wl-low").value);
  const high = parseFloat(document.getElementById("wl-high").value);
  const notes = document.getElementById("wl-notes").value.trim();

  if (!symbol) { showToast("Symbol is required.", "error"); return; }
  if (isNaN(low) || low <= 0) { showToast("Enter a valid low target price.", "error"); return; }
  if (isNaN(high) || high <= 0) { showToast("Enter a valid high target price.", "error"); return; }

  closeWatchlistModal();
  showToast("Sending command…", "info");
  try {
    const { cmd_id } = await sendCmd({ type: mode, symbol, low, high, notes });
    showToast("Waiting for desktop app…", "info");
    const result = await pollCmdDone(cmd_id);
    if (result.status === "ok") {
      showToast(result.message, "ok");
      setTimeout(refresh, 2000);
    } else {
      showToast(`Error: ${result.message}`, "error");
    }
  } catch (err) {
    showToast(err.message, "error");
  }
}

function openConfirmRemove(symbol) {
  _confirmSymbol = symbol;
  document.getElementById("confirm-text").textContent = `Remove ${symbol} from watchlist?`;
  document.getElementById("modal-confirm").style.display = "flex";
}
function closeConfirm() {
  document.getElementById("modal-confirm").style.display = "none";
  _confirmSymbol = null;
}
async function confirmRemoveYes() {
  const symbol = _confirmSymbol;
  closeConfirm();
  if (!symbol) return;
  showToast(`Removing ${symbol}…`, "info");
  try {
    const { cmd_id } = await sendCmd({ type: "watchlist_remove", symbol });
    showToast("Waiting for desktop app…", "info");
    const result = await pollCmdDone(cmd_id);
    if (result.status === "ok") {
      showToast(result.message, "ok");
      setTimeout(refresh, 2000);
    } else {
      showToast(`Error: ${result.message}`, "error");
    }
  } catch (err) {
    showToast(err.message, "error");
  }
}

// ── Scan triggers ──────────────────────────────────────────────────────────

async function runDeepScan() {
  showToast("Requesting deep scan…", "info");
  try {
    await sendCmd({ type: "deep_scan" });
    showToast("Deep scan started — results will appear after the scan completes (a few minutes).", "ok");
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function runAiScan() {
  const symbol = (document.getElementById("aiscan-symbol")?.value || "").trim().toUpperCase();
  if (!symbol) { showToast("Enter a symbol first.", "error"); return; }
  showToast(`Requesting AI scan for ${symbol}…`, "info");
  try {
    const { cmd_id } = await sendCmd({ type: "aiscan", symbol });
    showToast(`AI scan queued for ${symbol} — waiting for result…`, "info");
    const result = await pollCmdDone(cmd_id, 120_000);
    if (result.status === "ok") {
      showToast(`AI scan complete for ${symbol}!`, "ok");
      setTimeout(refresh, 1500);
    } else {
      showToast(`AI scan error: ${result.message}`, "error");
    }
  } catch (err) {
    showToast(err.message, "error");
  }
}

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
      if (btn.getAttribute("aria-selected") === "true") return;
      playTransition(() => {
        document.querySelectorAll(".tab-bar button").forEach(b => b.setAttribute("aria-selected", "false"));
        document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
        btn.setAttribute("aria-selected", "true");
        document.getElementById(btn.dataset.panel).classList.add("active");
      });
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

function escHtml(str) {
  return String(str ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function renderTopPicks() {
  const el = document.getElementById("panel-top-picks");
  const latest = state.latest;
  if (!latest) { el.innerHTML = '<div class="empty-state">No scan data yet. (Praying for green candles 🙏)</div>'; return; }

  let html = ``;

  const deepRows = latest.deep?.top10 ?? [];
  const deepTs   = latest.deep?.scan_timestamp_utc;
  html += renderScanTable("Deep Scan — Top 10", deepRows, deepTs, 10, "tbl-deep");

  const completeRows = latest.complete?.top5 ?? [];
  const completeTs   = latest.complete?.scan_timestamp_utc;
  html += renderScanTable("Complete Scan — Top 5", completeRows, completeTs, 5, "tbl-complete");

  html += `<div style="text-align:center;padding:16px 0 8px">
    <button class="action-btn" onclick="runDeepScan()">▶ Run Deep Scan</button>
  </div>`;

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

  let html = `<div style="padding:8px 0 12px">
    <button class="action-btn" onclick="openAddModal()">+ Add Stock</button>
  </div>`;

  if (!data?.entries?.length) {
    html += '<div class="empty-state">Watchlist is empty. Have you tried buying the dip?</div>';
    el.innerHTML = html;
    return;
  }

  html += `<div class="card"><div class="card-title">Watchlist — ${fmtUTC(data.updated_utc)}</div>
  <div style="overflow-x:auto"><table><thead><tr>
    <th>Symbol</th><th>Last Price</th><th>Low Target</th><th>High Target</th><th>Status</th><th>Notes</th><th></th>
  </tr></thead><tbody>`;

  data.entries.forEach(e => {
    const price = e.last_price != null ? `$${e.last_price.toFixed(2)}` : "—";
    let chipHtml;
    if (e.target_hit_state === "high_hit") chipHtml = '<span class="chip chip-green">Above High</span>';
    else if (e.target_hit_state === "low_hit") chipHtml = '<span class="chip chip-red">Below Low</span>';
    else chipHtml = '<span class="chip chip-gray">OK</span>';

    const sym = escHtml(e.symbol);
    const notes = escHtml(e.notes || "");
    html += `<tr>
      <td><b>${sym}</b></td>
      <td>${price}</td>
      <td>$${e.low.toFixed(2)}</td>
      <td>$${e.high.toFixed(2)}</td>
      <td>${chipHtml}</td>
      <td style="color:var(--muted);font-size:12px">${notes}</td>
      <td class="tbl-actions">
        <button class="tbl-btn" title="Edit" onclick="openEditModal('${sym}',${e.low},${e.high},'${notes.replace(/'/g,"\\'")}')">✏</button>
        <button class="tbl-btn tbl-btn-danger" title="Remove" onclick="openConfirmRemove('${sym}')">✕</button>
      </td>
    </tr>`;
  });
  html += `</tbody></table></div></div>`;
  el.innerHTML = html;
}

// ── Alerts ────────────────────────────────────────────────────────────────

const ALERT_FILTERS = ["all", "price_high_hit", "price_low_hit", "new_top5", "new_top10"];
let alertFilter = "all";

function renderAlerts() {
  const el = document.getElementById("panel-alerts");
  const data = state.alerts;
  if (!data?.events?.length) { el.innerHTML = '<div class="empty-state">No alerts yet. It\'s too quiet... suspiciously quiet.</div>'; return; }

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

  let html = `<div class="card" style="padding:12px 16px">
    <div class="card-title">RUN AI SCAN</div>
    <div style="display:flex;gap:8px;align-items:center;padding:12px 0 4px;flex-wrap:wrap">
      <input id="aiscan-symbol" class="modal-input" style="width:140px;text-transform:uppercase" placeholder="Symbol e.g. NVDA">
      <button class="action-btn" onclick="runAiScan()">▶ Run AI Scan</button>
    </div>
  </div>`;

  if (!idx?.entries?.length) { el.innerHTML = html + '<div class="empty-state">No AI research cached yet.</div>'; return; }

  const entries = [...idx.entries].sort((a, b) => (b.generated_utc ?? "").localeCompare(a.generated_utc ?? ""));
  html += `<div class="card"><div class="card-title">${entries.length} AI Reports Cached</div>`;
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

// ── Boot Sequence ──

function playTransition(callback) {
  const loader = document.getElementById("loading-screen");
  const video = document.getElementById("loading-video");
  
  if (loader && video) {
    loader.style.display = "flex";
    // force reflow
    void loader.offsetWidth;
    loader.classList.remove("hidden");
    
    video.playbackRate = 5.0;
    video.currentTime = 0;
    video.play().catch(e => console.warn("Video autoplay blocked:", e));

    setTimeout(() => {
      if (callback) callback();
      loader.classList.add("hidden");
      setTimeout(() => {
        loader.style.display = "none";
      }, 200);
    }, 900);
  } else {
    if (callback) callback();
  }
}

function showLoadingTransition() {
  const discScreen = document.getElementById("disclaimer-screen");
  if (discScreen) {
    discScreen.style.display = "none";
  }
  playTransition(() => {
    scheduleRefresh();
  });
}

// ── Disclaimer Interaction ──
function handleDisclaimer(choice) {
  const discContent = document.getElementById("disclaimer-content");
  const memeReaction = document.getElementById("meme-reaction");
  const memeText = document.getElementById("meme-text");
  const memeGif = document.getElementById("meme-gif-container");

  // If closed directly, skip meme and go to transition
  if (choice === "close") {
    showLoadingTransition();
    return;
  }

  discContent.style.display = "none";
  memeReaction.style.display = "flex";
  
  if (choice === "ignore") {
    memeText.textContent = "Niceee!!";
    memeText.style.color = "var(--accent)";
    memeGif.innerHTML = `<div class="tenor-gif-embed" data-postid="3521589" data-share-method="host" data-aspect-ratio="1.31915" data-width="100%"><a href="https://tenor.com/view/jeremiah-johnson-robert-redford-nod-of-approval-yes-you-got-it-gif-3521589">Jeremiah Johnson Robert Redford GIF</a>from <a href="https://tenor.com/search/jeremiah+johnson-gifs">Jeremiah Johnson GIFs</a></div>`;
  } else {
    memeText.textContent = "Seriously?? you disappoint me!!";
    memeText.style.color = "var(--red)";
    memeGif.innerHTML = `<div class="tenor-gif-embed" data-postid="3084011263963247857" data-share-method="host" data-aspect-ratio="1" data-width="100%"><a href="https://tenor.com/view/disappointed-wriotags-gif-3084011263963247857">Disappointed Wriotags GIF</a>from <a href="https://tenor.com/search/disappointed-gifs">Disappointed GIFs</a></div>`;
  }
  
  // Tenor script trigger
  const script = document.createElement('script');
  script.src = "https://tenor.com/embed.js";
  script.async = true;
  document.body.appendChild(script);

  // Wait 3.5s then show loading transition
  setTimeout(() => {
    showLoadingTransition();
  }, 3500);
}

document.getElementById("btn-ignore")?.addEventListener("click", () => handleDisclaimer("ignore"));
document.getElementById("btn-ack")?.addEventListener("click", () => handleDisclaimer("ack"));
document.getElementById("btn-close-disc")?.addEventListener("click", () => handleDisclaimer("close"));

// Boot: Fetch data immediately, but do NOT show loader or dashboard. 
// Disclaimer screen is already visible via HTML.
refresh().catch(err => console.error("Boot error:", err));

// Badge age ticks every minute regardless of data poll rate
setInterval(() => updateFreshnessBadge(state.meta?.last_updated_utc ?? null), 60_000);
