import requests
from flask import Flask, request, jsonify

# === קונפיג קשיח (לצרכי בדיקה בלבד!) ===
TOKEN = "7626617241:AAHhxyP-CESGTxyP1EdpMZcfIxAhyl7fu8E"  # ⚠️ חשוף בקוד
WEBHOOK_SECRET = "tg-webhook-123456"  # אפשר לשנות לכל מחרוזת אחרת

API = f"https://api.telegram.org/bot{TOKEN}"

app = Flask(__name__)

def send_message(chat_id: int, text: str):
    try:
        requests.post(
            f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10
        )
    except Exception as e:
        print("send_message error:", e)

@app.route("/")
def index():
    return "OK - Telegram bot is alive!"

# טלגרם ישלח לפה עדכונים
@app.route(f"/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    msg = data.get("message") or data.get("edited_message")
    if msg:
        chat_id = msg["chat"]["id"]
        text = msg.get("text") or ""
        reply = f"קיבלתי: {text}" if text else "קיבלתי הודעה 👍"
        send_message(chat_id, reply)
    return jsonify(ok=True)

# קריאה חד-פעמית אחרי דפלוי כדי להגדיר webhook בטלגרם
@app.route("/setwebhook")
def set_webhook():
    # נשתמש בכתובת הנוכחית של השרת כדי לרשום webhook (Render מחזיר https)
    base = request.url_root.replace("http://", "https://")
    if not base.endswith("/"):
        base += "/"
    url = f"{base}{WEBHOOK_SECRET}"
    r = requests.get(f"{API}/setWebhook", params={"url": url}, timeout=10)
    return r.text, r.status_code, {"Content-Type": "application/json"}

# לביטול ה-webhook אם צריך
@app.route("/deletewebhook")
def delete_webhook():
    r = requests.get(f"{API}/deleteWebhook", timeout=10)
    return r.text, r.status_code, {"Content-Type": "application/json"}

# להרצה מקומית (לא חובה ב-Render)
if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
