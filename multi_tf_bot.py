import time
import os
import yfinance as yf
import pandas as pd
import numpy as np
from tickerall import Tickerall
import forexfactory
from datetime import datetime, timedelta

# --- CONFIGURATION & AUTO-CONNECT ---
MIN_RR_RATIO = 3.0

API_KEY = os.getenv("TICKERALL_API_KEY")
MT5_LOGIN = os.getenv("MT5_LOGIN")
MT5_PASSWORD = os.getenv("MT5_PASSWORD")
MT5_SERVER = os.getenv("MT5_SERVER")

if not all([API_KEY, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER]):
    print("ERROR: Missing MT5 or TickerAll credentials in GitHub Secrets.")
    exit(1)

client = Tickerall(api_key=API_KEY)
print("Connecting to NairaProp account via TickerAll...")

try:
    session = client.sessions.start(
        broker="mt5",
        server=MT5_SERVER,
        account=int(MT5_LOGIN),
        password=MT5_PASSWORD
    )
    ACCOUNT_ID = session.account_id
    print(f"✅ Connected! Account ID: {ACCOUNT_ID}")
except Exception as e:
    print(f"❌ Failed to connect to broker: {e}")
    exit(1)

# --- SAFETY LIMITS ---
MAX_OPEN_TRADES = 2  

# --- FIXED LOT SIZES (Exness 'm' suffix) ---
LOT_SIZES = {
    "EURUSDm": "0.05",   
    "GBPUSDm": "0.05",   
    "GBPJPYm": "0.02",   
    "AUDJPYm": "0.02",
    "XAUUSDm": "0.01"    
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
    ("GC=F", "XAUUSDm")     
]

print("Starting Advanced SMC Bot (Smart Execution & Global Risk Validator)...")

# --- HELPER FUNCTION: SAFE ATTRIBUTE EXTRACTION ---
def get_pos_attr(pos, names, default=None):
    for name in names:
        if hasattr(pos, name):
            val = getattr(pos, name)
            if val is not None: return val
    return default

# --- HELPER FUNCTION: IS THE TRADE RISK-FREE? ---
def is_position_risk_free(pos):
    try:
        entry = float(get_pos_attr(pos, ['open_price', 'price_open', 'openPrice'], 0.0))
        sl = float(get_pos_attr(pos, ['stop_loss', 'sl', 'stopLoss'], 0.0) or 0.0)
        side = str(get_pos_attr(pos, ['side', 'type'], '')).lower()
        
        if side == '0': side = 'buy'
        elif side == '1': side = 'sell'
        
        if side == 'buy' and sl >= entry and entry > 0: return True
        elif side == 'sell' and sl <= entry and sl > 0: return True
        return False
    except:
        return False

# --- NEWS FILTER ---
def is_news_time():
    if ALLOW_NEWS_TRADING: return False 
    try:
        now = datetime.utcnow()
        df = forexfactory.read(currencies=["USD", "EUR", "GBP", "JPY"], impacts=["high"])
        df['datetime_utc'] = pd.to_datetime(df['datetime_utc'])
        upcoming = df[(df['datetime_utc'] >= now) & (df['datetime_utc'] <= now + timedelta(minutes=30))]
        if not upcoming.empty: return True
    except Exception:
        pass
    return False

# --- PROP FIRM RULE CHECKER ---
def check_prop_firm_rules():
    try:
        acc_detail = client.accounts.get(ACCOUNT_ID)
        acc_info = acc_detail.account
        balance = float(acc_info.balance)
        equity = float(acc_info.equity)
        
        current_daily_loss = ((balance - equity) / balance) if balance > 0 else 0
        total_loss = ((INITIAL_ACCOUNT_BALANCE - equity) / INITIAL_ACCOUNT_BALANCE) if INITIAL_ACCOUNT_BALANCE > 0 else 0
        
        print(f"📊 Account: Balance={balance:.2f}, Equity={equity:.2f}")
        
        if current_daily_loss >= DAILY_LOSS_LIMIT_PERCENT:
            print(f"🛑 DAILY LOSS BREACH: Stopping all new trades.")
            return False
        if total_loss >= MAX_LOSS_LIMIT_PERCENT:
            print(f"🛑 MAX LOSS BREACH: Stopping all new trades.")
            return False
        return True
    except Exception as e:
        print(f"⚠️ Could not check rules: {e}. Proceeding.")
        return True

