import time
import os
import yfinance as yf
import pandas as pd
from tickerall import Tickerall
from datetime import datetime, timedelta, timezone

# --- CONFIGURATION & AUTO-CONNECT ---
MIN_RR_RATIO = 3.0
TRADES_PLACED_THIS_RUN = 0  

API_KEY = os.getenv("TICKERALL_API_KEY")
MT5_LOGIN = os.getenv("MT5_LOGIN")
MT5_PASSWORD = os.getenv("MT5_PASSWORD")
MT5_SERVER = os.getenv("MT5_SERVER")

if not all([API_KEY, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER]):
    print("ERROR: Missing MT5 or TickerAll credentials in GitHub Secrets.")
    exit(1)

# --- WEEKEND FILTER ---
now = datetime.now(timezone.utc)
if (now.weekday() == 5) or (now.weekday() == 6 and now.hour < 21) or (now.weekday() == 4 and now.hour >= 21):
    print("🛑 WEEKEND DETECTED: Forex market is closed. Going back to sleep to protect account from gap risk.")
    exit(0)

client = Tickerall(api_key=API_KEY)
print("Connecting to NairaProp account via TickerAll (Keep-Alive Mode)...")

# 🚀 FIX 1: Use keep_alive instead of start
try:
    session = client.sessions.keep_alive(
        broker="mt5",
        server=MT5_SERVER,
        account=int(MT5_LOGIN),
        password=MT5_PASSWORD,
    )
    ACCOUNT_ID = session.account_id
    print(f"✅ Connected! Account ID: {ACCOUNT_ID}")
except Exception as e:
    print(f"❌ Failed to connect to broker: {repr(e)}")
    exit(1)

# --- SAFETY LIMITS ---
MAX_OPEN_TRADES = 2

# 🛑 Minimum SL distances separated by pair type
MIN_SL_PIPS_STANDARD = 15.0  
MIN_SL_PIPS_JPY = 20.0       
MIN_SL_PIPS_GOLD = 200.0     

# --- FIXED LOT SIZES (Exness 'm' suffix) ---
LOT_SIZES = {
    "EURUSDm": "0.05",
    "GBPUSDm": "0.05",
    "GBPJPYm": "0.02",
    "AUDJPYm": "0.02",
    "XAUUSDm": "0.01",
}
DEFAULT_LOT_SIZE = "0.01"

# --- PROP FIRM RULES (NAIRAPROP) ---
DAILY_LOSS_LIMIT_PERCENT = 0.03
MAX_LOSS_LIMIT_PERCENT = 0.06
INITIAL_ACCOUNT_BALANCE = 1500000.00
ALLOW_NEWS_TRADING = True

PAIRS = [
    ("EURUSD=X", "EURUSDm"),
    ("GBPUSD=X", "GBPUSDm"),
    ("GBPJPY=X", "GBPJPYm"),
    ("AUDJPY=X", "AUDJPYm"),
    ("GC=F", "XAUUSDm"),
]

ENTRY_FIELD_NAMES = [
    "entry_price", "entry", "open_price", "price_open", "openPrice", "price", "avg_price", "average_price", "open"
]

print("Starting Advanced SMC Bot (Validating Limit Prices)...")


# --- HELPER FUNCTIONS ---
def get_attr_safe(obj, names, default=None):
    for name in names:
        if isinstance(obj, dict) and name in obj and obj[name] is not None:
            return obj[name]
        if hasattr(obj, name):
            val = getattr(obj, name)
            if val is not None:
                return val
    return default


def normalize_side(raw_side):
    s = str(raw_side or "").strip().lower()
    if s in ("0", "buy", "order_side_buy", "position_type_buy"):
        return "buy"
    if s in ("1", "sell", "order_side_sell", "position_type_sell"):
        return "sell"
    return s


def is_position_risk_free(pos) -> bool:
    try:
        entry = float(get_attr_safe(pos, ENTRY_FIELD_NAMES, 0.0) or 0.0)
        sl = float(get_attr_safe(pos, ["stop_loss", "sl", "stopLoss"], 0.0) or 0.0)
        side = normalize_side(get_attr_safe(pos, ["side", "type"]))

        if entry <= 0.0 or sl <= 0.0:
            return False
        if side == "buy" and sl >= entry:
            return True
        if side == "sell" and sl <= entry:
            return True
        return False
    except Exception:
        return False


