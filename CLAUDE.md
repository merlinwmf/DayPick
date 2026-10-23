# DayPick

A Windows desktop app that scans a pooled set of custom Moomoo watchlists
against a growing library of technical "pick logics" (OR-combined - a
stock qualifies if it satisfies at least one enabled logic) and writes the
survivors into the Moomoo custom list `Pick`. Each pick's log entry lists
every enabled logic that stock actually satisfied. Sibling project to
`DayTrade` (a different app - live breakout monitor, still Tkinter-based)
but fully independent: separate folder, separate exe, no shared code.

**UI stack: pywebview, not Tkinter.** The app started as Tkinter (styled
with a shared iOS16 theme kit, `ios16_ui_kit/theme.py`, hand-drawing
rounded corners/shadows/gradients on a `Canvas`), but that rendering never
matched real CSS quality (visibly flatter, no true anti-aliasing/blur).
It was migrated to **pywebview**: the UI is a real HTML/CSS/JS page
(`web/daypick.html`) rendered in the OS's native web view (Edge WebView2 on
Windows), with Python as a thin backend exposed to JS via `js_api`. This
gets genuine browser-quality rendering (real shadows, gradients, anti-
aliasing, flex-wrap chips) essentially for free. `theme.py` is no longer
used by this app (DayTrade still uses it). The CSS itself
(`web/ios16.css`) is a copy of the shared design kit at
`E:\Coding\Stock\UIDesign\ios16_web_kit\ios16.css` — PyInstaller needs
assets inside the app's own folder tree, so it's copied in rather than
referenced across folders; keep them in sync by hand if the shared kit is
tuned. `UIDesign\ios16_ui_kit` is the sibling Tkinter equivalent, still
used by DayTrade.

## Files

- `picker_engine.py` — all backend logic (no UI, untouched by the pywebview
  migration). Connects to Futu OpenD, pools/dedupes selected watchlists,
  evaluates pick logics, writes results.
- `daypick_app.py` — the pywebview host: creates the window, loads
  `web/daypick.html`, and exposes an `Api` class to JS (`pywebview.api.*`)
  that wraps `picker_engine` calls. Long-running engine calls spawn their
  own background thread and push updates into the page via
  `window.evaluate_js('someJsFunction(args)')` rather than blocking the
  JS→Python call. Run with `python daypick_app.py`.
- `web/daypick.html` — the entire UI: layout, styling, and the JS that
  calls `pywebview.api.*` and defines the `appStatus`/`appGroups`/
  `appPoolCount`/`appRunning`/`appResults` functions Python pushes into.
- `DayPick.spec` — PyInstaller build spec (see Packaging below).
- `daypick_YYYYMMDD.log` — verbose engineering/debug log (per-stock
  diagnostics for every logic, every run). Not shown in the GUI.
- `picks_YYYYMMDD.log` — compact machine-readable results log, pipe-delimited
  `timestamp|symbol|logic_names_passed`. This is what the GUI's "Logs" panel
  reads and renders as a table.
- `daypick_config.json` — persisted UI state (selected pool list(s), pick
  logic names/enabled flags), written next to the exe/script.

## Runtime dependency: Futu OpenD

Same as DayTrade: nothing works without Futu's OpenD gateway running and
logged in on `127.0.0.1:11111`. If OpenD isn't up, `picker_engine.connect()`
retries for up to 5 minutes then raises `EngineError`.

## How the app works

1. On launch, the page boots (see the pywebview gotcha below), calls
   `pywebview.api.get_initial_state()` for the saved logic/pool config,
   then `pywebview.api.start_app()`, which connects to OpenD and lists the
   user's custom watchlists as **multi-select chips** (`get_custom_groups`,
   same CUSTOM-only filter as DayTrade), excluding a hardcoded set of lists
   that should never be used as pick pools (`EXCLUDED_GROUPS` in
   `picker_engine.py`: `ETF`, `反指`, `股息`, `大宗`, `传统`).
2. Selecting one or more lists pools their members and **de-duplicates by
   code** — a stock in two selected lists is only evaluated once. A label
   shows the combined unique count, not the stock details.
