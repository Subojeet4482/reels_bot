#!/usr/bin/env python3
"""
Telegram admin bot + website + backend  (ONE process, Telegram POLLING)

Needs an always-on host (Render, Railway, VPS ...). Vercel cannot run polling.
NO Firebase: everything is stored in one local SQLite file (data.db).

Install:  pip install -U python-telegram-bot aiohttp
Run:      python bot.py

Environment variables (or paste the values in CONFIG below):
  BOT_TOKEN     token from @BotFather
  ADMIN_ID      your numeric Telegram user id (ask @userinfobot)
  KEY_64        64-char random secret: python -c "import secrets;print(secrets.token_hex(32))"
                (signs web sessions + salts PINs. Do NOT change it later, PINs would stop working)
  WEB_URL       (optional) public https URL of the website -> "Watch" button for users.
                On Render it is picked up from RENDER_EXTERNAL_URL automatically.
  DB_PATH       (optional) where data.db lives, default: next to bot.py
  PORT          set automatically by Render/Railway, default 8080

How links work
  Website start     -> built-in DEFAULT_VIDEOS (below) + Web Start list, in RANDOM order
  After PIN         -> "india" videos (random) + search box + menu (India/Japan/Russia/Cute/Random)
  Search            -> bot reads links straight from SQLite table `links`.
                       If the keyword has no links yet, bot calls link_file.on_search(keyword)
                       (request = "search=<keyword>"). Your code in link_file.py fetches links
                       and saves them in the DB. Format + DB details are written in link_file.py.

Admin panel (Telegram, every step has Back)
  Link Remove - by keyword (last 1 | last 50 | all | by link/number, 2x confirm)
  Web Start   - extra videos that play when the website opens (add / remove / remove all)
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
  POST /api/feed     {cat,n,exclude} -> random links for keyword `cat` ("random" = mix of all)
                                        {items, reset, searching}
  POST /api/defaults              -> start videos (built-in + Web Start), shuffled
  POST /api/resolve  {page,force} -> fresh .m3u8 for a saved page url (cached 1 hr)  {url, exp}

SQLite tables (data.db)
  links(id, search, title, url, added)   UNIQUE(search, url)
  kv(key, value)                         start / web / force / refer settings (JSON)
  tg_users(id, data)                     bot users (JSON)
  web_users(uid, salt, hash, created)    web PIN (only a salted PBKDF2 hash)
  domains(host) / requests(hid, host, status, last, count)
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
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from aiohttp import web
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
PORT = int(os.getenv("PORT", "8080"))
WEB_URL = (os.getenv("WEB_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
DB_PATH = os.getenv("DB_PATH") or str(Path(__file__).resolve().parent / "data.db")

# Menu buttons on the website. key = the search keyword ("random" = mix of every keyword).
PRESETS = {
    "random": ("🎲", "Random"),
    "india": ("🇮🇳", "India"),
    "japan": ("🇯🇵", "Japan"),
    "russia": ("🇷🇺", "Russia"),
    "cute": ("🥰", "Cute"),
}
START_SEARCH = "india"  # what plays right after the PIN
ONLY_M3U8 = False  # True = accept only links that contain ".m3u8" (Web Start list)
LINK_TIMEOUT = 120  # seconds link_file.on_search may run
ASK_COOLDOWN = 120  # seconds before the same empty keyword is requested again
MAX_SEARCHING = 5  # keywords link_file may work on at the same time
MAX_NEW_SEARCH = 10  # new keywords one web user may request per hour
LINK_TTL = int(os.getenv("LINK_TTL", "3600"))  # seconds: scraped stream links expire after this (1 hr)
RESOLVE_TIMEOUT = 45  # seconds link_file.resolve_page may run
TOPUP_EVERY = 600  # seconds: a keyword is topped up in the background at most this often
MAX_RESOLVE = 30  # fresh stream resolves one web user may trigger per hour

# Videos that play when the website opens (always shuffled). Web Start (admin) adds more.
DEFAULT_VIDEOS = [
    ("Big Buck Bunny", "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"),
    ("Mux Test 001", "https://test-streams.mux.dev/test_001/stream.m3u8"),
    ("Mux PTS Shift", "https://test-streams.mux.dev/pts_shift/master.m3u8"),
    ("Mux Tears of Steel", "https://test-streams.mux.dev/tos_ismc/main.m3u8"),
    ("Apple BipBop Advanced fMP4", "https://devstreaming-cdn.apple.com/videos/streaming/examples/img_bipbop_adv_example_fmp4/master.m3u8"),
    ("Apple BipBop Advanced TS", "https://devstreaming-cdn.apple.com/videos/streaming/examples/img_bipbop_adv_example_ts/master.m3u8"),
    ("Apple BipBop 4x3", "https://devstreaming-cdn.apple.com/videos/streaming/examples/bipbop_4x3/bipbop_4x3_variant.m3u8"),
    ("Apple BipBop 16x9", "https://devstreaming-cdn.apple.com/videos/streaming/examples/bipbop_16x9/bipbop_16x9_variant.m3u8"),
    ("Apple Dolby Atmos", "https://devstreaming-cdn.apple.com/videos/streaming/examples/adv_dv_atmos/main.m3u8"),
    ("Sintel", "https://bitdash-a.akamaihd.net/content/sintel/hls/playlist.m3u8"),
    ("Tears of Steel", "https://demo.unified-streaming.com/k8s/features/stable/video/tears-of-steel/tears-of-steel.ism/.m3u8"),
    ("Bitmovin Art of Motion", "https://cdn.bitmovin.com/content/assets/art-of-motion-dash-hls-progressive/m3u8s/f08e80da-bf1d-4e3d-8899-f0f6155f6efa.m3u8"),
    ("Bitmovin MI201109210084", "https://bitmovin-a.akamaihd.net/content/MI201109210084_1/m3u8s/f08e80da-bf1d-4e3d-8899-f0f6155f6efa.m3u8"),
    ("Oceans AES", "https://playertest.longtailvideo.com/adaptive/oceans_aes/oceans_aes.m3u8"),
    ("Wowza ID3", "https://playertest.longtailvideo.com/adaptive/wowzaid3/playlist.m3u8"),
    ("Big Buck Bunny Full", "https://playertest.longtailvideo.com/adaptive/bbbfull/bbbfull.m3u8"),
    ("Elephants Dream", "https://playertest.longtailvideo.com/adaptive/elephants_dream_v4/index.m3u8"),
    ("Elephants Dream Wowza", "https://wowzaec2demo.streamlock.net/vod-multitrack/_definst_/smil:ElephantsDream/elephantsdream2.smil/playlist.m3u8"),
    ("Big Buck Bunny CloudFront", "https://d2zihajmogu5jn.cloudfront.net/big-buck-bunny/master.m3u8"),
    ("BipBop CloudFront", "https://d2zihajmogu5jn.cloudfront.net/bipbop-advanced/bipbop_16x9_variant.m3u8"),
    ("Shaka Angel One", "https://storage.googleapis.com/shaka-demo-assets/angel-one-hls/hls.m3u8"),
    ("Skate 4K", "https://sample.vodobox.net/skate_phantom_flex_4k/skate_phantom_flex_4k.m3u8"),
    ("Akamai Live Test", "https://cph-p2p-msl.akamaized.net/hls/live/2000341/test/master.m3u8"),
    ("Akamai Live Eight", "https://moctobpltc-i.akamaihd.net/hls/live/571329/eight/playlist.m3u8"),
    ("Big Buck Bunny Akamai", "https://multiplatform-f.akamaihd.net/i/multi/will/bunny/big_buck_bunny_,640x360_400,640x360_700,640x360_1000,950x540_1500,.f4v.csmil/master.m3u8"),
]

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

try:
    import link_file  # your fetch code, see link_file.py
except Exception:  # a missing / broken link_file.py must not stop the bot
    log.exception("link_file.py could not be imported (searches will only use links already in the DB)")
    link_file = None

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
LEGACY_TITLE_RE = re.compile(r"^(Random|India|Japan|Russia|Cute) #\d+$")

# ---- RAM state. Links are NOT cached: the web reads them straight from SQLite ----
TOPUP: dict[str, float] = {}  # keyword -> last background top-up time
SEARCHING: dict[str, float] = {}  # keyword -> start time, while link_file works on it
LAST_ASK: dict[str, float] = {}  # keyword -> when link_file last finished
ASKS: dict[str, list[float]] = {}  # web uid -> times of new keyword requests
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
    return int(item["id"])


def norm_kw(s) -> str:
    """'  Goa  Trip ' -> 'goa trip'. Empty string if it is not a usable keyword."""
    s = re.sub(r"\s+", " ", str(s or "").strip().lower())[:40].strip()
    return s if re.fullmatch(r"\w[\w \-]*", s) else ""


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


# ------------------------------------------------------------------ SQLite (blocking; call with to_thread)
def sql(query: str, args=(), fetch: str | None = None):
    """One short connection per call (safe with threads and with link_file.py writing at the same time)."""
    c = sqlite3.connect(DB_PATH, timeout=30)
    try:
        c.row_factory = sqlite3.Row
        cur = c.execute(query, args)
        if fetch == "all":
            out = [dict(r) for r in cur.fetchall()]
        elif fetch == "one":
            r = cur.fetchone()
            out = dict(r) if r else None
        else:
            out = cur.rowcount
        c.commit()
        return out
    finally:
        c.close()


def init_db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS links(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search TEXT NOT NULL, title TEXT DEFAULT '', url TEXT NOT NULL, added REAL,
                UNIQUE(search, url));
            CREATE INDEX IF NOT EXISTS links_search ON links(search);
            CREATE TABLE IF NOT EXISTS resolved(page TEXT PRIMARY KEY, url TEXT NOT NULL, ts REAL);
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS tg_users(id INTEGER PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS web_users(uid TEXT PRIMARY KEY, salt TEXT, hash TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS domains(host TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS requests(
                hid TEXT PRIMARY KEY, host TEXT, status TEXT, last REAL, count INTEGER DEFAULT 1);
            """
        )
        if "page" not in [r[1] for r in c.execute("PRAGMA table_info(links)")]:  # old data.db
            c.execute("ALTER TABLE links ADD COLUMN page TEXT DEFAULT ''")
        c.commit()
    finally:
        c.close()


