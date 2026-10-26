# ═══════════════════════════════════════════════════════════════
# ICT Gold v15 REAL-TIME BOT (GitHub Actions Edition)
# يعمل لمدة 30 دقيقة ثم يتوقف، وGitHub يشغله مرة ثانية تلقائياً
# ══════════════════════════════════════════════════════════════

import os, json, time, asyncio, traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import aiohttp
import pandas as pd
import numpy as np

# ═══ ENV ═══
TICKERALL_API_KEY = os.environ.get("TICKERALL_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_GROUP_CHAT_ID = os.environ.get("TELEGRAM_GROUP_CHAT_ID")
MT5_SERVER = os.environ.get("MT5_SERVER")
MT5_ACCOUNT = os.environ.get("MT5_ACCOUNT")
MT5_PASSWORD = os.environ.get("MT5_PASSWORD")

STATE_FILE = "state.json"
BASE_URL = "https://api.tickerall.com"
SYMBOL = "XAUUSD!"
NY_TZ = ZoneInfo("America/New_York")
MOSUL_TZ = ZoneInfo("Asia/Baghdad")

# ═══ STRATEGY PARAMS ═══
MULT = 10.0
ATR_PERIOD = 14
MIN_SL_PTS = 40.0
MAX_SL_PTS = 130.0
SL_BUF_PTS = 10.0
COOLDOWN = 10
MAX_BARS_TRADE = 60
TP1_PTS = 100.0
TP2_PTS = 150.0
TP3_PTS = 200.0

PT_P1 = TP1_PTS / MULT
PT_P2 = TP2_PTS / MULT
PT_P3 = TP3_PTS / MULT
BUF_P = SL_BUF_PTS / MULT

POLL_INTERVAL = 30
MAX_RUNTIME = 30 * 60  # 30 دقيقة فقط (GitHub يشغله مرة ثانية بعد 30 دقيقة)

# ═══ TIME FORMATTER ═══
def fmt_mosul(dt):
    local = dt.astimezone(MOSUL_TZ)
    h = local.hour % 12
    h = 12 if h == 0 else h
    m = local.minute
    ap = "PM" if local.hour >= 12 else "AM"
    return f"{h}:{m:02d} {ap}"

# ═══ TELEGRAM ═══
async def send_telegram(msg, session=None):
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️ TG token missing")
        return
    chats = [c for c in [TELEGRAM_CHAT_ID, TELEGRAM_GROUP_CHAT_ID] if c]
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in chats:
        try:
            if session:
                async with session.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}, timeout=10) as r:
                    pass
            else:
                import requests
                requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}, timeout=10)
            print(f"✅ TG -> {chat_id}")
        except Exception as e:
            print(f"❌ TG error: {e}")

# ═══ MT5 API ═══
async def open_session(session):
    url = f"{BASE_URL}/v1/sessions"
    headers = {"Authorization": f"Bearer {TICKERALL_API_KEY}"}
    payload = {
        "broker": "mt5",
        "server": MT5_SERVER,
        "account": int(MT5_ACCOUNT) if MT5_ACCOUNT and MT5_ACCOUNT.isdigit() else MT5_ACCOUNT,
        "password": MT5_PASSWORD
    }
    for attempt in range(3):
        try:
            async with session.post(url, headers=headers, json=payload, timeout=15) as r:
                if r.status == 200:
                    data = await r.json()
                    return data.get("accountId")
                print(f"Session fail {r.status}")
                await asyncio.sleep(3)
        except Exception as e:
            print(f"Session error: {e}")
            await asyncio.sleep(3)
    return None

async def fetch_candles(session, account_id, symbol, timeframe, limit=100):
    url = f"{BASE_URL}/v1/accounts/{account_id}/candles"
    headers = {"Authorization": f"Bearer {TICKERALL_API_KEY}"}
    params = {"symbol": symbol, "timeframe": timeframe, "limit": limit, "hours": 200}
    for attempt in range(3):
        try:
            async with session.get(url, headers=headers, params=params, timeout=15) as r:
                if r.status != 200:
                    await asyncio.sleep(2)
                    continue
                data = await r.json()
                candles = data.get("candles", [])
                if not candles:
                    await asyncio.sleep(2)
                    continue
                df = pd.DataFrame(candles)
                df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
                df = df[["datetime", "open", "high", "low", "close"]].copy()
                for c in ["open", "high", "low", "close"]:
                    df[c] = pd.to_numeric(df[c])
                df = df.sort_values("datetime").reset_index(drop=True)
                return df
        except Exception as e:
            print(f"Fetch error: {e}")
            await asyncio.sleep(2)
    return None

