"""
MASICOT Position Tracker Server - Google Sheets Version
Receives webhooks from TradingView strategies and updates Google Sheet

Deploy to: Railway.app, Render.com, or Fly.io
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime
import json
import os
import threading
import schedule
import time
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# ============================================================================
# CONFIGURATION (Set via environment variables)
# ============================================================================
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON', '')  # Service account JSON
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID', '')  # Your Google Sheet ID
SHEET_NAME_POSITIONS = 'Current Positions'
SHEET_NAME_SIGNALS = 'Signals'
UPDATE_TIME = os.getenv('UPDATE_TIME', '16:30')  # Time to snapshot daily signals

# ============================================================================
# GOOGLE SHEETS CLIENT
# ============================================================================
def get_sheets_service():
    """Initialize Google Sheets API client"""
    try:
        credentials_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        credentials = service_account.Credentials.from_service_account_info(
            credentials_dict,
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        service = build('sheets', 'v4', credentials=credentials)
        return service.spreadsheets()
    except Exception as e:
        print(f"❌ Error initializing Google Sheets: {e}")
        return None

# ============================================================================
# STATE STORAGE
# ============================================================================
positions = {}  # Current positions: {"AAPL": {"position": "LONG", "stop": 225.50, "price": 230.00, "updated": "2026-02-21"}}
previous_positions = {}  # Yesterday's snapshot for comparison

# ============================================================================
# WEBHOOK ENDPOINT
# ============================================================================
@app.route('/webhook', methods=['POST'])
def webhook():
    """Receives position updates from TradingView strategies"""
    try:
        data = request.json
        
        # Extract data
        symbol = data.get('symbol')
        exchange = data.get('exchange', '')
        position = data.get('position')  # "LONG", "SHORT", or "NEUTRAL"
        price = data.get('price')
        stop = data.get('stop')
        timestamp = data.get('timestamp')
        
        if not symbol or not position:
            return jsonify({"error": "Missing required fields"}), 400
        
        # Update position tracking IN MEMORY ONLY
        positions[symbol] = {
            'position': position,
            'price': price,
            'stop': stop,
            'exchange': exchange,
            'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        print(f"[{timestamp}] {symbol}: {position} @ {price} (stop: {stop})")
        
        # RESPOND IMMEDIATELY (don't update sheet here)
        return jsonify({"status": "success", "symbol": symbol, "position": position}), 200
        
    except Exception as e:
        print(f"Error processing webhook: {e}")
        return jsonify({"error": str(e)}), 500

# ============================================================================
# HEALTH CHECK
# ============================================================================
@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "positions_tracked": len(positions),
        "last_update": max([p['updated'] for p in positions.values()]) if positions else "never"
    }), 200

# ============================================================================
# MANUAL TRIGGER (for testing)
# ============================================================================
@app.route('/update-sheet', methods=['GET'])
def manual_update():
    """Manually trigger sheet update (for testing)"""
    update_positions_sheet()
    update_signals_sheet()
    return jsonify({"status": "Sheets updated"}), 200

# ============================================================================
# GOOGLE SHEETS UPDATES
# ============================================================================
def update_positions_sheet():
    """Update Current Positions sheet with latest data"""
    try:
        sheets = get_sheets_service()
        if not sheets:
            print("❌ Sheets service not available")
            return
        
        # Prepare data for sheet (keep it minimal)
        headers = [['Symbol', 'Position', 'Price', 'Stop', 'Exchange', 'Last Updated']]
        rows = []
        
        for symbol in sorted(positions.keys()):
            pos = positions[symbol]
            rows.append([
                symbol,
                pos['position'],
                pos['price'],
                pos.get('stop', ''),
                pos.get('exchange', ''),
                pos['updated']
            ])
        
        if not rows:
            print("ℹ️ No positions to update")
            return
        
        # Clear existing data (only the range we need, not 1000 rows)
        range_to_clear = f"{SHEET_NAME_POSITIONS}!A2:F{len(rows) + 10}"
        sheets.values().clear(
            spreadsheetId=SPREADSHEET_ID,
            range=range_to_clear
        ).execute()
        
        # Write new data
        range_name = f"{SHEET_NAME_POSITIONS}!A1:F{len(rows) + 1}"
        body = {'values': headers + rows}
        
        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption='RAW',
            body=body
        ).execute()
        
        print(f"✅ Updated positions sheet: {len(rows)} symbols")
        
        # Clear references to help garbage collection
        del rows
        del headers
        
    except Exception as e:
        print(f"❌ Error updating positions sheet: {e}")

def update_signals_sheet():
    """Update Signals sheet with NEW/EXIT signals"""
    global previous_positions
    
    try:
        sheets = get_sheets_service()
        if not sheets:
            print("❌ Sheets service not available")
            return
        
        # Check if this is the first run (no baseline yet)
        if not previous_positions:
            print("\n" + "="*80)
            print("ℹ️  FIRST RUN DETECTED - ESTABLISHING BASELINE")
            print("="*80)
            print(f"Recorded {len(positions)} symbols as baseline")
            print("No signals generated today (nothing to compare against)")
            print("Signals will begin on the next market day")
            print("="*80 + "\n")
            previous_positions.update(positions.copy())
            return  # Skip signal generation on first run
        
        # Get all symbols
        all_symbols = set(list(positions.keys()) + list(previous_positions.keys()))
        
        # Detect changes
        signals = []
        
        for symbol in sorted(all_symbols):
            today_pos = positions.get(symbol, {}).get('position', 'NEUTRAL')
            yesterday_pos = previous_positions.get(symbol, {}).get('position', 'NEUTRAL')
            
            signal_type = None
            
            # Detect NEW entries
            if today_pos == 'LONG' and yesterday_pos == 'NEUTRAL':
                signal_type = 'NEW LONG'
            elif today_pos == 'SHORT' and yesterday_pos == 'NEUTRAL':
                signal_type = 'NEW SHORT'
            # Detect EXITs
            elif today_pos == 'NEUTRAL' and yesterday_pos == 'LONG':
                signal_type = 'LONG EXIT'
            elif today_pos == 'NEUTRAL' and yesterday_pos == 'SHORT':
                signal_type = 'SHORT EXIT'
            
            if signal_type:
                signals.append([
                    datetime.now().strftime('%Y-%m-%d'),
                    datetime.now().strftime('%H:%M:%S'),
                    symbol,
                    signal_type,
                    positions.get(symbol, {}).get('price', ''),
                    positions.get(symbol, {}).get('stop', '')
                ])
        
        if signals:
            # Append to signals sheet (preserve history)
            range_name = f"{SHEET_NAME_SIGNALS}!A:F"
            body = {'values': signals}
            
            sheets.values().append(
                spreadsheetId=SPREADSHEET_ID,
                range=range_name,
                valueInputOption='RAW',
                insertDataOption='INSERT_ROWS',
                body=body
            ).execute()
            
            print(f"✅ Added {len(signals)} new signals to sheet")
        else:
            print("ℹ️ No new signals to add")
        
        # Update previous positions for next comparison
        previous_positions.clear()
        previous_positions.update(positions.copy())
        
    except Exception as e:
        print(f"❌ Error updating signals sheet: {e}")

# ============================================================================
# SCHEDULED TASKS
# ============================================================================
def run_scheduler():
    """Background thread for scheduled tasks"""
    # TEMPORARY TEST SCHEDULE - Testing tonight at 10:39 PM ET (03:39 UTC)
    # TODO: Change back to 21:05/21:30 UTC after successful test
    
    # Sheet update at 10:39 PM ET (03:39 UTC)
    schedule.every().day.at("03:39").do(update_positions_sheet)
    
    # Signal detection at 10:44 PM ET (03:44 UTC) 
    schedule.every().day.at("03:44").do(update_signals_sheet)
    
    print(f"📅 Scheduler started (TEST MODE - UTC times).")
    print(f"   - Position sheet updates at 03:39 UTC (10:39 PM ET) - TONIGHT")
    print(f"   - Signal detection at 03:44 UTC (10:44 PM ET) - TONIGHT")
    print(f"   ⚠️  REMEMBER TO CHANGE BACK TO 21:05/21:30 AFTER TEST!")
    
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute

# ============================================================================
# STARTUP - Start scheduler when module loads
# ============================================================================
scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
scheduler_thread.start()

print(f"\n{'='*80}")
print(f"🚀 MASICOT Position Tracker Server Starting (Google Sheets Mode)")
print(f"{'='*80}")
print(f"Webhook URL: /webhook")
print(f"Health check: /health")
print(f"Manual update: /update-sheet")
print(f"{'='*80}\n")

# For local development
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
