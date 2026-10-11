#!/usr/bin/env python3
"""
Telegram admin bot + website + backend  (ONE process, Telegram POLLING)

Needs an always-on host (Render, Railway, VPS ...). Vercel cannot run polling.

Install:  pip install -U python-telegram-bot firebase-admin aiohttp
Run:      python bot.py

Environment variables (or paste the values in CONFIG below):
  BOT_TOKEN     token from @BotFather
  ADMIN_ID      your numeric Telegram user id (ask @userinfobot)
  KEY_64        64-char random secret: python -c "import secrets;print(secrets.token_hex(32))"
                (signs web sessions + salts PINs. Do NOT change it later, PINs would stop working)
  FIREBASE_B64  base64 of the Firebase *service account* JSON  (base64 -w0 serviceAccount.json)
  PORT          set automatically by Render/Railway, default 8080

What it does
  Telegram (admin):  /start -> Link Add | Domain | Request | Status | Start Msg   (every step has Back)
  Telegram (others): /start -> shows the custom message the admin set under "Start Msg"
  Website:           GET /  serves web/index.html from this same server, so it needs no approval.
                     A page hosted on ANOTHER domain must be approved by the admin first (Request).
  Web API:           POST /api/register {pin}        -> user creates own PIN, gets uid + token
                     POST /api/auth     {uid, pin}   -> login with own PIN, gets token
                     POST /api/feed     {cat,n,exclude} -> random links (from RAM cache)

Firestore layout
  India_video / Japan_video / Russia_video / Cute_video / Random_video
      doc 000001, 000002 ...  fields: n, url, cat, title, added
  meta/<collection>   { count }
  domains/<id>        { host, added }
  requests/<id>       { host, status: pending|approved|rejected, last, count }
  users/<uid>         { salt, hash, created }      (PIN is stored only as a salted PBKDF2 hash)
  settings/start      { text }                     (message shown to normal users on /start)
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

import firebase_admin
from aiohttp import web
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ------------------------------------------------------------------ CONFIG
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_BOT_TOKEN_HERE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
KEY_64 = os.getenv("KEY_64", "PASTE_KEY_64_HERE")
FIREBASE_B64 = os.getenv("FIREBASE_B64", "PASTE_BASE64_OF_SERVICE_ACCOUNT_JSON_HERE")
PORT = int(os.getenv("PORT", "8080"))

# key = Firestore collection name. Order = order of the buttons in the bot.
CATEGORIES = {
    "Random_video": ("🎲", "Random"),
    "India_video": ("🇮🇳", "India"),
    "Japan_video": ("🇯🇵", "Japan"),
    "Russia_video": ("🇷🇺", "Russia"),
    "Cute_video": ("🥰", "Cute"),
}
ONLY_M3U8 = False  # True = accept only links that contain ".m3u8"
CHUNK = 400  # links written per transaction (Firestore limit is 500)
TOKEN_TTL = 12 * 3600  # web login lasts 12 h
PAGE = 10  # requests per page in the bot
# -------------------------------------------------------------------------

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
LEGACY_TITLE_RE = re.compile(r"^(Random|India|Japan|Russia|Cute) #\d+$")

db = None
ADD_LOCK: asyncio.Lock | None = None

# ---- RAM cache: the web never reads Firestore, it reads these ----
CACHE = {k: [] for k in CATEGORIES}  # list of {id,url,title,cat}
CACHE_URLS = {k: set() for k in CATEGORIES}
CACHE_MAXN = {k: 0 for k in CATEGORIES}
DOMAINS: set[str] = set()  # approved hosts
USERS: dict[str, dict] = {}  # uid -> {"salt","hash"}   (loaded on demand)
DEFAULT_START = "👋 Welcome!"
START_MSG = ""  # custom text for normal users on /start ("" = default)


def set_start_msg(text: str):
    global START_MSG
    START_MSG = text


# ------------------------------------------------------------------ helpers
def norm_host(s: str) -> str:
    """'https://My_Site.app/x' -> 'my_site.app'. Empty string if invalid."""
    s = (s or "").strip().lower()
    if not s or s == "null":
        return ""
    if "://" not in s:
        s = "https://" + s
    host = urlparse(s).netloc.split("@")[-1]
    return host if re.fullmatch(r"[a-z0-9._\-]+(:\d+)?", host) else ""


def hid_of(host: str) -> str:
    return hashlib.sha1(host.encode()).hexdigest()[:12]


def clean_title(t) -> str:
    t = (t or "").strip()
    return "" if LEGACY_TITLE_RE.match(t) else t[:120]


def parse_entries(text: str) -> list[tuple[str, str]]:
    """
    Reads a text like
        mandal project 1
        https://....m3u8
        Mandal project 2
        https://....m3u8
    and returns [(url, title), ...]. A line with only a link gets an empty title.
    "Title https://link" on one line also works.
    """
    out, seen, pending = [], set(), ""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        urls = URL_RE.findall(line)
        if not urls:
            pending = line
            continue
        before = line[: line.find(urls[0])].strip(" -:|\t")
        title = (before or pending)[:120]
        for i, raw in enumerate(urls):
            url = raw.rstrip(").,;]}>")
            if ONLY_M3U8 and ".m3u8" not in url.lower():
                continue
            if url not in seen:
                seen.add(url)
                out.append((url, title if i == 0 else ""))
        pending = ""
    return out


# ------------------------------------------------------------------ Firestore (blocking; call with to_thread)
def load_all():
    for cat in CATEGORIES:
        items, urls, maxn = [], set(), 0
        for d in db.collection(cat).stream():
            x = d.to_dict() or {}
            url, n = x.get("url"), int(x.get("n", 0))
            if not url:
                continue
            items.append({"id": f"{cat}:{n}", "url": url, "title": clean_title(x.get("title")), "cat": cat})
            urls.add(url)
            maxn = max(maxn, n)
        CACHE[cat], CACHE_URLS[cat], CACHE_MAXN[cat] = items, urls, maxn
        log.info("cache %s: %d links", cat, len(items))
    DOMAINS.clear()
    for d in db.collection("domains").stream():
        h = (d.to_dict() or {}).get("host")
        if h:
            DOMAINS.add(h)
    log.info("approved domains: %d", len(DOMAINS))
    snap = db.collection("settings").document("start").get()
    set_start_msg((snap.to_dict() or {}).get("text", "") if snap.exists else "")


@firestore.transactional
def _write_chunk(tx, cat, meta_ref, base, chunk):
    snap = meta_ref.get(transaction=tx)
    count = (snap.to_dict() or {}).get("count", 0) if snap.exists else 0
    start = max(count, base)  # never reuse a number
    col = db.collection(cat)
    label = CATEGORIES[cat][1]
    for k, (url, title) in enumerate(chunk, 1):
        n = start + k
        tx.set(
            col.document(f"{n:06d}"),
            {"n": n, "url": url, "cat": cat, "title": title, "label": label, "added": firestore.SERVER_TIMESTAMP},
        )
    tx.set(meta_ref, {"count": start + len(chunk)}, merge=True)
    return start + 1, start + len(chunk)


def store_links(cat: str, entries: list[tuple[str, str]]) -> dict:
    known = CACHE_URLS[cat]  # duplicate check uses the cache, no Firestore reads
    new = [(u, t) for u, t in entries if u not in known]
    base = CACHE_MAXN[cat]
    meta_ref = db.collection("meta").document(cat)
    added, first, last = [], None, None
    for i in range(0, len(new), CHUNK):
        chunk = new[i : i + CHUNK]
        a, b = _write_chunk(db.transaction(), cat, meta_ref, base, chunk)
        for k, (u, t) in enumerate(chunk):
            added.append({"id": f"{cat}:{a + k}", "url": u, "title": t, "cat": cat})
        base, first, last = b, first or a, b
    return {"items": added, "dup": len(entries) - len(new), "first": first, "last": last}


def fs_add_domain(host):
    h = hid_of(host)
    db.collection("domains").document(h).set({"host": host, "added": firestore.SERVER_TIMESTAMP})
    db.collection("requests").document(h).set(
        {"host": host, "status": "approved", "last": firestore.SERVER_TIMESTAMP}, merge=True
    )


def fs_remove_domain(host, status="rejected"):
    h = hid_of(host)
    db.collection("domains").document(h).delete()
    db.collection("requests").document(h).set(
        {"host": host, "status": status, "last": firestore.SERVER_TIMESTAMP}, merge=True
    )


def fs_get_host(hid):
    snap = db.collection("requests").document(hid).get()
    return (snap.to_dict() or {}).get("host") if snap.exists else None


def fs_list_requests(page):
    q = (
        db.collection("requests")
        .order_by("last", direction=firestore.Query.DESCENDING)
        .offset(page * PAGE)
        .limit(PAGE + 1)
    )
    rows = [{"hid": d.id, **(d.to_dict() or {})} for d in q.stream()]
    return rows[:PAGE], len(rows) > PAGE


def fs_pending_count():
    q = db.collection("requests").where(filter=FieldFilter("status", "==", "pending"))
    return sum(1 for _ in q.stream())


def fs_save_start(text):
    db.collection("settings").document("start").set({"text": text})


def fs_get_user(uid):
    snap = db.collection("users").document(uid).get()
    return snap.to_dict() if snap.exists else None


def fs_create_user(uid, salt, h):
    db.collection("users").document(uid).set({"salt": salt, "hash": h, "created": firestore.SERVER_TIMESTAMP})


def fs_count_users():
    try:
        return db.collection("users").count().get()[0][0].value
    except Exception:
        return sum(1 for _ in db.collection("users").select([]).stream())


def fs_note_request(host):
    """Called when an unapproved domain hits the API. Returns True if it is a brand-new request."""
    ref = db.collection("requests").document(hid_of(host))
    if ref.get().exists:
        ref.update({"last": firestore.SERVER_TIMESTAMP, "count": firestore.Increment(1)})
        return False
    ref.set({"host": host, "status": "pending", "last": firestore.SERVER_TIMESTAMP, "count": 1})
    return True


# ------------------------------------------------------------------ web API
NOTED: dict[str, float] = {}  # host -> last time we wrote a request for it
NEW_WRITES: list[float] = []  # global cap against spam of fake origins
FAILS: dict[str, list[float]] = {}  # "ip:x" / "uid:x" -> failed PIN times
REG: dict[str, list[float]] = {}  # ip -> account creations
WEB_FILE = Path(__file__).resolve().parent / "web" / "index.html"


def cors_headers(origin):
    return {
        "Access-Control-Allow-Origin": origin or "*",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


@web.middleware
async def cors_mw(request, handler):
    origin = request.headers.get("Origin", "")
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers(origin))
    try:
        resp = await handler(request)
    except web.HTTPException as ex:
        ex.headers.update(cors_headers(origin))
        raise
    resp.headers.update(cors_headers(origin))
    return resp


def jerr(status, code):
    return web.json_response({"ok": False, "error": code}, status=status)


async def read_json(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def client_ip(request):
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or request.remote or "?"


def make_token(host, uid):
    payload = f"{host}|{uid}|{int(time.time()) + TOKEN_TTL}"
    sig = hmac.new(KEY_64.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + sig


def check_token(token, host):
    """Returns the uid when the token is valid for this host, else None."""
    try:
        p, sig = token.split(".")
        payload = base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)).decode()
        good = hmac.new(KEY_64.encode(), payload.encode(), hashlib.sha256).hexdigest()
        t_host, uid, exp = payload.split("|")
        if hmac.compare_digest(sig, good) and t_host == host and int(exp) > time.time():
            return uid
    except Exception:
        pass
    return None


def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", (pin + KEY_64).encode(), bytes.fromhex(salt), 100_000).hex()


def too_many(key, limit=5, window=300):
    now = time.time()
    FAILS[key] = [t for t in FAILS.get(key, []) if now - t < window]
    return len(FAILS[key]) >= limit


def own_host(request):
    raw = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or ""
    return norm_host(raw.split(",")[0])


async def register_request(tg, host):
    try:
        is_new = await asyncio.to_thread(fs_note_request, host)
    except Exception:
        log.exception("could not log request")
        return
    if is_new:
        try:
            await tg.bot.send_message(
                ADMIN_ID,
                f"🌐 New domain request\n\n{host}",
                reply_markup=InlineKeyboardMarkup([[B("📨 Open requests", "rq:0")]]),
            )
        except Exception:
            log.exception("could not notify admin")


async def gate(request):
    """Returns (host, None) if the caller may use the API, else (None, error response).
    Allowed: this server's own website, or a domain the admin approved."""
    own = own_host(request)
    host = norm_host(request.headers.get("Origin", ""))
    if not host and request.headers.get("Sec-Fetch-Site") == "same-origin":
        host = own
    if not host:
        return None, jerr(403, "no_origin")
    if host == own or host in DOMAINS:
        return host, None
    now = time.time()
    NEW_WRITES[:] = [t for t in NEW_WRITES if now - t < 3600]
    if now - NOTED.get(host, 0) > 600 and len(NEW_WRITES) < 30:
        NOTED[host] = now
        NEW_WRITES.append(now)
        task = asyncio.create_task(register_request(request.app["tg"], host))
        request.app["tasks"].add(task)
        task.add_done_callback(request.app["tasks"].discard)
    return None, jerr(403, "domain_pending")


