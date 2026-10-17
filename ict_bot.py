# ═══════════════════════════════════════════════════════════════
# ICT Gold Strategy Bot — XAUUSD! (M30)
# Models: Judas, 2022-AM, SB-PM, OB, FVG   (Lunch & Mitigation removed)
# v2: bar-by-bar replica of Pine "ICT Gold v9.5.1 SELECTIVE"
#   - Pending limit entry (OB mid / FVG CE) exactly like Pine
#   - Sessions evaluated on the CANDLE time (NY), not on "now"
#   - Closed candles only, every missed candle is re-processed
#   - Same daily/cooldown/model-once rules as Pine
# ═══════════════════════════════════════════════════════════════

import os
import json
import time
import requests
import pandas as pd
import numpy as np
import traceback
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

# ── ENV ──
TICKERALL_API_KEY = os.environ.get("TICKERALL_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_GROUP_CHAT_ID = os.environ.get("TELEGRAM_GROUP_CHAT_ID")
MT5_PASSWORD = os.environ.get("MT5_PASSWORD")
MT5_SERVER = os.environ.get("MT5_SERVER")
MT5_ACCOUNT = os.environ.get("MT5_ACCOUNT")
GH_PAT = os.environ.get("GH_PAT")

# ── GITHUB SELF-DISPATCH ──
GH_REPO_OWNER = "mohammed97k"
GH_REPO_NAME = "gold_30m_bot"
GH_WORKFLOW_FILE = "ict_gold.yml"

# ── CONFIG ──
STATE_FILE = "state.json"
NY_TZ = ZoneInfo("America/New_York")
MOSUL_TZ = ZoneInfo("Asia/Baghdad")
BASE_URL = "https://api.tickerall.com"
SERVER_OFFSET_HOURS = 3

# ── STRATEGY PARAMS (same as Pine) ──
MULT = 10.0
ATR_PERIOD = 14
MIN_SL_PTS = 40.0
MAX_SL_PTS = 150.0
COOLDOWN_NORMAL = 10
COOLDOWN_STRONG = 3
MAX_TRADES_PER_DAY = 3
MAX_BARS_TRADE = 60
MAX_PENDING_BARS = 30
TP1_R = 1.0
TP2_R = 2.0
TP3_R = 3.0
SYMBOL = "XAUUSD!"
TIMEFRAME_ENTRY = "M30"
TIMEFRAME_TREND = "H1"
PIVOT_LEFT = 3
PIVOT_RIGHT = 3
SWEEP_LB = 20
FVG_EXPIRY = 15
OB_EXPIRY = 15
MSS_EXPIRY = 15
SWEEP_EXPIRY = 20

# ── LIVE SAFETY ──
EXEC_MAX_AGE_MIN = 25      # orders are executed only if the signal candle closed less than this many minutes ago
MAX_CHASE_R = 0.3          # skip market execution if price is farther than 0.3R from the Pine entry
BOOT_BARS = 2              # when state.json is missing: re-check only the last N closed candles

# ── AUTO TRADE ──
ENABLE_AUTO_TRADE = False
LOT_TP1 = 0.05
LOT_TP2 = 0.03
LOT_TP3 = 0.02
MAGIC_NUMBER = 20261012

# ── RETRY SETTINGS ──
SESSION_RETRIES = 5
CANDLE_RETRIES = 3
ORDER_RETRIES = 3
SESSION_TIMEOUT = 15
CANDLE_TIMEOUT = 30
ORDER_TIMEOUT = 20
BACKOFF_DELAYS = [2, 4, 6, 10, 15]

# ── LOOP SETTINGS ──
MAX_RUNTIME_SECONDS = 55 * 60
LOOP_INTERVAL_SECONDS = 5 * 60


def fmt_mosul(dt_utc):
    return dt_utc.astimezone(MOSUL_TZ).strftime('%I:%M %p')


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN:
        print("TG: token missing, skip")
        return
    chat_ids = [c for c in [TELEGRAM_CHAT_ID, TELEGRAM_GROUP_CHAT_ID] if c]
    for chat_id in chat_ids:
        url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
        for attempt in range(1, 4):
            try:
                r = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=15)
                if r.json().get('ok'):
                    print("TG->" + str(chat_id) + ": ok=True")
                    break
                else:
                    print("TG->" + str(chat_id) + ": attempt " + str(attempt) + " failed")
                    if attempt < 3:
                        time.sleep(3)
            except Exception as e:
                print("TG Error attempt " + str(attempt) + ": " + str(e))
                if attempt < 3:
                    time.sleep(3)


