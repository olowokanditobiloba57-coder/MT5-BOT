import time
import yfinance as yf
import pandas as pd
import numpy as np
from fxapis import Fxapis
import forexfactory
from datetime import datetime, timedelta
import pyvsmc as smc

# --- CONFIGURATION ---
ACCOUNT_ID = "34f8d1d0-6efc-40b6-8ee0-dc420a61197d" 
LOT_SIZE = "0.01"
RR_RATIO = 3.0

PAIRS = [
    ("EURUSD=X", "EURUSD"),
    ("GC=F", "XAUUSD"),     
    ("GBPJPY=X", "GBPJPY"),
    ("GBPUSD=X", "GBPUSD"),
    ("AUDJPY=X", "AUDJPY")
]

client = Fxapis()
print("Starting Advanced SMC Bot...")

# --- 1. NEWS FILTER ---
def is_high_impact_news():
    try:
        now = datetime.utcnow()
        df = forexfactory.read(currencies=["USD", "EUR", "GBP", "JPY"], impacts=["high"])
        upcoming = df[(df['datetime_utc'] >= now) & (df['datetime_utc'] <= now + timedelta(minutes=30))]
        if not upcoming.empty:
            print(f"🚨 NEWS ALERT: High impact event at {upcoming['datetime_utc'].iloc[0]}. Pausing trades.")
            return True
    except Exception as e:
        print("News check error (ignoring and proceeding):", e)
    return False

# --- 2. MANAGE OPEN TRADES (Dynamic 1:1 Breakeven + Spread Buffer) ---
def manage_open_trades():
    try:
        positions = client.positions.list(account_id=ACCOUNT_ID)
        for pos in positions:
            entry = float(pos['openPrice'])
            current_price = float(pos['currentPrice'])
            current_sl = float(pos['stopLoss']) if pos['stopLoss'] else 0.0
            
            if current_sl == 0.0:
                continue
                
            # Calculate the exact risk distance based on the actual trade
            initial_risk = abs(entry - current_sl)
            
            # Dynamic spread buffer to ensure true risk-free breakeven
            if "JPY" in pos['symbol']:
                buffer = 0.015
            elif pos['symbol'] == "XAUUSD":
                buffer = 0.15
            else:
                buffer = 0.00015
                
            if pos['side'] == 'buy':
                price_distance = current_price - entry
                
                # If profit reaches 1:1 risk, move SL to entry + buffer
                if price_distance >= initial_risk and current_sl < entry:
                    new_sl = entry + buffer
                    print(f"🛡️ 1:1 Profit hit! Moving SL to Risk-Free Breakeven for {pos['symbol']}")
                    try:
                        client.positions.modify(account_id=ACCOUNT_ID, position_id=pos['id'], stop_loss=f"{new_sl:.5f}")
                    except Exception as e:
                        print("Failed to modify SL:", e)
                        
            elif pos['side'] == 'sell':
                price_distance = entry - current_price
                
                if price_distance >= initial_risk and (current_sl > entry or current_sl == 0):
                    new_sl = entry - buffer
                    print(f"🛡️ 1:1 Profit hit! Moving SL to Risk-Free Breakeven for {pos['symbol']}")
                    try:
                        client.positions.modify(account_id=ACCOUNT_ID, position_id=pos['id'], stop_loss=f"{new_sl:.5f}")
                    except Exception as e:
                        print("Failed to modify SL:", e)
    except Exception as e:
        print("Trade management error:", e)

