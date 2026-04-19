# Web Publishing Plan — `trader.akshaydhenge.uk`

> **Audience:** Claude Code (and future me). This is a self-contained implementation plan for adding a web-publishing pipeline to the local PyQt5 stock monitor so its state is viewable from anywhere on `trader.akshaydhenge.uk`.
>
> **Status:** Design locked with user. Ready to implement.

---

## 1. Goal

Make the local stock-monitor app's state viewable from any browser at `trader.akshaydhenge.uk`, without moving core logic off the user's machine. The site is **read-only**, fed by periodic pushes from the local app. When the app is offline the site keeps showing the last-published snapshot with a clear freshness indicator.

### Product principles

- **Publish, don't serve.** The laptop is never a web origin. Static assets + static JSON on Cloudflare, nothing to keep online.
- **Freshness is first-class.** Every view shows when the data was last refreshed, with color-coded staleness.
- **Fail safe.** If a publish fails, the previous snapshot stays live, and the app retries. Never leave the site broken.
- **Secret-safe by construction.** An allowlist of publishable fields per store — never a denylist. Telegram tokens, API keys, and raw `settings.json` must never leak.

### User-confirmed decisions

| Decision | Choice |
|---|---|
| Hosting | Cloudflare Pages (frontend) + Cloudflare R2 (JSON payloads) |
| Visibility | Fully public |
| Content | Latest scan results, watchlist + targets, recent alerts feed, AI research summaries + 30-day history |
| Triggers | After every Deep/Complete scan, on watchlist changes, on new alerts, and a 15-min safety-net interval |
| Domain | `trader.akshaydhenge.uk` (Pages), `data.trader.akshaydhenge.uk` (R2 bucket custom domain) |

---

## 2. Architecture

```
          ┌─────────────────────────────────┐
          │  PyQt5 app (local, Windows)     │
          │  ┌──────────────────────────┐   │
          │  │ core/web_publisher.py    │   │
          │  │   · serialize (allowlist)│   │
          │  │   · write local staging  │   │
          │  │   · upload to R2 (S3 API)│   │
          │  │   · retry queue          │   │
          │  └──────────────┬───────────┘   │
          └─────────────────┼───────────────┘
                            │ HTTPS PUT (S3-compatible)
                            ▼
          ┌─────────────────────────────────┐
          │  Cloudflare R2 bucket           │
          │  data.trader.akshaydhenge.uk    │
          │    meta.json                    │
          │    latest.json                  │
          │    watchlist.json               │
          │    alerts.json                  │
          │    ai_research/<SYMBOL>.json    │
          │    history/index.json           │
          │    history/YYYY-MM-DD.json      │
          └─────────────────┬───────────────┘
                            │ fetch() from browser
                            ▼
          ┌─────────────────────────────────┐
          │  Cloudflare Pages               │
          │  trader.akshaydhenge.uk         │
          │    index.html + app.js + app.css│
          │    (static, deployed once)      │
          └─────────────────────────────────┘
```

Why two surfaces: the HTML shell rarely changes, but data payloads change on every scan. Keeping them separate means the app just PUTs JSON blobs — no site redeploy per publish.

---

## 3. Cloudflare setup (one-time)

Prerequisites: `akshaydhenge.uk` is already on Cloudflare nameservers. User has MCP connections in Claude Code that can be used for automation, but these steps can also be done manually in the Cloudflare dashboard.

1. **Create R2 bucket** `trader-data` in the Cloudflare account.
2. **Attach custom domain** `data.trader.akshaydhenge.uk` to the bucket (R2 → bucket → Settings → Custom Domains). This auto-creates the CNAME in DNS.
3. **Enable public access** on the bucket (R2 dashboard → Settings → Public access).
4. **Set CORS policy** on the bucket so the Pages site can fetch JSON:
   ```json
   [
     {
       "AllowedOrigins": ["https://trader.akshaydhenge.uk"],
       "AllowedMethods": ["GET", "HEAD"],
       "AllowedHeaders": ["*"],
       "MaxAgeSeconds": 300
     }
   ]
   ```
5. **Create R2 API token** with `Object Read & Write` scoped to the `trader-data` bucket only. Capture:
   - Access Key ID
   - Secret Access Key
   - Account ID (shown on the R2 dashboard)
   - Endpoint URL: `https://<account_id>.r2.cloudflarestorage.com`
6. **Create Pages project** `trader-frontend` — connect it to a new GitHub repo (see §9) or use Direct Upload via `wrangler`.
7. **Bind `trader.akshaydhenge.uk`** to the Pages project (Pages → project → Custom domains).
8. **Verify** both subdomains resolve and serve content with correct TLS.