# ═══════════════════════════════════════════════════════════════
# GITHUB SELF-DISPATCH
# ═══════════════════════════════════════════════════════════════
def dispatch_next_run():
    if not GH_PAT:
        print("GH_PAT not set — cannot self-dispatch")
        return False
    url = "https://api.github.com/repos/" + GH_REPO_OWNER + "/" + GH_REPO_NAME + "/actions/workflows/" + GH_WORKFLOW_FILE + "/dispatches"
    headers = {
        "Authorization": "token " + GH_PAT,
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    payload = {"ref": "main"}
    for attempt in range(1, 4):
        try:
            print("Dispatch attempt " + str(attempt) + "/3...")
            r = requests.post(url, headers=headers, json=payload, timeout=20)
            if r.status_code in [200, 201, 204]:
                print("Next run dispatched successfully")
                return True
            else:
                print("Dispatch failed: " + str(r.status_code) + " | " + r.text[:200])
                if attempt < 3:
                    time.sleep(5)
        except Exception as e:
            print("Dispatch error attempt " + str(attempt) + ": " + str(e))
            if attempt < 3:
                time.sleep(5)
    print("All dispatch attempts failed")
    return False


# ═══════════════════════════════════════════════════════════════
# MT5 EXECUTION
# ═══════════════════════════════════════════════════════════════
def place_order(account_id, symbol, direction, volume, sl, tp, tag=""):
    url = BASE_URL + "/v1/accounts/" + account_id + "/orders"
    side = "buy" if direction == "BUY" else "sell"
    payload = {
        "symbol": symbol,
        "side": side,
        "type": "market",
        "volume": volume,
        "sl": float(sl),
        "tp": float(tp),
        "comment": "ICT-" + tag,
        "magic": MAGIC_NUMBER,
    }
    idem_key = "ict-" + tag + "-" + str(int(time.time())) + "-" + str(uuid.uuid4())[:8]
    headers = {
        "Authorization": "Bearer " + TICKERALL_API_KEY,
        "Content-Type": "application/json",
        "Idempotency-Key": idem_key,
    }
    for attempt in range(1, ORDER_RETRIES + 1):
        try:
            print("Order " + tag + " attempt " + str(attempt) + ": " + side + " " + str(volume) + " key=" + idem_key[:20])
            r = requests.post(url, headers=headers, json=payload, timeout=ORDER_TIMEOUT)
            if r.status_code in [200, 201]:
                result = r.json()
                order_id = result.get("orderId") or result.get("ticket") or result.get("id")
                print("Order " + tag + " placed: " + str(order_id))
                return {"success": True, "order_id": order_id}
            else:
                print("Order " + tag + " failed: " + str(r.status_code) + " | " + r.text[:200])
                if attempt < ORDER_RETRIES:
                    time.sleep(BACKOFF_DELAYS[min(attempt - 1, len(BACKOFF_DELAYS) - 1)])
        except Exception as e:
            print("Order " + tag + " error: " + str(e))
            if attempt < ORDER_RETRIES:
                time.sleep(BACKOFF_DELAYS[min(attempt - 1, len(BACKOFF_DELAYS) - 1)])
    return {"success": False, "order_id": None}


def modify_sl(account_id, order_id, new_sl):
    url = BASE_URL + "/v1/accounts/" + account_id + "/positions/" + str(order_id)
    payload = {"sl": float(new_sl)}
    idem_key = "ict-mod-" + str(int(time.time())) + "-" + str(uuid.uuid4())[:8]
    headers = {
        "Authorization": "Bearer " + TICKERALL_API_KEY,
        "Content-Type": "application/json",
        "Idempotency-Key": idem_key,
    }
    for attempt in range(1, 4):
        try:
            r = requests.put(url, headers=headers, json=payload, timeout=ORDER_TIMEOUT)
            if r.status_code in [200, 201]:
                print("SL modified: " + str(order_id) + " -> " + str(new_sl))
                return True
            else:
                print("SL modify failed: " + str(r.status_code) + " | " + r.text[:200])
                if attempt < 3:
                    time.sleep(3)
        except Exception as e:
            print("SL modify error: " + str(e))
            if attempt < 3:
                time.sleep(3)
    return False


def close_position(account_id, order_id):
    url = BASE_URL + "/v1/accounts/" + account_id + "/positions/" + str(order_id)
    idem_key = "ict-cls-" + str(int(time.time())) + "-" + str(uuid.uuid4())[:8]
    headers = {
        "Authorization": "Bearer " + TICKERALL_API_KEY,
        "Idempotency-Key": idem_key,
    }
    for attempt in range(1, 4):
        try:
            r = requests.delete(url, headers=headers, timeout=ORDER_TIMEOUT)
            if r.status_code in [200, 201, 204]:
                print("Position closed: " + str(order_id))
                return True
            else:
                print("Close failed: " + str(r.status_code) + " | " + r.text[:100])
                if attempt < 3:
                    time.sleep(3)
        except Exception as e:
            print("Close error: " + str(e))
            if attempt < 3:
                time.sleep(3)
    return False


def open_session(max_retries=SESSION_RETRIES):
    url = BASE_URL + "/v1/sessions"
    headers = {"Authorization": "Bearer " + TICKERALL_API_KEY, "Content-Type": "application/json"}
    payload = {
        "broker": "mt5",
        "server": MT5_SERVER,
        "account": int(MT5_ACCOUNT) if MT5_ACCOUNT and MT5_ACCOUNT.isdigit() else MT5_ACCOUNT,
        "password": MT5_PASSWORD
    }
    for attempt in range(1, max_retries + 1):
        try:
            print("Session attempt " + str(attempt) + "/" + str(max_retries))
            r = requests.post(url, headers=headers, json=payload, timeout=SESSION_TIMEOUT)
            if r.status_code == 200:
                account_id = r.json().get("accountId")
                print("Session opened: " + str(account_id))
                return account_id
            else:
                print("Session failed: " + str(r.status_code) + " " + r.text[:200])
                if attempt < max_retries:
                    delay = BACKOFF_DELAYS[min(attempt - 1, len(BACKOFF_DELAYS) - 1)]
                    print("Waiting " + str(delay) + "s before retry...")
                    time.sleep(delay)
        except Exception as e:
            print("Session error attempt " + str(attempt) + ": " + str(e))
            if attempt < max_retries:
                delay = BACKOFF_DELAYS[min(attempt - 1, len(BACKOFF_DELAYS) - 1)]
                print("Waiting " + str(delay) + "s before retry...")
                time.sleep(delay)
    print("All session attempts failed")
    return None


def fetch_candles(account_id, symbol, timeframe, limit=500, hours=500, server_offset_hours=SERVER_OFFSET_HOURS):
    url = BASE_URL + "/v1/accounts/" + account_id + "/candles"
    headers = {"Authorization": "Bearer " + TICKERALL_API_KEY}
    params = {"symbol": symbol, "timeframe": timeframe, "limit": limit, "hours": hours}

    for attempt in range(1, CANDLE_RETRIES + 1):
        try:
            print("Fetch " + timeframe + " attempt " + str(attempt) + "/" + str(CANDLE_RETRIES))
            r = requests.get(url, headers=headers, params=params, timeout=CANDLE_TIMEOUT)
            if r.status_code != 200:
                print("Fail " + timeframe + ": " + str(r.status_code))
                if attempt < CANDLE_RETRIES:
                    time.sleep(3)
                continue
            data = r.json()
            candles = data.get("candles", [])
            if not candles:
                print("No candles for " + timeframe)
                if attempt < CANDLE_RETRIES:
                    time.sleep(3)
                continue
            df = pd.DataFrame(candles)
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True) - pd.Timedelta(hours=server_offset_hours)
            df = df[["datetime", "open", "high", "low", "close"]].copy()
            for c in ["open", "high", "low", "close"]:
                df[c] = pd.to_numeric(df[c])
            df = df.sort_values("datetime").reset_index(drop=True)
            print("OK " + timeframe + ": " + str(len(df)) + " candles | last: " + str(df.iloc[-1]['close']))
            return df
        except Exception as e:
            print("Fetch " + timeframe + " attempt " + str(attempt) + " error: " + str(e))
            if attempt < CANDLE_RETRIES:
                time.sleep(3)

    print("All fetch attempts failed for " + timeframe)
    return None