3. The **Market Cap Filter** row (between Start Pick and Pick Logic) is a
   user-configurable global filter: an on/off toggle plus a $1B/$10B/$100B
   preset (`MARKET_CAP_PRESETS` in `picker_engine.py`). Toggled off, no
   market cap requirement applies to logics that don't have their own
   override. **Logic 3 always requires >$10B regardless of this setting**
   (`market_cap_override` on `PickLogic` - see `LOGIC3_MARKET_CAP_OVERRIDE`)
   - if the global filter is set to $100B, Logic 3 still only needs $10B;
     a logic's own override always wins over the global setting.
4. Clicking "Start Pick" runs `run_pick`, a two-stage funnel:
   - **Stage 0 (cheap):** batch `get_market_snapshot` across the whole pool.
     Each enabled logic's *effective* required cap is its own override if
     it has one, else the global filter's threshold (or no requirement at
     all if the global filter is off). Stage 0 can only pre-drop stocks
     below the *lowest* requirement among enabled logics - if even one
     enabled logic needs no cap check (global off, no override), nothing
     gets pre-dropped and every logic's cap check happens per-stock in
     stage 1 instead.
   - **Stage 1 (kline quota):** for survivors, fetch one historical daily
     kline series per unique symbol (`request_history_kline`, `K_DAY`,
     `QFQ`). Each enabled logic independently checks the stock's market cap
     against its own required threshold before evaluating its pattern -
     failing that check skips `evaluate()` entirely for that logic.
   - A stock is written to the `Pick` list if it passes **any one or more**
     of the enabled logics (OR combination); its results entry lists every
     enabled logic it actually satisfied, not just the first match.
5. **"Today" always comes from the live snapshot/quote**
   (`get_stock_quote`'s `open_price`/`high_price`/`low_price`/`last_price`/
   `prev_close_price`/`volume`), never from `request_history_kline` — this
   is what lets DayPick run intraday (today = live, still-forming bar) or
   after close (today = the quote's final settled values) with the same
   code path. Historical daily bars cover everything strictly before today.
6. On finish, the `Pick` list is **replaced** (cleared via
   `ModifyUserSecurityOp.DEL` then re-added via `ADD`) with exactly that
   run's results — not accumulated across runs — and one line per pick is
   appended to `picks_YYYYMMDD.log`.
7. Pick logics can be renamed (persisted) and toggled on/off live; their
   descriptions are static text in code — there is deliberately no
   description-edit UI, since descriptions should only change when the
   underlying logic itself is reprogrammed.

## Current pick logics

Baseline window for Logic 1 & 2 is always the 10 most recently *completed*
trading days (fixed relative to today, not to whichever day is being
tested). 振幅 (amplitude) for Logic 4/5's consolidation range is
`(range_high - range_low) / range_high`.

- **Logic 1: Today Big Gain** — today's 涨幅 ≥ 5× the 10-day max 涨幅 AND
  today's volume ≥ 2× the 10-day average volume.
- **Logic 2: Recent Breakout Pullback** — any day in the last 5 (incl.
  today) with 涨幅 ≥ 6× the 10-day max and volume ≥ 1.5× the 10-day average
  (most recent qualifying day wins if several do); current price must sit
  in the lower 50% of that day's `[low, high]` range.
- **Logic 3: EMA20/250 Cross, Stabilize** — EMA20 golden-crosses then
  death-crosses EMA250; today must be the first day the trailing 3-day low
  makes no new low since that death cross (ties OK). Always requires market
  cap >$10B regardless of the Market Cap Filter setting (see above).
- **Logic 4: Range Bottom** — stock has consolidated ≥20 trading days
  (dynamically extended as far back as amplitude stays ≤20% stocks/10%
  ETFs); current price within 2% of that range's low.
- **Logic 5: Range Breakout** — same range as Logic 4; today breaks ≥5%
  above the range high with volume ≥1.5× the 20-day average.
- **Logic 6: Crash Stabilization** — a single-day drop ≥15% within the last
  20 days (most recent one if several); today must be the first day the
  trailing 2-day low is *strictly* higher than the lowest low since that
  crash.

All thresholds are named constants at the top of `picker_engine.py` (e.g.
`L1_GAIN_MULT`, `RANGE_AMP_CAP_STOCK`, `L6_CRASH_PCT`) — tune them there
without touching logic bodies. Adding logic #7+ is: write one new
`PickLogic` subclass, add it to `default_pick_logics()`. No other file
needs to change.

## Non-obvious gotchas (learned the hard way this session)

- **`get_market_snapshot` fails its ENTIRE batch if even one code in it is
  inaccessible** (confirmed empirically: `US.BYDDY`/`US.TCEHY` - US OTC ADRs
  this account has no data permission for - each poisoned their whole
  200-code chunk with `ret=-1, "US OTC market quote is not available for
  ..."`, silently losing market cap data for every other stock in that
  chunk too). This is why "no stock passed the market cap filter" could
  happen even with a pool full of obviously-large-cap stocks. Fixed in
  `get_market_caps`/`_fetch_snapshot_batch`: on a batch failure, bisect and
  retry each half recursively so only the genuinely bad code(s) get
  dropped, not their innocent neighbors. Also note: crypto (`CC.*`) and
  ETF-type snapshots don't populate `total_market_val` at all (not a
  permission issue, just not a meaningful field for those instrument
  types) - which means ETFs currently can never pass the market-cap stage
  and so never reach Logic 4/5's ETF amplitude branch. Not yet fixed.
