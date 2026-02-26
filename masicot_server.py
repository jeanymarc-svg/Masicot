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
        
        # Update position tracking
        positions[symbol] = {
            'position': position,
            'price': price,
            'stop': stop,
            'exchange': exchange,
            'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        print(f"[{timestamp}] {symbol}: {position} @ {price} (stop: {stop})")
        
        # Update Google Sheet immediately
        update_positions_sheet()
        
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
        
        # Prepare data for sheet
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
        
        # Clear and update sheet
        range_name = f"{SHEET_NAME_POSITIONS}!A1:F{len(rows) + 1}"
        
        # Clear existing data
        sheets.values().clear(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_NAME_POSITIONS}!A2:F1000"
        ).execute()
        
        # Write new data
        body = {'values': headers + rows}
        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption='RAW',
            body=body
        ).execute()
        
        print(f"✅ Updated positions sheet: {len(rows)} symbols")
        
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
    """Background thread for scheduled signal updates"""
    # Schedule signal detection for specified time on weekdays
    schedule.every().monday.at(UPDATE_TIME).do(update_signals_sheet)
    schedule.every().tuesday.at(UPDATE_TIME).do(update_signals_sheet)
    schedule.every().wednesday.at(UPDATE_TIME).do(update_signals_sheet)
    schedule.every().thursday.at(UPDATE_TIME).do(update_signals_sheet)
    schedule.every().friday.at(UPDATE_TIME).do(update_signals_sheet)
    
    print(f"📅 Scheduler started. Signals will be detected at {UPDATE_TIME} on weekdays.")
    
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