def purge_expired() -> int:
    """Deletes scraped links + resolve cache older than LINK_TTL. Returns how many links were removed."""
    cut = time.time() - LINK_TTL
    n = sql("DELETE FROM links WHERE added < ?", (cut,))
    sql("DELETE FROM resolved WHERE ts < ?", (cut,))
    return n


def db_get_resolved(page: str):
    """(url, expires_at) if a stream for this page was resolved less than LINK_TTL-120 s ago, else None."""
    r = sql("SELECT url, ts FROM resolved WHERE page=?", (page,), "one")
    if r and time.time() - r["ts"] < LINK_TTL - 120:
        return r["url"], r["ts"] + LINK_TTL
    return None


def db_set_resolved(page: str, url: str):
    now = time.time()
    sql("INSERT OR REPLACE INTO resolved(page, url, ts) VALUES (?,?,?)", (page, url, now))
    return now + LINK_TTL


def kv_get(name: str) -> dict:
    r = sql("SELECT value FROM kv WHERE key=?", (name,), "one")
    try:
        return json.loads(r["value"]) if r else {}
    except ValueError:
        return {}


def kv_set(name: str, obj: dict):
    sql(
        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (name, json.dumps(obj)),
    )


def load_all():
    init_db()
    DOMAINS.clear()
    DOMAINS.update(r["host"] for r in sql("SELECT host FROM domains", (), "all"))
    log.info("approved domains: %d", len(DOMAINS))
    st = kv_get("start")
    START["text"], START["photo"] = st.get("text", "") or "", st.get("photo", "") or ""
    WEB_VIDS[:] = [
        {"url": x["url"], "title": clean_title(x.get("title"))}
        for x in kv_get("web").get("items", [])
        if isinstance(x, dict) and x.get("url")
    ]
    fj = kv_get("force")
    FORCE["on"] = bool(fj.get("on"))
    FORCE["channels"] = [c for c in fj.get("channels", []) if isinstance(c, dict) and c.get("chat")]
    REF["points"] = int(kv_get("refer").get("points", DEFAULT_REF_POINTS))
    TGU.clear()
    for r in sql("SELECT id, data FROM tg_users", (), "all"):
        try:
            x = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        TGU[int(r["id"])] = {
            "name": x.get("name", ""),
            "username": x.get("username", ""),
            "points": int(x.get("points", 0)),
            "refs": int(x.get("refs", 0)),
            "ref_by": int(x.get("ref_by", 0) or 0),
            "credited": bool(x.get("credited")),
            "blocked": bool(x.get("blocked")),
        }
    log.info(
        "links: %d | bot users: %d | web-start videos: %d | force channels: %d",
        db_total_links(), len(TGU), len(WEB_VIDS), len(FORCE["channels"]),
    )