# ══ INDICATORS ═══
def calc_atr(df, period=14):
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
    out = np.full(n, np.nan)
    if n >= period:
        out[period-1] = tr[:period].mean()
        for i in range(period, n):
            out[i] = (out[i-1] * (period-1) + tr[i]) / period
    return out

def ta_pivothigh(highs, left=3, right=3):
    n = len(highs)
    result = np.full(n, np.nan)
    for i in range(left, n - right):
        window = highs[i-left:i+right+1]
        if highs[i] == window.max() and (window == highs[i]).sum() == 1:
            result[i + right] = highs[i]
    return result

def ta_pivotlow(lows, left=3, right=3):
    n = len(lows)
    result = np.full(n, np.nan)
    for i in range(left, n - right):
        window = lows[i-left:i+right+1]
        if lows[i] == window.min() and (window == lows[i]).sum() == 1:
            result[i + right] = lows[i]
    return result

# ═══ SESSIONS ═══
def in_win(dt_ny, h1, m1, h2, m2):
    t = dt_ny.hour * 60 + dt_ny.minute
    return (h1 * 60 + m1) <= t < (h2 * 60 + m2)

def session_flags(dt_ny):
    return {
        "judas": in_win(dt_ny, 9, 30, 10, 0),
        "sbam": in_win(dt_ny, 10, 0, 11, 0),
        "sbpm": in_win(dt_ny, 14, 0, 15, 0),
        "sbldn": in_win(dt_ny, 3, 0, 4, 0),
        "moc": in_win(dt_ny, 15, 15, 15, 45),
        "am_kz": in_win(dt_ny, 9, 30, 11, 30),
        "sess_ok": bool((in_win(dt_ny, 2, 0, 3, 0) or in_win(dt_ny, 3, 0, 4, 0) or
                        in_win(dt_ny, 9, 30, 10, 0) or in_win(dt_ny, 10, 0, 11, 0) or
                        in_win(dt_ny, 11, 50, 12, 10) or in_win(dt_ny, 14, 0, 15, 0) or
                        in_win(dt_ny, 15, 15, 15, 45) or in_win(dt_ny, 14, 0, 14, 30) or
                        in_win(dt_ny, 8, 30, 9, 0)) and not in_win(dt_ny, 12, 10, 13, 59))
    }

# ═══ TREND ═══
def build_trend(df30, df1h):
    h1 = df1h.copy().sort_values("datetime").reset_index(drop=True)
    ema200 = h1["close"].ewm(span=200, adjust=False).mean().values
    ema50 = h1["close"].ewm(span=50, adjust=False).mean().values
    h1c = h1["close"].values
    h1_ns = h1["datetime"].dt.tz_convert("UTC").dt.tz_localize(None).values.astype("datetime64[ns]")
    t30_ns = df30["datetime"].dt.tz_convert("UTC").dt.tz_localize(None).values.astype("datetime64[ns]")
    pos = np.searchsorted(h1_ns, t30_ns, side="right") - 1
    pos = np.clip(pos, 0, len(h1) - 1)
    hc = h1c[pos]
    e200 = ema200[pos]
    e50 = ema50[pos]
    up = hc > e200
    down = hc < e200
    return {"up": up, "down": down, "sbull": up & (hc > e50), "sbear": down & (hc < e50)}