def price_decimals(symbol: str) -> int:
    sym = symbol.upper()
    if sym == "XAUUSDm".upper() or "XAU" in sym:
        return 2
    if "JPY" in sym:
        return 3
    return 5


def fmt_price(symbol: str, value: float) -> float:
    return round(float(value), price_decimals(symbol))


def get_pip_buffer(symbol: str, tier_step: int = 1) -> float:
    sym = symbol.upper()
    if sym == "XAUUSDm".upper() or "XAU" in sym:
        base = 0.15
    elif "JPY" in sym:
        base = 0.015
    else:
        base = 0.00015
    return base * tier_step


def get_sl_buffer(symbol: str) -> float:
    sym = symbol.upper()
    if sym == "XAUUSDm".upper() or "XAU" in sym:
        return 0.20
    if "JPY" in sym:
        return 0.020
    return 0.00020


# --- NEWS FILTER ---
def is_news_time():
    if ALLOW_NEWS_TRADING:
        return False
    try:
        import forexfactory
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        df = forexfactory.read(currencies=["USD", "EUR", "GBP", "JPY"], impacts=["high"])
        df["datetime_utc"] = pd.to_datetime(df["datetime_utc"])
        upcoming = df[(df["datetime_utc"] >= now) & (df["datetime_utc"] <= now + timedelta(minutes=30))]
        if not upcoming.empty:
            return True
    except Exception as e:
        print(f"⚠️ News filter check error: {repr(e)}")
    return False


# --- PROP FIRM RULE CHECKER ---
def check_prop_firm_rules():
    try:
        acc_detail = client.accounts.get(ACCOUNT_ID)
        acc_info = get_attr_safe(acc_detail, ["account"], acc_detail)
        balance = float(get_attr_safe(acc_info, ["balance"], 0.0) or 0.0)
        equity = float(get_attr_safe(acc_info, ["equity"], 0.0) or 0.0)

        current_daily_loss = ((balance - equity) / balance) if balance > 0 else 0.0
        total_loss = ((INITIAL_ACCOUNT_BALANCE - equity) / INITIAL_ACCOUNT_BALANCE) if INITIAL_ACCOUNT_BALANCE > 0 else 0.0

        print(f"📊 Account: Balance={balance:.2f}, Equity={equity:.2f}, DailyFloatingDD={current_daily_loss*100:.2f}%, TotalDD={total_loss*100:.2f}%")

        if current_daily_loss >= DAILY_LOSS_LIMIT_PERCENT:
            print("🛑 DAILY LOSS BREACH: Stopping all new trades.")
            return False
        if total_loss >= MAX_LOSS_LIMIT_PERCENT:
            print("🛑 MAX LOSS BREACH: Stopping all new trades.")
            return False
        return True
    except Exception as e:
        print(f"⚠️ Could not check rules: {repr(e)}. Proceeding.")
        return True