async def api_register(request):
    """A new user chooses their own 4-digit PIN."""
    host, err = await gate(request)
    if err:
        return err
    ip, now = client_ip(request), time.time()
    REG[ip] = [t for t in REG.get(ip, []) if now - t < 3600]
    if len(REG[ip]) >= 10:
        return jerr(429, "rate_limited")
    pin = str((await read_json(request)).get("pin", ""))
    if not re.fullmatch(r"\d{4}", pin):
        return jerr(400, "bad_pin_format")
    REG[ip].append(now)
    uid, salt = secrets.token_hex(16), secrets.token_hex(16)
    h = await asyncio.to_thread(hash_pin, pin, salt)
    await asyncio.to_thread(fs_create_user, uid, salt, h)
    USERS[uid] = {"salt": salt, "hash": h}
    return web.json_response({"ok": True, "uid": uid, "token": make_token(host, uid)})


async def api_auth(request):
    """Login with the PIN the user created earlier (uid is kept in their browser)."""
    host, err = await gate(request)
    if err:
        return err
    ip = client_ip(request)
    data = await read_json(request)
    uid, pin = str(data.get("uid", "")), str(data.get("pin", ""))
    if too_many("ip:" + ip, limit=10) or too_many("uid:" + uid):
        return jerr(429, "rate_limited")
    user = USERS.get(uid) if re.fullmatch(r"[0-9a-f]{32}", uid) else None
    if not user and re.fullmatch(r"[0-9a-f]{32}", uid):
        user = await asyncio.to_thread(fs_get_user, uid)
        if user:
            USERS[uid] = user
    if not user:
        FAILS.setdefault("ip:" + ip, []).append(time.time())
        return jerr(404, "unknown_user")
    h = await asyncio.to_thread(hash_pin, pin, user["salt"])
    if not hmac.compare_digest(h, user["hash"]):
        FAILS.setdefault("uid:" + uid, []).append(time.time())
        FAILS.setdefault("ip:" + ip, []).append(time.time())
        return jerr(401, "bad_pin")
    return web.json_response({"ok": True, "token": make_token(host, uid)})


