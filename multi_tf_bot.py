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

# Get credentials from GitHub Secrets
API_KEY = os.getenv("TICKERALL_API_KEY")
MT5_LOGIN = os.getenv("MT5_LOGIN")
MT5_PASSWORD = os.getenv("MT5_PASSWORD")
MT5_SERVER = os.getenv("MT5_SERVER")

if not all([API_KEY, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER]):
    print("ERROR: Missing MT5 or TickerAll credentials in GitHub Secrets.")
    exit(1)

client = Tickerall(api_key=API_KEY)
print("Connecting to NairaProp account via TickerAll...")

# Start a fresh session
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

print("Starting Advanced SMC Bot (Auto-Connect Engine)...")

# --- HELPER FUNCTION: IS THE TRADE RISK-FREE? ---
def is_position_risk_free(pos):
    try:
        entry = float(pos.open_price)
        sl = float(pos.stop_loss) if pos.stop_loss else 0.0
        if pos.side.lower() == 'buy' and sl >= entry: return True
        elif pos.side.lower() == 'sell' and sl <= entry and sl > 0: return True
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
        acc = client.accounts.get(ACCOUNT_ID)
        balance = float(acc.balance)
        equity = float(acc.equity)
        
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
            pos_ticket = getattr(pos, 'ticket', getattr(pos, 'id', None))
            if not pos_ticket: continue

            entry = float(pos.open_price)
            tp = float(pos.take_profit) if pos.take_profit else 0.0
            current_price = float(pos.current_price)
            current_sl = float(pos.stop_loss) if pos.stop_loss else 0.0
            
            if current_sl == 0.0 or tp == 0.0: continue
                
            original_risk = abs(tp - entry) / MIN_RR_RATIO
            
            if "JPY" in pos.symbol: buffer = 0.015
            elif pos.symbol == "XAUUSDm": buffer = 0.15
            else: buffer = 0.00015
                
            if pos.side.lower() == 'buy':
                profit_r = (current_price - entry) / original_risk
                new_sl = current_sl
                
                if profit_r >= 1.0 and current_sl < entry: new_sl = entry + buffer
                elif profit_r >= 1.5 and current_sl < (entry + (original_risk * 0.5)): new_sl = entry + (original_risk * 0.5) + buffer
                elif profit_r >= 2.0 and current_sl < (entry + original_risk): new_sl = entry + original_risk + buffer
                elif profit_r >= 2.5 and current_sl < (entry + (original_risk * 1.5)): new_sl = entry + (original_risk * 1.5) + buffer

                if new_sl > current_sl and new_sl < current_price:
                    print(f"🛡️ Trailing SL for {pos.symbol} to lock in profit (Current: {profit_r:.2f}R)")
                    client.positions.modify(ACCOUNT_ID, ticket=pos_ticket, stop_loss=f"{new_sl:.5f}")
                        
            elif pos.side.lower() == 'sell':
                profit_r = (entry - current_price) / original_risk
                new_sl = current_sl
                
                if profit_r >= 1.0 and (current_sl > entry or current_sl == 0): new_sl = entry - buffer
                elif profit_r >= 1.5 and current_sl > (entry - (original_risk * 0.5)): new_sl = entry - (original_risk * 0.5) - buffer
                elif profit_r >= 2.0 and current_sl > (entry - original_risk): new_sl = entry - original_risk - buffer
                elif profit_r >= 2.5 and current_sl > (entry - (original_risk * 1.5)): new_sl = entry - (original_risk * 1.5) - buffer

                if (new_sl < current_sl or current_sl == 0) and new_sl > current_price:
                    print(f"🛡️ Trailing SL for {pos.symbol} to lock in profit (Current: {profit_r:.2f}R)")
                    client.positions.modify(ACCOUNT_ID, ticket=pos_ticket, stop_loss=f"{new_sl:.5f}")
    except Exception as e:
        print("Trade management error:", e)