---

## 4. Backend — new module `core/web_publisher.py`

### 4.1 Public interface

```python
class WebPublisher(QThread):
    publish_started    = pyqtSignal(str)           # trigger name
    publish_succeeded  = pyqtSignal(str)           # ISO8601 UTC ts
    publish_failed     = pyqtSignal(str, str)      # trigger, error message

    def __init__(self, settings_store, watchlist_store, scan_results_store,
                 ai_research_store, alerts_log, parent=None): ...

    def request_publish(self, trigger: str) -> None:
        """Thread-safe: enqueues a publish request. Called from main thread slots."""

    def stop(self) -> None: ...
```

Internally a `QThread` run-loop that:

1. Blocks on an internal queue (`queue.Queue`) of trigger names.
2. For each trigger: serializes → writes staging → uploads to R2 → on success clears the retry queue, on failure appends to retry queue.
3. Coalesces rapid-fire triggers: if several arrive within 2 seconds, collapse to one publish (prevents thrash when multiple signals fire for the same scan).

### 4.2 Serialization — allowlist per store

Add a `PUBLISHABLE_FIELDS` constant in each store module. `WebPublisher` imports and uses only those. Example:

```python
# core/watchlist_store.py
PUBLISHABLE_FIELDS = ("symbol", "low", "high", "notes", "last_price",
                      "last_price_ts", "target_hit_state")
```

Fields NEVER to publish (enforced by test in §11): anything from `settings.json`, the Telegram bot token, `anthropic_api_key`, `ollama_host`, `r2` credentials.

### 4.3 Staging and atomic swap

1. Write all JSON files to `data/web_publish/<rand>/` locally. This is a scratch copy for debugging and for offline inspection.
2. Upload each file to R2 with `Cache-Control: no-cache, max-age=0`.
3. Upload `meta.json` **last** — it's the freshness pointer the frontend reads first. Writing it last means the frontend never sees a torn state (new `meta` pointing at old payload files).

### 4.4 Retry queue

`data/web_publish_queue.json` — list of pending triggers with attempt counts. On startup the publisher drains the queue if any entries remain. Backoff: 30s / 2m / 10m / 1h (capped).

### 4.5 Thread-safety rules (inherits project convention)

- Never call QThread methods directly across threads — all signaling via Qt signals/slots.
- `request_publish()` is the only public entry point; it pushes onto a `queue.Queue` which the thread reads.
- `stop()` sets `_running = False` and puts a sentinel on the queue.
- Sleeps inside the run loop happen in 1-second chunks so `stop()` is responsive (matches `PricePoller`, `StockScanner` pattern).

---

## 5. Settings additions (`core/settings_store.py`)

Add to `_DEFAULTS`:

```python
"web_publishing_enabled": False,
"web_publish_interval_minutes": 15,   # safety-net tick
"web_public_url": "https://trader.akshaydhenge.uk",
"r2": {
    "account_id": "",
    "access_key_id": "",
    "secret_access_key": "",
    "bucket": "trader-data",
    "endpoint_url": "",               # auto-derived from account_id if blank
    "public_base_url": "https://data.trader.akshaydhenge.uk",
},
```

Settings dialog gets a new **Web Publishing** tab:

- Enable/disable checkbox
- R2 credential fields (password-masked for the secret)
- Interval spinner (5 – 120 min)
- **Test connection** button — does a dry-run PUT of `meta.json` with a marker key, then DELETE.
- **Publish now** button (also exposed on the main toolbar).

Secret-masking: when serializing settings for any logging or publishing path, `get_safe_settings()` must redact the R2 secret, Telegram token, and Anthropic API key.

---

## 6. GUI changes (`gui/main_window.py`)

### 6.1 Wiring

- Instantiate `WebPublisher` with refs to stores and the alerts log. Hold a ref to prevent GC.
- Connect these to `WebPublisher.request_publish`:
  - `StockScanner.deep_scan_complete` → `request_publish("deep_scan_complete")`
  - `StockScanner.complete_scan_complete` → `request_publish("complete_scan_complete")`
  - `WatchlistStore.watchlist_changed` → `request_publish("watchlist_changed")` *(signal needs to be added — see §6.3)*
  - Alert emission paths (new top-5 / top-10 / target hit) → `request_publish("alert")`
- A new `QTimer(interval_minutes * 60 * 1000)` calls `request_publish("interval")`.

