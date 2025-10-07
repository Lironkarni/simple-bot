import os
import json
import time
import threading
import requests
from flask import Flask, request, jsonify

# ===== קונפיג בסיסי (לבדיקה) =====
TOKEN = os.getenv("TOKEN")
WEBHOOK_SECRET = "tg-webhook-123456"                      # שנה למחרוזת אקראית משלך
API = f"https://api.telegram.org/bot{TOKEN}"

DB_PATH = "subs.json"        # כאן נשמור נתונים פר-קבוצה: members + blacklist
LOCK = threading.Lock()
MENTION_CHUNK = 100          # כמה נקודות (תיוגים) בהודעה אחת
MENTION_DELAY = 0.15         # שניה/חלק שניה בין הודעות כדי לא להיחנק מרייט-לימיט

app = Flask(__name__)

# ---------- קריאה/כתיבה ל"מסד" עם תאימות לאחור ----------
def _ensure_chat_struct(db, chat_id_str):
    """
    מבטיח שלקבוצה יש מבנה:
    db[chat] = { "members": {uid: {...}}, "blacklist": {uid: {...}} }
    ואם ישן (מפה של משתמשים בלבד), משדרג במקום.
    """
    if chat_id_str not in db:
        db[chat_id_str] = {"members": {}, "blacklist": {}}
    elif isinstance(db[chat_id_str], dict) and "members" not in db[chat_id_str]:
        # מבנה ישן: dict של משתמשים → המרה
        old = db[chat_id_str]
        db[chat_id_str] = {"members": old, "blacklist": {}}

def load_db():
    with LOCK:
        if not os.path.exists(DB_PATH):
            return {}
        try:
            with open(DB_PATH, "r", encoding="utf-8") as f:
                db = json.load(f)
                # הבטחת מבנה
                for k in list(db.keys()):
                    _ensure_chat_struct(db, k)
                return db
        except Exception:
            return {}

def save_db(db):
    with LOCK:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)

def is_blacklisted(chat_id: int, user_id: int) -> bool:
    db = load_db()
    cs = db.get(str(chat_id))
    if not cs:
        return False
    return str(user_id) in cs.get("blacklist", {})

def add_user(chat_id: int, user):
    """מוסיף משתמש ל-members אם אינו ב-blacklist."""
    if not user or "id" not in user:
        return
    if is_blacklisted(chat_id, user["id"]):
        return
    db = load_db()
    s = str(chat_id)
    _ensure_chat_struct(db, s)
    uid = str(user["id"])
    db[s]["members"][uid] = {
        "id": user["id"],
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "username": user.get("username"),
        "added_at": int(time.time())
    }
    save_db(db)

def remove_user(chat_id: int, user_id: int):
    db = load_db()
    s = str(chat_id)
    if s in db:
        db[s]["members"].pop(str(user_id), None)
        save_db(db)

def blacklist_add(chat_id: int, user):
    """מוסיף ל-blacklist ומסיר מ-members."""
    if not user or "id" not in user:
        return False
    db = load_db()
    s = str(chat_id)
    _ensure_chat_struct(db, s)
    uid = str(user["id"])
    db[s]["blacklist"][uid] = {
        "id": user["id"],
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "username": user.get("username"),
        "added_at": int(time.time())
    }
    # מסירים מהחברים אם נמצא שם
    db[s]["members"].pop(uid, None)
    save_db(db)
    return True

def blacklist_remove(chat_id: int, user_id: int):
    db = load_db()
    s = str(chat_id)
    if s in db and str(user_id) in db[s]["blacklist"]:
        db[s]["blacklist"].pop(str(user_id), None)
        save_db(db)
        return True
    return False

def list_members_ids(chat_id: int):
    db = load_db()
    s = str(chat_id)
    if s not in db:
        return []
    return [int(uid) for uid in db[s]["members"].keys()]

def count_users(chat_id: int) -> int:
    return len(list_members_ids(chat_id))

def export_users(chat_id: int):
    db = load_db()
    return list(db.get(str(chat_id), {}).get("members", {}).values())

def list_blacklist(chat_id: int):
    db = load_db()
    return list(db.get(str(chat_id), {}).get("blacklist", {}).values())