# ═══ STRATEGY ENGINE ═══
def check_signals(df, trend):
    n = len(df)
    if n < 50:
        return []
    
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    atr = calc_atr(df, ATR_PERIOD)
    sh_arr = ta_pivothigh(h, 3, 3)
    sl_arr = ta_pivotlow(l, 3, 3)
    
    i = n - 1
    atr_i = atr[i] if not np.isnan(atr[i]) else atr[-2]
    dt_ny = df.iloc[i]["datetime"].astimezone(NY_TZ)
    s = session_flags(dt_ny)
    
    last_sh, last_sl = np.nan, np.nan
    for j in range(max(0, i-30), i+1):
        if not np.isnan(sh_arr[j]):
            last_sh = sh_arr[j]
        if not np.isnan(sl_arr[j]):
            last_sl = sl_arr[j]
    
    rec_low = df["low"].iloc[max(0,i-20):i].min() if i > 0 else l[i]
    rec_high = df["high"].iloc[max(0,i-20):i].max() if i > 0 else h[i]
    bull_sweep = bool(l[i] < rec_low and c[i] > rec_low)
    bear_sweep = bool(h[i] > rec_high and c[i] < rec_high)
    
    bull_mss = False
    bear_mss = False
    for j in range(max(0, i-15), i):
        if not np.isnan(last_sh) and c[j] > last_sh and c[j-1] <= last_sh:
            bull_mss = True
        if not np.isnan(last_sl) and c[j] < last_sl and c[j-1] >= last_sl:
            bear_mss = True
    
    b_active, s_active = False, False
    b_ce, s_ce = np.nan, np.nan
    b_bot, s_top = np.nan, np.nan
    if i >= 2:
        if l[i] > h[i-2]:
            b_active = True
            b_ce = (l[i] + h[i-2]) / 2.0
            b_bot = h[i-2]
        if h[i] < l[i-2]:
            s_active = True
            s_ce = (l[i-2] + h[i]) / 2.0
            s_top = l[i-2]
    
    b_ob_active, s_ob_active = False, False
    b_ob_mt, s_ob_mt = np.nan, np.nan
    b_ob_low, s_ob_high = np.nan, np.nan
    b_ob_bar_age, s_ob_bar_age = 999, 999
    if i >= 1:
        if c[i] > o[i] and c[i-1] < o[i-1]:
            b_ob_active = True
            b_ob_mt = (o[i-1] + c[i-1]) / 2.0
            b_ob_low = l[i-1]
            b_ob_bar_age = 1
        if c[i] < o[i] and c[i-1] > o[i-1]:
            s_ob_active = True
            s_ob_mt = (o[i-1] + c[i-1]) / 2.0
            s_ob_high = h[i-1]
            s_ob_bar_age = 1
    
    swing_eq = (last_sh + last_sl) / 2.0 if not (np.isnan(last_sh) or np.isnan(last_sl)) else np.nan
    
    tu = bool(trend["up"][i])
    td = bool(trend["down"][i])
    sb = bool(trend["sbull"][i])
    sbr = bool(trend["sbear"][i])
    
    signals = []
    mosul_time = fmt_mosul(df.iloc[i]["datetime"])
    
    def check_risk(entry, sl, is_long):
        dist = abs(entry - sl) * MULT
        return MIN_SL_PTS <= dist <= MAX_SL_PTS
    
    # 1. MMBM
    if s["am_kz"] and bull_sweep and bull_mss and b_ob_active and l[i] <= b_ob_mt and l[i] >= b_ob_low:
        if not np.isnan(swing_eq) and b_ob_mt < swing_eq and tu:
            entry, sl = b_ob_mt, b_ob_low - atr_i * 0.3 - BUF_P
            if check_risk(entry, sl, True):
                signals.append({"model": "MMBM", "dir": "BUY", "entry": entry, "sl": sl, "time": mosul_time})
    
    # 2. ICT 2022
    if s["sbam"]:
        if bull_sweep and bull_mss and b_active and l[i] <= b_ce and l[i] >= b_bot and tu:
            entry, sl = b_ce, b_bot - atr_i * 0.3 - BUF_P
            if check_risk(entry, sl, True):
                signals.append({"model": "2022", "dir": "BUY", "entry": entry, "sl": sl, "time": mosul_time})
        if bear_sweep and bear_mss and s_active and h[i] >= s_ce and h[i] <= s_top and td:
            entry, sl = s_ce, s_top + atr_i * 0.3 + BUF_P
            if check_risk(entry, sl, False):
                signals.append({"model": "2022", "dir": "SELL", "entry": entry, "sl": sl, "time": mosul_time})
    
    # 3. Silver Bullet
    sb_win = s["sbldn"] or s["sbam"] or s["sbpm"]
    if sb_win:
        if sb and b_active and l[i] <= b_ce and c[i] > b_ce and c[i] > b_bot:
            entry, sl = b_ce, b_bot - atr_i * 0.3 - BUF_P
            if check_risk(entry, sl, True):
                signals.append({"model": "SilverBullet", "dir": "BUY", "entry": entry, "sl": sl, "time": mosul_time})
        if sbr and s_active and h[i] >= s_ce and c[i] < s_ce and c[i] < s_top:
            entry, sl = s_ce, s_top + atr_i * 0.3 + BUF_P
            if check_risk(entry, sl, False):
                signals.append({"model": "SilverBullet", "dir": "SELL", "entry": entry, "sl": sl, "time": mosul_time})
    
    # 4. Bearish Reaper
    if s["sess_ok"] and sbr and not np.isnan(swing_eq) and c[i] > swing_eq and s_active and h[i] >= s_ce and h[i] <= s_top and c[i] < o[i]:
        entry, sl = s_ce, s_top + atr_i * 0.3 + BUF_P
        if check_risk(entry, sl, False):
            signals.append({"model": "Reaper", "dir": "SELL", "entry": entry, "sl": sl, "time": mosul_time})
    
    # 5. IOFED
    if s["sess_ok"]:
        if (bull_mss or bull_sweep) and b_ob_active and l[i] <= b_ob_mt and l[i] >= b_ob_low and c[i] > o[i]:
            if not np.isnan(swing_eq) and b_ob_mt < swing_eq and tu:
                entry, sl = b_ob_mt, b_ob_low - atr_i * 0.3 - BUF_P
                if check_risk(entry, sl, True):
                    signals.append({"model": "IOFED", "dir": "BUY", "entry": entry, "sl": sl, "time": mosul_time})
        if (bear_mss or bear_sweep) and s_ob_active and h[i] >= s_ob_mt and h[i] <= s_ob_high and c[i] < o[i]:
            if not np.isnan(swing_eq) and s_ob_mt > swing_eq and td:
                entry, sl = s_ob_mt, s_ob_high + atr_i * 0.3 + BUF_P
                if check_risk(entry, sl, False):
                    signals.append({"model": "IOFED", "dir": "SELL", "entry": entry, "sl": sl, "time": mosul_time})
    
    # 6. FVG-Direct
    if s["sess_ok"]:
        if sb and b_active and l[i] <= b_ce and c[i] > b_ce and c[i] > b_bot:
            entry, sl = b_ce, b_bot - atr_i * 0.3 - BUF_P
            if check_risk(entry, sl, True):
                signals.append({"model": "FVG-Direct", "dir": "BUY", "entry": entry, "sl": sl, "time": mosul_time})
        if sbr and s_active and h[i] >= s_ce and c[i] < s_ce and c[i] < s_top:
            entry, sl = s_ce, s_top + atr_i * 0.3 + BUF_P
            if check_risk(entry, sl, False):
                signals.append({"model": "FVG-Direct", "dir": "SELL", "entry": entry, "sl": sl, "time": mosul_time})
    
    # 7. SBPM-MSS
    if s["sbpm"]:
        if bull_mss and b_active and tu:
            entry, sl = b_ce if not np.isnan(b_ce) else c[i], rec_low - atr_i * 0.3 - BUF_P
            if check_risk(entry, sl, True):
                signals.append({"model": "SBPM-MSS", "dir": "BUY", "entry": entry, "sl": sl, "time": mosul_time})
        if bear_mss and s_active and td:
            entry, sl = s_ce if not np.isnan(s_ce) else c[i], rec_high + atr_i * 0.3 + BUF_P
            if check_risk(entry, sl, False):
                signals.append({"model": "SBPM-MSS", "dir": "SELL", "entry": entry, "sl": sl, "time": mosul_time})
    
    # 8. OB-Pro
    if s["sess_ok"]:
        if sb and b_ob_active and b_ob_bar_age <= 10 and bull_mss:
            if not np.isnan(swing_eq) and b_ob_mt < swing_eq and l[i] <= b_ob_mt and l[i] >= b_ob_low and c[i] > o[i] and c[i] > b_ob_mt:
                entry, sl = b_ob_mt, b_ob_low - atr_i * 0.3 - BUF_P
                if check_risk(entry, sl, True):
                    signals.append({"model": "OB-Pro", "dir": "BUY", "entry": entry, "sl": sl, "time": mosul_time})
        if sbr and s_ob_active and s_ob_bar_age <= 10 and bear_mss:
            if not np.isnan(swing_eq) and s_ob_mt > swing_eq and h[i] >= s_ob_mt and h[i] <= s_ob_high and c[i] < o[i] and c[i] < s_ob_mt:
                entry, sl = s_ob_mt, s_ob_high + atr_i * 0.3 + BUF_P
                if check_risk(entry, sl, False):
                    signals.append({"model": "OB-Pro", "dir": "SELL", "entry": entry, "sl": sl, "time": mosul_time})
    
    return signals