### 6.2 UI elements

- **Toolbar button**: "🌐 Publish now" — calls `request_publish("manual")`.
- **Status-bar badge**: compact widget showing last-published time, colored:
  - Green: <1h
  - Amber: 1–24h
  - Red: >24h or publish currently failing
  - Tooltip shows the exact UTC timestamp and the last trigger.
- Clicking the badge opens a small popover with publish history (last 10 events: timestamp, trigger, outcome).

### 6.3 New signal on `WatchlistStore`

`WatchlistStore` currently mutates state without emitting. Add:

```python
watchlist_changed = pyqtSignal(str, str)   # (action, symbol)
```

Emit from `add()`, `remove()`, `revise()` after persisting.

---

## 7. Telegram command additions

In `notifiers/telegram_command_poller.py`:

- `/publish` — emit `cmd_publish` signal. `MainWindow` slot calls `request_publish("telegram")`, replies `📡 Publish queued.`
- `/lastpublished` — reply with last-published timestamp and staleness age.

Optional: after a successful publish, if `telegram_publish_confirmation_enabled` is set, send a one-liner: `📡 Site updated (trigger: deep_scan_complete) — https://trader.akshaydhenge.uk`.

Update the bot-commands table in `CLAUDE.md`.

---

## 8. Data contract — JSON files written to R2

All payloads are UTF-8 JSON. All timestamps are ISO 8601 UTC (`Z` suffix).

### `meta.json` (single source of truth for freshness)

```json
{
  "schema_version": 1,
  "last_updated_utc": "2026-04-17T18:42:11Z",
  "trigger": "deep_scan_complete",
  "app_version": "0.3.0",
  "files": {
    "latest":      { "etag": "...", "bytes": 4821 },
    "watchlist":   { "etag": "...", "bytes": 2104 },
    "alerts":      { "etag": "...", "bytes": 6382 },
    "history_idx": { "etag": "...", "bytes": 612  }
  }
}
```

### `latest.json` — latest scan results

```json
{
  "deep": {
    "scan_timestamp_utc": "2026-04-17T18:30:04Z",
    "universe_size": 503,
    "top10": [
      {
        "symbol": "NVDA",
        "total_score": 87.2,
        "value": 72.1,
        "growth": 95.0,
        "technical": 90.3,
        "price": 912.44,
        "market_cap": 2245000000000,
        "sector": "Technology"
      }
    ]
  },
  "complete": {
    "scan_timestamp_utc": "2026-04-17T14:00:12Z",
    "universe_size": 1498,
    "top5": [ /* same shape */ ]
  }
}
```

### `watchlist.json`

```json
{
  "updated_utc": "2026-04-17T18:42:11Z",
  "entries": [
    {
      "symbol": "AAPL",
      "low": 170.00,
      "high": 210.00,
      "notes": "accumulate below 175",
      "last_price": 184.22,
      "last_price_ts": "2026-04-17T18:41:55Z",
      "target_hit_state": "none"     // none | low_hit | high_hit
    }
  ]
}
```

### `alerts.json` — last 200 events, newest first

```json
{
  "updated_utc": "2026-04-17T18:42:11Z",
  "events": [
    {
      "ts_utc": "2026-04-17T18:30:04Z",
      "type": "new_top5",            // price_low_hit | price_high_hit | new_top10 | new_top5
      "symbol": "NVDA",
      "context": {
        "total_score": 87.2,
        "previous_rank": 7,
        "new_rank": 3
      }
    }
  ]
}
```

### `history/index.json`

```json
{
  "snapshots": [
    { "date": "2026-04-17", "deep_top10_path": "history/2026-04-17.json" },
    { "date": "2026-04-16", "deep_top10_path": "history/2026-04-16.json" }
  ],
  "retention_days": 30
}
```

Rotation: on each publish, if today's snapshot doesn't exist yet, write it; drop any entries older than 30 days.

### `ai_research/<SYMBOL>.json`

```json
{
  "symbol": "NVDA",
  "generated_utc": "2026-04-17T17:05:22Z",
  "provider": "claude",
  "model": "claude-haiku-20240307",
  "summary": "...",
  "sections": {
    "business_overview": "...",
    "bull_case":  ["...", "..."],
    "bear_case":  ["...", "..."],
    "valuation":  "...",
    "catalysts":  ["..."],
    "risks":      ["..."]
  }
}
```

Published when `AIResearcher.research_complete` fires. The site's AI-research index reads from a manifest:

### `ai_research/index.json`

