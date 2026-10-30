import time
import yfinance as yf
import pandas as pd
import numpy as np
from fxapis import Fxapis

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

# --- 1. MANAGE OPEN TRADES (Dynamic 1:1 Breakeven) ---
def manage_open_trades():
    try:
        positions = client.positions.list(account_id=ACCOUNT_ID)
        for pos in positions:
            entry = float(pos['openPrice'])
            current_price = float(pos['currentPrice'])
            current_sl = float(pos['stopLoss']) if pos['stopLoss'] else 0.0
            
            if current_sl == 0.0:
                continue
                
            initial_risk = abs(entry - current_sl)
            
            # Spread buffer for safe breakeven
            if "JPY" in pos['symbol']:
                buffer = 0.015
            elif pos['symbol'] == "XAUUSD":
                buffer = 0.15
            else:
                buffer = 0.00015
                
            if pos['side'] == 'buy':
                if (current_price - entry) >= initial_risk and current_sl < entry:
                    new_sl = entry + buffer
                    print(f"🛡️ 1:1 Profit hit! Moving SL to Risk-Free for {pos['symbol']}")
                    try:
                        client.positions.modify(account_id=ACCOUNT_ID, position_id=pos['id'], stop_loss=f"{new_sl:.5f}")
                    except Exception as e:
                        print("Modify Error:", e)
                        
            elif pos['side'] == 'sell':
                if (entry - current_price) >= initial_risk and (current_sl > entry or current_sl == 0):
                    new_sl = entry - buffer
                    print(f"🛡️ 1:1 Profit hit! Moving SL to Risk-Free for {pos['symbol']}")
                    try:
                        client.positions.modify(account_id=ACCOUNT_ID, position_id=pos['id'], stop_loss=f"{new_sl:.5f}")
                    except Exception as e:
                        print("Modify Error:", e)
    except Exception as e:
        print("Trade management error:", e)

# --- 2. SMC STRATEGY & EXECUTION ---
def check_pair(yf_symbol, mt5_symbol):
    print(f"\n--- Checking {mt5_symbol} ---")
    
    # --- A. Fetch Live Price from fxapis safely ---
    live_price = None
    try:
        quotes = client.quotes.get(account_id=ACCOUNT_ID, symbols=mt5_symbol)
        if quotes and len(quotes) > 0:
            quote = quotes[0]
            # Safely extract ask or bid
            ask = quote.get('ask') if isinstance(quote, dict) else getattr(quote, 'ask', None)
            bid = quote.get('bid') if isinstance(quote, dict) else getattr(quote, 'bid', None)
            
            # Check if the data is not None or a string "None"
            if ask is not None and str(ask).lower() != 'none':
                live_price = float(ask)
            elif bid is not None and str(bid).lower() != 'none':
                live_price = float(bid)
                
            if live_price is not None:
                print(f"📡 Live broker price for {mt5_symbol}: {live_price:.5f}")
            else:
                print(f"⚠️ Broker returned empty price for {mt5_symbol}. Using Yahoo.")
    except Exception as e:
        print(f"⚠️ Could not fetch live price ({e}). Using Yahoo.")

    # --- B. Account & Data Checks ---
    try:
        positions = client.positions.list(account_id=ACCOUNT_ID)
        orders = client.orders.list(account_id=ACCOUNT_ID)
        for pos in positions:
            if pos['symbol'] == mt5_symbol:
                print(f"Already in a trade. Skipping.")
                return
        for order in orders:
            if order['symbol'] == mt5_symbol and order.get('state') == 'placed':
                print(f"Pending order exists. Skipping.")
                return
    except Exception as e:
        print("Account check error:", e)
        return

    try:
        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(period="5d", interval="15m")
    except Exception as e:
        print("Data Error:", e)
        return
    
    if len(df) < 50:
        return

    # Use live price if we got it, otherwise fallback to Yahoo Finance
    current_price = live_price if live_price is not None else df['Close'].iloc[-1]
    
    # Dynamic buffer
    sl_buffer = 0.020 if "JPY" in mt5_symbol else (0.20 if mt5_symbol == "XAUUSD" else 0.00020)

    # --- C. Manual SMC Logic (FVG Detection) ---
    c1_high = df['High'].iloc[-3]
    c1_low = df['Low'].iloc[-3]
    c3_high = df['High'].iloc[-1]
    c3_low = df['Low'].iloc[-1]
    
    bullish_fvg = c3_low > c1_high
    bearish_fvg = c3_high < c1_low

    df_h1 = ticker.history(period="2d", interval="1h")
    df_h1['EMA_50'] = df_h1['Close'].ewm(span=50, adjust=False).mean()
    h1_bullish = df_h1['Close'].iloc[-1] > df_h1['EMA_50'].iloc[-1]

    # --- D. Execution ---
    if bullish_fvg and h1_bullish:
        print("🟢 Setup Found: BULLISH FVG + H1 Trend")
        
        poi_price = c1_high 
        sl = df['Low'].iloc[-3:].min() - sl_buffer
        risk = poi_price - sl
        tp = poi_price + (risk * RR_RATIO)
        
        print(f"🎯 BUY LIMIT {mt5_symbol} at {poi_price:.5f} | SL: {sl:.5f} | TP: {tp:.5f}")
        try:
            client.orders.limit(
                account_id=ACCOUNT_ID, symbol=mt5_symbol, side="buy", volume=LOT_SIZE,
                price=f"{poi_price:.5f}", stop_loss=f"{sl:.5f}", take_profit=f"{tp:.5f}"
            )
            print("✅ BUY LIMIT PLACED!")
        except Exception as e:
            print("Trade Error:", e)

    elif bearish_fvg and not h1_bullish:
        print("🔴 Setup Found: BEARISH FVG + H1 Trend")
        
        poi_price = c1_low
        sl = df['High'].iloc[-3:].max() + sl_buffer
        risk = sl - poi_price
        tp = poi_price - (risk * RR_RATIO)
        
        print(f"🎯 SELL LIMIT {mt5_symbol} at {poi_price:.5f} | SL: {sl:.5f} | TP: {tp:.5f}")
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
print("1. Managing open trades (Moving SL to Risk-Free if 1:1 hit)...")
manage_open_trades()

print("\n2. Scanning pairs for new SMC FVG setups...")
for yf_sym, mt5_sym in PAIRS:
    check_pair(yf_sym, mt5_sym)
    time.sleep(5) 

print("\nFinished checking all pairs. Going back to sleep.")