def pick(pools, n, exclude):
    """n random items, never the ones in `exclude`. When everything was seen, start over."""
    total = sum(len(p) for p in pools)
    if not total:
        return [], False

    def at(i):
        for p in pools:
            if i < len(p):
                return p[i]
            i -= len(p)

    out, ids, tries = [], set(), 0
    while len(out) < n and tries < n * 30:
        tries += 1
        x = at(random.randrange(total))
        if x["id"] in exclude or x["id"] in ids:
            continue
        ids.add(x["id"])
        out.append(x)
    if len(out) < n:  # most links already seen -> exact pass
        rest = [x for p in pools for x in p if x["id"] not in exclude and x["id"] not in ids]
        random.shuffle(rest)
        out += rest[: n - len(out)]
    if out:
        return out, False
    every = [x for p in pools for x in p]
    random.shuffle(every)
    return every[:n], True


async def api_feed(request):
    host, err = await gate(request)
    if err:
        return err
    auth = request.headers.get("Authorization", "")
    if not (auth.startswith("Bearer ") and check_token(auth[7:], host)):
        return jerr(401, "bad_token")
    data = await read_json(request)
    key = str(data.get("cat", "random"))
    try:
        n = max(1, min(30, int(data.get("n", 10))))
    except (TypeError, ValueError):
        n = 10
    ex = data.get("exclude")
    exclude = {str(x) for x in ex[:500]} if isinstance(ex, list) else set()
    if key == "random":
        pools = [CACHE[c] for c in CATEGORIES]
    elif key in CACHE:
        pools = [CACHE[key]]
    else:
        return jerr(400, "bad_cat")
    items, reset = pick(pools, n, exclude)
    return web.json_response(
        {
            "ok": True,
            "reset": reset,
            "items": [{"id": x["id"], "url": x["url"], "title": x["title"], "cat": x["cat"]} for x in items],
        }
    )