```json
{
  "updated_utc": "2026-04-17T17:05:22Z",
  "entries": [
    { "symbol": "NVDA", "generated_utc": "2026-04-17T17:05:22Z", "provider": "claude" }
  ]
}
```

---

## 9. Frontend — `trader.akshaydhenge.uk`

### 9.1 Repo layout

Create a small separate repo `trader-frontend` (or a subdir `web/` inside this repo — pick one and stay consistent; the separate repo is cleaner for Pages Git integration).

```
trader-frontend/
  index.html
  app.js
  app.css
  manifest.webmanifest
  favicon.svg
  icons/ (192, 512)
  README.md
```

No build step. No frameworks required. If a small React or Preact setup is preferred, keep the build output in `dist/` and point Pages at that — but vanilla JS is a better fit for the size.

### 9.2 Page structure

A single page with tabs:

1. **Top Picks** — Deep top-10 table + Complete top-5 table, sortable columns, sub-score breakdowns with small inline bars.
2. **Watchlist** — entries with last price, targets, target-hit chips (green/red).
3. **Alerts** — reverse-chronological feed, filter by type.
4. **AI Research** — searchable symbol list; click to expand the structured report.
5. **History** — last 30 days of Deep top-10; a simple "appearance count" leaderboard showing symbols that keep recurring.

### 9.3 Header — freshness badge

Shows `Last updated: 2m ago` with color:

- Green: last_updated_utc within 1 hour
- Amber: 1–24 hours
- Red: >24 hours

Tooltip/hover: absolute UTC timestamp, trigger name, and app version.

### 9.4 Data loading

```js
const DATA_BASE = "https://data.trader.akshaydhenge.uk";

async function refresh() {
  const meta = await fetchJSON(`${DATA_BASE}/meta.json?v=${Date.now()}`);
  if (meta.last_updated_utc !== state.lastSeen) {
    state.lastSeen = meta.last_updated_utc;
    await Promise.all([loadLatest(), loadWatchlist(), loadAlerts(), loadHistory()]);
    renderAll();
  }
  updateFreshnessBadge(meta.last_updated_utc);
}

setInterval(refresh, 60_000);
```

Cache-busting via `?v=<ts>` query string; R2 is set to `no-cache` anyway, but this handles any intermediate caches.

### 9.5 Accessibility + mobile

- Semantic HTML; all tabs are `<button role="tab">`.
- Mobile-first responsive CSS (Grid/Flexbox, no framework needed).
- Dark mode via `prefers-color-scheme`.
- `manifest.webmanifest` + icons so "Add to Home Screen" works on iOS/Android.

### 9.6 Error states

- Fetch error on first load: show a banner "Can't reach data server. Last known snapshot not available." with a retry button. If any cached payloads are in browser memory from a previous tab, keep rendering them.
- Stale data (>24h): red badge plus a subtle banner "Data may be outdated — the monitor hasn't published in >24h."

---

## 10. Triggers (decided) and coalescing

| Trigger | Source | Debounce |
|---|---|---|
| `deep_scan_complete` | `StockScanner.deep_scan_complete` | 2s coalesce |
| `complete_scan_complete` | `StockScanner.complete_scan_complete` | 2s coalesce |
| `watchlist_changed` | `WatchlistStore.watchlist_changed` | 2s coalesce |
| `alert` | alert emission paths | 2s coalesce |
| `interval` | 15-min `QTimer` | skipped if another publish ran within the last 14 min |
| `manual` | toolbar button or `/publish` | no debounce |
| `telegram` | `/publish` bot command | same as manual |

Coalescing: if multiple triggers arrive inside the 2s window, one publish is performed and the trigger recorded in `meta.json` is the highest-priority one in that batch. Priority order: `manual` > `deep_scan_complete` > `complete_scan_complete` > `alert` > `watchlist_changed` > `interval`.

---

## 11. Tests

The project has no test infra today. Add a minimal `tests/` folder using `unittest` (stdlib; no new dependency):

- **`tests/test_publishable_fields.py`** — load `settings.json` with dummy secrets, serialize every store's publishable view, assert no substring match for the secret values. This is the single most important test: it prevents future additions from leaking credentials.
- **`tests/test_web_publisher_offline.py`** — mock the R2 client; simulate upload failure; assert retry queue is written and replayed on next attempt.
- **`tests/test_schema_shape.py`** — for each payload file, assert required keys and types.

Add a one-liner to `CLAUDE.md`: `py -3.9 -m unittest discover tests`.

---

## 12. Rollout phases