# ═══════════════════════════════════════════════════════════════
# INDICATORS (Pine-compatible)
# ═══════════════════════════════════════════════════════════════
def ta_atr(df, period=14):
    # Pine ta.atr = RMA(true range), seeded with SMA
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    n = len(df)
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    out = np.full(n, np.nan)
    if n >= period:
        out[period - 1] = tr[:period].mean()
        for i in range(period, n):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def ta_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def ta_pivothigh(highs, left, right):
    n = len(highs)
    result = np.full(n, np.nan)
    for i in range(left, n - right):
        window = highs[i-left:i+right+1]
        if highs[i] == window.max() and (window == highs[i]).sum() == 1:
            result[i + right] = highs[i]
    return result


def ta_pivotlow(lows, left, right):
    n = len(lows)
    result = np.full(n, np.nan)
    for i in range(left, n - right):
        window = lows[i-left:i+right+1]
        if lows[i] == window.min() and (window == lows[i]).sum() == 1:
            result[i + right] = lows[i]
    return result


def ta_lowest(series, length):
    return series.rolling(length).min()


def ta_highest(series, length):
    return series.rolling(length).max()


# ═══════════════════════════════════════════════════════════════
# SESSIONS — evaluated on the candle OPEN time (NY), like Pine time()
# ═══════════════════════════════════════════════════════════════
def in_win(dt_ny, h1, m1, h2, m2):
    t = dt_ny.hour * 60 + dt_ny.minute
    return (h1 * 60 + m1) <= t < (h2 * 60 + m2)


def session_flags(dt_ny):
    in_london_o = in_win(dt_ny, 2, 0, 3, 0)
    in_sb_ldn = in_win(dt_ny, 3, 0, 4, 0)
    in_judas = in_win(dt_ny, 9, 30, 10, 0)
    in_sb_am = in_win(dt_ny, 10, 0, 11, 0)
    in_2022 = in_win(dt_ny, 11, 0, 11, 30)
    in_lunch = in_win(dt_ny, 11, 50, 12, 10)
    in_sb_pm = in_win(dt_ny, 14, 0, 15, 0)
    in_moc = in_win(dt_ny, 15, 15, 15, 45)
    in_fomc = in_win(dt_ny, 14, 0, 14, 30)
    in_nfp = in_win(dt_ny, 8, 30, 9, 0)
    in_lunch_bl = in_win(dt_ny, 12, 10, 13, 59)
    any_kz = (in_london_o or in_sb_ldn or in_judas or in_sb_am or in_2022 or in_lunch
              or in_sb_pm or in_moc or in_fomc or in_nfp)
    return {
        "judas": in_judas,
        "m2022": in_2022,
        "sbpm": in_sb_pm,
        "sess_ok": bool(any_kz and not in_lunch_bl),
    }