- **`get_user_security` (reading a watchlist's members) is rate-limited to
  10 calls per 30 seconds** ("Get Watchlist Groups request failed due to
  high frequency"). `get_pooled_members` calls it once per selected list in
  a tight loop — fine for the handful of lists a user would realistically
  multi-select, but don't be tempted to loop it over *all* groups rapidly
  (e.g. for a "show all list sizes" diagnostic) without pacing calls.
- **Historical kline quota is per unique symbol, not per request or per
  day.** Once a symbol is pulled, it's free to re-query (any date range)
  until its quota slot **releases 7 days later** (confirmed via Futu's
  official docs, not the ~30 days initially guessed). The quota ceiling
  itself scales with account assets/trading activity: 100 (assets <10,000
  HKD) / 300 (≥10,000 HKD) / 1,000 / 2,000 — check via
  `quote_ctx.get_history_kl_quota(get_detail=True)`.
- **`get_stock_quote` requires `subscribe(..., subscribe_push=False)`
  first**, every time, same as DayTrade.
- **Market cap comes from `get_market_snapshot`'s `total_market_val`
  field**, not from any field on `get_stock_quote` or `request_history_kline`.
- **`Get-Process`'s `Responding` flag on this app is flaky right after
  launch and can read `False` for 10-30+ seconds even on a fully healthy
  run** - confirmed by running the exact same launch back to back: one
  attempt showed `Responding: False` with zero CPU growth for 50+ seconds
  (looked exactly like a deadlock, `SetForegroundWindow` even failed to
  bring it to front), the very next attempt with identical code showed
  `Responding: True` within seconds. The noisy cause is pywebview/pythonnet
  spamming stderr with `[pywebview] Error while processing
  window.native.AccessibilityObject.Bounds...: maximum recursion depth
  exceeded` and similar `CoreWebView2Controller members can only be
  accessed from the UI thread` COM interop errors - these come from
  something (Windows accessibility tooling, most likely) probing the
  WebView2 control's automation properties, not from anything in this
  app's own code, and they appear to be non-fatal noise rather than an
  actual crash. Don't chase this as a code bug: if a launch looks hung,
  fully clear the process and `msedgewebview2.exe` children first (see the
  force-kill gotcha above) and just retry once before assuming the code
  broke.
- **Win32 UI automation in this dev environment needs an "Alt-tap" before
  `SetForegroundWindow`** to reliably steal focus — the editor/IDE window
  in this session kept stealing focus back between separate PowerShell tool
  invocations, causing screenshots/clicks to silently land on the wrong
  window. Fix: `keybd_event(VK_MENU, down)` then `up` immediately before
  `SetForegroundWindow`, and verify with `GetForegroundWindow() == hwnd`
  before clicking — do the whole focus-grab-click-screenshot sequence in
  **one** PowerShell invocation, since focus can get stolen back in the gap
  between separate tool calls.
- **`Get-Process`'s `MainWindowHandle` is more reliable than `FindWindow`
  by title** for locating this app's window in test scripts — `FindWindow`
  returned a null handle even though the process clearly had that exact
  window title.
- **`window.addEventListener('pywebviewready', ...)` can silently never
  fire.** If `window.pywebview.api` is already injected by the time the
  page's `<script>` runs (more likely when the page loads from bundled
  resources in the packaged exe than from disk in dev mode - a real race
  that reproduced exactly this way: worked in `python daypick_app.py`,
  hung forever on "Connecting to OpenD..." with empty Pick Logic rows in
  the built exe), the event already fired and a bare listener waits
  forever. Guard it: `if (window.pywebview && window.pywebview.api) {
  boot() } else { window.addEventListener('pywebviewready', boot) }`.
- **PyInstaller 6.x's onedir output nests bundled data under
  `dist/<name>/_internal/`**, not directly beside the exe (e.g.
  `web/daypick.html` lands at `dist/DayPick/_internal/web/daypick.html`).
  `sys._MEIPASS` already points inside `_internal` for onedir builds too,
  so `Path(sys._MEIPASS) / 'web' / 'daypick.html'` resolves correctly
  without special-casing onedir vs onefile - just don't assume the naive
  `dist/<name>/web/...` path when checking the build manually.
- **Force-killing `DayPick.exe` repeatedly (e.g. during rapid rebuild-test
  cycles) can leave stale `msedgewebview2.exe` child processes behind**,
  and the NEXT launch then hangs on startup — process alive but
  `Get-Process`'s `Responding` shows `False`, stuck indefinitely on
  "Connecting to OpenD..." with nothing rendered. This looked exactly like
  a code bug (chased it as a `pywebviewready` race, then a `debug=True`
  side effect) before realizing every `msedgewebview2.exe` from the prior
  run was still alive. Fix: `Stop-Process` the exe AND
  `Get-Process msedgewebview2 | Stop-Process -Force` before relaunching;
  a clean launch after that works immediately. Closing the window normally
  (not force-killing) avoids this entirely.
- **Rebuilding wipes `dist/DayPick/`** (PyInstaller's COLLECT step removes
  the old output dir first), which deletes any `daypick_config.json` /
  `*.log` files that only ever lived in a previous build's folder. Expected
  behavior, not a bug, but it means real usage state doesn't survive a
  rebuild unless the exe's own folder is preserved/backed up first.

## Packaging

Requires `pywebview` (`pip install pywebview`; pulls in `pythonnet`/
`clr_loader` on Windows for the WebView2 backend) in addition to `futu-api`
and `pandas`.

```
python -m PyInstaller --noconfirm DayPick.spec
```
Output: `dist/DayPick/DayPick.exe` (double-click to run — keep the whole
`DayPick` folder together, don't move just the exe; note bundled data now
lands under `dist/DayPick/_internal/`, see the gotcha above). Same two
`futu-api` packaging fixes as DayTrade's spec (force `futu/common/pb/*.py`
and `futu/VERSION.txt` into `datas`), plus `web/daypick.html` explicitly
added to `datas` (PyInstaller doesn't pick up non-Python asset files on its
own). Same `--onedir` + exclusions (matplotlib/scipy/PIL/openpyxl/Qt) to
dodge the Windows Defender scan hang seen on `--onefile` builds in
DayTrade. No `ios16_ui_kit` `pathex` entry needed anymore - `theme.py` isn't
used by this app since the pywebview migration.

## Testing approach used in this project

Same as DayTrade: no automated test suite. Verified this session by running
`picker_engine.run_pick` directly against live watchlists (confirmed
realistic diagnostics and an actual pick being written to and read back
from Moomoo's `Pick` list), then by running the actual GUI (both
`python daypick_app.py` and the packaged exe) and using Win32 API calls
from PowerShell to screenshot the live window and simulate clicks (see the
Alt-tap gotcha above). This caught the `FindWindow`/window handle issue,
the `pywebviewready` race condition (only reproduced in the packaged exe,
not dev mode), and confirmed the Start button's running/idle states, live
push-based status updates during a real run, multi-select pool chips, and
config persistence all work correctly end-to-end.