# --- TRADE MANAGEMENT (TRAILING STOP & GHOST CLEANUP) ---
def manage_open_trades():
    try:
        acc_detail = client.accounts.get(ACCOUNT_ID)
        positions = get_attr_safe(acc_detail, ["positions"], []) or []
        print(f"🔎 Open positions count: {len(positions)}")

        for pos in positions:
            ticket = get_attr_safe(pos, ["ticket", "id", "position_id", "order_ticket"])
            symbol = str(get_attr_safe(pos, ["symbol", "instrument"], ""))
            side = normalize_side(get_attr_safe(pos, ["side", "type"]))
            entry = float(get_attr_safe(pos, ENTRY_FIELD_NAMES, 0.0) or 0.0)
            current_price = float(get_attr_safe(pos, ["current_price", "price_current", "priceCurrent", "close_price", "market_price"], 0.0) or 0.0)
            current_sl = float(get_attr_safe(pos, ["stop_loss", "sl", "stopLoss"], 0.0) or 0.0)
            tp = float(get_attr_safe(pos, ["take_profit", "tp", "takeProfit"], 0.0) or 0.0)
            risk_free = is_position_risk_free(pos)

            print(f"   [DEBUG Pos] symbol={symbol} ticket={ticket} side={side} entry={entry} curr={current_price} sl={current_sl} tp={tp} risk_free={risk_free}")

            if not ticket or not symbol or side not in ("buy", "sell"):
                continue

            # 🚨 GHOST TRADE DETECTOR 🚨
            if current_sl > 0 and current_price > 0:
                buffer = get_sl_buffer(symbol)
                if side == "buy" and current_price < (current_sl - buffer):
                    print(f"👻 GHOST TRADE DETECTED on {symbol} (#{ticket}). Price ({current_price}) is below SL ({current_sl}). Force closing...")
                    try:
                        client.positions.close(ACCOUNT_ID, ticket=ticket)
                        print(f"   ✅ Ghost trade closed successfully.")
                    except Exception as e:
                        print(f"   ❌ Failed to close ghost trade: {repr(e)}")
                    continue  
                    
                elif side == "sell" and current_price > (current_sl + buffer):
                    print(f"👻 GHOST TRADE DETECTED on {symbol} (#{ticket}). Price ({current_price}) is above SL ({current_sl}). Force closing...")
                    try:
                        client.positions.close(ACCOUNT_ID, ticket=ticket)
                        print(f"   ✅ Ghost trade closed successfully.")
                    except Exception as e:
                        print(f"   ❌ Failed to close ghost trade: {repr(e)}")
                    continue  

            # --- Normal Trailing Stop Logic Below ---
            if entry <= 0.0 or current_price <= 0.0:
                continue

            if current_sl > 0.0:
                risk_distance = abs(entry - current_sl)
            elif tp > 0.0:
                risk_distance = abs(tp - entry) / MIN_RR_RATIO
            else:
                risk_distance = 0.0

            if risk_distance <= 0:
                continue

            buf = get_pip_buffer(symbol, tier_step=1)
            new_sl = None

            if side == "buy":
                profit_r = (current_price - entry) / risk_distance
                print(f"   📈 {symbol} (#{ticket}) BUY profit_r={profit_r:.2f}R")

                if profit_r >= 2.5:
                    candidate = max(entry + (risk_distance * 1.5) + buf, current_price - risk_distance)
                elif profit_r >= 2.0:
                    candidate = entry + (risk_distance * 1.0) + buf
                elif profit_r >= 1.5:
                    candidate = entry + (risk_distance * 0.5) + buf
                elif profit_r >= 1.0:
                    candidate = entry + buf
                else:
                    candidate = None

                if candidate is not None:
                    formatted_candidate = fmt_price(symbol, candidate)
                    if formatted_candidate > current_sl and formatted_candidate < current_price:
                        new_sl = formatted_candidate

            elif side == "sell":
                profit_r = (entry - current_price) / risk_distance
                print(f"   📉 {symbol} (#{ticket}) SELL profit_r={profit_r:.2f}R")

                if profit_r >= 2.5:
                    candidate = min(entry - (risk_distance * 1.5) - buf, current_price + risk_distance)
                elif profit_r >= 2.0:
                    candidate = entry - (risk_distance * 1.0) - buf
                elif profit_r >= 1.5:
                    candidate = entry - (risk_distance * 0.5) - buf
                elif profit_r >= 1.0:
                    candidate = entry - buf
                else:
                    candidate = None

                if candidate is not None:
                    formatted_candidate = fmt_price(symbol, candidate)
                    sl_improves = (current_sl == 0.0) or (formatted_candidate < current_sl)
                    if sl_improves and formatted_candidate > current_price:
                        new_sl = formatted_candidate

            if new_sl is not None:
                try:
                    print(f"🛡️ Modifying SL for {symbol} (#{ticket}) from {current_sl} -> {new_sl}")
                    client.positions.modify(ACCOUNT_ID, ticket=ticket, stop_loss=new_sl)
                except Exception as mod_err:
                    print(f"   ❌ Failed to modify SL: {repr(mod_err)}")

    except Exception as e:
        print(f"❌ Trade management error: {repr(e)}")