# ═══════════════════════════════════════════════════════════════
# H1 TREND — Pine: request.security("60", close[1] / ema[1], lookahead_off)
# = values of the PREVIOUS completed H1 bar for every M30 bar
# ═══════════════════════════════════════════════════════════════
def build_trend(df30, df1h):
    h1 = df1h.copy().sort_values("datetime").reset_index(drop=True)
    ema200 = ta_ema(h1["close"], 200).values
    ema50 = ta_ema(h1["close"], 50).values
    h1c = h1["close"].values
    h1_ns = h1["datetime"].dt.tz_convert("UTC").dt.tz_localize(None).values.astype("datetime64[ns]")
    t30 = df30["datetime"].dt.tz_convert("UTC").dt.tz_localize(None)
    t30_ns = t30.values.astype("datetime64[ns]")
    floor_ns = t30.dt.floor("h").values.astype("datetime64[ns]")

    pos = np.searchsorted(h1_ns, t30_ns, side="right") - 1
    pos_c = np.clip(pos, 0, len(h1) - 1)
    contains = (h1_ns[pos_c] == floor_ns) & (pos >= 0)
    idx = np.where(contains, pos - 1, pos)
    valid = idx >= 0
    idx_c = np.clip(idx, 0, len(h1) - 1)

    hc = np.where(valid, h1c[idx_c], np.nan)
    e200 = np.where(valid, ema200[idx_c], np.nan)
    e50 = np.where(valid, ema50[idx_c], np.nan)
    up = hc > e200
    down = hc < e200
    return {
        "up": up,
        "down": down,
        "sbull": up & (hc > e50),
        "sbear": down & (hc < e50),
    }