async def index(request):
    if WEB_FILE.exists():
        return web.FileResponse(WEB_FILE, headers={"Cache-Control": "no-cache"})
    return web.Response(text="ok (web/index.html not found)")


async def health(request):
    return web.Response(text="ok")


# ------------------------------------------------------------------ bot UI
def B(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(rows)


HOME_TEXT = "👋 Admin panel\n\nKya karna hai? 👇"


def main_kb():
    return kb(
        [
            [B("➕ Link Add", "la"), B("🌐 Domain", "dm")],
            [B("📨 Request", "rq:0"), B("📊 Status", "st")],
            [B("💬 Start Msg", "sm")],
        ]
    )


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_ID)


async def show(update: Update, text: str, markup=None):
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


def cat_kb():
    keys = list(CATEGORIES)
    btn = [B(f"{CATEGORIES[k][0]} {CATEGORIES[k][1]}", f"lc:{k}") for k in keys] + [B("⬅️ Back", "m")]
    return kb([btn[i : i + 2] for i in range(0, len(btn), 2)])


def tag(cat):
    e, n = CATEGORIES[cat]
    return f"{e} {n}"


# ------------------------------------------------------------------ handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text(START_MSG or DEFAULT_START)  # normal user: admin's custom message
        return
    context.user_data.clear()
    await show(update, HOME_TEXT, main_kb())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("⛔ Admin only", show_alert=True)
        return
    d, ud = q.data, context.user_data
    parts = d.split(":")
    cmd = parts[0]
    await q.answer()

    # ---- home
    if cmd == "m":
        ud.clear()
        await show(update, HOME_TEXT, main_kb())
    elif cmd == "noop":
        pass

    # ---- link add: category -> method -> input
    elif cmd == "la":
        ud.clear()
        await show(update, "➕ Link Add\n\nKis category mein add karna hai? 👇", cat_kb())
    elif cmd == "lc":
        cat = parts[1]
        if cat not in CATEGORIES:
            return
        ud.clear()
        ud["cat"] = cat
        await show(
            update,
            f"{tag(cat)}\n\nKaise add karna hai?",
            kb([[B("📄 TXT file", "lm:txt"), B("🔗 URL", "lm:url")], [B("⬅️ Back", "la")]]),
        )
    elif cmd == "lm":
        cat = ud.get("cat")
        if not cat:
            await show(update, HOME_TEXT, main_kb())
            return
        ud["mode"] = parts[1]
        if parts[1] == "txt":
            msg = (
                f"{tag(cat)} · 📄 TXT\n\n.txt file bhejo.\nFormat:\n\n"
                "mandal project 1\nhttps://site.com/a.m3u8\nmandal project 2\nhttps://site.com/b.m3u8\n\n"
                "(title line optional hai)"
            )
        else:
            msg = f"{tag(cat)} · 🔗 URL\n\nLink bhejo (ek ya zyada).\nTitle chahiye to link se pehle line mein likho."
        await show(update, msg, kb([[B("⬅️ Back", f"lc:{cat}")]]))

    # ---- domains
    elif cmd == "dm":
        ud.clear()
        hosts = sorted(DOMAINS)
        text = f"🌐 Approved domains ({len(hosts)})\n\n" + ("\n".join(f"• {h}" for h in hosts) or "Abhi koi domain nahi.")
        rows = [[B(f"🗑 {h[:34]}", f"dd:{hid_of(h)}")] for h in hosts]
        rows.append([B("➕ Add domain", "da")])
        rows.append([B("⬅️ Back", "m")])
        await show(update, text, kb(rows))
    elif cmd == "da":
        ud.clear()
        ud["mode"] = "domain"
        await show(
            update,
            "➕ Domain bhejo\n\nExample:\nhttps://my_project_vercel.app\n\nSirf isi domain ki web ko data milega.",
            kb([[B("⬅️ Back", "dm")]]),
        )
    elif cmd == "dd":
        host = next((h for h in DOMAINS if hid_of(h) == parts[1]), None)
        if not host:
            await show(update, "Domain nahi mila.", kb([[B("⬅️ Back", "dm")]]))
            return
        await show(
            update,
            f"🗑 Remove karna hai?\n\n{host}\n\nIske baad is web ko data nahi milega.",
            kb([[B("✅ Haan, remove", f"dx:{parts[1]}"), B("⬅️ Nahi", "dm")]]),
        )
    elif cmd == "dx":
        host = next((h for h in DOMAINS if hid_of(h) == parts[1]), None)
        if host:
            DOMAINS.discard(host)
            await asyncio.to_thread(fs_remove_domain, host)
        await show(update, f"🗑 Removed: {host or '-'}", kb([[B("⬅️ Back", "dm"), B("🏠 Menu", "m")]]))

    # ---- requests (last 10 per page)
    elif cmd == "rq":
        ud.clear()
        await render_requests(update, int(parts[1]))
    elif cmd == "ra1":
        host = await asyncio.to_thread(fs_get_host, parts[1])
        p = parts[2]
        await show(
            update,
            f"✅ Approve karna hai? (1/2)\n\n{host}",
            kb([[B("✅ Haan", f"ra2:{parts[1]}:{p}"), B("⬅️ Back", f"rq:{p}")]]),
        )
    elif cmd == "ra2":
        host = await asyncio.to_thread(fs_get_host, parts[1])
        p = parts[2]
        await show(
            update,
            f"⚠️ Final confirm (2/2)\n\n{host}\n\nApprove hote hi is domain ki web ko data milne lagega.",
            kb([[B("✅ Confirm approve", f"ra3:{parts[1]}:{p}"), B("❌ Cancel", f"rq:{p}")]]),
        )
    elif cmd == "ra3":
        host = await asyncio.to_thread(fs_get_host, parts[1])
        if host:
            await asyncio.to_thread(fs_add_domain, host)
            DOMAINS.add(host)
        await render_requests(update, int(parts[2]))
    elif cmd == "rr":
        host = await asyncio.to_thread(fs_get_host, parts[1])
        if host:
            DOMAINS.discard(host)
            await asyncio.to_thread(fs_remove_domain, host)
        await render_requests(update, int(parts[2]))

    # ---- start message for normal users
    elif cmd == "sm":
        ud.clear()
        await show_start_msg(update)
    elif cmd == "sme":
        ud.clear()
        ud["mode"] = "smsg"
        await show(
            update,
            "✏️ Naya message bhejo (text).\n\nNormal users ko /start par yahi dikhega.",
            kb([[B("⬅️ Back", "sm")]]),
        )
    elif cmd == "smp":
        await q.message.reply_text(START_MSG or DEFAULT_START)  # exactly what users see
    elif cmd == "smr":
        await asyncio.to_thread(fs_save_start, "")
        set_start_msg("")
        await show_start_msg(update)

    # ---- status
    elif cmd == "st":
        ud.clear()
        lines, total = [], 0
        for cat in CATEGORIES:
            c = len(CACHE[cat])
            total += c
            lines.append(f"{tag(cat)}: {c}")
        pending = await asyncio.to_thread(fs_pending_count)
        users = await asyncio.to_thread(fs_count_users)
        text = (
            "📊 Status\n\n" + "\n".join(lines) + f"\n\n🔗 Total links: {total}\n"
            f"🌐 Approved domains: {len(DOMAINS)}\n⏳ Pending requests: {pending}\n👤 Web users (own PIN): {users}"
        )
        await show(update, text, kb([[B("🔄 Refresh", "st")], [B("⬅️ Back", "m")]]))