# --- TRADE MANAGEMENT (TRAILING STOP ENGINE) ---
def manage_open_trades():
    try:
        acc_detail = client.accounts.get(ACCOUNT_ID)
        positions = acc_detail.positions
        
        for pos in positions:
            ticket = get_pos_attr(pos, ['ticket', 'id', 'position_id'])
            if not ticket: continue

            entry = float(get_pos_attr(pos, ['open_price', 'price_open', 'openPrice'], 0.0))
            current_price = float(get_pos_attr(pos, ['current_price', 'price_current', 'priceCurrent'], 0.0))
            tp = float(get_pos_attr(pos, ['take_profit', 'tp', 'takeProfit'], 0.0) or 0.0)
            current_sl = float(get_pos_attr(pos, ['stop_loss', 'sl', 'stopLoss'], 0.0) or 0.0)
            symbol = str(get_pos_attr(pos, ['symbol', 'instrument'], ''))
            
            side = str(get_pos_attr(pos, ['side', 'type'], '')).lower()
            if side == '0': side = 'buy'
            elif side == '1': side = 'sell'

            if current_sl == 0.0 or tp == 0.0 or entry == 0.0 or current_price == 0.0: continue
                
            original_risk = abs(tp - entry) / MIN_RR_RATIO
            
            if "JPY" in symbol: buffer = 0.015
            elif symbol == "XAUUSDm": buffer = 0.15
            else: buffer = 0.00015
                
            if side == 'buy':
                profit_r = (current_price - entry) / original_risk
                new_sl = current_sl
                
                if profit_r >= 1.0 and current_sl < entry: new_sl = entry + buffer
                elif profit_r >= 1.5 and current_sl < (entry + (original_risk * 0.5)): new_sl = entry + (original_risk * 0.5) + buffer
                elif profit_r >= 2.0 and current_sl < (entry + original_risk): new_sl = entry + original_risk + buffer
                elif profit_r >= 2.5 and current_sl < (entry + (original_risk * 1.5)): new_sl = entry + (original_risk * 1.5) + buffer

                if new_sl > current_sl and new_sl < current_price:
                    print(f"🛡️ Trailing SL for {symbol} to lock in profit (Current: {profit_r:.2f}R)")
                    client.positions.modify(ACCOUNT_ID, ticket=ticket, stop_loss=f"{new_sl:.5f}")
                        
            elif side == 'sell':
                profit_r = (entry - current_price) / original_risk
                new_sl = current_sl
                
                if profit_r >= 1.0 and (current_sl > entry or current_sl == 0): new_sl = entry - buffer
                elif profit_r >= 1.5 and current_sl > (entry - (original_risk * 0.5)): new_sl = entry - (original_risk * 0.5) - buffer
                elif profit_r >= 2.0 and current_sl > (entry - original_risk): new_sl = entry - original_risk - buffer
                elif profit_r >= 2.5 and current_sl > (entry - (original_risk * 1.5)): new_sl = entry - (original_risk * 1.5) - buffer

                if (new_sl < current_sl or current_sl == 0) and new_sl > current_price:
                    print(f"🛡️ Trailing SL for {symbol} to lock in profit (Current: {profit_r:.2f}R)")
                    client.positions.modify(ACCOUNT_ID, ticket=ticket, stop_loss=f"{new_sl:.5f}")
    except Exception as e:
        print("Trade management error:", e)