# ═══════════════════════════════════════════════════════════════
# SIMULATION — bar-by-bar copy of the Pine logic (closed candles only)
# ═══════════════════════════════════════════════════════════════
def simulate(df, trend):
    n = len(df)
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    atr = ta_atr(df, ATR_PERIOD)
    sh_arr = ta_pivothigh(h, PIVOT_LEFT, PIVOT_RIGHT)
    sl_arr = ta_pivotlow(l, PIVOT_LEFT, PIVOT_RIGHT)
    rec_low = ta_lowest(df["low"], SWEEP_LB).shift(1).values
    rec_high = ta_highest(df["high"], SWEEP_LB).shift(1).values
    dts = df["datetime"].dt.tz_convert(NY_TZ)

    nan = np.nan
    events = []

    # structure state
    last_sh = nan
    last_sl = nan
    bull_sweep_ok = False
    bull_sweep_bar = -1
    bull_sweep_low = nan
    bear_sweep_ok = False
    bear_sweep_bar = -1
    bear_sweep_high = nan
    bull_mss = False
    bull_mss_bar = -1
    bear_mss = False
    bear_mss_bar = -1
    bTop = nan
    bBot = nan
    bBar = -1
    bActive = False
    sTop = nan
    sBot = nan
    sBar = -1
    sActive = False
    bOBHigh = nan
    bOBLow = nan
    bOBMT = nan
    bOBBar = -1
    bOBActive = False
    sOBHigh = nan
    sOBLow = nan
    sOBMT = nan
    sOBBar = -1
    sOBActive = False

    # daily / trade state
    trade_count = 0
    last_day = None
    judas_done = False
    m2022_done = False
    sbpm_done = False
    last_entry_bar = -10000
    flat_from = 0

    # pending order
    p_on = False
    p_is_long = False
    p_entry = nan
    p_sl = nan
    p_tp1 = nan
    p_tp2 = nan
    p_tp3 = nan
    p_bar = -1
    p_model = ""

    # active trade
    a_on = False
    a_closed = True
    a_is_long = False
    a_entry = nan
    a_sl = nan
    a_tp1 = nan
    a_tp2 = nan
    a_tp3 = nan
    a_risk = nan
    a_tp1_hit = False
    a_tp2_hit = False
    a_tp3_hit = False
    a_entry_bar = -1
    a_model = ""

    def pend_dict():
        return {
            "model": p_model,
            "direction": "BUY" if p_is_long else "SELL",
            "entry": round(float(p_entry), 2),
            "sl": round(float(p_sl), 2),
            "tp1": round(float(p_tp1), 2),
            "tp2": round(float(p_tp2), 2),
            "tp3": round(float(p_tp3), 2),
            "sl_pts": round(abs(float(p_entry) - float(p_sl)) * MULT, 1),
        }

    def act_dict():
        return {
            "model": a_model,
            "direction": "BUY" if a_is_long else "SELL",
            "entry": round(float(a_entry), 2),
            "sl": round(float(a_sl), 2),
            "tp1": round(float(a_tp1), 2),
            "tp2": round(float(a_tp2), 2),
            "tp3": round(float(a_tp3), 2),
            "sl_pts": round(float(a_risk), 1),
        }

    for i in range(n):
        dt_ny = dts.iloc[i]
        atr_i = atr[i]

        # ── swings ──
        if not np.isnan(sh_arr[i]):
            last_sh = sh_arr[i]
        if not np.isnan(sl_arr[i]):
            last_sl = sl_arr[i]

        # ── sweep ──
        bull_sweep = bool(l[i] < rec_low[i] and c[i] > rec_low[i])
        bear_sweep = bool(h[i] > rec_high[i] and c[i] < rec_high[i])
        if bull_sweep:
            bull_sweep_ok = True
            bull_sweep_bar = i
            bull_sweep_low = l[i]
        if bear_sweep:
            bear_sweep_ok = True
            bear_sweep_bar = i
            bear_sweep_high = h[i]
        if bull_sweep_ok and (i - bull_sweep_bar) > SWEEP_EXPIRY:
            bull_sweep_ok = False
        if bear_sweep_ok and (i - bear_sweep_bar) > SWEEP_EXPIRY:
            bear_sweep_ok = False

        # ── MSS ──
        if i > 0:
            if (not np.isnan(last_sh)) and c[i] > last_sh and c[i-1] <= last_sh and bull_sweep_ok:
                bull_mss = True
                bull_mss_bar = i
            if (not np.isnan(last_sl)) and c[i] < last_sl and c[i-1] >= last_sl and bear_sweep_ok:
                bear_mss = True
                bear_mss_bar = i
        if bull_mss and (i - bull_mss_bar) > MSS_EXPIRY:
            bull_mss = False
        if bear_mss and (i - bear_mss_bar) > MSS_EXPIRY:
            bear_mss = False

        # ── FVG ──
        if i >= 2:
            if l[i] > h[i-2]:
                bTop = l[i]
                bBot = h[i-2]
                bBar = i
                bActive = True
            if h[i] < l[i-2]:
                sTop = l[i-2]
                sBot = h[i]
                sBar = i
                sActive = True
        if bActive and (i - bBar) > FVG_EXPIRY:
            bActive = False
        if sActive and (i - sBar) > FVG_EXPIRY:
            sActive = False
        b_ce = (bTop + bBot) / 2.0 if bActive else nan
        s_ce = (sTop + sBot) / 2.0 if sActive else nan

        # ── OB ──
        if i >= 1:
            if c[i] > o[i] and c[i-1] < o[i-1]:
                bOBHigh = h[i-1]
                bOBLow = l[i-1]
                bOBMT = (o[i-1] + c[i-1]) / 2.0
                bOBBar = i - 1
                bOBActive = True
            if c[i] < o[i] and c[i-1] > o[i-1]:
                sOBHigh = h[i-1]
                sOBLow = l[i-1]
                sOBMT = (o[i-1] + c[i-1]) / 2.0
                sOBBar = i - 1
                sOBActive = True
        if bOBActive and (i - bOBBar) > OB_EXPIRY:
            bOBActive = False
        if sOBActive and (i - sOBBar) > OB_EXPIRY:
            sOBActive = False

        # ── daily counter ──
        day = dt_ny.date()
        if day != last_day:
            trade_count = 0
            last_day = day
            judas_done = False
            m2022_done = False
            sbpm_done = False

        # ── signals ──
        s = session_flags(dt_ny)
        sess_ok = s["sess_ok"]
        is_flat = i >= flat_from
        tu = bool(trend["up"][i])
        td = bool(trend["down"][i])
        sb = bool(trend["sbull"][i])
        sbr = bool(trend["sbear"][i])
        dyn_cd = COOLDOWN_STRONG if (sb or sbr) else COOLDOWN_NORMAL
        can_enter = (is_flat and sess_ok and trade_count < MAX_TRADES_PER_DAY
                     and (i - last_entry_bar) > dyn_cd and (not p_on) and (not a_on))

        s2L = can_enter and s["judas"] and (not judas_done) and bull_mss and bActive and tu
        s2S = can_enter and s["judas"] and (not judas_done) and bear_mss and sActive and td
        s4L = can_enter and s["m2022"] and (not m2022_done) and bull_mss and bActive and tu
        s4S = can_enter and s["m2022"] and (not m2022_done) and bear_mss and sActive and td
        s6L = can_enter and s["sbpm"] and (not sbpm_done) and bull_mss and bActive and tu
        s6S = can_enter and s["sbpm"] and (not sbpm_done) and bear_mss and sActive and td
        s17L = can_enter and sess_ok and bOBActive and bool(l[i] <= bOBMT) and bool(l[i] >= bOBLow) and tu
        s17S = can_enter and sess_ok and sOBActive and bool(h[i] >= sOBMT) and bool(h[i] <= sOBHigh) and td
        fvgL = can_enter and sess_ok and sb and bActive and bool(l[i] <= b_ce) and bool(c[i] > b_ce) and bool(c[i] > bBot)
        fvgS = can_enter and sess_ok and sbr and sActive and bool(h[i] >= s_ce) and bool(c[i] < s_ce) and bool(c[i] < sTop)

        sigL = s2L or s4L or s6L or s17L or fvgL
        sigS = s2S or s4S or s6S or s17S or fvgS

        if s2L or s2S:
            model_name = "Judas"
        elif s4L or s4S:
            model_name = "2022-AM"
        elif s6L or s6S:
            model_name = "SB-PM"
        elif s17L or s17S:
            model_name = "OB"
        elif fvgL or fvgS:
            model_name = "FVG"
        else:
            model_name = "Unknown"

        setup_made = False

        if sigL:
            if s17L and bOBActive:
                entry = bOBMT
                slv = bOBLow - atr_i * 0.3
            elif fvgL and bActive:
                entry = b_ce
                slv = bBot - atr_i * 0.3
            elif bActive:
                entry = b_ce
                slv = bull_sweep_low - atr_i * 0.3
            else:
                entry = c[i]
                slv = bull_sweep_low - atr_i * 0.3
            if (not np.isnan(entry)) and (not np.isnan(slv)):
                sl_pts = abs(entry - slv) * MULT
                if sl_pts >= MIN_SL_PTS and sl_pts <= MAX_SL_PTS:
                    sl_dist = abs(entry - slv)
                    p_on = True
                    p_is_long = True
                    p_entry = entry
                    p_sl = slv
                    p_tp1 = entry + sl_dist * TP1_R
                    p_tp2 = entry + sl_dist * TP2_R
                    p_tp3 = entry + sl_dist * TP3_R
                    p_bar = i
                    p_model = model_name
                    setup_made = True

        if sigS:
            if s17S and sOBActive:
                entry = sOBMT
                slv = sOBHigh + atr_i * 0.3
            elif fvgS and sActive:
                entry = s_ce
                slv = sTop + atr_i * 0.3
            elif sActive:
                entry = s_ce
                slv = bear_sweep_high + atr_i * 0.3
            else:
                entry = c[i]
                slv = bear_sweep_high + atr_i * 0.3
            if (not np.isnan(entry)) and (not np.isnan(slv)):
                sl_pts = abs(slv - entry) * MULT
                if sl_pts >= MIN_SL_PTS and sl_pts <= MAX_SL_PTS:
                    sl_dist = abs(slv - entry)
                    p_on = True
                    p_is_long = False
                    p_entry = entry
                    p_sl = slv
                    p_tp1 = entry - sl_dist * TP1_R
                    p_tp2 = entry - sl_dist * TP2_R
                    p_tp3 = entry - sl_dist * TP3_R
                    p_bar = i
                    p_model = model_name
                    setup_made = True

        if setup_made:
            events.append({"type": "signal", "i": i, **pend_dict()})

        # ── pending expiry / session cancel ──
        if p_on and (i - p_bar) > MAX_PENDING_BARS:
            events.append({"type": "cancel", "i": i, "reason": "expired (30 bars)", **pend_dict()})
            p_on = False
        if p_on and not sess_ok:
            events.append({"type": "cancel", "i": i, "reason": "session ended", **pend_dict()})
            p_on = False

        # ── fill long ──
        if p_on and p_is_long and is_flat and l[i] <= p_entry:
            a_entry = p_entry
            a_sl = p_sl
            a_tp1 = p_tp1
            a_tp2 = p_tp2
            a_tp3 = p_tp3
            a_risk = abs(p_entry - p_sl) * MULT
            a_is_long = True
            a_tp1_hit = False
            a_tp2_hit = False
            a_tp3_hit = False
            a_closed = False
            a_on = True
            a_entry_bar = i
            a_model = p_model
            trade_count += 1
            last_entry_bar = i
            p_on = False
            if p_model == "Judas":
                judas_done = True
            elif p_model == "2022-AM":
                m2022_done = True
            elif p_model == "SB-PM":
                sbpm_done = True
            events.append({"type": "fill", "i": i, **act_dict()})

        # ── fill short ──
        if p_on and (not p_is_long) and is_flat and h[i] >= p_entry:
            a_entry = p_entry
            a_sl = p_sl
            a_tp1 = p_tp1
            a_tp2 = p_tp2
            a_tp3 = p_tp3
            a_risk = abs(p_sl - p_entry) * MULT
            a_is_long = False
            a_tp1_hit = False
            a_tp2_hit = False
            a_tp3_hit = False
            a_closed = False
            a_on = True
            a_entry_bar = i
            a_model = p_model
            trade_count += 1
            last_entry_bar = i
            p_on = False
            if p_model == "Judas":
                judas_done = True
            elif p_model == "2022-AM":
                m2022_done = True
            elif p_model == "SB-PM":
                sbpm_done = True
            events.append({"type": "fill", "i": i, **act_dict()})

        # ── management ──
        can_check = a_on and (not a_closed) and i > a_entry_bar

        if can_check and (i - a_entry_bar) >= MAX_BARS_TRADE:
            a_closed = True
            a_on = False
            flat_from = i + 1
            fpts = (c[i] - a_entry) * MULT if a_is_long else (a_entry - c[i]) * MULT
            events.append({"type": "time", "i": i, "pts": round(float(fpts), 1), **act_dict()})

        if can_check and a_is_long and (not a_closed):
            if (not a_tp1_hit) and h[i] >= a_tp1:
                a_tp1_hit = True
                events.append({"type": "tp1", "i": i, **act_dict()})
                a_sl = a_entry
            if (not a_tp2_hit) and a_tp1_hit and h[i] >= a_tp2:
                a_tp2_hit = True
                events.append({"type": "tp2", "i": i, **act_dict()})
            if (not a_tp3_hit) and a_tp2_hit and h[i] >= a_tp3:
                a_tp3_hit = True
                a_closed = True
                a_on = False
                flat_from = i + 1
                events.append({"type": "tp3", "i": i, **act_dict()})
            if (not a_tp1_hit) and l[i] <= a_sl:
                a_closed = True
                a_on = False
                flat_from = i + 1
                events.append({"type": "sl", "i": i, **act_dict()})

        if can_check and (not a_is_long) and (not a_closed):
            if (not a_tp1_hit) and l[i] <= a_tp1:
                a_tp1_hit = True
                events.append({"type": "tp1", "i": i, **act_dict()})
                a_sl = a_entry
            if (not a_tp2_hit) and a_tp1_hit and l[i] <= a_tp2:
                a_tp2_hit = True
                events.append({"type": "tp2", "i": i, **act_dict()})
            if (not a_tp3_hit) and a_tp2_hit and l[i] <= a_tp3:
                a_tp3_hit = True
                a_closed = True
                a_on = False
                flat_from = i + 1
                events.append({"type": "tp3", "i": i, **act_dict()})
            if (not a_tp1_hit) and h[i] >= a_sl:
                a_closed = True
                a_on = False
                flat_from = i + 1
                events.append({"type": "sl", "i": i, **act_dict()})

    return events