# --- GLOBAL CLEANUP: Cancel all invalid orders across ALL pairs at once ---
def cleanup_invalid_orders():
    try:
        # 🚀 FIX 2: Add wait_ms to list_pending
        all_pending_orders = client.orders.list_pending(ACCOUNT_ID, wait_ms=500) or []
        if not all_pending_orders: return

        ticker_cache = {}
        for order in all_pending_orders:
            o_symbol = str(get_attr_safe(order, ["symbol", "instrument"], ""))
            o_state = str(get_attr_safe(order, ["state", "status"], "")).lower()
            o_id = get_attr_safe(order, ["order_ticket", "ticket", "id"])
            o_side = normalize_side(get_attr_safe(order, ["side", "type"]))
            o_sl = float(get_attr_safe(order, ["stop_loss", "sl", "stopLoss"], 0.0) or 0.0)
            o_tp = float(get_attr_safe(order, ["take_profit", "tp", "takeProfit"], 0.0) or 0.0)

            if o_state and o_state != "placed": continue

            yf_symbol = next((yf for yf, mt5 in PAIRS if mt5 == o_symbol), None)
            if not yf_symbol: continue

            if o_symbol not in ticker_cache:
                ticker = yf.Ticker(yf_symbol)
                df_h1 = ticker.history(period="2d", interval="1h")
                if isinstance(df_h1.columns, pd.MultiIndex):
                    df_h1.columns = df_h1.columns.get_level_values(0)
                if df_h1.empty: continue
                df_h1["EMA_50"] = df_h1["Close"].ewm(span=50, adjust=False).mean()
                h1_close = float(df_h1["Close"].iloc[-1])
                h1_ema = float(df_h1["EMA_50"].iloc[-1])
                ticker_cache[o_symbol] = (h1_close, h1_close > h1_ema)

            check_price, h1_bullish = ticker_cache[o_symbol]
            is_valid = True
            reason = ""

            if o_side == "sell" and h1_bullish:
                is_valid = False; reason = "H1 trend flipped Bullish"
            elif o_side == "buy" and not h1_bullish:
                is_valid = False; reason = "H1 trend flipped Bearish"
            elif check_price > 0 and o_sl > 0:
                if o_side == "sell" and check_price > o_sl:
                    is_valid = False; reason = f"Price blew past SELL SL"
                elif o_side == "buy" and check_price < o_sl:
                    is_valid = False; reason = f"Price blew past BUY SL"
            if is_valid and check_price > 0 and o_tp > 0:
                if o_side == "sell" and check_price <= o_tp:
                    is_valid = False; reason = f"Price reached SELL TP"
                elif o_side == "buy" and check_price >= o_tp:
                    is_valid = False; reason = f"Price reached BUY TP"

            if not is_valid and o_id is not None:
                print(f"🗑️ Canceling INVALID {o_side.upper()} limit on {o_symbol}. Reason: {reason}")
                try:
                    client.orders.cancel_pending(ACCOUNT_ID, o_id)
                except Exception as c_err:
                    print(f"   ⚠️ Failed to cancel: {repr(c_err)}")
    except Exception as e:
        print(f"⚠️ Error during global cleanup: {repr(e)}")


