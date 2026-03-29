import requests
from flask import Flask, jsonify

app = Flask(__name__)

# بيانات التليجرام
TOKEN = "8706088814:AAF9yz41489u0Jr8wyzHvwJ1IxVcrF_0A"
CHAT_ID = "6497025227"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    response = requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": msg
    })
    print("TELEGRAM STATUS:", response.status_code)
    print("TELEGRAM RESPONSE:", response.text)
    return response.status_code, response.text

# هذا المسار للاختبار
@app.route("/api/test-telegram", methods=["POST"])
def test_telegram():
    status, text = send_telegram("TEST FROM API")
    return jsonify({
        "ok": status == 200,
        "status": status,
        "response": text
    })

@app.route("/")
def home():
    return "APP WORKING"

if __name__ == "__main__":
    print("APP STARTED")
    app.run(host="0.0.0.0", port=5000)
