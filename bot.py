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
  WEB_URL       (optional) public https URL of the website -> "Watch" button for users.
                On Render it is picked up from RENDER_EXTERNAL_URL automatically.
  PORT          set automatically by Render/Railway, default 8080

Admin panel (Telegram, every step has Back)
  Link Add / Link Remove (last 1 | last 50 | all | by link/number, 2x confirm)
  Web Start   - videos that play when the website opens (add / remove / remove all)
  Start Msg   - welcome text + banner photo for normal users
  Broadcast   - send any message (text / photo / video ...) to all bot users
  Force Join  - users must join your channels before using the bot
  Refer       - points per referral
  Domain / Request / Status

Users (Telegram)
  /start -> welcome (banner photo + text) + buttons: Watch, Refer & Earn, Balance, Leaderboard

Web API
  POST /api/register {pin}        -> user creates own PIN, gets uid + token
  POST /api/auth     {uid, pin}   -> login with own PIN, gets token
  POST /api/feed     {cat,n,exclude} -> random links (from RAM cache)
  POST /api/defaults              -> videos shown when the website starts (set in bot > Web Start)

Firestore layout
  India_video / Japan_video / Russia_video / Cute_video / Random_video
      doc 000001, 000002 ...  fields: n, url, cat, title, added
  meta/<collection>   { count }
  domains/<id>        { host, added }
  requests/<id>       { host, status: pending|approved|rejected, last, count }
  users/<uid>         { salt, hash, created }      (web PIN, only a salted PBKDF2 hash)
  tg_users/<tg_id>    { name, username, points, refs, ref_by, credited, blocked, joined }
  settings/start      { text, photo }              (welcome message + banner file_id)
  settings/web        { items: [{url,title}] }     (website start videos)
  settings/force      { on, channels: [{chat,title,link}] }
  settings/refer      { points }
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
from urllib.parse import quote, urlparse