# ---- links (written by link_file.py, read by the web + admin) ----
def db_total_links() -> int:
    return sql("SELECT COUNT(*) AS n FROM links", (), "one")["n"]


def db_keywords() -> list[dict]:
    """[{search, n}, ...] biggest first."""
    return sql("SELECT search, COUNT(*) AS n FROM links GROUP BY search ORDER BY n DESC, search", (), "all")


def db_count(kw: str) -> int:
    return sql("SELECT COUNT(*) AS n FROM links WHERE search=? AND added > ?", (kw, time.time() - LINK_TTL), "one")["n"]


def db_needs_topup(kw: str) -> bool:
    """Same rule as link_file.needs_links: few fresh links, or the newest batch is older than 2/3 of LINK_TTL."""
    fn = getattr(link_file, "needs_links", None)
    return bool(fn(kw)) if fn else False


def db_pick(kw: str, n: int, exclude: list[int]):
    """n random links for `kw` ("random" = every keyword), never the ids in `exclude`.
    When everything was already seen, starts over (reset=True)."""
    def q(ex):
        cond, args = ["added > ?"], [time.time() - LINK_TTL]  # expired links are never served
        if kw != "random":
            cond.append("search=?")
            args.append(kw)
        if ex:
            cond.append("id NOT IN (" + ",".join(str(i) for i in ex) + ")")
        where = ("WHERE " + " AND ".join(cond)) if cond else ""
        return sql(f"SELECT id, search, title, url, page FROM links {where} ORDER BY RANDOM() LIMIT ?", (*args, n), "all")

    rows = q(exclude)
    if rows or not exclude:
        return rows, False
    return q([]), True


