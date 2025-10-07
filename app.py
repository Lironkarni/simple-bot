import os
import json
import time
import threading
import requests
from flask import Flask, request, jsonify

# ===== קונפיג בסיסי =====
TOKEN = os.getenv("TOKEN")  # ודא שהוגדר ב-Render → Environment (Key=TOKEN)
if not TOKEN or ":" not in TOKEN:
    raise RuntimeError("Missing/invalid TOKEN env var. Set TOKEN in Render → Environment.")

WEBHOOK_SECRET = "tg-webhook-123456"  # מומלץ להחליף למחרוזת אקראית ארוכה
API = f"https://api.telegram.org/bot{TOKEN}"

DB_PATH = "subs.json"        # כאן נשמור: members, blacklist, settings לכל קבוצה
LOCK = threading.Lock()

# תצורת שליחה של /dotall
MENTION_CHUNK = 100          # כמה תיוגי-נקודה בהודעה אחת
MENTION_DELAY = 0.15         # השהיה בין הודעות כדי לא להיחנק מרייט-לימיט

# טקסט עזרה לפרטי (/start, /help)
HELP_TEXT = (
    "👋 Hi! I'm a group management bot.\n\n"
    "Group commands:\n"
    "• /count — Show how many users are stored (excludes blacklist).\n"
    "• /export — (Admins) Print a preview of stored users.\n"
    "• /bl_add <id> — (Admins) Add a user to the blacklist. You can also reply to the user's message instead of <id>.\n"
    "• /bl_remove <id> — (Admins) Remove a user from the blacklist (or reply).\n"
    "• /bl_list — (Admins) Show the current blacklist (truncated).\n"
    "• /all_users — Show who is allowed to run /dotall (admins only vs everyone).\n"
    "• /all_users on|off — (Admins) Allow everyone to run /dotall or restrict it to admins only.\n"
    "• /dotall — Send mass dot-mentions for all stored users in batches.\n\n"

    "Notes:\n"
    "• The bot auto-saves anyone who writes or joins; removes users when they leave.\n"
    "• Users in the blacklist are not saved and won't be mentioned.\n"
)


app = Flask(__name__)

# ---------- DB עזר ----------
def _ensure_chat_struct(db, chat_id_str):
    """
    מבטיח מבנה עדכני לכל קבוצה:
    {
      "members": { "<uid>": {...} },
      "blacklist": { "<uid>": {...} },
      "settings": { "dotall_anyone": false }
    }
    ותואם לגרסאות ישנות (ישדרג אם חסר).
    """
    if chat_id_str not in db:
        db[chat_id_str] = {"members": {}, "blacklist": {}, "settings": {"dotall_anyone": False}}
    else:
        chat_obj = db[chat_id_str]
        if "members" not in chat_obj or not isinstance(chat_obj["members"], dict):
            chat_obj["members"] = {}
        if "blacklist" not in chat_obj or not isinstance(chat_obj["blacklist"], dict):
            chat_obj["blacklist"] = {}
        if "settings" not in chat_obj or not isinstance(chat_obj["settings"], dict):
            chat_obj["settings"] = {"dotall_anyone": False}
        if "dotall_anyone" not in chat_obj["settings"]:
            chat_obj["settings"]["dotall_anyone"] = False

def load_db():
    with LOCK:
        if not os.path.exists(DB_PATH):
            return {}
        try:
            with open(DB_PATH, "r", encoding="utf-8") as f:
                db = json.load(f)
        except Exception:
            db = {}
        for k in list(db.keys()):
            _ensure_chat_struct(db, k)
        return db

def save_db(db):
    with LOCK:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)

def is_blacklisted(chat_id: int, user_id: int) -> bool:
    db = load_db()
    cs = db.get(str(chat_id))
    if not cs:
        return False
    return str(user_id) in cs["blacklist"]

def add_user(chat_id: int, user):
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
    if s in db and str(user_id) in db[s]["members"]:
        db[s]["members"].pop(str(user_id), None)
        save_db(db)

def blacklist_add(chat_id: int, user):
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
    s = str(chat_id)
    if s not in db:
        return []
    return list(db[s]["members"].values())

def list_blacklist(chat_id: int):
    db = load_db()
    s = str(chat_id)
    if s not in db:
        return []
    return list(db[s]["blacklist"].values())

# ---- Settings (per chat) ----
def get_setting(chat_id: int, key: str, default=None):
    db = load_db()
    s = str(chat_id)
    if s not in db:
        return default
    return db[s]["settings"].get(key, default)

def set_setting(chat_id: int, key: str, value):
    db = load_db()
    s = str(chat_id)
    _ensure_chat_struct(db, s)
    db[s]["settings"][key] = value
    save_db(db)