# --- STRATEGY & EXECUTION ---
def check_pair(yf_symbol, mt5_symbol):
    print(f"\n--- Checking {mt5_symbol} ---")
    
    # 1. GET LIVE PRICE
    live_price = None
    try:
        quote = client.quotes.get(ACCOUNT_ID, mt5_symbol)
        if quote:
            q = quote[0] if isinstance(quote, list) else quote
            ask = q.get('ask') if isinstance(q, dict) else getattr(q, 'ask', None)
            bid = q.get('bid') if isinstance(q, dict) else getattr(q, 'bid', None)
            if ask: live_price = float(ask)
            elif bid: live_price = float(bid)
    except Exception:
        pass

    # 2. FETCH H1 DATA FOR TREND
    try:
        ticker = yf.Ticker(yf_symbol)
        df_h1 = ticker.history(period="2d", interval="1h")
        if isinstance(df_h1.columns, pd.MultiIndex):
            df_h1.columns = df_h1.columns.get_level_values(0)
        df_h1['EMA_50'] = df_h1['Close'].ewm(span=50, adjust=False).mean()
        h1_bullish = df_h1['Close'].iloc[-1] > df_h1['EMA_50'].iloc[-1]
    except Exception as e:
        print(f"⚠️ H1 Data Error: {e}")
        return

    check_price = live_price if live_price is not None else 0 

    # 3. GLOBAL VALIDATION: CHECK ALL PENDING ORDERS & POSITIONS
    try:
        all_pending_orders = client.orders.list_pending(ACCOUNT_ID)
        acc_detail = client.accounts.get(ACCOUNT_ID)
        positions = acc_detail.positions
        
        # A. Clean up invalid pending orders and count valid ones
        valid_pending_orders = []
        for order in all_pending_orders:
            o_symbol = str(getattr(order, 'symbol', order.get('symbol') if isinstance(order, dict) else ''))
            o_state = str(getattr(order, 'state', order.get('state') if isinstance(order, dict) else '')).lower()
            if o_state != 'placed': continue

            o_id = getattr(order, 'ticket', getattr(order, 'id', None))
            o_side = str(getattr(order, 'side', order.get('side') if isinstance(order, dict) else '')).lower()
            o_sl = float(getattr(order, 'stop_loss', order.get('stop_loss') if isinstance(order, dict) else 0.0) or 0.0)
            
            is_valid = True
            reason = ""
            
            # Check trend flip
            if o_side == 'sell' and h1_bullish:
                is_valid = False; reason = "H1 trend flipped Bullish"
            elif o_side == 'buy' and not h1_bullish:
                is_valid = False; reason = "H1 trend flipped Bearish"
            # Check if price blew past SL
            elif check_price > 0 and o_sl > 0:
                if o_side == 'sell' and check_price > o_sl:
                    is_valid = False; reason = "Price blew past Stop Loss"
                elif o_side == 'buy' and check_price < o_sl:
                    is_valid = False; reason = "Price blew past Stop Loss"

            if not is_valid and o_id:
                print(f"🗑️ Canceling INVALID {o_side.upper()} limit on {o_symbol}. Reason: {reason}")
                try:
                    client.orders.cancel(account_id=ACCOUNT_ID, order_id=o_id)
                except Exception as e:
                    print(f"⚠️ Failed to cancel order: {e}")
            else:
                valid_pending_orders.append(o_symbol)

        # B. Count total global risk (Risky Positions + Valid Pending Orders)
        risky_positions = sum(1 for pos in positions if not is_position_risk_free(pos))
        total_risky_exposure = risky_positions + len(valid_pending_orders)
        
        if total_risky_exposure >= MAX_OPEN_TRADES:
            print(f"🛑 MAX RISKY EXPOSURE REACHED ({total_risky_exposure}/{MAX_OPEN_TRADES}). Skipping.")
            return

        # C. Check if this specific pair already has a valid order or open position
        if mt5_symbol in valid_pending_orders:
            print(f"Valid pending order already exists for {mt5_symbol}. Skipping.")
            return

        for pos in positions:
            pos_symbol = str(get_pos_attr(pos, ['symbol', 'instrument'], ''))
            if pos_symbol == mt5_symbol and not is_position_risk_free(pos):
                print(f"Already in a RISKY trade for {mt5_symbol}. Skipping.")
                return

    except Exception as e:
        print(f"⚠️ Error checking global risk: {e}")
        return

    # 4. FETCH M15 DATA & RUN STRATEGIES (If passed all global checks)
    try:
        df = ticker.history(period="5d", interval="15m")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
    except Exception as e:
        print(f"⚠️ M15 Data Error: {e}")
        return
    
    if len(df) < 50: return

    current_price = live_price if live_price is not None else df['Close'].iloc[-1]
    sl_buffer = 0.020 if "JPY" in mt5_symbol else (0.20 if mt5_symbol == "XAUUSDm" else 0.00020)
    
    major_high = df['High'].iloc[-50:].max()
    major_low = df['Low'].iloc[-50:].min()

    # STRATEGY 1: SMC
    c1_high = df['High'].iloc[-3]; c1_low = df['Low'].iloc[-3]
    c3_high = df['High'].iloc[-1]; c3_low = df['Low'].iloc[-1]
    
    if (c3_low > c1_high) and h1_bullish:
        print("🟢 SMC Bullish FVG -> Limit Order")
        execute_trade(mt5_symbol, "buy", "limit", c1_high, df['Low'].iloc[-3:].min() - sl_buffer, major_high); return 
    elif (c3_high < c1_low) and not h1_bullish:
        print("🔴 SMC Bearish FVG -> Limit Order")
        execute_trade(mt5_symbol, "sell", "limit", c1_low, df['High'].iloc[-3:].max() + sl_buffer, major_low); return

    # STRATEGY 2: B&R
    recent_high = df['High'].iloc[-25:-1].max(); recent_low = df['Low'].iloc[-25:-1].min()
    prev_close = df['Close'].iloc[-2]; curr_open = df['Open'].iloc[-1]; curr_close = df['Close'].iloc[-1]
    curr_low = df['Low'].iloc[-1]; curr_high = df['High'].iloc[-1]
    
    if (prev_close > recent_high) and (curr_low <= recent_high) and (curr_close > curr_open) and h1_bullish:
        print("🟢 Bullish B&R -> Limit Order")
        execute_trade(mt5_symbol, "buy", "limit", recent_high, curr_low - sl_buffer, major_high); return
    elif (prev_close < recent_low) and (curr_high >= recent_low) and (curr_close < curr_open) and not h1_bullish:
        print("🔴 Bearish B&R -> Limit Order")
        execute_trade(mt5_symbol, "sell", "limit", recent_low, curr_high + sl_buffer, major_low); return

    # STRATEGY 3: APA (Market Order)
    body_size = abs(curr_close - curr_open); lower_wick = min(curr_open, curr_close) - curr_low; upper_wick = curr_high - max(curr_open, curr_close)
    prev_open = df['Open'].iloc[-2]
    
    if ((lower_wick > (body_size * 2)) and (upper_wick < body_size) and (curr_close > curr_open) or (prev_close < prev_open) and (curr_close > curr_open) and (curr_close > prev_open) and (curr_open < prev_close)) and h1_bullish:
        print("🟢 APA Bullish -> Market Order")
        execute_trade(mt5_symbol, "buy", "market", current_price, curr_low - sl_buffer, major_high); return
    elif ((upper_wick > (body_size * 2)) and (lower_wick < body_size) and (curr_close < curr_open) or (prev_close > prev_open) and (curr_close < curr_open) and (curr_close < prev_open) and (curr_open > prev_close)) and not h1_bullish:
        print("🔴 APA Bearish -> Market Order")
        execute_trade(mt5_symbol, "sell", "market", current_price, curr_high + sl_buffer, major_low); return

    print("No clear setup right now.")

