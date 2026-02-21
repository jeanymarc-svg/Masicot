"""
MASICOT Position Tracker Server
Receives webhooks from TradingView strategies and generates daily digest

Deploy to: Railway.app, Render.com, or Fly.io
"""

from flask import Flask, request, jsonify
from datetime import datetime, time as dt_time
import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import threading
import schedule
import time

app = Flask(__name__)

# ============================================================================
# STATE STORAGE
# ============================================================================
# In production, use Redis or PostgreSQL. For now, in-memory is fine.
positions = {}  # Current positions: {"AAPL": {"position": "LONG", "stop": 225.50, "updated": "2026-02-19"}}
previous_positions = {}  # Yesterday's snapshot for comparison

# ============================================================================
# CONFIGURATION (Set via environment variables)
# ============================================================================
EMAIL_FROM = os.getenv('EMAIL_FROM', 'your-email@gmail.com')
EMAIL_TO = os.getenv('EMAIL_TO', 'your-email@gmail.com')
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD', 'your-app-password')  # Gmail app password
SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', '465'))
DIGEST_TIME = os.getenv('DIGEST_TIME', '16:30')  # 4:30 PM ET

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
@app.route('/generate-digest', methods=['GET'])
def manual_digest():
    """Manually trigger digest generation (for testing)"""
    generate_and_send_digest()
    return jsonify({"status": "Digest generated and sent"}), 200

# ============================================================================
# DIGEST GENERATION
# ============================================================================
def generate_and_send_digest():
    """Compare current positions to previous day and generate digest"""
    global previous_positions
    
    print(f"\n{'='*80}")
    print(f"Generating digest at {datetime.now()}")
    print(f"{'='*80}")
    
    # Get all symbols
    all_symbols = set(list(positions.keys()) + list(previous_positions.keys()))
    
    # Initialize lists
    new_long = []
    new_short = []
    long_exit = []
    short_exit = []
    current_long = []
    current_short = []
    current_neutral = []
    
    # Compare positions
    for symbol in sorted(all_symbols):
        today_pos = positions.get(symbol, {}).get('position', 'NEUTRAL')
        yesterday_pos = previous_positions.get(symbol, {}).get('position', 'NEUTRAL')
        
        # Detect changes
        if today_pos == 'LONG' and yesterday_pos == 'NEUTRAL':
            new_long.append(symbol)
        elif today_pos == 'SHORT' and yesterday_pos == 'NEUTRAL':
            new_short.append(symbol)
        elif today_pos == 'NEUTRAL' and yesterday_pos == 'LONG':
            long_exit.append(symbol)
        elif today_pos == 'NEUTRAL' and yesterday_pos == 'SHORT':
            short_exit.append(symbol)
        
        # Current state
        if today_pos == 'LONG':
            current_long.append(symbol)
        elif today_pos == 'SHORT':
            current_short.append(symbol)
        else:
            current_neutral.append(symbol)
    
    # Build email
    email_body = f"""TandansAI MASICOT — EOD Digest ({datetime.now().strftime('%Y-%m-%d')})

New Long Alert: [{len(new_long)}] {', '.join(new_long) if new_long else '—'}
New Short Alert: [{len(new_short)}] {', '.join(new_short) if new_short else '—'}
New Long Exit: [{len(long_exit)}] {', '.join(long_exit) if long_exit else '—'}
New Short Exit: [{len(short_exit)}] {', '.join(short_exit) if short_exit else '—'}

Current State — Long: [{len(current_long)}] {', '.join(current_long) if current_long else '—'}
Current State — Short: [{len(current_short)}] {', '.join(current_short) if current_short else '—'}
Current State — Neutral: [{len(current_neutral)}] {', '.join(current_neutral) if current_neutral else '—'}

---
Generated by MASICOT Position Tracker
"""
    
    # Send email
    send_email("TandansAI MASICOT — EOD Digest", email_body)
    
    # Update Google Sheet (optional - implement if needed)
    # update_google_sheet(new_long, new_short, long_exit, short_exit, current_long, current_short, current_neutral)
    
    # Save today's positions as yesterday's for tomorrow
    previous_positions.clear()
    previous_positions.update(positions.copy())
    
    print(f"Digest sent! Summary:")
    print(f"  New Long: {len(new_long)}, New Short: {len(new_short)}")
    print(f"  Long Exits: {len(long_exit)}, Short Exits: {len(short_exit)}")
    print(f"  Current: {len(current_long)} Long, {len(current_short)} Short, {len(current_neutral)} Neutral")

# ============================================================================
# EMAIL SENDING
# ============================================================================
def send_email(subject, body):
    """Send email via SMTP"""
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_FROM
        msg['To'] = EMAIL_TO
        msg['Subject'] = subject
        
        msg.attach(MIMEText(body, 'plain'))
        
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.send_message(msg)
        
        print(f"✅ Email sent to {EMAIL_TO}")
        
    except Exception as e:
        print(f"❌ Error sending email: {e}")

# ============================================================================
# SCHEDULED TASKS
# ============================================================================
def run_scheduler():
    """Background thread for scheduled digest generation"""
    # Schedule digest for 4:30 PM ET on weekdays
    schedule.every().monday.at(DIGEST_TIME).do(generate_and_send_digest)
    schedule.every().tuesday.at(DIGEST_TIME).do(generate_and_send_digest)
    schedule.every().wednesday.at(DIGEST_TIME).do(generate_and_send_digest)
    schedule.every().thursday.at(DIGEST_TIME).do(generate_and_send_digest)
    schedule.every().friday.at(DIGEST_TIME).do(generate_and_send_digest)
    
    print(f"📅 Scheduler started. Digest will be sent at {DIGEST_TIME} on weekdays.")
    
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute

# ============================================================================
# STARTUP - Start scheduler when module loads (for gunicorn)
# ============================================================================
# Start scheduler in background thread when module is loaded
scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
scheduler_thread.start()

print(f"\n{'='*80}")
print(f"🚀 MASICOT Position Tracker Server Starting")
print(f"{'='*80}")
print(f"Webhook URL: /webhook")
print(f"Health check: /health")
print(f"Manual trigger: /generate-digest")
print(f"{'='*80}\n")

# For local development
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
