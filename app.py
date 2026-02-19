import os, json, time, threading, requests
from flask import Flask, request, jsonify
from redis import Redis, ConnectionError, TimeoutError

# ===== קונפיג בסיסי =====
TOKEN = os.getenv("TOKEN")  # Render → Environment: TOKEN=123456:ABC...
if not TOKEN or ":" not in TOKEN:
    raise RuntimeError("Missing/invalid TOKEN env var. Set TOKEN in Render → Environment.")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "tg-webhook-123456")  # מומלץ להחליף למחרוזת אקראית ארוכה
API = f"https://api.telegram.org/bot{TOKEN}"

# ---- Owner (מנהל־על) ----
OWNER_ID = os.getenv("OWNER_ID")
if OWNER_ID:
    try:
        OWNER_ID = int(OWNER_ID)
    except ValueError:
        OWNER_ID = None

# ==== Redis (Upstash) ====
REDIS_URL = os.getenv("REDIS_URL")  # Render → Environment: REDIS_URL=rediss://...
if not REDIS_URL:
    raise RuntimeError("Missing REDIS_URL env var.")

def new_client():
    # לא להעביר ssl=True כשמשתמשים ב-rediss://
    return Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=5,  # התחברות
        socket_timeout=5,          # קריאה/כתיבה
        retry_on_timeout=True,
        health_check_interval=30,  # בדיקת חיות קבועה
    )

r = new_client()

def redis_call(fn, *, retries=2, default=None):
    """
    מריץ קריאת Redis עם עד 2 רטרייז. אם יש כשל חיבור/טיים־אאוט,
    נפתח קליינט חדש ומנסים שוב. מחזיר default אם לא הצליח.
    """
    last_exc = None
    for i in range(retries + 1):
        try:
            return fn()
        except (ConnectionError, TimeoutError) as e:
            last_exc = e
            try:
                globals()['r'] = new_client()
            except Exception as e2:
                last_exc = e2
            time.sleep(0.2 * (i + 1))
        except Exception as e:
            print("❌ Redis op failed:", repr(e))
            return default
    print("❌ Redis connection failed:", repr(last_exc))
    return default

# דיאגנוסטיקה בהעלאה
try:
    redis_call(lambda: r.ping())
    print("✅ Redis connected")
except Exception as e:
    print("❌ Redis ping failed at startup:", e)

# תצורת שליחה של /dotall
MENTION_CHUNK = int(os.getenv("MENTION_CHUNK", "100"))
MENTION_DELAY = float(os.getenv("MENTION_DELAY", "0.15"))