async def show_start_msg(update: Update):
    cur = START_MSG or f"(default)\n{DEFAULT_START}"
    await show(
        update,
        "💬 Start message\n\nNormal user /start dabata hai to ye dikhta hai:\n\n" + cur,
        kb([[B("✏️ Set / Edit", "sme")], [B("👁 Preview", "smp"), B("🗑 Reset", "smr")], [B("⬅️ Back", "m")]]),
    )


async def render_requests(update: Update, page: int):
    rows, has_next = await asyncio.to_thread(fs_list_requests, page)
    icon = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
    lines, buttons = [], []
    for i, r in enumerate(rows, 1 + page * PAGE):
        st, host, hid = r.get("status", "pending"), r.get("host", "?"), r["hid"]
        lines.append(f"{i}. {icon.get(st, '•')} {host} · {st} · {r.get('count', 1)} hit")
        short = host[:24]
        row = []
        if st != "approved":
            row.append(B(f"✅ {short}", f"ra1:{hid}:{page}"))
        if st != "rejected":
            row.append(B(f"❌ {short}" if st == "approved" else "❌", f"rr:{hid}:{page}"))
        buttons.append(row)
    nav = []
    if page > 0:
        nav.append(B("◀️ Prev", f"rq:{page - 1}"))
    if has_next:
        nav.append(B("Next ▶️", f"rq:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([B("⬅️ Back", "m")])
    text = "📨 Domain requests (latest first)\n\n" + ("\n".join(lines) or "Abhi koi request nahi aayi.")
    await show(update, text, kb(buttons))


async def ingest(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    cat = context.user_data.get("cat")
    nav = kb([[B("⬅️ Back", f"lc:{cat}"), B("🏠 Menu", "m")]]) if cat else main_kb()
    entries = parse_entries(text)
    if not entries:
        await update.message.reply_text("❌ Koi valid link nahi mila.", reply_markup=nav)
        return
    status = await update.message.reply_text(f"⏳ {len(entries)} link save ho rahe hain…")
    try:
        async with ADD_LOCK:
            r = await asyncio.to_thread(store_links, cat, entries)
            for it in r["items"]:  # keep the RAM cache in sync, no re-read needed
                CACHE[cat].append(it)
                CACHE_URLS[cat].add(it["url"])
            if r["last"]:
                CACHE_MAXN[cat] = max(CACHE_MAXN[cat], r["last"])
    except Exception as e:
        log.exception("Firestore write failed")
        await status.edit_text(f"⚠️ Firebase error:\n{e}", reply_markup=nav)
        return
    titled = sum(1 for it in r["items"] if it["title"])
    lines = [tag(cat), f"✅ Added: {len(r['items'])}"]
    if r["items"]:
        lines.append(f"🔢 Numbers: #{r['first']} – #{r['last']}")
        lines.append(f"🏷 Title ke saath: {titled}")
    if r["dup"]:
        lines.append(f"♻️ Duplicate skipped: {r['dup']}")
    lines.append("\nAur bhejo, ya Back dabao.")
    await status.edit_text("\n".join(lines), reply_markup=nav)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    ud = context.user_data
    mode = ud.get("mode")
    text = update.message.text
    if mode == "domain":
        host = norm_host(text.split()[0] if text.split() else "")
        nav = kb([[B("⬅️ Back", "dm"), B("🏠 Menu", "m")]])
        if not host:
            await update.message.reply_text("❌ Domain sahi nahi hai. Example: https://my_project_vercel.app", reply_markup=nav)
            return
        await asyncio.to_thread(fs_add_domain, host)
        DOMAINS.add(host)
        await update.message.reply_text(f"✅ Domain added:\n{host}", reply_markup=nav)
    elif mode == "smsg":
        text = text.strip()[:3500]
        await asyncio.to_thread(fs_save_start, text)
        set_start_msg(text)
        ud.pop("mode", None)
        await update.message.reply_text(
            "✅ Start message saved.\n\nNormal user ab /start karega to ye dikhega.",
            reply_markup=kb([[B("👁 Preview", "smp"), B("⬅️ Back", "sm")], [B("🏠 Menu", "m")]]),
        )
    elif mode in ("txt", "url") and ud.get("cat"):
        await ingest(update, context, text)
    else:
        await update.message.reply_text("👇 Pehle option chuno", reply_markup=main_kb())


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    ud = context.user_data
    if ud.get("mode") not in ("txt", "url") or not ud.get("cat"):
        await update.message.reply_text("Pehle Link Add > category > TXT file chuno 👇", reply_markup=main_kb())
        return
    doc = update.message.document
    name = (doc.file_name or "").lower()
    if not (name.endswith(".txt") or doc.mime_type == "text/plain"):
        await update.message.reply_text("📄 Sirf .txt file bhejo.")
        return
    try:
        data = await (await doc.get_file()).download_as_bytearray()
    except Exception as e:
        await update.message.reply_text(f"⚠️ File download nahi hui: {e}")
        return
    await ingest(update, context, bytes(data).decode("utf-8", errors="ignore"))


# ------------------------------------------------------------------ startup
async def post_init(app: Application):
    global ADD_LOCK
    ADD_LOCK = asyncio.Lock()
    await asyncio.to_thread(load_all)

    api = web.Application(middlewares=[cors_mw])
    api["tg"], api["tasks"] = app, set()
    api.add_routes(
        [
            web.get("/", index),
            web.get("/index.html", index),
            web.get("/health", health),
            web.post("/api/register", api_register),
            web.post("/api/auth", api_auth),
            web.post("/api/feed", api_feed),
        ]
    )
    runner = web.AppRunner(api)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    app.bot_data["runner"] = runner
    log.info("Web API listening on :%d", PORT)


async def post_shutdown(app: Application):
    runner = app.bot_data.get("runner")
    if runner:
        await runner.cleanup()


def main():
    global db
    for name, val in (("BOT_TOKEN", BOT_TOKEN), ("KEY_64", KEY_64), ("FIREBASE_B64", FIREBASE_B64)):
        if "PASTE_" in val:
            raise SystemExit(f"Set {name} (environment variable or at the top of bot.py).")
    cred = credentials.Certificate(json.loads(base64.b64decode(FIREBASE_B64)))
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