def db_newest(kw: str, n: int | None) -> list[dict]:
    lim = " LIMIT %d" % n if n else ""
    return sql(f"SELECT id, search, title, url FROM links WHERE search=? ORDER BY id DESC{lim}", (kw,), "all")


def db_match(kw: str, nums: set[int], urls: set[str]) -> list[dict]:
    return [x for x in db_newest(kw, None) if x["id"] in nums or x["url"] in urls]


def db_delete_links(ids: list[int]):
    for i in range(0, len(ids), 400):
        part = ids[i : i + 400]
        sql("DELETE FROM links WHERE id IN (%s)" % ",".join("?" * len(part)), part)


# ---- domains / requests ----
def db_add_domain(host):
    h = hid_of(host)
    sql("INSERT OR IGNORE INTO domains(host) VALUES(?)", (host,))
    sql(
        "INSERT INTO requests(hid,host,status,last,count) VALUES(?,?,'approved',?,0) "
        "ON CONFLICT(hid) DO UPDATE SET status='approved', last=excluded.last",
        (h, host, time.time()),
    )


def db_remove_domain(host, status="rejected"):
    h = hid_of(host)
    sql("DELETE FROM domains WHERE host=?", (host,))
    sql(
        "INSERT INTO requests(hid,host,status,last,count) VALUES(?,?,?,?,0) "
        "ON CONFLICT(hid) DO UPDATE SET status=excluded.status, last=excluded.last",
        (h, host, status, time.time()),
    )


def db_get_host(hid):
    r = sql("SELECT host FROM requests WHERE hid=?", (hid,), "one")
    return r["host"] if r else None


def db_list_requests(page):
    rows = sql(
        "SELECT hid, host, status, last, count FROM requests ORDER BY last DESC LIMIT ? OFFSET ?",
        (PAGE + 1, page * PAGE),
        "all",
    )
    return rows[:PAGE], len(rows) > PAGE


def db_pending_count():
    return sql("SELECT COUNT(*) AS n FROM requests WHERE status='pending'", (), "one")["n"]


def db_note_request(host):
    """Called when an unapproved domain hits the API. Returns True if it is a brand-new request."""
    h = hid_of(host)
    if sql("SELECT 1 AS x FROM requests WHERE hid=?", (h,), "one"):
        sql("UPDATE requests SET last=?, count=count+1 WHERE hid=?", (time.time(), h))
        return False
    sql("INSERT INTO requests(hid,host,status,last,count) VALUES(?,?,'pending',?,1)", (h, host, time.time()))
    return True


# ---- settings / users ----
def db_save_start():
    kv_set("start", {"text": START["text"], "photo": START["photo"]})


def db_save_web():
    kv_set("web", {"items": WEB_VIDS})


def db_save_force():
    kv_set("force", {"on": FORCE["on"], "channels": FORCE["channels"]})


def db_save_refer():
    kv_set("refer", {"points": REF["points"]})


def db_save_user(uid: int, data: dict):
    r = sql("SELECT data FROM tg_users WHERE id=?", (uid,), "one")
    cur = json.loads(r["data"]) if r else {}
    cur.update(data)
    sql(
        "INSERT INTO tg_users(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
        (uid, json.dumps(cur)),
    )


def db_get_user(uid):
    return sql("SELECT salt, hash FROM web_users WHERE uid=?", (uid,), "one")


def db_create_user(uid, salt, h):
    sql("INSERT INTO web_users(uid,salt,hash,created) VALUES(?,?,?,?)", (uid, salt, h, time.time()))


def db_count_users():
    return sql("SELECT COUNT(*) AS n FROM web_users", (), "one")["n"]


def default_items() -> list[dict]:
    """Built-in DEFAULT_VIDEOS + admin Web Start videos, no duplicates, random order."""
    out, seen = [], set()
    for title, url in DEFAULT_VIDEOS + [(x.get("title", ""), x["url"]) for x in WEB_VIDS]:
        if url not in seen:
            seen.add(url)
            out.append({"url": url, "title": title})
    random.shuffle(out)
    return out


async def run_link_file(kw: str):
    """Calls link_file.on_search(kw) in a thread. The links it saves into the DB are picked up by the next /api/feed."""
    SEARCHING[kw] = time.time()
    try:
        fn = getattr(link_file, "on_search", None)
        if not fn:
            log.warning("link_file.on_search not found, nothing fetched for search=%s", kw)
        else:
            log.info("request: search=%s", kw)
            await asyncio.wait_for(asyncio.to_thread(fn, kw), LINK_TIMEOUT)
    except asyncio.TimeoutError:
        log.warning("link_file.on_search(%s) took more than %ds", kw, LINK_TIMEOUT)
    except Exception:
        log.exception("link_file.on_search(%s) failed", kw)
    finally:
        SEARCHING.pop(kw, None)
        LAST_ASK[kw] = time.time()