# ═══ MAIN LOOP ═══
async def main():
    print("=" * 60)
    print(" ICT Gold v15 REAL-TIME BOT (GitHub Actions)")
    print(f"⏱️  Polling: every {POLL_INTERVAL} seconds")
    print(f"🔄 Runtime: {MAX_RUNTIME // 60} minutes")
    print("=" * 60)
    
    start = time.time()
    last_signals = {}
    
    async with aiohttp.ClientSession() as session:
        account_id = await open_session(session)
        if not account_id:
            print("❌ Failed to open session")
            return
        
        print(f"✅ Session opened: {account_id}")
        
        while True:
            elapsed = time.time() - start
            if elapsed >= MAX_RUNTIME:
                print("✅ 30 min reached, stopping (GitHub will restart)")
                break
            
            try:
                df = await fetch_candles(session, account_id, SYMBOL, "M30", 100)
                if df is None or len(df) < 50:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                
                df1h = await fetch_candles(session, account_id, SYMBOL, "H1", 500)
                if df1h is None or len(df1h) < 60:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                
                trend = build_trend(df, df1h)
                signals = check_signals(df, trend)
                
                current_bar_time = df.iloc[-1]["datetime"].isoformat()
                for sig in signals:
                    key = f"{current_bar_time}_{sig['model']}_{sig['dir']}"
                    if key not in last_signals:
                        last_signals[key] = True
                        
                        entry = sig['entry']
                        tp1 = entry + (PT_P1 if sig['dir']=='BUY' else -PT_P1)
                        tp2 = entry + (PT_P2 if sig['dir']=='BUY' else -PT_P2)
                        tp3 = entry + (PT_P3 if sig['dir']=='BUY' else -PT_P3)
                        
                        msg = (
                            f" <b>إشارة دخول جديدة!</b>\n"
                            f"━━━━━━━━━━━━━━━\n"
                            f"📊 <b>النموذج:</b> {sig['model']}\n"
                            f"📈 <b>الاتجاه:</b> {sig['dir']}\n"
                            f"💰 <b>الدخول:</b> <code>{entry:.2f}</code>\n"
                            f"🛑 <b>الستوب:</b> <code>{sig['sl']:.2f}</code>\n"
                            f"🎯 <b>الهدف 1:</b> <code>{tp1:.2f}</code> (100 نقطة)\n"
                            f"🎯 <b>الهدف 2:</b> <code>{tp2:.2f}</code> (150 نقطة)\n"
                            f"🎯🎯 <b>الهدف 3:</b> <code>{tp3:.2f}</code> (200 نقطة)\n"
                            f"━━━━━━━━━━━━━━━\n"
                            f"🕰️ <b>وقت الدخول (الموصل):</b> {sig['time']}"
                        )
                        await send_telegram(msg, session)
                        print(f"✅ Signal: {sig['model']} {sig['dir']} @ {sig['time']}")
                
            except Exception as e:
                print(f"❌ Loop error: {e}")
                traceback.print_exc()
            
            await asyncio.sleep(POLL_INTERVAL)
    
    print("🏁 Bot stopped")

if __name__ == "__main__":
    asyncio.run(main())
