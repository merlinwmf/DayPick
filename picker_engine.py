"""
Backend engine for DayPick: scans a pooled set of custom Moomoo watchlists,
evaluates every enabled PickLogic (OR-combined - a stock is picked if it
satisfies at least one enabled logic) against each candidate, and writes
the survivors into the Moomoo custom list "Pick". Each pick's log/results
entry lists every enabled logic that stock actually satisfied, not just
the first one matched.

Pure logic, no UI. Driven by daypick_app.py via callbacks.
"""

import os
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from futu import (
    OpenQuoteContext, RET_OK,
    SubType, KLType, AuType,
    ModifyUserSecurityOp,
)

# ---------------------------------------------------------------------------
# Config / tunable thresholds - named constants so pick logic can be tuned
# without touching the logic bodies.
# ---------------------------------------------------------------------------
OPEND_HOST = '127.0.0.1'
OPEND_PORT = 11111
PICK_LIST_NAME = 'Pick'

# Custom watchlists that should never show up as selectable pick pools.
EXCLUDED_GROUPS = {'ETF', '反指', '股息', '大宗', '传统'}

# User-selectable global market cap filter presets (UI: 1B / 10B / 100B +
# an enable/disable toggle). A logic with its own market_cap_override
# ignores this entirely - see PickLogic and run_pick.
MARKET_CAP_PRESETS = [1_000_000_000, 10_000_000_000, 100_000_000_000]
MARKET_CAP_DEFAULT = 10_000_000_000
HISTORY_LOOKBACK_DAYS_DEFAULT = 30
HISTORY_LOOKBACK_DAYS_LONG = 750  # ~3 years: EMA250 warm-up + cross search room
SNAPSHOT_CHUNK_SIZE = 200  # defensive chunking for get_market_snapshot

# Logic 1
L1_GAIN_MULT = 5
L1_VOL_MULT = 2
BASELINE_DAYS = 10  # "last 10 days" fixed window for Logic 1 & 2

# Logic 2
L2_GAIN_MULT = 6
L2_VOL_MULT = 1.5
L2_LOOKBACK_DAYS = 5  # "last 5 days any day" including today
L2_LOW_BAND_PCT = 0.5  # lower 50% of the big green bar's range

# Logic 3
EMA_SHORT_SPAN = 20
EMA_LONG_SPAN = 250
L3_STABILIZE_WINDOW = 3

# Logic 4 / 5 - consolidation range definition
RANGE_MIN_DAYS = 10
RANGE_MAX_DAYS = 45
RANGE_AMP_CAP_STOCK = 0.30  # range distance must stay under this fraction of max_high
RANGE_AMP_CAP_ETF = 0.15
RANGE_CONTAINMENT_PCT = 0.95  # max_high/min_low = this percentile of the window's highs/lows
RANGE_TOUCH_TOLERANCE_PCT = 0.01  # "within 1% of range distance" counts as touching that side
RANGE_TOUCH_MIN_COUNT = 2  # bars required to have touched each side of the range
RANGE_TOP_ZONE_PCT = 0.10  # "reach top" = top 10% of range distance
RANGE_BOTTOM_ZONE_PCT = 0.10  # "reach bottom" = bottom 10% of range distance
L5_BREAKOUT_ZONE_PCT = 0.10  # breakout = this much of range distance above max_high
L5_VOL_MULT = 1.2  # vs the range bars' own average volume

# Logic 6
L6_CRASH_PCT = -15.0  # single-day change_rate threshold
L6_CRASH_LOOKBACK_DAYS = 20
L6_STABILIZE_WINDOW = 2

ET = ZoneInfo('America/New_York')

log = logging.getLogger('DayPick')

_LOG_DIR = '.'


class EngineError(Exception):
    pass


# ---------------------------------------------------------------------------
# Logging / misc helpers
# ---------------------------------------------------------------------------
def now_et():
    return datetime.now(ET)


def to_symbol(code):
    """'US.AAPL' -> 'AAPL'"""
    return code.split('.', 1)[1] if '.' in code else code