# --- STRATEGY & EXECUTION ---
def check_pair(yf_symbol, mt5_symbol):
    global TRADES_PLACED_THIS_RUN
    
    print(f"\n--- Checking {mt5_symbol} ({yf_symbol}) ---")

    live_price = None
    print(f"   📡 Live price fetch not available via SDK, using yfinance.")

    try:
        ticker = yf.Ticker(yf_symbol)
        df_h1 = ticker.history(period="2d", interval="1h")
        if isinstance(df_h1.columns, pd.MultiIndex):
            df_h1.columns = df_h1.columns.get_level_values(0)
        if df_h1.empty or len(df_h1) < 2:
            print(f"⚠️ Insufficient H1 data.")
            return
        df_h1["EMA_50"] = df_h1["Close"].ewm(span=50, adjust=False).mean()
        h1_close = float(df_h1["Close"].iloc[-1])
        h1_ema = float(df_h1["EMA_50"].iloc[-1])
        h1_bullish = h1_close > h1_ema
    except Exception as e:
        print(f"⚠️ H1 Data Error: {repr(e)}")
        return

    check_price = live_price if live_price is not None else h1_close

    # 3. GLOBAL VALIDATION (Includes local memory)
    try:
        # 🚀 FIX 2: Add wait_ms to list_pending
        all_pending_orders = client.orders.list_pending(ACCOUNT_ID, wait_ms=500) or []
        acc_detail = client.accounts.get(ACCOUNT_ID)
        positions = get_attr_safe(acc_detail, ["positions"], []) or []

        valid_pending_symbols = [str(get_attr_safe(o, ["symbol", "instrument"], "")) for o in all_pending_orders if str(get_attr_safe(o, ["state", "status"], "")).lower() == "placed"]
        
        risky_positions = [pos for pos in positions if not is_position_risk_free(pos)]
        
        # 🧠 MEMORY FIX
        total_risky_exposure = len(risky_positions) + len(valid_pending_symbols) + TRADES_PLACED_THIS_RUN

        if total_risky_exposure >= MAX_OPEN_TRADES:
            print(f"🛑 MAX RISKY EXPOSURE REACHED ({total_risky_exposure}/{MAX_OPEN_TRADES}). Skipping.")
            return

        if mt5_symbol in valid_pending_symbols:
            print(f"ℹ️ Valid pending order already exists for {mt5_symbol}. Skipping.")
            return

        for pos in positions:
            pos_symbol = str(get_attr_safe(pos, ["symbol", "instrument"], ""))
            if pos_symbol == mt5_symbol:
                print(f"ℹ️ Active position already exists for {mt5_symbol}. Skipping.")
                return

    except Exception as e:
        print(f"⚠️ Error checking global risk/orders: {repr(e)}")
        return

    # 4. FETCH M15 DATA & RUN STRATEGIES
    try:
        df = ticker.history(period="5d", interval="15m")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
    except Exception as e:
        print(f"⚠️ M15 Data Error: {repr(e)}")
        return

    if len(df) < 50:
        return

    current_price = live_price if live_price is not None else float(df["Close"].iloc[-1])
    sl_buffer = get_sl_buffer(mt5_symbol)

    major_high = float(df["High"].iloc[-50:].max())
    major_low = float(df["Low"].iloc[-50:].min())

    # STRATEGY 1: SMC FVG
    c1_high = float(df["High"].iloc[-3])
    c1_low = float(df["Low"].iloc[-3])
    c3_high = float(df["High"].iloc[-1])
    c3_low = float(df["Low"].iloc[-1])

    if (c3_low > c1_high) and h1_bullish:
        print("🟢 SMC Bullish FVG -> Limit Order")
        execute_trade(mt5_symbol, "buy", "limit", c1_high, float(df["Low"].iloc[-3:].min()) - sl_buffer, major_high, current_price)
        return
    elif (c3_high < c1_low) and not h1_bullish:
        print("🔴 SMC Bearish FVG -> Limit Order")
        execute_trade(mt5_symbol, "sell", "limit", c1_low, float(df["High"].iloc[-3:].max()) + sl_buffer, major_low, current_price)
        return

    # STRATEGY 2: B&R
    recent_high = float(df["High"].iloc[-25:-1].max())
    recent_low = float(df["Low"].iloc[-25:-1].min())
    prev_close = float(df["Close"].iloc[-2])
    curr_open = float(df["Open"].iloc[-1])
    curr_close = float(df["Close"].iloc[-1])
    curr_low = float(df["Low"].iloc[-1])
    curr_high = float(df["High"].iloc[-1])

    if (prev_close > recent_high) and (curr_low <= recent_high) and (curr_close > curr_open) and h1_bullish:
        print("🟢 Bullish B&R -> Limit Order")
        execute_trade(mt5_symbol, "buy", "limit", recent_high, curr_low - sl_buffer, major_high, current_price)
        return
    elif (prev_close < recent_low) and (curr_high >= recent_low) and (curr_close < curr_open) and not h1_bullish:
        print("🔴 Bearish B&R -> Limit Order")
        execute_trade(mt5_symbol, "sell", "limit", recent_low, curr_high + sl_buffer, major_low, current_price)
        return

    # STRATEGY 3: APA
    body_size = abs(curr_close - curr_open)
    lower_wick = min(curr_open, curr_close) - curr_low
    upper_wick = curr_high - max(curr_open, curr_close)
    prev_open = float(df["Open"].iloc[-2])

    bull_pin = (lower_wick > (body_size * 2)) and (upper_wick < body_size) and (curr_close > curr_open)
    bull_engulf = (prev_close < prev_open) and (curr_close > curr_open) and (curr_close > prev_open) and (curr_open < prev_close)
    bear_pin = (upper_wick > (body_size * 2)) and (lower_wick < body_size) and (curr_close < curr_open)
    bear_engulf = (prev_close > prev_open) and (curr_close < curr_open) and (curr_close < prev_open) and (curr_open > prev_close)

    if (bull_pin or bull_engulf) and h1_bullish:
        print("🟢 APA Bullish -> Market Order")
        execute_trade(mt5_symbol, "buy", "market", current_price, curr_low - sl_buffer, major_high, current_price)
        return
    elif (bear_pin or bear_engulf) and not h1_bullish:
        print("🔴 APA Bearish -> Market Order")
        execute_trade(mt5_symbol, "sell", "market", current_price, curr_high + sl_buffer, major_low, current_price)
        return

    print("No clear setup right now.")