# ═══════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════
def load_state():
    base = {"active_trade": None, "last_bar_time": None}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            for k, v in base.items():
                if k not in s:
                    s[k] = v
            print("State: active=" + ("YES" if s.get("active_trade") else "NO") + " | last_bar=" + str(s.get("last_bar_time")))
            return s
        except Exception as e:
            print("State file corrupted, resetting: " + str(e))
    print("State new")
    return base


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)
    print("State saved")


# ═══════════════════════════════════════════════════════════════
# EVENTS → TELEGRAM / ORDERS
# ═══════════════════════════════════════════════════════════════
def process_events(events, df30, state, account_id, now_utc):
    n = len(df30)
    signal_bars = set(e["i"] for e in events if e["type"] == "signal")
    fill_bars = set(e["i"] for e in events if e["type"] == "fill")

    for ev in events:
        i = ev["i"]
        etype = ev["type"]
        bar_close = df30.iloc[i]["datetime"] + pd.Timedelta(minutes=30)
        age_min = (now_utc - bar_close).total_seconds() / 60.0
        fresh = (i == n - 1) and (age_min <= EXEC_MAX_AGE_MIN)
        late_tag = "" if age_min <= EXEC_MAX_AGE_MIN else "\n⚠️ LATE (" + str(int(age_min)) + " min after bar close)"
        tstr = fmt_mosul(bar_close)
        d = ev.get("direction", "")
        head = "Model: " + str(ev.get("model", "")) + "\nDir: " + d + "\nEntry: " + str(ev.get("entry", "")) + "\n"

        if etype == "signal":
            touched = "\nEntry level already touched on this bar" if i in fill_bars else "\nWaiting for price to touch entry (max 30 bars)"
            send_telegram(
                "NEW SIGNAL\n"
                "━━━━━━━━━━━━━━━\n"
                "Model: " + str(ev['model']) + "\n"
                "Direction: " + d + "\n"
                "Limit Entry: " + str(ev['entry']) + "\n"
                "Stop: " + str(ev['sl']) + " (" + str(ev['sl_pts']) + " pts)\n"
                "TP1: " + str(ev['tp1']) + "\n"
                "TP2: " + str(ev['tp2']) + "\n"
                "TP3: " + str(ev['tp3']) +
                touched + "\n"
                "━━━━━━━━━━━━━━━\n"
                "Bar close: " + tstr + " (Baghdad)" + late_tag
            )

        elif etype == "cancel":
            send_telegram(
                "PENDING CANCELLED\n"
                "━━━━━━━━━━━━━━━\n"
                "Reason: " + ev["reason"] + "\n" + head +
                "⏰ " + tstr
            )

        elif etype == "fill":
            orders = {}
            if i not in signal_bars:
                send_telegram(
                    "ENTRY FILLED\n"
                    "━━━━━━━━━━━━━━━\n" + head +
                    "Stop: " + str(ev['sl']) + "\n"
                    "TP1: " + str(ev['tp1']) + "\n"
                    "⏰ " + tstr + late_tag
                )
            if ENABLE_AUTO_TRADE and fresh:
                last_close = float(df30.iloc[i]["close"])
                risk_price = abs(ev["entry"] - ev["sl"])
                if abs(last_close - ev["entry"]) > MAX_CHASE_R * risk_price:
                    send_telegram(
                        "ENTRY MISSED — no order placed\n"
                        "━━━━━━━━━━━━━━━\n"
                        "Price " + str(round(last_close, 2)) + " is too far from entry " + str(ev['entry']) + "\n"
                        "⏰ " + tstr
                    )
                else:
                    print("Executing 3 partial orders...")
                    r1 = place_order(account_id, SYMBOL, d, LOT_TP1, ev['sl'], ev['tp1'], tag="tp1")
                    if r1["success"]:
                        orders["tp1"] = r1["order_id"]
                    r2 = place_order(account_id, SYMBOL, d, LOT_TP2, ev['sl'], ev['tp2'], tag="tp2")
                    if r2["success"]:
                        orders["tp2"] = r2["order_id"]
                    r3 = place_order(account_id, SYMBOL, d, LOT_TP3, ev['sl'], ev['tp3'], tag="tp3")
                    if r3["success"]:
                        orders["tp3"] = r3["order_id"]
                    if len(orders) >= 1:
                        send_telegram(
                            "ORDERS EXECUTED\n"
                            "━━━━━━━━━━━━━━━\n"
                            "Total: " + str(len(orders)) + "/3 orders\n"
                            "• TP1: " + str(LOT_TP1) + " lot\n"
                            "• TP2: " + str(LOT_TP2) + " lot\n"
                            "• TP3: " + str(LOT_TP3) + " lot\n"
                            "Symbol: " + SYMBOL + "\n"
                            "Time: " + tstr
                        )
                    else:
                        send_telegram(
                            "ALL ORDERS FAILED\n"
                            "━━━━━━━━━━━━━━━\n"
                            "Signal sent but no execution\n"
                            "Time: " + tstr
                        )
            state["active_trade"] = {
                "model": ev["model"],
                "direction": d,
                "entry": ev["entry"],
                "orders": orders,
                "account_id": account_id,
            }

        elif etype == "tp1":
            at = state.get("active_trade")
            updated = 0
            if at and at.get("orders") and at.get("account_id"):
                for tag in ["tp2", "tp3"]:
                    oid = at["orders"].get(tag)
                    if oid and modify_sl(at["account_id"], oid, ev["entry"]):
                        updated += 1
            send_telegram(
                "TP1 HIT (50% closed)\n"
                "━━━━━━━━━━━━━━━\n" + head +
                "TP1: " + str(ev['tp1']) + "\n"
                "→ SL to BE: " + str(ev['entry']) + "\n"
                "→ Updated " + str(updated) + "/2 remaining\n"
                "⏰ " + tstr + late_tag
            )

        elif etype == "tp2":
            send_telegram(
                "TP2 HIT (30% closed)\n"
                "━━━━━━━━━━━━━━━\n" + head +
                "TP2: " + str(ev['tp2']) + "\n"
                "⏰ " + tstr + late_tag
            )

        elif etype == "tp3":
            send_telegram(
                "TP3 HIT (100% closed)\n"
                "━━━━━━━━━━━━━━━\n" + head +
                "TP3: " + str(ev['tp3']) + "\n"
                "TRADE COMPLETE\n"
                "⏰ " + tstr + late_tag
            )
            state["active_trade"] = None

        elif etype == "sl":
            send_telegram(
                "STOP LOSS\n"
                "━━━━━━━━━━━━━━━\n" + head +
                "SL: " + str(ev['sl']) + "\n"
                "→ All orders closed\n"
                "⏰ " + tstr + late_tag
            )
            state["active_trade"] = None

        elif etype == "time":
            at = state.get("active_trade")
            if at and at.get("orders") and at.get("account_id"):
                for tag in ["tp1", "tp2", "tp3"]:
                    oid = at["orders"].get(tag)
                    if oid:
                        close_position(at["account_id"], oid)
            send_telegram(
                "TIME EXIT (60 bars)\n"
                "━━━━━━━━━━━━━━━\n" + head +
                "Points: " + str(ev['pts']) + "\n"
                "→ All positions closed\n"
                "⏰ " + tstr + late_tag
            )
            state["active_trade"] = None