def configure_logging(directory='.'):
    global _LOG_DIR
    _LOG_DIR = directory
    day = now_et().strftime('%Y%m%d')
    handler = logging.FileHandler(f'{directory}/daypick_{day}.log', encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.propagate = False


def picks_log_path(directory, day=None):
    day = day or now_et().date()
    return os.path.join(directory, f'picks_{day.strftime("%Y%m%d")}.log')


def log_pick_event(code, logic_names):
    ts = now_et().strftime('%Y-%m-%d %H:%M:%S')
    line = f'{ts}|{to_symbol(code)}|{",".join(logic_names)}\n'
    try:
        with open(picks_log_path(_LOG_DIR), 'a', encoding='utf-8') as f:
            f.write(line)
    except OSError as e:
        log.error('Could not write picks log: %s', e)


def read_pick_events(directory, day):
    path = picks_log_path(directory, day)
    events = []
    if not os.path.exists(path):
        return events
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            parts = line.rstrip('\n').split('|', 2)
            if len(parts) == 3:
                events.append({'time': parts[0], 'symbol': parts[1], 'logics': parts[2]})
    return events


# ---------------------------------------------------------------------------
# Connection / watchlist helpers (mirrors monitor_engine.py in DayTrade)
# ---------------------------------------------------------------------------
def connect(timeout_seconds=300, poll_cb=None):
    deadline = time.time() + timeout_seconds
    while True:
        try:
            ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
            log.info('Connected to OpenD at %s:%s', OPEND_HOST, OPEND_PORT)
            return ctx
        except Exception as e:
            if time.time() > deadline:
                raise EngineError(f'Could not connect to OpenD after {timeout_seconds}s: {e}')
            if poll_cb:
                poll_cb(f'OpenD not reachable yet ({e}). Retrying...')
            time.sleep(5)


def get_custom_groups(ctx):
    ret, data = ctx.get_user_security_group()
    if ret != RET_OK:
        raise EngineError(f'Could not read watchlist groups: {data}')
    names = data[data['group_type'] == 'CUSTOM']['group_name'].tolist()
    return [n for n in names if n not in EXCLUDED_GROUPS]


def get_group_members(ctx, group_name):
    """Returns list of (code, stock_type) tuples for a watchlist."""
    ret, data = ctx.get_user_security(group_name)
    if ret != RET_OK:
        raise EngineError(f'Could not read watchlist "{group_name}": {data}')
    pairs = list(zip(data['code'].tolist(), data['stock_type'].tolist()))
    log.info('"%s" list (%d stocks)', group_name, len(pairs))
    return pairs


def get_pooled_members(ctx, group_names):
    """Combines multiple custom watchlists into one deduped pool.
    Returns {code: stock_type}."""
    pool = {}
    for group_name in group_names:
        for code, stock_type in get_group_members(ctx, group_name):
            pool.setdefault(code, stock_type)
    log.info('Pooled %d unique stock(s) from %d list(s): %s',
              len(pool), len(group_names), ', '.join(group_names))
    return pool


# ---------------------------------------------------------------------------
# Market data helpers
# ---------------------------------------------------------------------------
def _fetch_snapshot_batch(ctx, codes, result):
    """Fetches total_market_val for `codes` into `result`. get_market_snapshot
    fails the ENTIRE batch if even one code in it is inaccessible (e.g. a US
    OTC ADR like BYDDY/TCEHY this account has no data permission for) -
    confirmed empirically: one bad code in a 200-stock batch silently lost
    market cap data for all 200. On failure, bisect and retry each half so
    only the genuinely bad code(s) get dropped, not innocent neighbors."""
    if not codes:
        return
    ret, data = ctx.get_market_snapshot(codes)
    if ret == RET_OK:
        for _, row in data.iterrows():
            val = row.get('total_market_val')
            result[row['code']] = float(val) if val is not None and val == val else None
        return
    if len(codes) == 1:
        log.warning('%s: get_market_snapshot failed, skipping (%s)', codes[0], data)
        return
    mid = len(codes) // 2
    _fetch_snapshot_batch(ctx, codes[:mid], result)
    _fetch_snapshot_batch(ctx, codes[mid:], result)


def get_market_caps(ctx, codes):
    """Returns {code: total_market_val or None}."""
    result = {}
    for i in range(0, len(codes), SNAPSHOT_CHUNK_SIZE):
        _fetch_snapshot_batch(ctx, codes[i:i + SNAPSHOT_CHUNK_SIZE], result)
    return result


def get_today_snapshots(ctx, codes):
    """Subscribes and fetches live quotes. Returns {code: dict(open, high,
    low, last_price, prev_close, volume, change_rate)}."""
    if not codes:
        return {}
    ret, err = ctx.subscribe(codes, [SubType.QUOTE], subscribe_push=False)
    if ret != RET_OK:
        raise EngineError(f'Subscribe failed: {err}')

    ret, data = ctx.get_stock_quote(codes)
    if ret != RET_OK:
        raise EngineError(f'get_stock_quote failed: {data}')

    result = {}
    for _, row in data.iterrows():
        prev_close = float(row['prev_close_price'])
        last_price = float(row['last_price'])
        if prev_close <= 0:
            continue
        result[row['code']] = {
            'open': float(row['open_price']),
            'high': float(row['high_price']),
            'low': float(row['low_price']),
            'close': last_price,
            'last_price': last_price,
            'prev_close': prev_close,
            'volume': float(row['volume']),
            'change_rate': (last_price - prev_close) / prev_close * 100,
        }
    return result


def fetch_daily_history(ctx, code, lookback_days):
    """Fetches daily kline strictly BEFORE today (today always comes from
    the live snapshot instead - see get_today_snapshots). Returns a
    DataFrame sorted ascending with columns open/high/low/close/volume/
    change_rate/time_key, or empty DataFrame on failure."""
    today = now_et().date()
    start = (today - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
    end = today.strftime('%Y-%m-%d')
    ret, data, _ = ctx.request_history_kline(
        code, start=start, end=end, ktype=KLType.K_DAY, autype=AuType.QFQ,
    )
    if ret != RET_OK or data is None or data.empty:
        log.warning('%s: could not fetch daily history (%s)', code, data)
        return pd.DataFrame()
    data = data.sort_values('time_key').reset_index(drop=True)
    today_str = today.strftime('%Y-%m-%d')
    data = data[~data['time_key'].str.startswith(today_str)].reset_index(drop=True)
    for col in ('open', 'high', 'low', 'close', 'volume', 'change_rate'):
        data[col] = data[col].astype(float)
    return data


# ---------------------------------------------------------------------------
# Shared pattern-detection helpers
# ---------------------------------------------------------------------------
def first_stabilization_day(lows, anchor_idx, window, strict):
    """lows: array-like of daily lows (position-indexed, ascending by date).
    From anchor_idx forward, finds the first index X such that the trailing
    `window`-day low (ending at X) does not undercut the lowest low seen
    since anchor_idx (exclusive of the trailing window itself). `strict`
    requires a strictly higher low; otherwise ties (no NEW low) are OK.
    Returns that index, or None if not found within the given data."""
    n = len(lows)
    earliest_x = anchor_idx + window
    for x in range(earliest_x, n):
        window_start = x - window + 1
        ref_low = min(lows[anchor_idx:window_start])
        win_low = min(lows[window_start:x + 1])
        if (win_low > ref_low) if strict else (win_low >= ref_low):
            return x
    return None


def find_consolidation_range(hist_df, amp_cap):
    """Finds a valid consolidation range among trailing windows from
    RANGE_MIN_DAYS to RANGE_MAX_DAYS bars (ending at the last row of
    hist_df), preferring the LONGEST window that's valid. A window is valid
    when all of:
      - max_high/min_low are the RANGE_CONTAINMENT_PCT-th percentile of the
        window's highs/lows (e.g. 95th-percentile-high, 5th-percentile-low)
        - i.e. roughly that fraction of bars are fully contained within
        [min_low, max_high], the rest allowed to spike outside as noise.
      - at least RANGE_TOUCH_MIN_COUNT bars have a high within
        RANGE_TOUCH_TOLERANCE_PCT of range_distance below max_high (touched
        the top), and likewise RANGE_TOUCH_MIN_COUNT bars touched the
        bottom.
      - range_distance / max_high <= amp_cap.
    Window lengths aren't strictly monotonic under these rules (unlike a
    plain min/max envelope), so each length is checked independently rather
    than assuming a single break point. Returns (range_high, range_low,
    window_len) or None if no length in [RANGE_MIN_DAYS, RANGE_MAX_DAYS]
    qualifies."""
    n = len(hist_df)
    if n < RANGE_MIN_DAYS:
        return None

    for window_len in range(min(RANGE_MAX_DAYS, n), RANGE_MIN_DAYS - 1, -1):
        window = hist_df.iloc[n - window_len:n]
        w_highs = window['high']
        w_lows = window['low']

        max_high = float(w_highs.quantile(RANGE_CONTAINMENT_PCT))
        min_low = float(w_lows.quantile(1 - RANGE_CONTAINMENT_PCT))
        range_distance = max_high - min_low
        if range_distance <= 0 or range_distance / max_high > amp_cap:
            continue

        tol = RANGE_TOUCH_TOLERANCE_PCT * range_distance
        top_touches = int((w_highs >= max_high - tol).sum())
        bottom_touches = int((w_lows <= min_low + tol).sum())
        if top_touches < RANGE_TOUCH_MIN_COUNT or bottom_touches < RANGE_TOUCH_MIN_COUNT:
            continue

        return (max_high, min_low, window_len)

    return None


# ---------------------------------------------------------------------------
# PickLogic framework
# ---------------------------------------------------------------------------
class PickLogic:
    def __init__(self, logic_id, name, description, lookback_days, enabled=True, market_cap_override=None):
        self.id = logic_id
        self.name = name
        self.description = description
        self.lookback_days = lookback_days
        self.enabled = enabled
        # If set, this logic always requires this exact market cap threshold
        # and ignores the global Market Cap Filter UI setting entirely -
        # e.g. Logic 3 always needs >$10B even if the global filter is set
        # to $100B or turned off.
        self.market_cap_override = market_cap_override

    def evaluate(self, hist_df, today, stock_type):
        """Returns (passed: bool, diagnostics: dict). Override in subclasses."""
        raise NotImplementedError


class Logic1BigGain(PickLogic):
    def __init__(self):
        super().__init__(
            'logic1', 'Logic 1: Today Big Gain',
            f'Today\'s 涨幅 is at least {L1_GAIN_MULT}x the max 涨幅 of the last '
            f'{BASELINE_DAYS} days, and today\'s volume is at least {L1_VOL_MULT}x '
            f'the {BASELINE_DAYS}-day average volume.',
            HISTORY_LOOKBACK_DAYS_DEFAULT,
        )

    def evaluate(self, hist_df, today, stock_type):
        baseline = hist_df.tail(BASELINE_DAYS)
        if len(baseline) < BASELINE_DAYS:
            return False, {'reason': 'insufficient baseline history'}
        max_amp10 = baseline['change_rate'].max()
        avg_vol10 = baseline['volume'].mean()
        gain_ok = today['change_rate'] >= L1_GAIN_MULT * max_amp10
        vol_ok = today['volume'] >= L1_VOL_MULT * avg_vol10
        diag = {
            'today_gain': round(today['change_rate'], 2),
            'max_amp10': round(max_amp10, 2),
            'today_vol': today['volume'],
            'avg_vol10': round(avg_vol10, 0),
        }
        return bool(gain_ok and vol_ok), diag


class Logic2BreakoutPullback(PickLogic):
    def __init__(self):
        super().__init__(
            'logic2', 'Logic 2: Recent Breakout Pullback',
            f'Any day in the last {L2_LOOKBACK_DAYS} days (incl. today) had 涨幅 at '
            f'least {L2_GAIN_MULT}x the {BASELINE_DAYS}-day max and volume at least '
            f'{L2_VOL_MULT}x the {BASELINE_DAYS}-day average; current price must sit '
            f'in the lower {int(L2_LOW_BAND_PCT * 100)}% of that day\'s range.',
            HISTORY_LOOKBACK_DAYS_DEFAULT,
        )

    def evaluate(self, hist_df, today, stock_type):
        baseline = hist_df.tail(BASELINE_DAYS)
        if len(baseline) < BASELINE_DAYS:
            return False, {'reason': 'insufficient baseline history'}
        max_amp10 = baseline['change_rate'].max()
        avg_vol10 = baseline['volume'].mean()

        candidates = []
        recent = hist_df.tail(L2_LOOKBACK_DAYS - 1)
        for _, row in recent.iterrows():
            if row['change_rate'] >= L2_GAIN_MULT * max_amp10 and row['volume'] >= L2_VOL_MULT * avg_vol10:
                candidates.append({'high': row['high'], 'low': row['low'], 'time_key': row['time_key']})
        if today['change_rate'] >= L2_GAIN_MULT * max_amp10 and today['volume'] >= L2_VOL_MULT * avg_vol10:
            candidates.append({'high': today['high'], 'low': today['low'], 'time_key': 'today'})

        if not candidates:
            return False, {'reason': 'no qualifying big green bar', 'max_amp10': round(max_amp10, 2)}

        bar = candidates[-1]  # most recent qualifying day
        band_top = bar['low'] + (bar['high'] - bar['low']) * L2_LOW_BAND_PCT
        in_band = bar['low'] <= today['last_price'] <= band_top
        diag = {
            'bar_day': bar['time_key'], 'bar_high': round(bar['high'], 2),
            'bar_low': round(bar['low'], 2), 'band_top': round(band_top, 2),
            'last_price': round(today['last_price'], 2),
        }
        return bool(in_band), diag


LOGIC3_MARKET_CAP_OVERRIDE = 10_000_000_000  # always >$10B, ignores the global Market Cap Filter


class Logic3EmaCrossStabilize(PickLogic):
    def __init__(self):
        super().__init__(
            'logic3', 'Logic 3: EMA20/250 Cross, Stabilize',
            f'EMA{EMA_SHORT_SPAN} crossed up through EMA{EMA_LONG_SPAN} then back down; '
            f'today must be the first day the trailing {L3_STABILIZE_WINDOW}-day low makes '
            'no new low since that death cross. Always requires market cap > '
            f'${LOGIC3_MARKET_CAP_OVERRIDE / 1e9:.0f}B, regardless of the Market Cap Filter setting.',
            HISTORY_LOOKBACK_DAYS_LONG,
            market_cap_override=LOGIC3_MARKET_CAP_OVERRIDE,
        )

    def evaluate(self, hist_df, today, stock_type):
        if len(hist_df) < EMA_LONG_SPAN:
            return False, {'reason': 'insufficient history for EMA250'}

        closes = list(hist_df['close']) + [today['close']]
        lows = list(hist_df['low']) + [today['low']]
        close_s = pd.Series(closes)
        ema_short = close_s.ewm(span=EMA_SHORT_SPAN, adjust=False).mean()
        ema_long = close_s.ewm(span=EMA_LONG_SPAN, adjust=False).mean()
        diff = (ema_short - ema_long).values
        today_idx = len(closes) - 1

        death_cross = None
        for i in range(today_idx, 0, -1):
            if diff[i - 1] >= 0 and diff[i] < 0:
                death_cross = i
                break
        if death_cross is None:
            return False, {'reason': 'no death cross found'}

        golden_cross = None
        for i in range(death_cross - 1, 0, -1):
            if diff[i - 1] <= 0 and diff[i] > 0:
                golden_cross = i
                break
        if golden_cross is None:
            return False, {'reason': 'no golden cross before death cross'}

        first_day = first_stabilization_day(lows, death_cross, L3_STABILIZE_WINDOW, strict=False)
        diag = {'death_cross_idx_from_today': death_cross - today_idx,
                'golden_cross_idx_from_today': golden_cross - today_idx,
                'first_stabilize_idx_from_today': None if first_day is None else first_day - today_idx}
        return bool(first_day == today_idx), diag


class Logic4RangeBottom(PickLogic):
    def __init__(self):
        super().__init__(
            'logic4', 'Logic 4: Range Bottom',
            f'Stock has traded in a {RANGE_MIN_DAYS}-{RANGE_MAX_DAYS} bar range '
            f'({int(RANGE_CONTAINMENT_PCT * 100)}% of bars contained, at least '
            f'{RANGE_TOUCH_MIN_COUNT} touches each of the top and bottom, range distance under '
            f'{int(RANGE_AMP_CAP_STOCK * 100)}% of price for stocks / {int(RANGE_AMP_CAP_ETF * 100)}% for ETFs), '
            f'and current price has reached the bottom {int(RANGE_BOTTOM_ZONE_PCT * 100)}% of the range.',
            HISTORY_LOOKBACK_DAYS_LONG,
        )

    def evaluate(self, hist_df, today, stock_type):
        cap = RANGE_AMP_CAP_ETF if stock_type == 'ETF' else RANGE_AMP_CAP_STOCK
        rng = find_consolidation_range(hist_df, cap)
        if rng is None:
            return False, {'reason': 'no qualifying consolidation range'}
        range_high, range_low, window_len = rng
        range_distance = range_high - range_low
        bottom_zone_top = range_low + RANGE_BOTTOM_ZONE_PCT * range_distance
        reached_bottom = today['last_price'] <= bottom_zone_top
        diag = {'range_high': round(range_high, 2), 'range_low': round(range_low, 2),
                'window_len': window_len, 'bottom_zone_top': round(bottom_zone_top, 2),
                'last_price': round(today['last_price'], 2)}
        return bool(reached_bottom), diag


class Logic5RangeBreakout(PickLogic):
    def __init__(self):
        super().__init__(
            'logic5', 'Logic 5: Range Breakout',
            f'Same consolidation range as Logic 4; today breaks above the range high by at least '
            f'{int(L5_BREAKOUT_ZONE_PCT * 100)}% of the range distance, with volume at least '
            f'{L5_VOL_MULT}x the range bars\' own average volume.',
            HISTORY_LOOKBACK_DAYS_LONG,
        )

    def evaluate(self, hist_df, today, stock_type):
        cap = RANGE_AMP_CAP_ETF if stock_type == 'ETF' else RANGE_AMP_CAP_STOCK
        rng = find_consolidation_range(hist_df, cap)
        if rng is None:
            return False, {'reason': 'no qualifying consolidation range'}
        range_high, range_low, window_len = rng
        range_distance = range_high - range_low
        avg_vol = hist_df.tail(window_len)['volume'].mean()
        breakout_level = range_high + L5_BREAKOUT_ZONE_PCT * range_distance
        price_ok = today['last_price'] >= breakout_level
        vol_ok = today['volume'] >= L5_VOL_MULT * avg_vol
        diag = {'range_high': round(range_high, 2), 'breakout_level': round(breakout_level, 2),
                'last_price': round(today['last_price'], 2), 'range_avg_vol': round(avg_vol, 0),
                'today_vol': today['volume'], 'window_len': window_len}
        return bool(price_ok and vol_ok), diag


class Logic6CrashStabilize(PickLogic):
    def __init__(self):
        super().__init__(
            'logic6', 'Logic 6: Crash Stabilization',
            f'Stock had a single-day drop of at least {abs(L6_CRASH_PCT)}% within the last '
            f'{L6_CRASH_LOOKBACK_DAYS} days; today must be the first day the trailing '
            f'{L6_STABILIZE_WINDOW}-day low is strictly higher than the lowest low since that crash.',
            HISTORY_LOOKBACK_DAYS_DEFAULT + L6_CRASH_LOOKBACK_DAYS,
        )

    def evaluate(self, hist_df, today, stock_type):
        recent = hist_df.tail(L6_CRASH_LOOKBACK_DAYS)
        crashes = recent[recent['change_rate'] <= L6_CRASH_PCT]
        if crashes.empty:
            return False, {'reason': 'no crash day found'}
        crash_row = crashes.iloc[-1]  # most recent crash day
        crash_pos_in_hist = hist_df.index[hist_df['time_key'] == crash_row['time_key']][0]

        lows = list(hist_df['low']) + [today['low']]
        today_idx = len(lows) - 1
        first_day = first_stabilization_day(lows, crash_pos_in_hist, L6_STABILIZE_WINDOW, strict=True)
        diag = {'crash_day': crash_row['time_key'], 'crash_change_rate': round(crash_row['change_rate'], 2),
                'first_stabilize_idx_from_today': None if first_day is None else first_day - today_idx}
        return bool(first_day == today_idx), diag


def default_pick_logics():
    return [
        Logic1BigGain(),
        Logic2BreakoutPullback(),
        Logic3EmaCrossStabilize(),
        Logic4RangeBottom(),
        Logic5RangeBreakout(),
        Logic6CrashStabilize(),
    ]


# ---------------------------------------------------------------------------
# Run orchestration
# ---------------------------------------------------------------------------
def set_pick_list(ctx, picked_codes):
    ret, existing = ctx.get_user_security(PICK_LIST_NAME)
    if ret != RET_OK:
        raise EngineError(f'Could not read "{PICK_LIST_NAME}" list: {existing}')
    existing_codes = existing['code'].tolist()

    if existing_codes:
        ret, res = ctx.modify_user_security(PICK_LIST_NAME, ModifyUserSecurityOp.DEL, existing_codes)
        if ret != RET_OK:
            log.warning('Could not clear "%s" list: %s', PICK_LIST_NAME, res)

    if picked_codes:
        ret, res = ctx.modify_user_security(PICK_LIST_NAME, ModifyUserSecurityOp.ADD, picked_codes)
        if ret != RET_OK:
            log.warning('Could not add picks to "%s" list: %s', PICK_LIST_NAME, res)
            return False
    log.info('"%s" list replaced with %d pick(s): %s', PICK_LIST_NAME, len(picked_codes),
              ', '.join(to_symbol(c) for c in picked_codes) or '(none)')
    return True


def run_pick(ctx, group_names, logics, market_cap_enabled=True, market_cap_threshold=MARKET_CAP_DEFAULT,
             status_cb=None, progress_cb=None):
    """Runs one full pick cycle. `market_cap_enabled`/`market_cap_threshold`
    set the GLOBAL Market Cap Filter; a logic with its own
    `market_cap_override` (e.g. Logic 3) ignores the global setting entirely
    and always requires its own threshold instead - see PickLogic.
    Returns a list of result dicts:
    {code, name, passed: bool, passed_logics: [...], diagnostics: {logic_id: {...}}}."""
    enabled_logics = [l for l in logics if l.enabled]
    if not enabled_logics:
        if status_cb:
            status_cb('No pick logic enabled - nothing to evaluate.')
        return []

    if status_cb:
        status_cb('Reading watchlists...')
    pool = get_pooled_members(ctx, group_names)
    codes = list(pool.keys())
    if not codes:
        if status_cb:
            status_cb('Selected list(s) are empty.')
        return []

    # Each enabled logic's own required cap: its override if it has one,
    # else the global filter's threshold (or no requirement at all if the
    # global filter is off).
    required_caps = {
        l.id: (l.market_cap_override if l.market_cap_override is not None
               else (market_cap_threshold if market_cap_enabled else None))
        for l in enabled_logics
    }
    needs_cap_data = any(v is not None for v in required_caps.values())
    # Can only pre-drop stocks in stage 0 if EVERY enabled logic has some cap
    # requirement - if even one logic has none, it wants the full pool.
    can_prefilter = needs_cap_data and all(v is not None for v in required_caps.values())

    market_caps = {}
    if needs_cap_data:
        if status_cb:
            status_cb(f'Checking market cap for {len(codes)} stock(s)...')
        market_caps = get_market_caps(ctx, codes)

    if can_prefilter:
        floor = min(required_caps.values())
        stage0 = [c for c in codes if (market_caps.get(c) or 0) > floor]
        log.info('Market cap filter (floor $%.0fB): %d -> %d candidate(s)',
                  floor / 1e9, len(codes), len(stage0))
        if not stage0:
            if status_cb:
                status_cb('No stock passed the market cap filter.')
            return []
    else:
        stage0 = codes

    if status_cb:
        status_cb(f'Fetching live quotes for {len(stage0)} stock(s)...')
    today_snapshots = get_today_snapshots(ctx, stage0)

    lookback = max((l.lookback_days for l in enabled_logics), default=HISTORY_LOOKBACK_DAYS_DEFAULT)

    results = []
    picked_codes = []
    for idx, code in enumerate(stage0):
        if progress_cb:
            progress_cb(idx + 1, len(stage0), code)
        today = today_snapshots.get(code)
        if today is None:
            continue
        hist_df = fetch_daily_history(ctx, code, lookback)
        if hist_df.empty:
            continue

        stock_cap = market_caps.get(code) if needs_cap_data else None
        passed_logics = []
        diagnostics = {}
        for logic in enabled_logics:
            required = required_caps[logic.id]
            if required is not None and (stock_cap or 0) <= required:
                diagnostics[logic.id] = {'reason': f'market cap below ${required / 1e9:.0f}B'}
                continue
            try:
                passed, diag = logic.evaluate(hist_df, today, pool[code])
            except Exception as e:
                log.exception('%s: logic "%s" raised an error', code, logic.name)
                passed, diag = False, {'error': str(e)}
            diagnostics[logic.id] = diag
            if passed:
                passed_logics.append(logic.name)

        any_passed = bool(passed_logics)  # OR-combined: any one enabled logic passing is enough
        log.info('%s [%s]: %s (%s)', code, to_symbol(code),
                  f'PICKED [{", ".join(passed_logics)}]' if any_passed else 'skip', diagnostics)

        if any_passed:
            picked_codes.append(code)
            results.append({
                'code': code, 'name': to_symbol(code), 'passed': True,
                'passed_logics': passed_logics, 'diagnostics': diagnostics,
            })

    if status_cb:
        status_cb(f'Writing {len(picked_codes)} pick(s) to the "{PICK_LIST_NAME}" list...')
    set_pick_list(ctx, picked_codes)
    for r in results:
        log_pick_event(r['code'], r['passed_logics'])

    if status_cb:
        status_cb(f'Done. {len(picked_codes)} stock(s) picked out of {len(stage0)} evaluated.')
    return results