### Phase 1 — Plumbing (no Cloudflare yet)

- [ ] Add `core/web_publisher.py` skeleton with local-only writer.
- [ ] Add settings fields and Web Publishing settings tab.
- [ ] Add `WatchlistStore.watchlist_changed` signal.
- [ ] Wire signals in `MainWindow`.
- [ ] Add toolbar button + status-bar badge (reading last local write).
- [ ] Implement serialization + allowlist + `tests/test_publishable_fields.py`.
- **Exit criteria:** clicking "Publish now" writes valid JSON to `data/web_publish/` and the test passes.

### Phase 2 — R2 upload

- [ ] Provision R2 bucket + custom domain + CORS + API token (per §3).
- [ ] Add boto3 dependency (`py -3.9 -m pip install boto3`); document in `requirements.txt` if one exists, create if not.
- [ ] Implement `R2Client` wrapper inside `web_publisher.py` (S3-compatible).
- [ ] Implement atomic-ish upload order (all files, then `meta.json` last).
- [ ] Implement retry queue with persistent `data/web_publish_queue.json`.
- [ ] Add **Test connection** button to settings.
- **Exit criteria:** clicking "Publish now" results in fresh JSON on `https://data.trader.akshaydhenge.uk/meta.json` and retries after simulated failure.

### Phase 3 — Frontend

- [ ] Create `trader-frontend` repo (or subdir) per §9.1.
- [ ] Build all 5 tabs against real data from R2.
- [ ] Freshness badge + banner.
- [ ] Mobile + dark mode + PWA manifest.
- [ ] Deploy to Cloudflare Pages, bind custom domain.
- **Exit criteria:** `https://trader.akshaydhenge.uk` renders current scan/watchlist/alerts/AI/history, mobile layout works, "Last updated" updates within 60s of a publish.

### Phase 4 — Polish & automation

- [ ] Add Telegram `/publish` and `/lastpublished` commands.
- [ ] Optional Telegram confirmation ping after publish.
- [ ] Add 15-min safety-net timer.
- [ ] Coalescing logic (§10).
- [ ] Add the remaining tests in §11.
- [ ] Update `CLAUDE.md`: new module, new commands, new tests, new settings.
- **Exit criteria:** 24-hour soak test — app left running, publishes fire on every trigger, freshness stays green, no credentials ever appear in published JSON.

---

## 13. Security and privacy checklist

- [ ] R2 secret key is stored in `settings.json` only; `settings.json` is already gitignored (confirm).
- [ ] `PUBLISHABLE_FIELDS` allowlist for every store (watchlist, settings, ai_research, scan_results).
- [ ] Allowlist test covers every secret-bearing setting key.
- [ ] CORS on R2 restricted to `https://trader.akshaydhenge.uk`.
- [ ] R2 API token scoped to the single bucket.
- [ ] No analytics, no third-party fonts, no tracking on the site.
- [ ] HSTS is on by default via Cloudflare.
- [ ] Confirm: "fully public" is intentional — watchlist symbols, targets, and AI theses will be visible to anyone with the URL. Add a short disclaimer on the site footer: "For personal use — not investment advice."

---

## 14. Open follow-ups (non-blocking, nice-to-have)

- **Historical price charts** on each watchlist/top-pick symbol (pull from yfinance into a small series file on publish).
- **RSS feed** of alerts, served as another R2 object (`alerts.rss`).
- **Redaction mode** — a settings toggle that publishes placeholder "tracking paused" payloads when the user is AFK.
- **Multi-device** — if ever useful, swap "fully public" for Cloudflare Access with Google login (no code change on the app side).
- **Analytics without tracking** — Cloudflare Web Analytics is privacy-friendly and free if the user wants to see traffic.

---

## 15. Acceptance criteria (end-to-end)

1. Run `py -3.9 main.py`; settings → Web Publishing → fill R2 creds → Test connection succeeds.
2. Click "Publish now" — `meta.json` on `data.trader.akshaydhenge.uk` updates; `last_updated_utc` is within 5 seconds of current time.
3. `trader.akshaydhenge.uk` loads, renders all 5 tabs, freshness badge green.
4. Add a symbol to the watchlist — within 3 seconds, site reflects the new symbol on the next poll.
5. Kill the app, wait 90 minutes; site still loads, freshness badge is amber, no errors.
6. Turn app back on; next scan triggers a publish; badge returns to green.
7. Grep every published JSON file for the substrings of the Telegram token, Anthropic API key, and R2 secret — zero matches.

---

**End of plan.**