# ---------- Bot API עזר ----------
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
    # reply
    reply = msg.get("reply_to_message")
    if reply and reply.get("from", {}).get("id"):
        u = reply["from"]
        return {"id": u["id"], "first_name": u.get("first_name"), "last_name": u.get("last_name"), "username": u.get("username")}
    # arg מספרי
    if arg:
        arg = arg.strip()
        if arg.isdigit():
            return {"id": int(arg)}
    return None

# ---------- Flask routes ----------
@app.route("/")
def index():
    return "OK - Group Manager Bot (members + blacklist + dot-mentions + all_users setting)!"

@app.route(f"/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}

    # ---- message / edited_message ----
    msg = update.get("message") or update.get("edited_message")
    if msg:
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        chat_type = chat.get("type")
        from_user = msg.get("from", {})
        text = (msg.get("text") or "").strip()

        # ===== פרטִי: /start או /help → עזרה =====
        if chat_type == "private":
            if text.startswith("/start") or text.startswith("/help"):
                send_message(chat_id, HELP_TEXT)
                return jsonify(ok=True)
            else:
                send_message(chat_id, "היי! כתוב /start כדי לראות את כל הפקודות הזמינות.")
                return jsonify(ok=True)

        # ===== קבוצה/סופרקבוצה: שמירות בסיס =====
        if chat_type in {"group", "supergroup"} and from_user.get("id"):
            if not is_blacklisted(chat_id, from_user["id"]):
                add_user(chat_id, from_user)

        new_members = msg.get("new_chat_members") or []
        for m in new_members:
            if not is_blacklisted(chat_id, m.get("id", 0)):
                add_user(chat_id, m)

        left = msg.get("left_chat_member")
        if left and left.get("id"):
            remove_user(chat_id, left["id"])

        # ---- פקודות בקבוצה ----
        if chat_type in {"group", "supergroup"} and text:
            lower = text.lower()

            # /count
            if lower == "/count":
                send_message(chat_id, f"נשמרו {count_users(chat_id)} משתמשים (ללא blacklist).")
                return jsonify(ok=True)

            # /export (admins)
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

            # /bl_add [id] (admins)
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

            # /bl_remove [id] (admins)
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

            # /bl_list (admins)
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

            # /all_users [on|off]  ← הגדרה מי יכול להריץ /dotall
            if lower.startswith("/all_users"):
                parts = text.split(maxsplit=1)
                if len(parts) == 1:
                    current = bool(get_setting(chat_id, "dotall_anyone", False))
                    who = "כולם" if current else "מנהלים בלבד"
                    send_message(chat_id, f"/dotall כרגע: {who}. לשינוי: /all_users on|off")
                    return jsonify(ok=True)

                if not is_admin(chat_id, from_user.get("id", 0)):
                    send_message(chat_id, "רק מנהלים יכולים לשנות /all_users.")
                    return jsonify(ok=True)

                arg = parts[1].strip().lower()
                if arg in {"on", "off"}:
                    value = (arg == "on")
                    set_setting(chat_id, "dotall_anyone", value)
                    who = "כולם" if value else "מנהלים בלבד"
                    send_message(chat_id, f"הוגדר: /dotall זמין ל- {who}.")
                else:
                    send_message(chat_id, "שימוש: /all_users on או /all_users off")
                return jsonify(ok=True)

            # /dotall – תיוג נקודות לכולם (לפי ההגדרה)
            if lower == "/dotall":
                allow_all = bool(get_setting(chat_id, "dotall_anyone", False))
                if not (is_admin(chat_id, from_user.get("id", 0)) or allow_all):
                    send_message(chat_id, "הפקודה /dotall זמינה למנהלים בלבד. ניתן לשנות עם /all_users on")
                    return jsonify(ok=True)

                ids = list_members_ids(chat_id)
                if not ids:
                    send_message(chat_id, "אין חברים ב-DB לתייג.")
                    return jsonify(ok=True)

                total_sent = 0
                batch = []
                for uid in ids:
                    batch.append(f"[.](tg://user?id={uid})")
                    if len(batch) >= MENTION_CHUNK:
                        send_message(chat_id, " ".join(batch), parse_mode="Markdown")
                        total_sent += len(batch)
                        batch = []
                        time.sleep(MENTION_DELAY)
                if batch:
                    send_message(chat_id, " ".join(batch), parse_mode="Markdown")
                    total_sent += len(batch)

                send_message(chat_id, f"בוצע תיוג נקודות ל-{total_sent} משתמשים (ללא blacklist).")
                return jsonify(ok=True)

        return jsonify(ok=True)

    # ---- chat_member (join/leave/kick/promote) ----
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