def ask_link_file(request, uid: str, kw: str) -> str:
    """'busy' (link_file is working on kw) | 'started' | 'later' (asked a moment ago, nothing came) | 'limit'."""
    now = time.time()
    if kw in SEARCHING:
        return "busy"
    if now - LAST_ASK.get(kw, 0) < ASK_COOLDOWN:
        return "later"
    ASKS[uid] = [t for t in ASKS.get(uid, []) if now - t < 3600]
    if len(ASKS[uid]) >= MAX_NEW_SEARCH or len(SEARCHING) >= MAX_SEARCHING:
        return "limit"
    ASKS[uid].append(now)
    task = asyncio.create_task(run_link_file(kw))
    request.app["tasks"].add(task)
    task.add_done_callback(request.app["tasks"].discard)
    return "started"


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
        is_new = await asyncio.to_thread(db_note_request, host)
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
    await asyncio.to_thread(db_create_user, uid, salt, h)
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
        user = await asyncio.to_thread(db_get_user, uid)
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


async def api_feed(request):
    host, err = await gate(request)
    if err:
        return err
    auth = request.headers.get("Authorization", "")
    uid = check_token(auth[7:], host) if auth.startswith("Bearer ") else None
    if not uid:
        return jerr(401, "bad_token")
    data = await read_json(request)
    kw = norm_kw(data.get("cat") or START_SEARCH)  # "random" or a search keyword
    if not kw:
        return jerr(400, "bad_search")
    try:
        n = max(1, min(30, int(data.get("n", 10))))
    except (TypeError, ValueError):
        n = 10
    ex = data.get("exclude")
    exclude = [int(x) for x in ex[:500] if str(x).isdigit()] if isinstance(ex, list) else []
    items, reset = await asyncio.to_thread(db_pick, kw, n, exclude)
    searching = False
    if items and kw != "random" and time.time() - TOPUP.get(kw, 0) > TOPUP_EVERY and await asyncio.to_thread(db_needs_topup, kw):
        TOPUP[kw] = time.time()  # links are about to expire: fetch new ones in the background, user keeps watching
        ask_link_file(request, uid, kw)
    if not items and kw != "random":  # nothing for this keyword yet -> request = "search=<kw>"
        state = ask_link_file(request, uid, kw)
        if state == "limit":
            return jerr(429, "search_limit")
        searching = state in ("started", "busy")
    return web.json_response(
        {
            "ok": True,
            "reset": reset,
            "searching": searching,
            "items": [
                {"id": x["id"], "url": x["url"], "page": x.get("page") or "", "title": clean_title(x["title"]), "cat": x["search"]}
                for x in items
            ],
        }
    )


RESOLVING: dict[str, asyncio.Lock] = {}  # page -> lock (one yt-dlp run per page at a time)
RESOLVE_LOG: dict[str, list[float]] = {}  # uid -> times of fresh resolves