# --- 3. SMC STRATEGY & EXECUTION ---
def check_pair(yf_symbol, mt5_symbol):
    print(f"\n--- Checking {mt5_symbol} ---")
    
    try:
        positions = client.positions.list(account_id=ACCOUNT_ID)
        orders = client.orders.list(account_id=ACCOUNT_ID)
        
        for pos in positions:
            if pos['symbol'] == mt5_symbol:
                print(f"Already in a trade for {mt5_symbol}. Skipping.")
                return
        for order in orders:
            if order['symbol'] == mt5_symbol and order.get('state') == 'placed':
                print(f"Pending order exists for {mt5_symbol}. Skipping.")
                return
    except Exception as e:
        print("Error checking account:", e)
        return

    try:
        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(period="5d", interval="15m")
    except Exception as e:
        print("Data Error:", e)
        return
    
    if len(df) < 50:
        return

    # Prepare arrays for pyvsmc
    high = np.array(df['High'], dtype=float)
    low = np.array(df['Low'], dtype=float)
    close = np.array(df['Close'], dtype=float)
    open_ = np.array(df['Open'], dtype=float)
    current_price = close[-1]

    # Detect SMC concepts
    try:
        structure = smc.detect_structure(high, low, close, window_size=2, break_mode="close")
        fvgs = smc.detect_fvg(high, low, close=close)
        obs = smc.detect_order_blocks(open_, high, low, close, lookback=5, zone_mode="body")
    except Exception as e:
        print("SMC calculation error:", e)
        return

    # Dynamic buffer for initial Stop Loss
    sl_buffer = 0.020 if "JPY" in mt5_symbol else (0.20 if mt5_symbol == "XAUUSD" else 0.00020)

    # --- BULLISH SETUP (BUY) ---
    if structure.trend == "bullish" and len(fvgs.bullish) > 0 and len(obs.bullish_ob) > 0:
        print("Setup Found: BULLISH FVG + Order Block")
        
        # Entry near the top of the most recent FVG
        poi_price = fvgs.top[-1] if hasattr(fvgs, 'top') else current_price
        sl = low[-5:].min() - sl_buffer
        risk = poi_price - sl
        tp = poi_price + (risk * RR_RATIO)
        
        print(f"BUY LIMIT {mt5_symbol} at {poi_price:.5f} | SL: {sl:.5f} | TP: {tp:.5f}")
        try:
            client.orders.limit(
                account_id=ACCOUNT_ID, symbol=mt5_symbol, side="buy", volume=LOT_SIZE,
                price=f"{poi_price:.5f}", stop_loss=f"{sl:.5f}", take_profit=f"{tp:.5f}"
            )
            print("✅ BUY LIMIT PLACED!")
        except Exception as e:
            print("Trade Error:", e)

    # --- BEARISH SETUP (SELL) ---
    elif structure.trend == "bearish" and len(fvgs.bearish) > 0 and len(obs.bearish_ob) > 0:
        print("Setup Found: BEARISH FVG + Order Block")
        
        poi_price = fvgs.bottom[-1] if hasattr(fvgs, 'bottom') else current_price
        sl = high[-5:].max() + sl_buffer
        risk = sl - poi_price
        tp = poi_price - (risk * RR_RATIO)
        
        print(f"SELL LIMIT {mt5_symbol} at {poi_price:.5f} | SL: {sl:.5f} | TP: {tp:.5f}")
        try:
            client.orders.limit(
                account_id=ACCOUNT_ID, symbol=mt5_symbol, side="sell", volume=LOT_SIZE,
                price=f"{poi_price:.5f}", stop_loss=f"{sl:.5f}", take_profit=f"{tp:.5f}"
            )
            print("✅ SELL LIMIT PLACED!")
        except Exception as e:
            print("Trade Error:", e)
    else:
        print("No clear setup right now.")

# --- MAIN RUN ---
print("1. Managing open trades (Trailing to Breakeven)...")
manage_open_trades()

print("2. Checking Economic Calendar...")
if is_high_impact_news():
    print("Skipping new entries due to high-impact news.")
else:
    print("3. Scanning pairs for new SMC setups...")
    for yf_sym, mt5_sym in PAIRS:
        check_pair(yf_sym, mt5_sym)
        time.sleep(5) 

print("\nFinished checking all pairs. Going back to sleep.")
