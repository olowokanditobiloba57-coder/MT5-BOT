

import time
import yfinance as yf
import pandas as pd
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
print("Starting Multi-Pair SMC Bot...")

def check_pair(yf_symbol, mt5_symbol):
    print(f"\n--- Checking {mt5_symbol} ---")
    
    # 1. Prevent multiple orders FOR THIS SPECIFIC PAIR
    try:
        positions = client.positions.list(account_id=ACCOUNT_ID)
        orders = client.orders.list(account_id=ACCOUNT_ID)
        
        # Check if we already have an OPEN TRADE for this pair
        for pos in positions:
            if pos['symbol'] == mt5_symbol:
                print(f"Already in a trade for {mt5_symbol}. Skipping.")
                return
                
        # Check if we have a PENDING order (state must be 'placed' - ignores filled/cancelled)
        for order in orders:
            if order['symbol'] == mt5_symbol and order.get('state') == 'placed':
                print(f"Already have a pending order for {mt5_symbol}. Skipping.")
                return
    except Exception as e:
        print("Error checking account:", e)
        return

    # 2. FETCH DATA
    try:
        ticker = yf.Ticker(yf_symbol)
        df_h1 = ticker.history(period="5d", interval="1h")
        df_m15 = ticker.history(period="2d", interval="15m")
    except Exception as e:
        print("Data Error:", e)
        return
    
    if len(df_h1) < 50 or len(df_m15) < 50:
        print("Not enough historical data.")
        return

    # --- DYNAMIC STOP LOSS BUFFER ---
    if "JPY" in mt5_symbol:
        buffer = 0.020  
    elif mt5_symbol == "XAUUSD":
        buffer = 0.20   
    else:
        buffer = 0.00020 

    # --- H4 BIAS ---
    df_h4 = df_h1.resample('4h').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}).dropna()
    df_h4['EMA_50'] = df_h4['Close'].ewm(span=50, adjust=False).mean()
    h4_bias = "BULLISH" if df_h4['Close'].iloc[-1] > df_h4['EMA_50'].iloc[-1] else "BEARISH"
    
    # --- H1 SWEEP ---
    recent_h1 = df_h1.iloc[-10:]
    swing_high_h1 = recent_h1['High'].max()
    swing_low_h1 = recent_h1['Low'].min()
    current_price = df_m15['Close'].iloc[-1]
    equilibrium = (swing_high_h1 + swing_low_h1) / 2

    # --- M15 EXECUTION ---
    if h4_bias == "BULLISH" and df_h1['Low'].iloc[-1] < swing_low_h1 and current_price < equilibrium:
        print("🟢 Setup Found: BUY")
        poi_price = (df_h1['High'].iloc[-1] + df_h1['Low'].iloc[-1]) / 2
        sl = df_h1['Low'].iloc[-1] - buffer
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

    elif h4_bias == "BEARISH" and df_h1['High'].iloc[-1] > swing_high_h1 and current_price > equilibrium:
        print("🔴 Setup Found: SELL")
        poi_price = (df_h1['High'].iloc[-1] + df_h1['Low'].iloc[-1]) / 2
        sl = df_h1['High'].iloc[-1] + buffer
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

# --- MAIN LOOP ---
while True:
    for yf_sym, mt5_sym in PAIRS:
        check_pair(yf_sym, mt5_sym)
        time.sleep(5) 
        
    print("\nFinished checking all 5 pairs. Waiting 15 minutes...")
    time.sleep(900) 