# --- HELPER FUNCTION TO PLACE TRADE (TICKERALL) ---
def execute_trade(symbol, side, order_type, entry_price, sl, original_major_target):
    current_lot = LOT_SIZES.get(symbol, DEFAULT_LOT_SIZE)
    order_side = "BUY" if side.lower() == "buy" else "SELL"
    
    risk_distance = abs(entry_price - sl)
    
    if order_side == "BUY":
        tp = max(original_major_target, entry_price + (risk_distance * MIN_RR_RATIO))
    else:
        tp = min(original_major_target, entry_price - (risk_distance * MIN_RR_RATIO))

    print(f"🎯 Placing {order_side} {symbol} via {order_type.upper()} | Entry: {entry_price:.5f} | Lots: {current_lot} | SL: {sl:.5f} | TP: {tp:.5f}")
    
    try:
        if order_type == "limit":
            client.orders.place(
                account_id=ACCOUNT_ID, 
                symbol=symbol, 
                side=order_side, 
                type="limit", 
                volume=float(current_lot), 
                price=float(f"{entry_price:.5f}"),
                stop_loss=float(f"{sl:.5f}"), 
                take_profit=float(f"{tp:.5f}")
            )
            print("✅ LIMIT ORDER PLACED!")
        else: 
            client.orders.place(
                account_id=ACCOUNT_ID, 
                symbol=symbol, 
                side=order_side, 
                type="market", 
                volume=float(current_lot), 
                stop_loss=float(f"{sl:.5f}"), 
                take_profit=float(f"{tp:.5f}")
            )
            print("✅ MARKET ORDER PLACED!")

    except Exception as e:
        if order_type == "limit":
            print(f"⚠️ Limit Order rejected ({e}). Falling back to Market Order...")
            try:
                client.orders.place(
                    account_id=ACCOUNT_ID, 
                    symbol=symbol, 
                    side=order_side, 
                    type="market", 
                    volume=float(current_lot), 
                    stop_loss=float(f"{sl:.5f}"), 
                    take_profit=float(f"{tp:.5f}")
                )
                print("✅ FALLBACK MARKET ORDER PLACED!")
            except Exception as fallback_error:
                print("❌ Fallback Trade Error:", fallback_error)
        else:
            print("❌ Trade Error:", e)

# --- MAIN RUN ---
print("1. Managing open trades (Trailing Stop Engine)...")
manage_open_trades()
print("\n2. Checking NairaProp Rules...")
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