import firebase_admin
from aiohttp import web
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, RetryAfter
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
WEB_URL = (os.getenv("WEB_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")

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
WEB_PAGE = 8  # web-start videos per page in the bot
MAX_WEB_VIDS = 300  # max videos in the website start list
MAX_FORCE = 6  # max force-join channels
DEFAULT_REF_POINTS = 10  # points per referral (admin can change it in the bot)
BC_DELAY = 0.05  # pause between broadcast messages (~20 msg/s, Telegram allows ~30)
# -------------------------------------------------------------------------

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
LEGACY_TITLE_RE = re.compile(r"^(Random|India|Japan|Russia|Cute) #\d+$")

db = None
ADD_LOCK: asyncio.Lock | None = None

# ---- RAM cache: the web never reads Firestore, it reads these ----
CACHE = {k: [] for k in CATEGORIES}  # list of {id,url,title,cat,doc}
CACHE_URLS = {k: set() for k in CATEGORIES}
CACHE_MAXN = {k: 0 for k in CATEGORIES}
DOMAINS: set[str] = set()  # approved hosts
USERS: dict[str, dict] = {}  # web uid -> {"salt","hash"}   (loaded on demand)

DEFAULT_START = "👋 Welcome {name}!"
START = {"text": "", "photo": ""}  # welcome message for normal users (photo = Telegram file_id)
WEB_VIDS: list[dict] = []  # videos shown when the website starts [{url,title}]
FORCE = {"on": False, "channels": []}  # force join  [{chat,title,link}]
REF = {"points": DEFAULT_REF_POINTS}
TGU: dict[int, dict] = {}  # telegram users: id -> {name,username,points,refs,ref_by,credited,blocked}
BC = {"running": False, "cancel": False}  # broadcast state


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


def num_of(item) -> int:
    return int(item["id"].split(":")[1])


def short(x: str, n: int = 40) -> str:
    x = (x or "").replace("\n", " ")
    return x if len(x) <= n else x[: n - 1] + "…"


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
            items.append({"id": f"{cat}:{n}", "url": url, "title": clean_title(x.get("title")), "cat": cat, "doc": d.id})
            urls.add(url)
            maxn = max(maxn, n)
        items.sort(key=num_of)
        CACHE[cat], CACHE_URLS[cat], CACHE_MAXN[cat] = items, urls, maxn
        log.info("cache %s: %d links", cat, len(items))
    DOMAINS.clear()
    for d in db.collection("domains").stream():
        h = (d.to_dict() or {}).get("host")
        if h:
            DOMAINS.add(h)
    log.info("approved domains: %d", len(DOMAINS))

    def setting(name):
        snap = db.collection("settings").document(name).get()
        return (snap.to_dict() or {}) if snap.exists else {}

    st = setting("start")
    START["text"], START["photo"] = st.get("text", "") or "", st.get("photo", "") or ""
    WEB_VIDS[:] = [
        {"url": x["url"], "title": clean_title(x.get("title"))}
        for x in setting("web").get("items", [])
        if isinstance(x, dict) and x.get("url")
    ]
    fj = setting("force")
    FORCE["on"] = bool(fj.get("on"))
    FORCE["channels"] = [c for c in fj.get("channels", []) if isinstance(c, dict) and c.get("chat")]
    REF["points"] = int(setting("refer").get("points", DEFAULT_REF_POINTS))
    TGU.clear()
    for d in db.collection("tg_users").stream():
        x = d.to_dict() or {}
        try:
            uid = int(d.id)
        except ValueError:
            continue
        TGU[uid] = {
            "name": x.get("name", ""),
            "username": x.get("username", ""),
            "points": int(x.get("points", 0)),
            "refs": int(x.get("refs", 0)),
            "ref_by": int(x.get("ref_by", 0) or 0),
            "credited": bool(x.get("credited")),
            "blocked": bool(x.get("blocked")),
        }
    log.info("bot users: %d | web-start videos: %d | force channels: %d", len(TGU), len(WEB_VIDS), len(FORCE["channels"]))


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
            added.append({"id": f"{cat}:{a + k}", "url": u, "title": t, "cat": cat, "doc": f"{a + k:06d}"})
        base, first, last = b, first or a, b
    return {"items": added, "dup": len(entries) - len(new), "first": first, "last": last}


def fs_delete_docs(cat: str, docs: list[str]):
    """Deletes documents of one collection in batches (Firestore limit 500 per batch)."""
    col = db.collection(cat)
    for i in range(0, len(docs), 400):
        batch = db.batch()
        for d in docs[i : i + 400]:
            batch.delete(col.document(d))
        batch.commit()


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


def fs_save_start():
    db.collection("settings").document("start").set({"text": START["text"], "photo": START["photo"]})


def fs_save_web():
    db.collection("settings").document("web").set({"items": WEB_VIDS})


def fs_save_force():
    db.collection("settings").document("force").set({"on": FORCE["on"], "channels": FORCE["channels"]})


def fs_save_refer():
    db.collection("settings").document("refer").set({"points": REF["points"]})


def fs_save_user(uid: int, data: dict):
    db.collection("tg_users").document(str(uid)).set(data, merge=True)


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


async def api_defaults(request):
    """Videos shown when the website starts (set by the admin in the bot > Web Start)."""
    host, err = await gate(request)
    if err:
        return err
    return web.json_response(
        {"ok": True, "items": [{"url": x["url"], "title": x.get("title", "")} for x in WEB_VIDS]}
    )


async def index(request):
    if WEB_FILE.exists():
        return web.FileResponse(WEB_FILE, headers={"Cache-Control": "no-cache"})
    return web.Response(text="ok (web/index.html not found)")


async def health(request):
    return web.Response(text="ok")


# ------------------------------------------------------------------ bot UI helpers
def B(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(rows)


LINE = "━━━━━━━━━━━━━━━"
BACK = [B("⬅️ Back", "m")]


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_ID)


def home_text():
    blocked = sum(1 for x in TGU.values() if x.get("blocked"))
    links = sum(len(v) for v in CACHE.values())
    fj = "🟢 ON" if FORCE["on"] and FORCE["channels"] else "🔴 OFF"
    return (
        f"🛠 ADMIN PANEL\n{LINE}\n"
        f"👥 Users: {len(TGU)}  (🚫 {blocked})\n"
        f"🔗 Links: {links}   🎬 Web start: {len(WEB_VIDS)}\n"
        f"🌐 Domains: {len(DOMAINS)}\n"
        f"🔒 Force join: {fj}   🎁 Refer: +{REF['points']} pts\n"
        f"{LINE}\nKya karna hai? 👇"
    )


def main_kb():
    return kb(
        [
            [B("➕ Link Add", "la"), B("🗑 Link Remove", "lr")],
            [B("🎬 Web Start", "ws:0"), B("💬 Start Msg", "sm")],
            [B("📣 Broadcast", "bc"), B("🔒 Force Join", "fj")],
            [B("🎁 Refer System", "rf"), B("🌐 Domain", "dm")],
            [B("📨 Request", "rq:0"), B("📊 Status", "st")],
            [B("👁 User View", "uv")],
        ]
    )


async def show(update: Update, text: str, markup=None):
    """Edits the message of a button press (works for photo messages too) or replies."""
    q = update.callback_query
    if q:
        try:
            if q.message and q.message.photo:
                await q.edit_message_caption(caption=text[:1024], reply_markup=markup)
            else:
                await q.edit_message_text(text, reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


def cat_kb(prefix, counts=False):
    btn = []
    for k, (e, n) in CATEGORIES.items():
        btn.append(B(f"{e} {n}" + (f" ({len(CACHE[k])})" if counts else ""), f"{prefix}:{k}"))
    rows = [btn[i : i + 2] for i in range(0, len(btn), 2)]
    rows.append(BACK)
    return kb(rows)


def tag(cat):
    e, n = CATEGORIES[cat]
    return f"{e} {n}"


# ------------------------------------------------------------------ telegram users, referral, force join
async def register_user(user, ref: int | None = None):
    """Creates / refreshes the bot user. Returns (record, is_new)."""
    u = TGU.get(user.id)
    if u is None:
        u = {
            "name": (user.first_name or "User")[:40],
            "username": user.username or "",
            "points": 0,
            "refs": 0,
            "ref_by": ref if (ref and ref != user.id and ref in TGU) else 0,
            "credited": False,
            "blocked": False,
        }
        TGU[user.id] = u
        await asyncio.to_thread(fs_save_user, user.id, {**u, "joined": firestore.SERVER_TIMESTAMP})
        return u, True
    changed = {}
    name, uname = (user.first_name or "User")[:40], user.username or ""
    if u.get("blocked"):
        u["blocked"] = False
        changed["blocked"] = False
    if u.get("name") != name:
        u["name"] = changed["name"] = name
    if u.get("username") != uname:
        u["username"] = changed["username"] = uname
    if changed:
        await asyncio.to_thread(fs_save_user, user.id, changed)
    return u, False


async def credit_referral(bot, uid: int):
    """Gives the referrer their points, once per invited user (after force join is passed)."""
    u = TGU.get(uid)
    if not u or not u.get("ref_by") or u.get("credited"):
        return
    u["credited"] = True
    await asyncio.to_thread(fs_save_user, uid, {"credited": True})
    r = TGU.get(u["ref_by"])
    if not r:
        return
    r["points"] += REF["points"]
    r["refs"] += 1
    await asyncio.to_thread(fs_save_user, u["ref_by"], {"points": r["points"], "refs": r["refs"]})
    try:
        await bot.send_message(
            u["ref_by"],
            f"🎉 {u['name']} tumhare refer link se join hua!\n\n➕ +{REF['points']} points\n💰 Balance: {r['points']} points",
        )
    except Exception:
        pass


async def missing_channels(bot, uid: int) -> list[dict]:
    """Force-join channels the user has NOT joined yet. A channel the bot cannot check is skipped."""
    if not FORCE["on"] or not FORCE["channels"] or uid == ADMIN_ID:
        return []
    miss = []
    for ch in FORCE["channels"]:
        try:
            m = await bot.get_chat_member(ch["chat"], uid)
            if m.status in ("left", "kicked") or (m.status == "restricted" and not getattr(m, "is_member", True)):
                miss.append(ch)
        except Exception as e:
            log.warning("force-join check failed for %s: %s", ch.get("title"), e)
    return miss


JOIN_TEXT = "🔒 Bot use karne ke liye pehle ye channel join karo 👇\n\nJoin karne ke baad ✅ I Joined dabao."


def join_kb(miss):
    rows = [[InlineKeyboardButton(f"📢 {short(c.get('title') or 'Join', 30)}", url=c["link"])] for c in miss]
    rows.append([B("✅ I Joined", "u:chk")])
    return kb(rows)


def user_kb():
    rows = []
    if WEB_URL.startswith(("http://", "https://")):
        rows.append([InlineKeyboardButton("🎬 Watch Videos", url=WEB_URL)])
    rows.append([B("🎁 Refer & Earn", "u:ref"), B("💰 Balance", "u:bal")])
    rows.append([B("🏆 Leaderboard", "u:top")])
    return kb(rows)


async def send_welcome(bot, chat_id: int, user):
    """Banner photo + text (as caption) when a photo is set, else plain text. Always with the menu buttons."""
    text = (START["text"] or DEFAULT_START).replace("{name}", user.first_name or "")
    markup = user_kb()
    if START["photo"]:
        try:
            if len(text) <= 1024:
                await bot.send_photo(chat_id, START["photo"], caption=text, reply_markup=markup)
            else:
                await bot.send_photo(chat_id, START["photo"])
                await bot.send_message(chat_id, text, reply_markup=markup)
            return
        except BadRequest:
            log.exception("welcome photo failed, sending text only")
    await bot.send_message(chat_id, text, reply_markup=markup)


def ref_link(bot_username: str, uid: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{uid}"


def ranked_list():
    """Referrers sorted: most refers first, then most points."""
    r = [(uid, u) for uid, u in TGU.items() if u.get("refs", 0) > 0]
    r.sort(key=lambda x: (-x[1]["refs"], -x[1]["points"], x[0]))
    return r


def leaderboard_text(my_id: int | None = None) -> str:
    ranked = ranked_list()
    top, ranks = ranked[:10], {uid: i for i, (uid, _) in enumerate(ranked, 1)}
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for i, (uid, u) in enumerate(top, 1):
        who = short(u.get("name") or "User", 18)
        me = " 👈 tum" if uid == my_id else ""
        lines.append(f"{medals.get(i, f'{i}.')} {who} — 👥 {u['refs']} · 💰 {u['points']}{me}")
    body = "\n".join(lines) or "Abhi koi refer nahi hua. Pehle tum bano! 🚀"
    foot = ""
    if my_id and my_id in TGU:
        mine = TGU[my_id]
        if my_id in ranks:
            foot = f"\n\n📍 Tumhara rank: #{ranks[my_id]} · 👥 {mine['refs']} refers"
        else:
            foot = "\n\n📍 Tum abhi rank mein nahi ho. Refer link share karo!"
    return f"🏆 Top Referrers\n{LINE}\n{body}{foot}"


# ------------------------------------------------------------------ user handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_admin(update):
        await register_user(user)
        context.user_data.clear()
        await show(update, home_text(), main_kb())
        return
    ref = None
    if context.args and context.args[0].startswith("ref_") and context.args[0][4:].isdigit():
        ref = int(context.args[0][4:])
    await register_user(user, ref)
    miss = await missing_channels(context.bot, user.id)
    if miss:
        await update.message.reply_text(JOIN_TEXT, reply_markup=join_kb(miss))
        return
    await credit_referral(context.bot, user.id)
    await send_welcome(context.bot, user.id, user)


async def on_user_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    cmd = q.data.split(":")[1]
    u, _ = await register_user(user)

    miss = await missing_channels(context.bot, user.id)
    if cmd == "chk":
        if miss:
            await q.answer("❌ Abhi sab channel join nahi kiye", show_alert=True)
            await show(update, JOIN_TEXT, join_kb(miss))
            return
        await q.answer("✅ Thanks!")
        try:
            await q.message.delete()
        except Exception:
            pass
        await credit_referral(context.bot, user.id)
        await send_welcome(context.bot, user.id, user)
        return
    if miss:
        await q.answer()
        await show(update, JOIN_TEXT, join_kb(miss))
        return
    await q.answer()
    await credit_referral(context.bot, user.id)

    nav = [B("🏠 Menu", "u:home")]
    if cmd == "home":
        try:
            await q.message.delete()
        except Exception:
            pass
        await send_welcome(context.bot, user.id, user)
    elif cmd == "ref":
        link = ref_link(context.bot.username, user.id)
        share = "https://t.me/share/url?url=" + quote(link) + "&text=" + quote("🎬 Join karo aur videos dekho!")
        text = (
            f"🎁 Refer & Earn\n{LINE}\n"
            f"Dost ko invite karo, har join par +{REF['points']} points! 💎\n\n"
            f"🔗 Tumhara refer link:\n{link}\n\n"
            f"👥 Refers: {u['refs']}   💰 Points: {u['points']}"
        )
        await show(
            update,
            text,
            kb(
                [
                    [InlineKeyboardButton("📤 Share Link", url=share)],
                    [B("💰 Balance", "u:bal"), B("🏆 Leaderboard", "u:top")],
                    nav,
                ]
            ),
        )
    elif cmd == "bal":
        text = (
            f"💰 Tumhara Balance\n{LINE}\n"
            f"💎 Points: {u['points']}\n"
            f"👥 Total refers: {u['refs']}\n"
            f"🎁 Har refer par: +{REF['points']} points"
        )
        await show(update, text, kb([[B("🎁 Refer & Earn", "u:ref"), B("🏆 Leaderboard", "u:top")], nav]))
    elif cmd == "top":
        await show(update, leaderboard_text(user.id), kb([[B("🎁 Refer & Earn", "u:ref"), B("💰 Balance", "u:bal")], nav]))


# ------------------------------------------------------------------ admin: link remove
def pick_remove(cat: str, mode: str) -> list[dict]:
    """mode: '1' / '50' = the newest N links, 'all' = everything in the category."""
    items = sorted(CACHE[cat], key=num_of)
    return items if mode == "all" else items[-int(mode) :]


def preview_lines(items: list[dict], limit=6) -> str:
    rows = [f"#{num_of(x)} {short(x['title'] or x['url'], 38)}" for x in items[-limit:]]
    more = f"\n… +{len(items) - limit} aur" if len(items) > limit else ""
    return "\n".join(rows) + more


async def do_remove(cat: str, items: list[dict]):
    async with ADD_LOCK:
        await asyncio.to_thread(fs_delete_docs, cat, [x["doc"] for x in items])
        gone = {x["id"] for x in items}
        CACHE[cat] = [x for x in CACHE[cat] if x["id"] not in gone]
        CACHE_URLS[cat] = {x["url"] for x in CACHE[cat]}


async def admin_link_remove(update, context, cmd, parts):
    ud = context.user_data
    if cmd == "lr":
        ud.clear()
        await show(update, f"🗑 Link Remove\n{LINE}\nKis category se remove karna hai? 👇", cat_kb("lrc", counts=True))
    elif cmd == "lrc":
        cat = parts[1]
        if cat not in CATEGORIES:
            return
        ud.clear()
        n = len(CACHE[cat])
        if not n:
            await show(update, f"{tag(cat)}\n\nIs category mein koi link nahi hai.", kb([[B("⬅️ Back", "lr")]]))
            return
        await show(
            update,
            f"🗑 {tag(cat)} · {n} links\n{LINE}\nKitne remove karne hain? 👇\n(naye wale pehle hatenge)",
            kb(
                [
                    [B("🗑 Last 1", f"lrn:{cat}:1"), B("🗑 Last 50", f"lrn:{cat}:50")],
                    [B(f"🧨 All ({n})", f"lrn:{cat}:all")],
                    [B("🎯 Link / Number se", f"lro:{cat}")],
                    [B("⬅️ Back", "lr")],
                ]
            ),
        )
    elif cmd in ("lrn", "lrn2", "lrn3"):
        cat, mode = parts[1], parts[2]
        if cat not in CATEGORIES or mode not in ("1", "50", "all"):
            return
        items = pick_remove(cat, mode)
        if not items:
            await show(update, "Remove karne ke liye kuch nahi bacha.", kb([[B("⬅️ Back", f"lrc:{cat}")]]))
            return
        if cmd == "lrn":
            await show(
                update,
                f"⚠️ Confirm (1/2)\n\n{tag(cat)} se {len(items)} link remove karna hai?\n\n{preview_lines(items)}",
                kb([[B("✅ Haan", f"lrn2:{cat}:{mode}"), B("⬅️ Nahi", f"lrc:{cat}")]]),
            )
        elif cmd == "lrn2":
            await show(
                update,
                f"🚨 Final confirm (2/2)\n\n{tag(cat)} ke {len(items)} link PERMANENT delete honge.\nYe wapas nahi aa sakta.",
                kb([[B(f"🗑 Delete {len(items)}", f"lrn3:{cat}:{mode}"), B("❌ Cancel", f"lrc:{cat}")]]),
            )
        else:
            await do_remove(cat, items)
            await show(
                update,
                f"✅ {len(items)} link remove ho gaye\n{tag(cat)}: ab {len(CACHE[cat])} links",
                kb([[B("⬅️ Back", f"lrc:{cat}"), B("🏠 Menu", "m")]]),
            )
    elif cmd == "lro":
        cat = parts[1]
        if cat not in CATEGORIES:
            return
        ud.clear()
        ud["cat"], ud["mode"] = cat, "rm_one"
        await show(
            update,
            f"🎯 {tag(cat)}\n\nNumber (jaise #15) ya link bhejo.\nEk se zyada bhi chalega, har line mein ek.",
            kb([[B("⬅️ Back", f"lrc:{cat}")]]),
        )
    elif cmd in ("lre2", "lre3"):
        cat, ids = ud.get("cat"), ud.get("rm_ids")
        if not cat or not ids:
            await show(update, "Session khatam. Dubara try karo.", kb([[B("⬅️ Back", "lr")]]))
            return
        items = [x for x in CACHE[cat] if x["id"] in ids]
        if cmd == "lre2":
            await show(
                update,
                f"🚨 Final confirm (2/2)\n\n{len(items)} link PERMANENT delete honge.\nYe wapas nahi aa sakta.",
                kb([[B(f"🗑 Delete {len(items)}", "lre3"), B("❌ Cancel", f"lrc:{cat}")]]),
            )
        else:
            await do_remove(cat, items)
            ud.clear()
            await show(
                update,
                f"✅ {len(items)} link remove ho gaye\n{tag(cat)}: ab {len(CACHE[cat])} links",
                kb([[B("⬅️ Back", f"lrc:{cat}"), B("🏠 Menu", "m")]]),
            )


async def rm_one_input(update, context, text):
    ud = context.user_data
    cat = ud.get("cat")
    nav = kb([[B("⬅️ Back", f"lrc:{cat}"), B("🏠 Menu", "m")]])
    nums = {int(n) for n in re.findall(r"(?<![\w/])#?(\d{1,9})(?![\w/])", " ".join(l for l in text.splitlines() if not URL_RE.search(l)))}
    urls = {u.rstrip(").,;]}>") for u in URL_RE.findall(text)}
    items = [x for x in CACHE[cat] if num_of(x) in nums or x["url"] in urls]
    if not items:
        await update.message.reply_text("❌ Koi match nahi mila. Number ya sahi link bhejo.", reply_markup=nav)
        return
    ud["rm_ids"] = {x["id"] for x in items}
    ud.pop("mode", None)
    await update.message.reply_text(
        f"⚠️ Confirm (1/2)\n\n{tag(cat)} se {len(items)} link remove karna hai?\n\n{preview_lines(items)}",
        reply_markup=kb([[B("✅ Haan", "lre2"), B("⬅️ Nahi", f"lrc:{cat}")]]),
    )


# ------------------------------------------------------------------ admin: website start videos
async def render_web(update: Update, page: int = 0):
    total = len(WEB_VIDS)
    pages = max(1, -(-total // WEB_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = WEB_VIDS[page * WEB_PAGE : (page + 1) * WEB_PAGE]
    lines = [f"{page * WEB_PAGE + i}. {short((x['title'] + ' · ' if x.get('title') else '') + x['url'], 42)}" for i, x in enumerate(chunk, 1)]
    text = (
        f"🎬 Web Start Videos ({total}/{MAX_WEB_VIDS})\n{LINE}\n"
        "Website khulte hi ye videos chalte hain.\n\n"
        + ("\n".join(lines) if lines else "Abhi khali hai, isliye built-in demo videos chal rahe hain.")
    )
    rows = []
    dels = [B(f"🗑 {page * WEB_PAGE + i}", f"wsd:{hid_of(x['url'])}:{page}") for i, x in enumerate(chunk, 1)]
    rows += [dels[i : i + 4] for i in range(0, len(dels), 4)]
    nav = []
    if page > 0:
        nav.append(B("◀️ Prev", f"ws:{page - 1}"))
    if page < pages - 1:
        nav.append(B("Next ▶️", f"ws:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B("➕ Add Videos", "wsa")] + ([B("🧨 Remove All", "wsx")] if total else []))
    rows.append(BACK)
    await show(update, text, kb(rows))


async def admin_web(update, context, cmd, parts):
    ud = context.user_data
    if cmd == "ws":
        ud.clear()
        await render_web(update, int(parts[1]) if len(parts) > 1 else 0)
    elif cmd == "wsa":
        ud.clear()
        ud["mode"] = "ws_add"
        await show(
            update,
            "➕ Web Start Videos\n\nLink bhejo (ek ya zyada) ya .txt file.\nTitle ke liye link se pehle line mein likho.\n\n"
            "Example:\nMera video\nhttps://site.com/a.m3u8",
            kb([[B("⬅️ Back", "ws:0")]]),
        )
    elif cmd in ("wsd", "wsd2"):
        hid, page = parts[1], int(parts[2])
        x = next((v for v in WEB_VIDS if hid_of(v["url"]) == hid), None)
        if not x:
            await render_web(update, page)
        elif cmd == "wsd":
            await show(
                update,
                f"🗑 Remove karna hai?\n\n{x.get('title') or ''}\n{x['url']}",
                kb([[B("✅ Haan, remove", f"wsd2:{hid}:{page}"), B("⬅️ Nahi", f"ws:{page}")]]),
            )
        else:
            WEB_VIDS.remove(x)
            await asyncio.to_thread(fs_save_web)
            await render_web(update, page)
    elif cmd == "wsx":
        await show(
            update,
            f"⚠️ Confirm (1/2)\n\nWeb Start ke saare {len(WEB_VIDS)} videos remove karne hain?",
            kb([[B("✅ Haan", "wsx2"), B("⬅️ Nahi", "ws:0")]]),
        )
    elif cmd == "wsx2":
        await show(
            update,
            f"🚨 Final confirm (2/2)\n\n{len(WEB_VIDS)} videos delete honge. Website phir built-in demo videos dikhayegi.",
            kb([[B("🗑 Delete all", "wsx3"), B("❌ Cancel", "ws:0")]]),
        )
    elif cmd == "wsx3":
        WEB_VIDS.clear()
        await asyncio.to_thread(fs_save_web)
        await render_web(update, 0)


async def web_ingest(update, context, text):
    nav = kb([[B("⬅️ Back", "ws:0"), B("🏠 Menu", "m")]])
    entries = parse_entries(text)
    if not entries:
        await update.message.reply_text("❌ Koi valid link nahi mila.", reply_markup=nav)
        return
    have = {x["url"] for x in WEB_VIDS}
    new = [(u, t) for u, t in entries if u not in have]
    room = MAX_WEB_VIDS - len(WEB_VIDS)
    take = new[: max(0, room)]
    for u, t in take:
        WEB_VIDS.append({"url": u, "title": t})
    if take:
        await asyncio.to_thread(fs_save_web)
    lines = [f"✅ Added: {len(take)}", f"🎬 Total: {len(WEB_VIDS)}/{MAX_WEB_VIDS}"]
    if len(entries) - len(new):
        lines.append(f"♻️ Duplicate skipped: {len(entries) - len(new)}")
    if len(new) > len(take):
        lines.append(f"⚠️ Limit full, {len(new) - len(take)} skip hue")
    lines.append("\nAur bhejo, ya Back dabao.")
    await update.message.reply_text("\n".join(lines), reply_markup=nav)


# ------------------------------------------------------------------ admin: start message (text + banner photo)
async def show_start_msg(update: Update):
    photo = "✅ Set hai" if START["photo"] else "❌ Nahi hai"
    cur = START["text"] or f"(default)\n{DEFAULT_START}"
    await show(
        update,
        f"💬 Start Message\n{LINE}\n🖼 Banner photo: {photo}\n\n📝 Text:\n{cur}\n\n"
        "Tip: {name} likhoge to user ka naam aa jayega.",
        kb(
            [
                [B("✏️ Edit Text", "sme"), B("🖼 Set Photo", "smi")],
                [B("🗑 Remove Photo", "smx"), B("🧹 Reset Text", "smr")],
                [B("👁 Preview", "smp")],
                BACK,
            ]
        ),
    )


async def admin_start_msg(update, context, cmd):
    ud, q = context.user_data, update.callback_query
    if cmd == "sm":
        ud.clear()
        await show_start_msg(update)
    elif cmd == "sme":
        ud.clear()
        ud["mode"] = "smsg"
        await show(
            update,
            "✏️ Naya text bhejo.\n\nNormal users ko /start par yahi dikhega.\n{name} = user ka naam",
            kb([[B("⬅️ Back", "sm")]]),
        )
    elif cmd == "smi":
        ud.clear()
        ud["mode"] = "smph"
        await show(
            update,
            "🖼 Banner photo bhejo (photo ki tarah, file nahi).\n\n"
            "Photo ke saath caption likhoge to wahi text ban jayega, aur photo ke neeche dikhega.",
            kb([[B("⬅️ Back", "sm")]]),
        )
    elif cmd == "smx":
        START["photo"] = ""
        await asyncio.to_thread(fs_save_start)
        await show_start_msg(update)
    elif cmd == "smr":
        START["text"] = ""
        await asyncio.to_thread(fs_save_start)
        await show_start_msg(update)
    elif cmd == "smp":
        await send_welcome(context.bot, q.message.chat_id, update.effective_user)  # exactly what users see


async def save_start_photo(update, context):
    msg, ud = update.message, context.user_data
    if not msg.photo:
        await update.message.reply_text("🖼 Photo bhejo (photo ki tarah, file nahi).", reply_markup=kb([[B("⬅️ Back", "sm")]]))
        return
    START["photo"] = msg.photo[-1].file_id
    if msg.caption:
        START["text"] = msg.caption.strip()[:3500]
    await asyncio.to_thread(fs_save_start)
    ud.pop("mode", None)
    await msg.reply_text(
        "✅ Banner photo save ho gayi.",
        reply_markup=kb([[B("👁 Preview", "smp"), B("⬅️ Back", "sm")], [B("🏠 Menu", "m")]]),
    )


# ------------------------------------------------------------------ admin: broadcast
def bc_recipients() -> list[int]:
    return [uid for uid, u in TGU.items() if not u.get("blocked") and uid != ADMIN_ID]


async def run_broadcast(app: Application, chat_id: int, status_id: int, src_chat: int, src_mid: int, ids: list[int]):
    BC["running"], BC["cancel"] = True, False
    ok = fail = blocked = 0
    stop_kb = kb([[B("🛑 Stop", "bcx")]])

    async def edit(text, markup=None):
        try:
            await app.bot.edit_message_text(text, chat_id=chat_id, message_id=status_id, reply_markup=markup)
        except Exception:
            pass

    done = 0
    try:
        for done, uid in enumerate(ids, 1):
            if BC["cancel"]:
                break
            try:
                try:
                    await app.bot.copy_message(uid, src_chat, src_mid)
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    await app.bot.copy_message(uid, src_chat, src_mid)
                ok += 1
            except Forbidden:
                blocked += 1
                if uid in TGU:
                    TGU[uid]["blocked"] = True
                    await asyncio.to_thread(fs_save_user, uid, {"blocked": True})
            except Exception as e:
                fail += 1
                log.warning("broadcast to %s failed: %s", uid, e)
            await asyncio.sleep(BC_DELAY)
            if done % 40 == 0:
                await edit(f"📣 Bhej raha hu… {done}/{len(ids)}\n✅ {ok}  🚫 {blocked}  ⚠️ {fail}", stop_kb)
    finally:
        BC["running"] = False
    state = "🛑 Rok diya" if BC["cancel"] else "✅ Broadcast complete"
    await edit(
        f"{state}\n{LINE}\n📨 Sent: {ok}\n🚫 Blocked: {blocked}\n⚠️ Failed: {fail}\n👥 Total: {len(ids)}",
        kb([[B("📣 Broadcast", "bc"), B("🏠 Menu", "m")]]),
    )


async def admin_broadcast(update, context, cmd):
    ud, q = context.user_data, update.callback_query
    if cmd == "bc":
        ud.clear()
        n = len(bc_recipients())
        blocked = sum(1 for x in TGU.values() if x.get("blocked"))
        await show(
            update,
            f"📣 Broadcast\n{LINE}\n👥 Total users: {len(TGU)}\n✅ Active (milega): {n}\n🚫 Blocked: {blocked}\n\n"
            "Text, photo, video, kuch bhi bhej sakte ho, jaisa bhejoge waisa hi sabko jayega.",
            kb([[B("✍️ New Broadcast", "bcn")], BACK]),
        )
    elif cmd == "bcn":
        ud.clear()
        if BC["running"]:
            await q.answer("⏳ Ek broadcast chal raha hai", show_alert=True)
            return
        ud["mode"] = "bc"
        await show(update, "✍️ Jo message sabko bhejna hai wo bhejo\n(text / photo / video / file).", kb([[B("⬅️ Back", "bc")]]))
    elif cmd == "bcs":
        src = ud.get("bc_src")
        if not src:
            await show(update, "Message nahi mila. Dubara try karo.", kb([[B("⬅️ Back", "bc")]]))
            return
        if BC["running"]:
            await q.answer("⏳ Ek broadcast chal raha hai", show_alert=True)
            return
        ids = bc_recipients()
        ud.clear()
        await show(update, f"📣 Shuru… 0/{len(ids)}", kb([[B("🛑 Stop", "bcx")]]))
        task = asyncio.create_task(
            run_broadcast(context.application, q.message.chat_id, q.message.message_id, src[0], src[1], ids)
        )
        context.application.bot_data.setdefault("tasks", set()).add(task)
        task.add_done_callback(context.application.bot_data["tasks"].discard)
    elif cmd == "bcx":
        BC["cancel"] = True
        await q.answer("🛑 Rok raha hu…")


async def bc_capture(update, context):
    msg, ud = update.message, context.user_data
    ud["bc_src"] = (msg.chat_id, msg.message_id)
    ud.pop("mode", None)
    n = len(bc_recipients())
    await context.bot.copy_message(msg.chat_id, msg.chat_id, msg.message_id)  # preview
    await msg.reply_text(
        f"👆 Ye preview hai.\n\n{n} users ko bhejna hai?",
        reply_markup=kb([[B(f"✅ Send to {n}", "bcs")], [B("❌ Cancel", "bc")]]),
    )


# ------------------------------------------------------------------ admin: force join
async def show_force(update: Update):
    on = FORCE["on"]
    chans = FORCE["channels"]
    lines = [f"{i}. {c.get('title') or c['chat']}" for i, c in enumerate(chans, 1)]
    text = (
        f"🔒 Force Join\n{LINE}\nStatus: {'🟢 ON' if on else '🔴 OFF'}\n\n"
        f"📢 Channels ({len(chans)}/{MAX_FORCE}):\n" + ("\n".join(lines) or "Abhi koi channel nahi.") +
        "\n\nON hone par user ko bot use karne se pehle ye sab join karne honge."
    )
    rows = [[B("🔴 Turn OFF" if on else "🟢 Turn ON", "fjt")], [B("➕ Add Channel", "fja")]]
    for i, c in enumerate(chans):
        rows.append([B(f"🗑 {short(c.get('title') or str(c['chat']), 28)}", f"fjd:{i}")])
    rows.append(BACK)
    await show(update, text, kb(rows))


async def admin_force(update, context, cmd, parts):
    ud = context.user_data
    if cmd == "fj":
        ud.clear()
        await show_force(update)
    elif cmd == "fjt":
        if not FORCE["on"] and not FORCE["channels"]:
            await update.callback_query.answer("Pehle channel add karo", show_alert=True)
            return
        FORCE["on"] = not FORCE["on"]
        await asyncio.to_thread(fs_save_force)
        await show_force(update)
    elif cmd == "fja":
        ud.clear()
        if len(FORCE["channels"]) >= MAX_FORCE:
            await update.callback_query.answer(f"Max {MAX_FORCE} channels", show_alert=True)
            return
        ud["mode"] = "fj_add"
        await show(
            update,
            "➕ Channel / group add karo\n\n1) Bot ko us channel mein ADMIN banao\n2) Phir yahan bhejo:\n"
            "   • @username  ya  t.me/username\n   • -100… chat id\n   • ya channel ka koi post forward karo",
            kb([[B("⬅️ Back", "fj")]]),
        )
    elif cmd in ("fjd", "fjd2"):
        i = int(parts[1])
        if i >= len(FORCE["channels"]):
            await show_force(update)
            return
        c = FORCE["channels"][i]
        if cmd == "fjd":
            await show(
                update,
                f"🗑 Remove karna hai?\n\n{c.get('title') or c['chat']}",
                kb([[B("✅ Haan, remove", f"fjd2:{i}"), B("⬅️ Nahi", "fj")]]),
            )
        else:
            FORCE["channels"].pop(i)
            if not FORCE["channels"]:
                FORCE["on"] = False
            await asyncio.to_thread(fs_save_force)
            await show_force(update)


async def add_force_channel(update, context):
    msg, ud, bot = update.message, context.user_data, context.bot
    nav = kb([[B("⬅️ Back", "fj"), B("🏠 Menu", "m")]])
    ref = None
    fo = getattr(msg, "forward_origin", None)
    fchat = getattr(fo, "chat", None) if fo else None
    if fchat:
        ref = fchat.id
    else:
        t = (msg.text or "").strip().split()[0] if (msg.text or "").strip() else ""
        m = re.search(r"t\.me/([A-Za-z0-9_]{4,})", t)
        if m:
            ref = "@" + m.group(1)
        elif t.startswith("@"):
            ref = t
        elif re.fullmatch(r"-?\d{5,}", t):
            ref = int(t)
    if ref is None:
        await msg.reply_text("❌ @username, t.me link, chat id ya forwarded post bhejo.", reply_markup=nav)
        return
    try:
        chat = await bot.get_chat(ref)
    except Exception:
        await msg.reply_text("❌ Channel nahi mila. Username / id check karo.", reply_markup=nav)
        return
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
        is_adm = me.status in ("administrator", "creator")
    except Exception:
        is_adm = False
    if not is_adm:
        await msg.reply_text("❌ Bot is channel mein admin nahi hai. Pehle admin banao, phir dubara bhejo.", reply_markup=nav)
        return
    if any(str(c["chat"]) == str(chat.id) for c in FORCE["channels"]):
        await msg.reply_text("♻️ Ye channel pehle se added hai.", reply_markup=nav)
        return
    link = f"https://t.me/{chat.username}" if chat.username else getattr(chat, "invite_link", None)
    if not link:
        try:
            link = await bot.export_chat_invite_link(chat.id)
        except Exception:
            link = None
    if not link:
        await msg.reply_text("❌ Invite link nahi ban paya. Bot ko 'Invite users' permission do.", reply_markup=nav)
        return
    FORCE["channels"].append({"chat": chat.id, "title": chat.title or str(chat.id), "link": link})
    await asyncio.to_thread(fs_save_force)
    ud.pop("mode", None)
    await msg.reply_text(
        f"✅ Added: {chat.title}\n\nForce join {'ON hai' if FORCE['on'] else 'abhi OFF hai, Force Join mein ON karo'}.",
        reply_markup=kb([[B("🔒 Force Join", "fj"), B("🏠 Menu", "m")]]),
    )


# ------------------------------------------------------------------ admin: refer
async def show_refer(update: Update):
    total = sum(u.get("refs", 0) for u in TGU.values())
    top = ranked_list()[:5]
    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    lines = [f"{medals[i]} {short(u['name'], 18)} — 👥 {u['refs']} · 💰 {u['points']}" for i, (_, u) in enumerate(top)]
    await show(
        update,
        f"🎁 Refer System\n{LINE}\n💎 Points per refer: {REF['points']}\n👥 Total refers: {total}\n\n"
        f"🏆 Top 5:\n" + ("\n".join(lines) or "Abhi koi refer nahi."),
        kb([[B("✏️ Set Points", "rfe")], BACK]),
    )


async def admin_refer(update, context, cmd):
    ud = context.user_data
    if cmd == "rf":
        ud.clear()
        await show_refer(update)
    elif cmd == "rfe":
        ud.clear()
        ud["mode"] = "rf_pts"
        await show(update, "✏️ Har refer par kitne points milen? Number bhejo (0 – 1000).", kb([[B("⬅️ Back", "rf")]]))


# ------------------------------------------------------------------ admin: domains / requests / status
async def admin_domains(update, context, cmd, parts):
    ud = context.user_data
    if cmd == "dm":
        ud.clear()
        hosts = sorted(DOMAINS)
        text = f"🌐 Approved domains ({len(hosts)})\n{LINE}\n" + ("\n".join(f"• {h}" for h in hosts) or "Abhi koi domain nahi.")
        rows = [[B(f"🗑 {h[:34]}", f"dd:{hid_of(h)}")] for h in hosts]
        rows.append([B("➕ Add domain", "da")])
        rows.append(BACK)
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


async def render_requests(update: Update, page: int):
    rows, has_next = await asyncio.to_thread(fs_list_requests, page)
    icon = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
    lines, buttons = [], []
    for i, r in enumerate(rows, 1 + page * PAGE):
        st, host, hid = r.get("status", "pending"), r.get("host", "?"), r["hid"]
        lines.append(f"{i}. {icon.get(st, '•')} {host} · {st} · {r.get('count', 1)} hit")
        short_h = host[:24]
        row = []
        if st != "approved":
            row.append(B(f"✅ {short_h}", f"ra1:{hid}:{page}"))
        if st != "rejected":
            row.append(B(f"❌ {short_h}" if st == "approved" else "❌", f"rr:{hid}:{page}"))
        buttons.append(row)
    nav = []
    if page > 0:
        nav.append(B("◀️ Prev", f"rq:{page - 1}"))
    if has_next:
        nav.append(B("Next ▶️", f"rq:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append(BACK)
    text = f"📨 Domain requests\n{LINE}\n" + ("\n".join(lines) or "Abhi koi request nahi aayi.")
    await show(update, text, kb(buttons))


async def admin_requests(update, context, cmd, parts):
    ud = context.user_data
    if cmd == "rq":
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


async def admin_status(update, context):
    context.user_data.clear()
    lines, total = [], 0
    for cat in CATEGORIES:
        c = len(CACHE[cat])
        total += c
        lines.append(f"{tag(cat)}: {c}")
    pending = await asyncio.to_thread(fs_pending_count)
    web_users = await asyncio.to_thread(fs_count_users)
    blocked = sum(1 for x in TGU.values() if x.get("blocked"))
    refs = sum(x.get("refs", 0) for x in TGU.values())
    text = (
        f"📊 Status\n{LINE}\n" + "\n".join(lines) + f"\n\n🔗 Total links: {total}\n"
        f"🎬 Web start videos: {len(WEB_VIDS)}\n"
        f"🌐 Approved domains: {len(DOMAINS)}   ⏳ Pending: {pending}\n"
        f"👤 Web users (own PIN): {web_users}\n\n"
        f"🤖 Bot users: {len(TGU)}  (🚫 blocked {blocked})\n"
        f"🎁 Total refers: {refs}   💎 {REF['points']} pts/refer\n"
        f"🔒 Force join: {'ON' if FORCE['on'] and FORCE['channels'] else 'OFF'} ({len(FORCE['channels'])} ch)"
    )
    await show(update, text, kb([[B("🔄 Refresh", "st")], BACK]))


# ------------------------------------------------------------------ admin: button dispatcher
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("⛔ Admin only", show_alert=True)
        return
    parts = q.data.split(":")
    cmd, ud = parts[0], context.user_data
    if cmd != "bcx":  # bcx answers itself
        await q.answer()

    if cmd == "m":
        ud.clear()
        await show(update, home_text(), main_kb())
    elif cmd == "noop":
        pass
    elif cmd == "uv":  # see the bot exactly like a normal user
        await send_welcome(context.bot, q.message.chat_id, update.effective_user)

    # ---- link add: category -> method -> input
    elif cmd == "la":
        ud.clear()
        await show(update, f"➕ Link Add\n{LINE}\nKis category mein add karna hai? 👇", cat_kb("lc", counts=True))
    elif cmd == "lc":
        cat = parts[1]
        if cat not in CATEGORIES:
            return
        ud.clear()
        ud["cat"] = cat
        await show(
            update,
            f"{tag(cat)} · {len(CACHE[cat])} links\n\nKaise add karna hai?",
            kb([[B("📄 TXT file", "lm:txt"), B("🔗 URL", "lm:url")], [B("⬅️ Back", "la")]]),
        )
    elif cmd == "lm":
        cat = ud.get("cat")
        if not cat:
            await show(update, home_text(), main_kb())
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

    # ---- link remove
    elif cmd in ("lr", "lrc", "lrn", "lrn2", "lrn3", "lro", "lre2", "lre3"):
        await admin_link_remove(update, context, cmd, parts)

    # ---- website start videos
    elif cmd in ("ws", "wsa", "wsd", "wsd2", "wsx", "wsx2", "wsx3"):
        await admin_web(update, context, cmd, parts)

    # ---- start message
    elif cmd in ("sm", "sme", "smi", "smx", "smr", "smp"):
        await admin_start_msg(update, context, cmd)

    # ---- broadcast
    elif cmd in ("bc", "bcn", "bcs", "bcx"):
        await admin_broadcast(update, context, cmd)

    # ---- force join
    elif cmd in ("fj", "fjt", "fja", "fjd", "fjd2"):
        await admin_force(update, context, cmd, parts)

    # ---- refer
    elif cmd in ("rf", "rfe"):
        await admin_refer(update, context, cmd)

    # ---- domains / requests / status
    elif cmd in ("dm", "da", "dd", "dx"):
        await admin_domains(update, context, cmd, parts)
    elif cmd in ("rq", "ra1", "ra2", "ra3", "rr"):
        await admin_requests(update, context, cmd, parts)
    elif cmd == "st":
        await admin_status(update, context)


# ------------------------------------------------------------------ admin: messages
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


async def read_input(update: Update):
    """Text of the message, or the content of a .txt file. None (after a reply) when it is neither."""
    msg = update.message
    if msg.document:
        name = (msg.document.file_name or "").lower()
        if not (name.endswith(".txt") or msg.document.mime_type == "text/plain"):
            await msg.reply_text("📄 Sirf .txt file bhejo.")
            return None
        try:
            data = await (await msg.document.get_file()).download_as_bytearray()
        except Exception as e:
            await msg.reply_text(f"⚠️ File download nahi hui: {e}")
            return None
        return bytes(data).decode("utf-8", errors="ignore")
    if msg.text:
        return msg.text
    await msg.reply_text("Text ya .txt file bhejo.")
    return None


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.message:
        return
    msg, ud = update.message, context.user_data
    mode = ud.get("mode")
    text = (msg.text or "").strip()

    if mode == "bc":
        await bc_capture(update, context)
    elif mode == "smph":
        await save_start_photo(update, context)
    elif mode == "fj_add":
        await add_force_channel(update, context)
    elif mode == "domain":
        host = norm_host(text.split()[0] if text.split() else "")
        nav = kb([[B("⬅️ Back", "dm"), B("🏠 Menu", "m")]])
        if not host:
            await msg.reply_text("❌ Domain sahi nahi hai. Example: https://my_project_vercel.app", reply_markup=nav)
            return
        await asyncio.to_thread(fs_add_domain, host)
        DOMAINS.add(host)
        await msg.reply_text(f"✅ Domain added:\n{host}", reply_markup=nav)
    elif mode == "smsg":
        if not text:
            await msg.reply_text("✏️ Text bhejo.")
            return
        START["text"] = text[:3500]
        await asyncio.to_thread(fs_save_start)
        ud.pop("mode", None)
        await msg.reply_text(
            "✅ Start message saved.\n\nNormal user ab /start karega to ye dikhega.",
            reply_markup=kb([[B("👁 Preview", "smp"), B("⬅️ Back", "sm")], [B("🏠 Menu", "m")]]),
        )
    elif mode == "rf_pts":
        if not re.fullmatch(r"\d{1,4}", text) or int(text) > 1000:
            await msg.reply_text("❌ 0 se 1000 ke beech number bhejo.")
            return
        REF["points"] = int(text)
        await asyncio.to_thread(fs_save_refer)
        ud.pop("mode", None)
        await msg.reply_text(
            f"✅ Ab har refer par {REF['points']} points milenge.", reply_markup=kb([[B("🎁 Refer", "rf"), B("🏠 Menu", "m")]])
        )
    elif mode == "rm_one" and ud.get("cat"):
        await rm_one_input(update, context, text)
    elif mode in ("txt", "url") and ud.get("cat"):
        data = await read_input(update)
        if data is not None:
            await ingest(update, context, data)
    elif mode == "ws_add":
        data = await read_input(update)
        if data is not None:
            await web_ingest(update, context, data)
    else:
        await msg.reply_text("👇 Pehle option chuno", reply_markup=main_kb())


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
            web.post("/api/defaults", api_defaults),
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
    app.add_handler(CallbackQueryHandler(on_user_button, pattern=r"^u:"))  # everybody
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(?!u:)"))  # admin only
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE & ~filters.COMMAND, on_message))
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