# ---------- עזר: שליחה וצ'קים ----------
def send_message(chat_id: int, text: str, reply_markup: dict | None = None, parse_mode: str | None = None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    r = requests.post(f"{API}/sendMessage", json=payload, timeout=20)
    if not r.ok:
        print("send_message fail:", r.status_code, r.text)

def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        r = requests.get(f"{API}/getChatMember",
                         params={"chat_id": chat_id, "user_id": user_id},
                         timeout=10)
        if r.ok:
            status = r.json().get("result", {}).get("status")
            return status in {"creator", "administrator"}
    except Exception as e:
        print("is_admin error:", e)
    return False

def resolve_target_user(msg, arg: str | None):
    """
    מחלץ user (dict) על בסיס:
    - reply להודעה → משתמש מה-reply
    - arg מספרי → כ-ID מפורש
    מחזיר dict {'id': ..} מינימלי או None.
    """
    # reply
    reply = msg.get("reply_to_message")
    if reply and reply.get("from", {}).get("id"):
        u = reply["from"]
        return {"id": u["id"], "first_name": u.get("first_name"), "last_name": u.get("last_name"), "username": u.get("username")}
    # arg
    if arg:
        arg = arg.strip()
        if arg.isdigit():
            return {"id": int(arg)}
    return None

# ---------- Flask routes ----------
@app.route("/")
def index():
    return "OK - Group Manager Bot (members + blacklist + dot-mentions)!"

@app.route(f"/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}

    # הודעות רגילות (כולל הצטרפות/עזיבה בסיסית)
    msg = update.get("message") or update.get("edited_message")
    if msg:
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        chat_type = chat.get("type")
        from_user = msg.get("from", {})
        text = (msg.get("text") or "").strip()

        # א. הוספת שולחים ל-members אם לא blacklist
        if chat_type in {"group", "supergroup"} and from_user.get("id"):
            if not is_blacklisted(chat_id, from_user["id"]):
                add_user(chat_id, from_user)

        # ב. מצטרפים חדשים
        new_members = msg.get("new_chat_members") or []
        for member in new_members:
            if not is_blacklisted(chat_id, member.get("id", 0)):
                add_user(chat_id, member)

        # ג. עזיבה
        left_member = msg.get("left_chat_member")
        if left_member and left_member.get("id"):
            remove_user(chat_id, left_member["id"])

        # ----- פקודות בקבוצה -----
        if chat_type in {"group", "supergroup"} and text:
            lower = text.lower()

            # /count – כמה חברים ב-DB
            if lower == "/count":
                send_message(chat_id, f"נשמרו {count_users(chat_id)} משתמשים (ללא blacklist).")
                return jsonify(ok=True)

            # /export – למנהלים בלבד: רשימת משתמשים (תמצית)
            if lower == "/export":
                if not is_admin(chat_id, from_user.get("id", 0)):
                    send_message(chat_id, "רק מנהלים יכולים להשתמש ב-/export.")
                    return jsonify(ok=True)
                users = export_users(chat_id)
                if not users:
                    send_message(chat_id, "אין נתונים.")
                    return jsonify(ok=True)
                lines = []
                for u in users[:200]:
                    name = (u.get("first_name") or "") + (" " + u.get("last_name") if u.get("last_name") else "")
                    name = name.strip() or (u.get("username") or "unknown")
                    lines.append(f"{name} — {u['id']}")
                send_message(chat_id, "Export (ראשונים):\n" + "\n".join(lines))
                return jsonify(ok=True)

            # /bl_add [<id>] – הוספה ל-blacklist (אפשר גם כ-reply)
            if lower.startswith("/bl_add") or lower.startswith("/blacklist_add"):
                if not is_admin(chat_id, from_user.get("id", 0)):
                    send_message(chat_id, "רק מנהלים יכולים להשתמש ב-/bl_add.")
                    return jsonify(ok=True)
                arg = text.split(maxsplit=1)[1] if " " in text else None
                target = resolve_target_user(msg, arg)
                if not target:
                    send_message(chat_id, "שימוש: השב על הודעת המשתמש, או /bl_add <user_id>")
                    return jsonify(ok=True)
                if blacklist_add(chat_id, target):
                    send_message(chat_id, f"נוסף ל-blacklist: {target['id']}. לא יישמר/יתוייג יותר.")
                else:
                    send_message(chat_id, "לא הצלחתי להוסיף ל-blacklist.")
                return jsonify(ok=True)

            # /bl_remove [<id>] – הסרה מ-blacklist (אפשר גם כ-reply)
            if lower.startswith("/bl_remove") or lower.startswith("/blacklist_remove"):
                if not is_admin(chat_id, from_user.get("id", 0)):
                    send_message(chat_id, "רק מנהלים יכולים להשתמש ב-/bl_remove.")
                    return jsonify(ok=True)
                arg = text.split(maxsplit=1)[1] if " " in text else None
                target = resolve_target_user(msg, arg)
                if not target:
                    send_message(chat_id, "שימוש: השב על הודעת המשתמש, או /bl_remove <user_id>")
                    return jsonify(ok=True)
                if blacklist_remove(chat_id, target["id"]):
                    send_message(chat_id, f"הוסר מה-blacklist: {target['id']}.")
                else:
                    send_message(chat_id, "לא נמצא ב-blacklist.")
                return jsonify(ok=True)

            # /bl_list – רשימת blacklisted (תמצית)
            if lower == "/bl_list":
                if not is_admin(chat_id, from_user.get("id", 0)):
                    send_message(chat_id, "רק מנהלים יכולים להשתמש ב-/bl_list.")
                    return jsonify(ok=True)
                bl = list_blacklist(chat_id)
                if not bl:
                    send_message(chat_id, "ה-blacklist ריק.")
                    return jsonify(ok=True)
                lines = []
                for u in bl[:200]:
                    name = (u.get("first_name") or "") + (" " + u.get("last_name") if u.get("last_name") else "")
                    name = name.strip() or (u.get("username") or "unknown")
                    lines.append(f"{name} — {u['id']}")
                send_message(chat_id, "Blacklist (ראשונים):\n" + "\n".join(lines))
                return jsonify(ok=True)

            # /dotall – תייג את כל ה-members בתור נקודות "[.](tg://user?id=...)"
            if lower == "/dotall":
                if not is_admin(chat_id, from_user.get("id", 0)):
                    send_message(chat_id, "רק מנהלים יכולים להשתמש ב-/dotall.")
                    return jsonify(ok=True)

                ids = list_members_ids(chat_id)
                if not ids:
                    send_message(chat_id, "אין חברים ב-DB לתייג.")
                    return jsonify(ok=True)

                # בניית הודעות במנות
                batch = []
                total_sent = 0
                for uid in ids:
                    # דולק־טקסט: נקודה מקושרת לפי ID, עם רווח אחרי
                    batch.append(f"[.](tg://user?id={uid})")
                    if len(batch) >= MENTION_CHUNK:
                        text_chunk = " ".join(batch)
                        send_message(chat_id, text_chunk, parse_mode="Markdown")
                        total_sent += len(batch)
                        batch = []
                        time.sleep(MENTION_DELAY)

                if batch:
                    text_chunk = " ".join(batch)
                    send_message(chat_id, text_chunk, parse_mode="Markdown")
                    total_sent += len(batch)

                send_message(chat_id, f"בוצע תיוג נקודות ל-{total_sent} משתמשים (ללא blacklist).")
                return jsonify(ok=True)

        return jsonify(ok=True)

    # chat_member – שינויים במצבי משתמש (נכנס/יצא/הורחק/קודם)
    chat_member_update = update.get("chat_member")
    if chat_member_update:
        chat = chat_member_update.get("chat", {})
        chat_id = chat.get("id")
        old = chat_member_update.get("old_chat_member", {})
        new = chat_member_update.get("new_chat_member", {})
        user = new.get("user") or old.get("user") or {}
        new_status = (new.get("status") or "").lower()

        if user.get("id"):
            if new_status in {"member", "administrator", "creator"}:
                if not is_blacklisted(chat_id, user["id"]):
                    add_user(chat_id, user)
            elif new_status in {"left", "kicked", "restricted"}:
                remove_user(chat_id, user["id"])

        return jsonify(ok=True)

    return jsonify(ok=True)

# ----- רישום/מחיקת webhook -----
@app.route("/setwebhook")
def set_webhook():
    base = request.url_root.replace("http://", "https://")
    if not base.endswith("/"):
        base += "/"
    url = f"{base}{WEBHOOK_SECRET}"
    r = requests.get(f"{API}/setWebhook", params={"url": url}, timeout=10)
    return r.text, r.status_code, {"Content-Type": "application/json"}

@app.route("/deletewebhook")
def delete_webhook():
    r = requests.get(f"{API}/deleteWebhook", timeout=10)
    return r.text, r.status_code, {"Content-Type": "application/json"}

# להרצה מקומית (לא חובה ב-Render)
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
