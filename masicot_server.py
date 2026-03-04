"""
MASICOT Position Tracker Server - Production Version
Receives webhooks from TradingView strategies and updates Google Sheet

Deploy to: Railway.app
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
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON', '')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID', '')
SHEET_NAME_POSITIONS = 'Current Positions'
SHEET_NAME_SIGNALS = 'Signals'

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
positions = {}
previous_positions = {}

# ============================================================================
# WEBHOOK ENDPOINT
# ============================================================================
@app.route('/webhook', methods=['POST'])
def webhook():
    """Receives position updates from TradingView strategies"""
    try:
        data = request.json
        
        symbol = data.get('symbol')
        exchange = data.get('exchange', '')
        position = data.get('position')
        price = data.get('price')
        stop = data.get('stop')
        timestamp = data.get('timestamp')
        
        if not symbol or not position:
            return jsonify({"error": "Missing required fields"}), 400
        
        positions[symbol] = {
            'position': position,
            'price': price,
            'stop': stop,
            'exchange': exchange,
            'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        print(f"[{timestamp}] {symbol}: {position} @ {price} (stop: {stop})")
        
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
# MANUAL TRIGGER
# ============================================================================
@app.route('/update-sheet', methods=['GET'])
def manual_update():
    """Manually trigger sheet update"""
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
        
        range_to_clear = f"{SHEET_NAME_POSITIONS}!A2:F{len(rows) + 10}"
        sheets.values().clear(
            spreadsheetId=SPREADSHEET_ID,
            range=range_to_clear
        ).execute()
        
        range_name = f"{SHEET_NAME_POSITIONS}!A1:F{len(rows) + 1}"
        body = {'values': headers + rows}
        
        sheets.values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption='RAW',
            body=body
        ).execute()
        
        print(f"✅ Updated positions sheet: {len(rows)} symbols")
        
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
        
        if not previous_positions:
            print("\n" + "="*80)
            print("ℹ️  FIRST RUN DETECTED - ESTABLISHING BASELINE")
            print("="*80)
            print(f"Recorded {len(positions)} symbols as baseline")
            print("No signals generated today (nothing to compare against)")
            print("Signals will begin on the next market day")
            print("="*80 + "\n")
            previous_positions.update(positions.copy())
            return
        
        all_symbols = set(list(positions.keys()) + list(previous_positions.keys()))
        signals = []
        
        for symbol in sorted(all_symbols):
            today_pos = positions.get(symbol, {}).get('position', 'NEUTRAL')
            yesterday_pos = previous_positions.get(symbol, {}).get('position', 'NEUTRAL')
            
            signal_type = None
            
            if today_pos == 'LONG' and yesterday_pos == 'NEUTRAL':
                signal_type = 'NEW LONG'
            elif today_pos == 'SHORT' and yesterday_pos == 'NEUTRAL':
                signal_type = 'NEW SHORT'
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
        
        previous_positions.clear()
        previous_positions.update(positions.copy())
        
    except Exception as e:
        print(f"❌ Error updating signals sheet: {e}")

# ============================================================================
# SCHEDULED TASKS
# ============================================================================
def run_scheduler():
    """Background thread for scheduled tasks"""
    # Position sheet updates at 21:05 UTC (4:05 PM ET)
    schedule.every().monday.at("21:05").do(update_positions_sheet)
    schedule.every().tuesday.at("21:05").do(update_positions_sheet)
    schedule.every().wednesday.at("21:05").do(update_positions_sheet)
    schedule.every().thursday.at("21:05").do(update_positions_sheet)
    schedule.every().friday.at("21:05").do(update_positions_sheet)
    
    # Signal detection at 21:30 UTC (4:30 PM ET)
    schedule.every().monday.at("21:30").do(update_signals_sheet)
    schedule.every().tuesday.at("21:30").do(update_signals_sheet)
    schedule.every().wednesday.at("21:30").do(update_signals_sheet)
    schedule.every().thursday.at("21:30").do(update_signals_sheet)
    schedule.every().friday.at("21:30").do(update_signals_sheet)
    
    print(f"📅 Scheduler started (UTC)")
    print(f"   - Position updates: 21:05 UTC (4:05 PM ET) weekdays")
    print(f"   - Signal detection: 21:30 UTC (4:30 PM ET) weekdays")
    
    while True:
        schedule.run_pending()
        time.sleep(60)

# ============================================================================
# STARTUP
# ============================================================================
scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
scheduler_thread.start()

print(f"\n{'='*80}")
print(f"🚀 MASICOT Position Tracker Server")
print(f"{'='*80}")
print(f"Webhook: /webhook")
print(f"Health: /health")
print(f"Manual: /update-sheet")
print(f"{'='*80}\n")

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