HELP_TEXT = (
    "👋 Hi! I'm a group management bot.\n\n"
    "Group commands:\n"
    "• /count — Show how many users are stored (excludes blacklist).\n"
    "• /export — (Admins) Print a preview of stored users.\n"
    "• /bl_add <id> — (Admins) Add a user to the blacklist. Can also reply to user's message.\n"
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

# ---------- Redis keys helpers ----------
def _k_members(chat_id: int) -> str:   return f"chat:{chat_id}:members"     # Hash: uid -> JSON
def _k_blacklist(chat_id: int) -> str: return f"chat:{chat_id}:blacklist"   # Hash: uid -> JSON
def _k_settings(chat_id: int) -> str:  return f"chat:{chat_id}:settings"    # Hash: key -> str

# ---------- Bot API עזר ----------
def send_message(
    chat_id: int,
    text: str,
    reply_markup: dict | None = None,
    parse_mode: str | None = None,
    reply_to_message_id: int | None = None,
    disable_web_page_preview: bool = True
):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup: payload["reply_markup"] = reply_markup
    if parse_mode: payload["parse_mode"] = parse_mode
    if reply_to_message_id: payload["reply_to_message_id"] = reply_to_message_id
    if disable_web_page_preview: payload["disable_web_page_preview"] = True
    rsp = requests.post(f"{API}/sendMessage", json=payload, timeout=20)
    if not rsp.ok:
        print("send_message fail:", rsp.status_code, rsp.text)

def is_admin(chat_id: int, user_id: int) -> bool:
    # הבעלים נחשב תמיד כאדמין
    if OWNER_ID and user_id == OWNER_ID:
        return True
    try:
        rsp = requests.get(f"{API}/getChatMember",
                           params={"chat_id": chat_id, "user_id": user_id},
                           timeout=10)
        if rsp.ok:
            status = rsp.json().get("result", {}).get("status")
            return status in {"creator", "administrator"}
    except Exception as e:
        print("is_admin error:", e)
    return False

def resolve_target_user(msg, arg: str | None):
    reply = msg.get("reply_to_message")
    if reply and reply.get("from", {}).get("id"):
        u = reply["from"]
        return {"id": u["id"], "first_name": u.get("first_name"), "last_name": u.get("last_name"), "username": u.get("username")}
    if arg:
        arg = arg.strip()
        if arg.isdigit():
            return {"id": int(arg)}
    return None

# ---------- DB על Redis (עם סינון בוטים) ----------
def is_blacklisted(chat_id: int, user_id: int) -> bool:
    return bool(redis_call(lambda: r.hexists(_k_blacklist(chat_id), str(user_id)), default=False))

def add_user(chat_id: int, user: dict):
    if not user or "id" not in user:
        return
    if user.get("is_bot"):  # ✅ לא שומרים בוטים
        return
    if is_blacklisted(chat_id, user["id"]):
        return
    uid = str(user["id"])
    slim = {
        "id": user["id"],
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "username": user.get("username"),
        "added_at": int(time.time())
    }
    redis_call(lambda: r.hset(_k_members(chat_id), uid, json.dumps(slim, ensure_ascii=False)))

def remove_user(chat_id: int, user_id: int):
    redis_call(lambda: r.hdel(_k_members(chat_id), str(user_id)))

def blacklist_add(chat_id: int, user: dict) -> bool:
    if not user or "id" not in user:
        return False
    uid = str(user["id"])
    slim = {
        "id": user["id"],
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "username": user.get("username"),
        "added_at": int(time.time())
    }
    def _tx():
        pipe = r.pipeline()
        pipe.hset(_k_blacklist(chat_id), uid, json.dumps(slim, ensure_ascii=False))
        pipe.hdel(_k_members(chat_id), uid)
        pipe.execute()
        return True
    return bool(redis_call(_tx, default=False))

def blacklist_remove(chat_id: int, user_id: int) -> bool:
    return bool(redis_call(lambda: r.hdel(_k_blacklist(chat_id), str(user_id)) == 1, default=False))

def list_members_ids(chat_id: int):
    ids = redis_call(lambda: r.hkeys(_k_members(chat_id)), default=[])
    return [int(x) for x in ids]

def count_users(chat_id: int) -> int:
    return int(redis_call(lambda: r.hlen(_k_members(chat_id)), default=0) or 0)

def export_users(chat_id: int):
    vals = redis_call(lambda: r.hvals(_k_members(chat_id)), default=[])
    out = []
    for v in vals:
        try:
            out.append(json.loads(v))
        except Exception:
            pass
    return out

def list_blacklist(chat_id: int):
    vals = redis_call(lambda: r.hvals(_k_blacklist(chat_id)), default=[])
    out = []
    for v in vals:
        try:
            out.append(json.loads(v))
        except Exception:
            pass
    return out

def get_setting(chat_id: int, key: str, default=None):
    v = redis_call(lambda: r.hget(_k_settings(chat_id), key), default=None)
    if v is None:
        return default
    if v in ("1","0"):
        return v == "1"
    return v

def set_setting(chat_id: int, key: str, value):
    if isinstance(value, bool):
        value = "1" if value else "0"
    redis_call(lambda: r.hset(_k_settings(chat_id), key, str(value)))

# ---------- /dotall ברקע (ללא תיוג בוטים) ----------
def run_dotall(chat_id: int, ids: list[int]):
    total_sent = 0
    batch = []

    # ניקוי בוטים שעשויים להיות ב-DB (ירושה היסטורית)
    users_data = export_users(chat_id)
    bots_ids = {
        u["id"] for u in users_data
        if ((u.get("username") or "").lower().endswith("bot"))
    }

    for uid in ids:
        if uid in bots_ids:      # ✅ לא מתייג בוטים
            continue
        batch.append(f"[.](tg://user?id={uid})")
        if len(batch) >= MENTION_CHUNK:
            send_message(chat_id, " ".join(batch), parse_mode="Markdown")
            total_sent += len(batch)
            batch = []
            time.sleep(MENTION_DELAY)

    if batch:
        send_message(chat_id, " ".join(batch), parse_mode="Markdown")
        total_sent += len(batch)

# ---------- Flask routes ----------
@app.route("/")
def index():
    return "OK - Group Manager Bot (Redis)!", 200

@app.route(f"/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    try:
        update = request.get_json(silent=True) or {}

        # ---- message / edited_message ----
        msg = update.get("message") or update.get("edited_message")
        if msg:
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            chat_type = chat.get("type")
            from_user = msg.get("from", {})
            text = (msg.get("text") or "").strip()

            # פרטי: /start /help
            if chat_type == "private":
                if text.startswith("/start") or text.startswith("/help"):
                    send_message(chat_id, HELP_TEXT)
                else:
                    send_message(chat_id, "היי! כתוב /start כדי לראות את כל הפקודות הזמינות.")
                return jsonify(ok=True)

            # קבוצה: תחזוקת DB (לא לשמור בוטים)
            if chat_type in {"group", "supergroup"} and from_user.get("id"):
                if not from_user.get("is_bot"):
                    if not is_blacklisted(chat_id, from_user["id"]):
                        add_user(chat_id, from_user)

            new_members = msg.get("new_chat_members") or []
            for m in new_members:
                if not (m.get("is_bot") or is_blacklisted(chat_id, m.get("id", 0))):
                    add_user(chat_id, m)

            left = msg.get("left_chat_member")
            if left and left.get("id"):
                remove_user(chat_id, left["id"])

            # ===== פקודות בקבוצה – פרסינג נכון =====
            if chat_type in {"group", "supergroup"} and text:
                lower = text.strip().lower()

                # חילוץ הפקודה כפי שטלגרם מסמן ב-entities
                entities = msg.get("entities") or []
                cmd = None
                for e in entities:
                    if e.get("type") == "bot_command":
                        off = e.get("offset", 0)
                        ln  = e.get("length", 0)
                        cmd = (text[off:off+ln] or "").lower()
                        break
                if cmd:
                    # מסירים @username אם נוסף (/count@mybot)
                    cmd = cmd.split("@", 1)[0]

                def is_cmd(name: str) -> bool:
                    if cmd:
                        return cmd == f"/{name}"
                    # fallback אם משום מה אין entities
                    return lower.startswith(f"/{name}")

                # ---- פקודות ----
                if is_cmd("whoami"):
                    uid = from_user.get("id")
                    isown = "✅" if (OWNER_ID and uid == OWNER_ID) else "❌"
                    send_message(chat_id, f"ID: {uid}\nOwner: {isown}")
                    return jsonify(ok=True)

                if is_cmd("count"):
                    send_message(chat_id, f"{count_users(chat_id)}")
                    return jsonify(ok=True)

                if is_cmd("export"):
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
                    
                
                if is_cmd("bl_add") or is_cmd("blacklist_add"):
                    if not is_admin(chat_id, from_user.get("id", 0)):
                        send_message(chat_id, "רק מנהלים יכולים להשתמש ב-/bl_add.")
                        return jsonify(ok=True)
                    arg = text.split(maxsplit=1)[1] if " " in text else None
                    target = resolve_target_user(msg, arg)
                    if not target:
                        #send_message(chat_id, "שימוש: השב על הודעת המשתמש, או /bl_add <user_id>")
                        return jsonify(ok=True)
                    if blacklist_add(chat_id, target):
                        send_message(chat_id, f" המשתמש לא יתוייג יותר")
                    else:
                        send_message(chat_id, "לא הצלחתי להוסיף ל-blacklist.")
                    return jsonify(ok=True)

                if is_cmd("bl_remove") or is_cmd("blacklist_remove"):
                    if not is_admin(chat_id, from_user.get("id", 0)):
                        pass
                        #send_message(chat_id, "רק מנהלים יכולים להשתמש ב-/bl_remove.")
                        return jsonify(ok=True)
                    arg = text.split(maxsplit=1)[1] if " " in text else None
                    target = resolve_target_user(msg, arg)
                    if not target:
                        send_message(chat_id, "שימוש: השב על הודעת המשתמש, או /bl_remove <user_id>")
                        return jsonify(ok=True)
                    if blacklist_remove(chat_id, target["id"]):
                        send_message(chat_id, f"הוסר")
                    else:
                        pass
                        #send_message(chat_id, "לא נמצא ב-blacklist.")
                    return jsonify(ok=True)

                if is_cmd("bl_list"):
                    if not is_admin(chat_id, from_user.get("id", 0)):
                        pass
                        #send_message(chat_id, "רק מנהלים יכולים להשתמש ב-/bl_list.")
                        return jsonify(ok=True)
                    bl = list_blacklist(chat_id)
                    if not bl:
                        pass
                        #send_message(chat_id, "ה-blacklist ריק.")
                        return jsonify(ok=True)
                    lines = []
                    for u in bl[:200]:
                        name = (u.get("first_name") or "") + (" " + u.get("last_name") if u.get("last_name") else "")
                        name = name.strip() or (u.get("username") or "unknown")
                        lines.append(f"{name} — {u['id']}")
                    send_message(chat_id, "Blacklist (ראשונים):\n" + "\n".join(lines))
                    return jsonify(ok=True)

                if is_cmd("all_users"):
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

                if is_cmd("dotall"):
                    uid = from_user.get("id", 0)
                    if  uid == 919782824:
                        send_message(chat_id, "אני לא עובד אצלך מותק")
                        return jsonify(ok=True)
                    send_message(chat_id, "בשבילך עד חצי המלכות, רק הארלי המכה שתלך לרקוד מקרנה")
                    allow_all = bool(get_setting(chat_id, "dotall_anyone", False))
                    if not (is_admin(chat_id, from_user.get("id", 0)) or allow_all):
                        send_message(chat_id, "הפקודה /dotall זמינה למנהלים בלבד. ניתן לשנות עם /all_users on")
                        return jsonify(ok=True)
                    ids = list_members_ids(chat_id)
                    if not ids:
                        send_message(chat_id, "אין חברים ב-DB לתייג.")
                        return jsonify(ok=True)
                    threading.Thread(target=run_dotall, args=(chat_id, ids), daemon=True).start()
                    return jsonify(ok=True)

            return jsonify(ok=True)

        # ---- chat_member (join/leave/kick/promote) ----
        chat_member_update = update.get("chat_member") or update.get("my_chat_member")
        if chat_member_update:
            chat = chat_member_update.get("chat", {})
            chat_id = chat.get("id")
            old = chat_member_update.get("old_chat_member", {})
            new = chat_member_update.get("new_chat_member", {})
            user = new.get("user") or old.get("user") or {}
            new_status = (new.get("status") or "").lower()

            if user.get("id"):
                if new_status in {"member", "administrator", "creator"}:
                    if not user.get("is_bot"):
                        if not is_blacklisted(chat_id, user["id"]):
                            add_user(chat_id, user)
                elif new_status in {"left", "kicked", "restricted"}:
                    remove_user(chat_id, user["id"])

            return jsonify(ok=True)

        return jsonify(ok=True)
    except Exception as e:
        # מחזירים 200 כדי לא לצבור pending ב-Telegram
        print("❌ webhook error:", repr(e))
        return jsonify(ok=True)

# ----- רישום/מחיקת webhook -----
@app.route("/setwebhook")
def set_webhook():
    base = request.url_root.replace("http://", "https://")
    if not base.endswith("/"): base += "/"
    url = f"{base}{WEBHOOK_SECRET}"
    rsp = requests.get(
        f"{API}/setWebhook",
        params={
            "url": url,
            "allowed_updates": json.dumps(["message","edited_message","chat_member","my_chat_member"])
        },
        timeout=10
    )
    return rsp.text, rsp.status_code, {"Content-Type": "application/json"}

@app.route("/deletewebhook")
def delete_webhook():
    rsp = requests.get(f"{API}/deleteWebhook", timeout=10)
    return rsp.text, rsp.status_code, {"Content-Type": "application/json"}

# להרצה מקומית (לא חובה ב-Render)
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