def run_once():
    state = None
    try:
        state = load_state()
        now_utc = datetime.now(ZoneInfo("UTC"))
        now_ny = now_utc.astimezone(NY_TZ)

        if now_ny.weekday() >= 5:
            print("Weekend - disabled")
            save_state(state)
            return "weekend"

        print("Opening session...")
        account_id = open_session()
        if not account_id:
            print("Cannot open session")
            save_state(state)
            return "no_session"

        print("Fetch M30...")
        df30_raw = fetch_candles(account_id, SYMBOL, TIMEFRAME_ENTRY, 500, 250)
        if df30_raw is None:
            print("M30 data unavailable")
            save_state(state)
            return "no_data"

        # closed candles only
        df30 = df30_raw[df30_raw["datetime"] + pd.Timedelta(minutes=30) <= now_utc].reset_index(drop=True)
        if len(df30) < 100:
            print("Not enough closed M30 candles: " + str(len(df30)))
            save_state(state)
            return "no_data"

        last_bar_dt = df30.iloc[-1]["datetime"]
        if state.get("last_bar_time") is not None:
            prev_dt = pd.Timestamp(state["last_bar_time"])
            if prev_dt.tzinfo is None:
                prev_dt = prev_dt.tz_localize("UTC")
            if prev_dt >= last_bar_dt:
                print("No new closed M30 bar since last check")
                save_state(state)
                return "no_new_bar"
        else:
            prev_dt = df30.iloc[max(0, len(df30) - 1 - BOOT_BARS)]["datetime"]
            print("No saved bar time — checking last " + str(BOOT_BARS) + " closed bars only")

        print("Fetch H1...")
        df1h = fetch_candles(account_id, SYMBOL, TIMEFRAME_TREND, 1000, 1500)
        if df1h is None or len(df1h) < 60:
            df1h = fetch_candles(account_id, SYMBOL, TIMEFRAME_TREND, 500, 500)
        if df1h is None or len(df1h) < 60:
            print("H1 data unavailable — will retry next cycle")
            save_state(state)
            return "no_h1"

        print("Price: " + str(round(float(df30.iloc[-1]['close']), 2)) + " | NY: " + now_ny.strftime('%H:%M') + " | Baghdad: " + fmt_mosul(now_utc))

        trend = build_trend(df30, df1h)
        events = simulate(df30, trend)
        new_events = [e for e in events if df30.iloc[e["i"]]["datetime"] > prev_dt]
        print("Events: total=" + str(len(events)) + " | new=" + str(len(new_events)))

        process_events(new_events, df30, state, account_id, now_utc)

        state["last_bar_time"] = last_bar_dt.isoformat()
        save_state(state)
        return "signal" if new_events else "no_signal"

    except Exception as e:
        print("Cycle Error: " + str(e))
        traceback.print_exc()
        try:
            if state is not None:
                save_state(state)
        except:
            pass
        return "error"