# --- HELPER FUNCTION TO PLACE TRADE (MIN SL DISTANCE & LIMIT VALIDATION) ---
def execute_trade(symbol, side, order_type, entry_price, sl, original_major_target, current_price):
    global TRADES_PLACED_THIS_RUN

    current_lot = float(LOT_SIZES.get(symbol, DEFAULT_LOT_SIZE))
    order_side = "BUY" if side.lower() == "buy" else "SELL"

    fmt_entry = fmt_price(symbol, entry_price)
    fmt_sl = fmt_price(symbol, sl)
    risk_distance = abs(fmt_entry - fmt_sl)

    # 🛑 SAFETY CHECK 1: Minimum SL distance
    if "JPY" in symbol:
        min_sl = MIN_SL_PIPS_JPY * 0.01
    elif "XAU" in symbol:
        min_sl = MIN_SL_PIPS_GOLD * 0.01
    else:
        min_sl = MIN_SL_PIPS_STANDARD * 0.0001

    if risk_distance < min_sl:
        print(f"❌ ABORTING TRADE: SL too tight for {symbol}. Risk ({risk_distance:.5f}) < min ({min_sl:.5f}).")
        return

    if risk_distance <= 0:
        print(f"❌ Invalid risk_distance (0) for {symbol}. Aborting order.")
        return

    # 🛑 SAFETY CHECK 2: Validate Limit Order placement
    if order_type == "limit":
        if order_side == "BUY" and fmt_entry >= current_price:
            print(f"❌ ABORTING LIMIT: BUY entry ({fmt_entry}) is ABOVE current price ({current_price}). Broker would reject.")
            return
        elif order_side == "SELL" and fmt_entry <= current_price:
            print(f"❌ ABORTING LIMIT: SELL entry ({fmt_entry}) is BELOW current price ({current_price}). Broker would reject.")
            return

    if order_side == "BUY":
        raw_tp = max(float(original_major_target), fmt_entry + (risk_distance * MIN_RR_RATIO))
    else:
        raw_tp = min(float(original_major_target), fmt_entry - (risk_distance * MIN_RR_RATIO))

    fmt_tp = fmt_price(symbol, raw_tp)

    print(f"🎯 Placing {order_side} {symbol} via {order_type.upper()} | Entry={fmt_entry} | Lots={current_lot} | SL={fmt_sl} | TP={fmt_tp}")

    try:
        if order_type == "limit":
            res = client.orders.place(
                account_id=ACCOUNT_ID, symbol=symbol, side=order_side, type="limit",
                volume=current_lot, price=fmt_entry, stop_loss=fmt_sl, take_profit=fmt_tp,
            )
            print(f"✅ LIMIT ORDER PLACED! Response: {res}")
        else:
            res = client.orders.place(
                account_id=ACCOUNT_ID, symbol=symbol, side=order_side, type="market",
                volume=current_lot, stop_loss=fmt_sl, take_profit=fmt_tp,
            )
            print(f"✅ MARKET ORDER PLACED! Response: {res}")
            
        TRADES_PLACED_THIS_RUN += 1
        
    except Exception as e:
        print(f"❌ Trade placement error for {symbol} ({order_type}): {repr(e)}")


# --- MAIN RUN ---
TRADES_PLACED_THIS_RUN = 0

print("1. Managing open trades (Trailing Stop Engine & Ghost Cleanup)...")
manage_open_trades()

print("\n2. Cleaning up invalid pending orders...")
cleanup_invalid_orders()

print("\n3. Checking NairaProp Rules...")
if not check_prop_firm_rules():
    print("🛑 Stopping all trading for this cycle.")
else:
    if is_news_time():
        print("📰 Skipping new entries due to news.")
    else:
        print("✅ Scanning pairs...")
        for yf_sym, mt5_sym in PAIRS:
            check_pair(yf_sym, mt5_sym)
            time.sleep(10)
print("\nFinished. Going back to sleep.")