# --- STRATEGY & EXECUTION ---
def check_pair(yf_symbol, mt5_symbol):
    print(f"\n--- Checking {mt5_symbol} ---")
    
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

    try:
        acc_detail = client.accounts.get(ACCOUNT_ID)
        positions = acc_detail.positions
        orders = client.orders.list(ACCOUNT_ID)
        
        risky_trades = sum(1 for pos in positions if not is_position_risk_free(pos)) + len(orders)
        
        if risky_trades >= MAX_OPEN_TRADES:
            print(f"🛑 MAX RISKY TRADES REACHED ({risky_trades}/{MAX_OPEN_TRADES}). Skipping.")
            return
            
        for pos in positions:
            if pos.symbol == mt5_symbol and not is_position_risk_free(pos):
                print(f"Already in a RISKY trade for {mt5_symbol}. Skipping.")
                return
                
        for order in orders:
            if order.symbol == mt5_symbol and getattr(order, 'state', '') == 'placed':
                print(f"Pending order exists. Skipping.")
                return
    except Exception:
        return

    try:
        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(period="5d", interval="15m", multi_level_index=False)
    except Exception:
        return
    
    if len(df) < 50: return

    current_price = live_price if live_price is not None else df['Close'].iloc[-1]
    
    sl_buffer = 0.020 if "JPY" in mt5_symbol else (0.20 if mt5_symbol == "XAUUSDm" else 0.00020)

    df_h1 = ticker.history(period="2d", interval="1h", multi_level_index=False)
    df_h1['EMA_50'] = df_h1['Close'].ewm(span=50, adjust=False).mean()
    h1_bullish = df_h1['Close'].iloc[-1] > df_h1['EMA_50'].iloc[-1]

    major_high = df['High'].iloc[-50:].max()
    major_low = df['Low'].iloc[-50:].min()

    # STRATEGY 1: SMC
    c1_high = df['High'].iloc[-3]; c1_low = df['Low'].iloc[-3]
    c3_high = df['High'].iloc[-1]; c3_low = df['Low'].iloc[-1]
    if (c3_low > c1_high) and h1_bullish:
        print("🟢 SMC Bullish FVG")
        poi_price = c1_high; sl = df['Low'].iloc[-3:].min() - sl_buffer
        risk = poi_price - sl
        tp = max(major_high, poi_price + (risk * MIN_RR_RATIO))
        execute_trade(mt5_symbol, "buy", poi_price, sl, tp); return 
    elif (c3_high < c1_low) and not h1_bullish:
        print("🔴 SMC Bearish FVG")
        poi_price = c1_low; sl = df['High'].iloc[-3:].max() + sl_buffer
        risk = sl - poi_price
        tp = min(major_low, poi_price - (risk * MIN_RR_RATIO))
        execute_trade(mt5_symbol, "sell", poi_price, sl, tp); return

    # STRATEGY 2: B&R
    recent_high = df['High'].iloc[-25:-1].max(); recent_low = df['Low'].iloc[-25:-1].min()
    prev_close = df['Close'].iloc[-2]; curr_open = df['Open'].iloc[-1]; curr_close = df['Close'].iloc[-1]
    curr_low = df['Low'].iloc[-1]; curr_high = df['High'].iloc[-1]
    if (prev_close > recent_high) and (curr_low <= recent_high) and (curr_close > curr_open) and h1_bullish:
        print("🟢 Bullish B&R")
        sl = curr_low - sl_buffer
        risk = current_price - sl
        tp = max(major_high, current_price + (risk * MIN_RR_RATIO))
        execute_trade(mt5_symbol, "buy", current_price, sl, tp); return
    elif (prev_close < recent_low) and (curr_high >= recent_low) and (curr_close < curr_open) and not h1_bullish:
        print("🔴 Bearish B&R")
        sl = curr_high + sl_buffer
        risk = sl - current_price
        tp = min(major_low, current_price - (risk * MIN_RR_RATIO))
        execute_trade(mt5_symbol, "sell", current_price, sl, tp); return

    # STRATEGY 3: APA
    body_size = abs(curr_close - curr_open); lower_wick = min(curr_open, curr_close) - curr_low; upper_wick = curr_high - max(curr_open, curr_close)
    prev_open = df['Open'].iloc[-2]
    if ((lower_wick > (body_size * 2)) and (upper_wick < body_size) and (curr_close > curr_open) or (prev_close < prev_open) and (curr_close > curr_open) and (curr_close > prev_open) and (curr_open < prev_close)) and h1_bullish:
        print("🟢 APA Bullish")
        sl = curr_low - sl_buffer
        risk = current_price - sl
        tp = max(major_high, current_price + (risk * MIN_RR_RATIO))
        execute_trade(mt5_symbol, "buy", current_price, sl, tp); return
    elif ((upper_wick > (body_size * 2)) and (lower_wick < body_size) and (curr_close < curr_open) or (prev_close > prev_open) and (curr_close < curr_open) and (curr_close < prev_open) and (curr_open > prev_close)) and not h1_bullish:
        print("🔴 APA Bearish")
        sl = curr_high + sl_buffer
        risk = sl - current_price
        tp = min(major_low, current_price - (risk * MIN_RR_RATIO))
        execute_trade(mt5_symbol, "sell", current_price, sl, tp); return

    print("No clear setup right now.")

# --- HELPER FUNCTION TO PLACE TRADE (TICKERALL) ---
def execute_trade(symbol, side, price, sl, tp):
    current_lot = LOT_SIZES.get(symbol, DEFAULT_LOT_SIZE)
    
    print(f"🎯 Placing {side.upper()} {symbol} at {price:.5f} | Lots: {current_lot} | SL: {sl:.5f} | TP: {tp:.5f}")
    try:
        client.orders.place(
            account_id=ACCOUNT_ID, 
            symbol=symbol, 
            side=side, 
            type="limit", 
            volume=float(current_lot),
            price=float(f"{price:.5f}"), 
            stop_loss=float(f"{sl:.5f}"), 
            take_profit=float(f"{tp:.5f}")
        )
        print("✅ ORDER PLACED!")
    except Exception as e:
        print("Trade Error:", e)

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
            time.sleep(5) 
print("\nFinished. Going back to sleep.")