def main():
    print("=" * 60)
    print("ICT Gold Bot — Self-Dispatch Mode")
    print("=" * 60)

    start_time = time.time()
    cycle = 0

    while True:
        cycle += 1
        elapsed = time.time() - start_time

        if elapsed >= MAX_RUNTIME_SECONDS:
            print("")
            print("=" * 60)
            print("55 min reached. Total cycles: " + str(cycle - 1))
            print("=" * 60)
            break

        print("")
        print("=" * 60)
        print("Cycle " + str(cycle) + " | Elapsed: " + str(int(elapsed / 60)) + " min")
        print("=" * 60)

        result = run_once()
        print("Cycle " + str(cycle) + " result: " + result)

        elapsed = time.time() - start_time
        remaining = MAX_RUNTIME_SECONDS - elapsed
        sleep_time = min(LOOP_INTERVAL_SECONDS, remaining)

        if sleep_time > 30:
            print("Sleeping " + str(int(sleep_time)) + "s...")
            time.sleep(sleep_time)
        else:
            print("Remaining time too short (" + str(int(sleep_time)) + "s), exiting loop")
            break

    print("")
    print("=" * 60)
    print("Dispatching next run...")
    print("=" * 60)
    dispatch_next_run()


if __name__ == "__main__":
    main()