async def api_resolve(request):
    """POST {page, force?} -> {url, exp}: fresh .m3u8 for a saved video page (favorites keep only the page url).
    Result is cached for LINK_TTL (1 hr); exp = epoch ms when it expires."""
    host, err = await gate(request)
    if err:
        return err
    auth = request.headers.get("Authorization", "")
    uid = check_token(auth[7:], host) if auth.startswith("Bearer ") else None
    if not uid:
        return jerr(401, "bad_token")
    data = await read_json(request)
    page = str(data.get("page") or "").strip()[:500]
    if not link_file or not hasattr(link_file, "resolve_page") or not link_file.allowed_page(page):
        return jerr(400, "bad_page")
    force = bool(data.get("force"))
    if len(RESOLVING) > 500:
        RESOLVING.clear()
    async with RESOLVING.setdefault(page, asyncio.Lock()):
        if not force:
            hit = await asyncio.to_thread(db_get_resolved, page)
            if hit:
                return web.json_response({"ok": True, "url": hit[0], "exp": int(hit[1] * 1000)})
        now = time.time()
        RESOLVE_LOG[uid] = [t for t in RESOLVE_LOG.get(uid, []) if now - t < 3600]
        if len(RESOLVE_LOG[uid]) >= MAX_RESOLVE:
            return jerr(429, "resolve_limit")
        RESOLVE_LOG[uid].append(now)
        try:
            url = await asyncio.wait_for(asyncio.to_thread(link_file.resolve_page, page), RESOLVE_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("resolve_page timed out: %s", page)
            url = None
        except Exception:
            log.exception("resolve_page failed: %s", page)
            url = None
        if not url:
            return jerr(502, "resolve_failed")
        exp = await asyncio.to_thread(db_set_resolved, page, url)
    return web.json_response({"ok": True, "url": url, "exp": int(exp * 1000)})


async def api_defaults(request):
    """Videos shown when the website starts: built-in DEFAULT_VIDEOS + Web Start, shuffled."""
    host, err = await gate(request)
    if err:
        return err
    return web.json_response({"ok": True, "items": default_items()})


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
    links = db_total_links()
    fj = "🟢 ON" if FORCE["on"] and FORCE["channels"] else "🔴 OFF"
    return (
        f"🛠 ADMIN PANEL\n{LINE}\n"
        f"👥 Users: {len(TGU)}  (🚫 {blocked})\n"
        f"🔗 Links: {links}   🎬 Web start: {len(DEFAULT_VIDEOS) + len(WEB_VIDS)}\n"
        f"🌐 Domains: {len(DOMAINS)}\n"
        f"🔒 Force join: {fj}   🎁 Refer: +{REF['points']} pts\n"
        f"{LINE}\nKya karna hai? 👇"
    )


def main_kb():
    return kb(
        [
            [B("🗑 Link Remove", "lr"), B("🎬 Web Start", "ws:0")],
            [B("💬 Start Msg", "sm"), B("📣 Broadcast", "bc")],
            [B("🔒 Force Join", "fj"), B("🎁 Refer System", "rf")],
            [B("🌐 Domain", "dm"), B("📨 Request", "rq:0")],
            [B("📊 Status", "st"), B("👁 User View", "uv")],
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


def kw_kb(prefix):
    """One button per keyword that has links in the DB (callback carries a hash of the keyword)."""
    btn = [B(f"🔎 {short(k['search'], 18)} ({k['n']})", f"{prefix}:{hid_of(k['search'])}") for k in db_keywords()[:40]]
    rows = [btn[i : i + 2] for i in range(0, len(btn), 2)]
    rows.append(BACK)
    return kb(rows)


def kw_by_hid(h: str) -> str | None:
    return next((k["search"] for k in db_keywords() if hid_of(k["search"]) == h), None)


def tag(kw):
    return f"🔎 {kw}"


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
        await asyncio.to_thread(db_save_user, user.id, {**u, "joined": time.time()})
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
        await asyncio.to_thread(db_save_user, user.id, changed)
    return u, False


async def credit_referral(bot, uid: int):
    """Gives the referrer their points, once per invited user (after force join is passed)."""
    u = TGU.get(uid)
    if not u or not u.get("ref_by") or u.get("credited"):
        return
    u["credited"] = True
    await asyncio.to_thread(db_save_user, uid, {"credited": True})
    r = TGU.get(u["ref_by"])
    if not r:
        return
    r["points"] += REF["points"]
    r["refs"] += 1
    await asyncio.to_thread(db_save_user, u["ref_by"], {"points": r["points"], "refs": r["refs"]})
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


# ------------------------------------------------------------------ admin: link remove (by keyword)
def preview_lines(items: list[dict], limit=6) -> str:
    rows = [f"#{num_of(x)} {short(x['title'] or x['url'], 38)}" for x in items[:limit]]
    more = f"\n… +{len(items) - limit} aur" if len(items) > limit else ""
    return "\n".join(rows) + more


async def do_remove(items: list[dict]):
    await asyncio.to_thread(db_delete_links, [x["id"] for x in items])


async def admin_link_remove(update, context, cmd, parts):
    ud = context.user_data
    if cmd == "lr":
        ud.clear()
        if not db_keywords():
            await show(update, f"🗑 Link Remove\n{LINE}\nDB abhi khali hai. Koi link nahi.", kb([BACK]))
            return
        await show(update, f"🗑 Link Remove\n{LINE}\nKis search se remove karna hai? 👇", kw_kb("lrc"))
        return
    kw = kw_by_hid(parts[1]) if len(parts) > 1 else ud.get("kw")
    if cmd in ("lre2", "lre3"):
        kw = ud.get("kw")
    if not kw:
        await show(update, "Ye search ab DB mein nahi hai.", kb([[B("⬅️ Back", "lr")]]))
        return
    h = hid_of(kw)
    if cmd == "lrc":
        ud.clear()
        n = db_count(kw)
        await show(
            update,
            f"🗑 {tag(kw)} · {n} links\n{LINE}\nKitne remove karne hain? 👇\n(naye wale pehle hatenge)",
            kb(
                [
                    [B("🗑 Last 1", f"lrn:{h}:1"), B("🗑 Last 50", f"lrn:{h}:50")],
                    [B(f"🧨 All ({n})", f"lrn:{h}:all")],
                    [B("🎯 Link / Number se", f"lro:{h}")],
                    [B("⬅️ Back", "lr")],
                ]
            ),
        )
    elif cmd in ("lrn", "lrn2", "lrn3"):
        mode = parts[2]
        if mode not in ("1", "50", "all"):
            return
        items = db_newest(kw, None if mode == "all" else int(mode))
        if not items:
            await show(update, "Remove karne ke liye kuch nahi bacha.", kb([[B("⬅️ Back", "lr")]]))
            return
        if cmd == "lrn":
            await show(
                update,
                f"⚠️ Confirm (1/2)\n\n{tag(kw)} se {len(items)} link remove karna hai?\n\n{preview_lines(items)}",
                kb([[B("✅ Haan", f"lrn2:{h}:{mode}"), B("⬅️ Nahi", f"lrc:{h}")]]),
            )
        elif cmd == "lrn2":
            await show(
                update,
                f"🚨 Final confirm (2/2)\n\n{tag(kw)} ke {len(items)} link PERMANENT delete honge.\nYe wapas nahi aa sakta.",
                kb([[B(f"🗑 Delete {len(items)}", f"lrn3:{h}:{mode}"), B("❌ Cancel", f"lrc:{h}")]]),
            )
        else:
            await do_remove(items)
            await show(
                update,
                f"✅ {len(items)} link remove ho gaye\n{tag(kw)}: ab {db_count(kw)} links",
                kb([[B("⬅️ Back", "lr"), B("🏠 Menu", "m")]]),
            )
    elif cmd == "lro":
        ud.clear()
        ud["kw"], ud["mode"] = kw, "rm_one"
        await show(
            update,
            f"🎯 {tag(kw)}\n\nNumber (jaise #15) ya link bhejo.\nEk se zyada bhi chalega, har line mein ek.",
            kb([[B("⬅️ Back", f"lrc:{h}")]]),
        )
    elif cmd in ("lre2", "lre3"):
        ids = ud.get("rm_ids")
        if not ids:
            await show(update, "Session khatam. Dubara try karo.", kb([[B("⬅️ Back", "lr")]]))
            return
        items = [x for x in db_newest(kw, None) if x["id"] in ids]
        if cmd == "lre2":
            await show(
                update,
                f"🚨 Final confirm (2/2)\n\n{len(items)} link PERMANENT delete honge.\nYe wapas nahi aa sakta.",
                kb([[B(f"🗑 Delete {len(items)}", "lre3"), B("❌ Cancel", f"lrc:{h}")]]),
            )
        else:
            await do_remove(items)
            ud.clear()
            await show(
                update,
                f"✅ {len(items)} link remove ho gaye\n{tag(kw)}: ab {db_count(kw)} links",
                kb([[B("⬅️ Back", "lr"), B("🏠 Menu", "m")]]),
            )


async def rm_one_input(update, context, text):
    ud = context.user_data
    kw = ud.get("kw")
    nav = kb([[B("⬅️ Back", f"lrc:{hid_of(kw)}"), B("🏠 Menu", "m")]])
    nums = {int(n) for n in re.findall(r"(?<![\w/])#?(\d{1,9})(?![\w/])", " ".join(l for l in text.splitlines() if not URL_RE.search(l)))}
    urls = {u.rstrip(").,;]}>") for u in URL_RE.findall(text)}
    items = db_match(kw, nums, urls)
    if not items:
        await update.message.reply_text("❌ Koi match nahi mila. Number ya sahi link bhejo.", reply_markup=nav)
        return
    ud["rm_ids"] = {x["id"] for x in items}
    ud.pop("mode", None)
    await update.message.reply_text(
        f"⚠️ Confirm (1/2)\n\n{tag(kw)} se {len(items)} link remove karna hai?\n\n{preview_lines(items)}",
        reply_markup=kb([[B("✅ Haan", "lre2"), B("⬅️ Nahi", f"lrc:{hid_of(kw)}")]]),
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
        f"Website khulte hi {len(DEFAULT_VIDEOS)} built-in videos + ye videos random order mein chalte hain.\n\n"
        + ("\n".join(lines) if lines else "Abhi koi extra video nahi. Sirf built-in videos chal rahe hain.")
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
            await asyncio.to_thread(db_save_web)
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
            f"🚨 Final confirm (2/2)\n\n{len(WEB_VIDS)} videos delete honge. Website phir sirf built-in videos dikhayegi.",
            kb([[B("🗑 Delete all", "wsx3"), B("❌ Cancel", "ws:0")]]),
        )
    elif cmd == "wsx3":
        WEB_VIDS.clear()
        await asyncio.to_thread(db_save_web)
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
        await asyncio.to_thread(db_save_web)
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
        await asyncio.to_thread(db_save_start)
        await show_start_msg(update)
    elif cmd == "smr":
        START["text"] = ""
        await asyncio.to_thread(db_save_start)
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
    await asyncio.to_thread(db_save_start)
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
                    await asyncio.to_thread(db_save_user, uid, {"blocked": True})
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
        await asyncio.to_thread(db_save_force)
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
            await asyncio.to_thread(db_save_force)
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
    await asyncio.to_thread(db_save_force)
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
            await asyncio.to_thread(db_remove_domain, host)
        await show(update, f"🗑 Removed: {host or '-'}", kb([[B("⬅️ Back", "dm"), B("🏠 Menu", "m")]]))


async def render_requests(update: Update, page: int):
    rows, has_next = await asyncio.to_thread(db_list_requests, page)
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
        host = await asyncio.to_thread(db_get_host, parts[1])
        p = parts[2]
        await show(
            update,
            f"✅ Approve karna hai? (1/2)\n\n{host}",
            kb([[B("✅ Haan", f"ra2:{parts[1]}:{p}"), B("⬅️ Back", f"rq:{p}")]]),
        )
    elif cmd == "ra2":
        host = await asyncio.to_thread(db_get_host, parts[1])
        p = parts[2]
        await show(
            update,
            f"⚠️ Final confirm (2/2)\n\n{host}\n\nApprove hote hi is domain ki web ko data milne lagega.",
            kb([[B("✅ Confirm approve", f"ra3:{parts[1]}:{p}"), B("❌ Cancel", f"rq:{p}")]]),
        )
    elif cmd == "ra3":
        host = await asyncio.to_thread(db_get_host, parts[1])
        if host:
            await asyncio.to_thread(db_add_domain, host)
            DOMAINS.add(host)
        await render_requests(update, int(parts[2]))
    elif cmd == "rr":
        host = await asyncio.to_thread(db_get_host, parts[1])
        if host:
            DOMAINS.discard(host)
            await asyncio.to_thread(db_remove_domain, host)
        await render_requests(update, int(parts[2]))


async def admin_status(update, context):
    context.user_data.clear()
    kws = db_keywords()
    total = sum(k["n"] for k in kws)
    lines = [f"{tag(k['search'])}: {k['n']}" for k in kws[:15]] or ["DB abhi khali hai"]
    if len(kws) > 15:
        lines.append(f"… +{len(kws) - 15} aur search")
    pending = await asyncio.to_thread(db_pending_count)
    web_users = await asyncio.to_thread(db_count_users)
    blocked = sum(1 for x in TGU.values() if x.get("blocked"))
    refs = sum(x.get("refs", 0) for x in TGU.values())
    working = ", ".join(SEARCHING) or "-"
    text = (
        f"📊 Status\n{LINE}\n" + "\n".join(lines) + f"\n\n🔗 Total links: {total}  ({len(kws)} search)\n"
        f"⏳ link_file kaam kar raha hai: {working}\n"
        f"🎬 Web start videos: {len(DEFAULT_VIDEOS)} built-in + {len(WEB_VIDS)} extra\n"
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
        await asyncio.to_thread(db_add_domain, host)
        DOMAINS.add(host)
        await msg.reply_text(f"✅ Domain added:\n{host}", reply_markup=nav)
    elif mode == "smsg":
        if not text:
            await msg.reply_text("✏️ Text bhejo.")
            return
        START["text"] = text[:3500]
        await asyncio.to_thread(db_save_start)
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
        await asyncio.to_thread(db_save_refer)
        ud.pop("mode", None)
        await msg.reply_text(
            f"✅ Ab har refer par {REF['points']} points milenge.", reply_markup=kb([[B("🎁 Refer", "rf"), B("🏠 Menu", "m")]])
        )
    elif mode == "rm_one" and ud.get("kw"):
        await rm_one_input(update, context, text)
    elif mode == "ws_add":
        data = await read_input(update)
        if data is not None:
            await web_ingest(update, context, data)
    else:
        await msg.reply_text("👇 Pehle option chuno", reply_markup=main_kb())


# ------------------------------------------------------------------ startup
async def purge_loop():
    """Every 2 min: scraped links older than LINK_TTL (1 hr) are deleted."""
    while True:
        try:
            n = await asyncio.to_thread(purge_expired)
            if n:
                log.info("expired links removed: %d", n)
        except Exception:
            log.exception("purge failed")
        await asyncio.sleep(120)


async def post_init(app: Application):
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
            web.post("/api/resolve", api_resolve),
        ]
    )
    app.bot_data["purge"] = asyncio.create_task(purge_loop())
    runner = web.AppRunner(api)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    app.bot_data["runner"] = runner
    log.info("Web API listening on :%d", PORT)


async def post_shutdown(app: Application):
    task = app.bot_data.get("purge")
    if task:
        task.cancel()
    runner = app.bot_data.get("runner")
    if runner:
        await runner.cleanup()


def main():
    for name, val in (("BOT_TOKEN", BOT_TOKEN), ("KEY_64", KEY_64)):
        if "PASTE_" in val:
            raise SystemExit(f"Set {name} (environment variable or at the top of bot.py).")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_user_button, pattern=r"^u:"))  # everybody
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(?!u:)"))  # admin only
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE & ~filters.COMMAND, on_message))
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
