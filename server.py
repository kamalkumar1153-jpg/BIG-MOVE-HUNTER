```python
from flask import Flask, render_template, jsonify
import requests
import os

app = Flask(__name__)

# Upstox API Configurations
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")

@app.route('/')
def home():
    return render_template('dashboard.html')

@app.route('/api/live-data')
def get_live_data():
    if not UPSTOX_ACCESS_TOKEN:
        return jsonify({
            "status": "error",
            "message": "UPSTOX_ACCESS_TOKEN not set in environment variables"
        })

    # Fetching Option Chain / Market Feeds from Upstox
    url = "https://api.upstox.com/v2/option/chain"
    params = {
        'instrument_key': 'NSE_INDEX|Nifty 50',
        'expiry_date': '2026-11-03' # Expiry date from Upstox feed
    }
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {UPSTOX_ACCESS_TOKEN}'
    }

    try:
        response = requests.get(url, headers=headers, params=params)
        data = response.json()
        return jsonify(data)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
```
