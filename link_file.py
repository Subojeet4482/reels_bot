"""
link_file.py  -  bot is file ko call karta hai:  on_search(keyword)

==================================================================================
1) REQUEST FORMAT
==================================================================================
Website par search / menu button  ->  agar DB mein us keyword ka ek bhi link nahi, bot yahan bhejta hai:

    search=india      search=japan      search=russia      search=cute
    search=<user ka keyword>            e.g.  search=goa trip

on_search(keyword) ko sirf keyword milta hai ("india", "goa trip" ...), lowercase, max 40 chars.
Bot ise alag thread mein chalata hai, max 120 sec (bot.py: LINK_TIMEOUT). Random button ki request nahi aati.

(Rat source ke liye keyword "rat:<keyword>" hota hai, wo rat_source.py mein jata hai.)

==================================================================================
2) LINK KAHAN SAVE HOTA HAI
==================================================================================
DB    : SQLite  data.db  (bot.py ke saath; env DB_PATH se badal sakte ho)
Table : links(id, search, title, url, added)   UNIQUE(search, url)
        search = wahi keyword jo request mein aaya.  url = .m3u8 (HLS) link, player sirf HLS chalata hai.

Har link milte hi save hota hai, isliye pehla video kuch second mein website par aa jata hai
(website har 4 sec mein DB check karti hai).

==================================================================================
3) YE FILE KYA KARTI HAI
==================================================================================
keyword -> "<keyword> popular video" search -> RANDOM result pages (1..total) -> video pages -> yt-dlp se .m3u8 stream -> save_links()
 - sirf .m3u8 save hote hain (mp4 player mein nahi chalta)
 - random / unrelated fallback NAHI: kuch nahi mila to keyword khali rehta hai ("No videos found")
 - time budget: BUDGET sec ke baad apne aap ruk jata hai (bot ke 120 sec timeout se pehle)
Settings niche hain: DOMAINS, TARGET, BUDGET, QUERY_SUFFIX, PAGES_PER_SEARCH.
"""
import os
import random
import re
import sqlite3
import time
import urllib.parse
import urllib.request

DB_PATH = os.getenv("DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

DOMAINS = [  # pehla jo chale wahi use hota hai
    "https://xhamster46.desi",
    "https://xhamster.com",
]
LINK_TTL = int(os.getenv("LINK_TTL") or 3600)  # seconds: stream link 1 hr baad expire (bot.py bhi yehi env padhta hai)
MIN_FRESH = 5  # keyword me itne se kam fresh links ho to naye lao
REFRESH_AFTER = int(LINK_TTL * 2 / 3)  # keyword ka sabse naya link itna purana ho gaya to purane expire hone se pehle naye lao (top-up)
TARGET = 20  # ek keyword ke max naye links
BUDGET = 100  # seconds (bot.py LINK_TIMEOUT = 120 se kam rakho)
QUERY_SUFFIX = " popular video"  # "india" -> site par "india popular video" search hota hai (DB me keyword "india" hi rehta hai)
PAGES_PER_SEARCH = 6  # ek search me itne RANDOM result pages uthao (1..total me se) -> har baar alag videos
DEFAULT_TOTAL_PAGES = 10  # agar site se total pages na mile to itne maano
PAGE_CAP = 40  # random page kabhi isse upar nahi (site deep pages block kar sakti hai)
PAGE_PAUSE = 1.0  # seconds between page requests (block hone se bachne ke liye)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def log(*a):
    print("[link_file]", *a, flush=True)


def ensure_schema(con):
    """Old data.db me `page` column / `resolved` table nahi hota, yahan add ho jata hai."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(links)")]
    if cols and "page" not in cols:
        con.execute("ALTER TABLE links ADD COLUMN page TEXT DEFAULT ''")
    con.execute("CREATE TABLE IF NOT EXISTS resolved(page TEXT PRIMARY KEY, url TEXT NOT NULL, ts REAL)")


def save_links(keyword, links):
    """links = [(title, url) ya (title, url, page_url), ...]  ->  table `links`, search=keyword.
    page_url = video ka main page (favorites me wahi save hota hai, stream url 1 hr me expire hota hai).
    Returns how many NEW links were saved."""
    now, added = time.time(), 0
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        ensure_schema(con)
        con.execute("DELETE FROM links WHERE added < ?", (now - LINK_TTL,))  # expired links hatao
        for item in links:
            title, url = item[0], item[1]
            page = item[2] if len(item) > 2 else ""
            if page and con.execute("SELECT 1 FROM links WHERE search=? AND page=?", (keyword, page)).fetchone():
                continue  # ye video is keyword me abhi fresh hai (top-up me dobara nahi)
            cur = con.execute(
                "INSERT OR IGNORE INTO links(search, title, url, added, page) VALUES (?, ?, ?, ?, ?)",
                (keyword, (title or "")[:120], url, now, page or ""),
            )
            added += cur.rowcount
            if page:  # abhi nikla hua stream = resolve cache (favorite turant chalega)
                con.execute("INSERT OR REPLACE INTO resolved(page, url, ts) VALUES (?, ?, ?)", (page, url, now))
        con.commit()
    finally:
        con.close()
    return added


def needs_links(keyword):
    """True = keyword ke fresh links kam hain ya sab purane ho rahe hain -> naye scrape karo."""
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        n, newest = con.execute(
            "SELECT COUNT(*), MAX(added) FROM links WHERE search=? AND added > ?", (keyword, time.time() - LINK_TTL)
        ).fetchone()
    except Exception:
        return True
    finally:
        con.close()
    return n < MIN_FRESH or (newest or 0) < time.time() - REFRESH_AFTER


def has_page(keyword, page):
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        return bool(con.execute(
            "SELECT 1 FROM links WHERE search=? AND page=? AND added > ?", (keyword, page, time.time() - LINK_TTL)
        ).fetchone())
    except Exception:
        return False
    finally:
        con.close()


def count_links(keyword):
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM links WHERE search=? AND added > ?", (keyword, time.time() - LINK_TTL)
        ).fetchone()[0]
    except Exception:
        return 0
    finally:
        con.close()


def fetch(url, timeout=12):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise OSError(f"HTTP {resp.status}")
        return resp.read().decode("utf-8", errors="ignore")


def search_query(keyword):
    """'india' -> 'india popular video'. Jo keyword already 'video(s)' pe khatam ho use waise hi rehne do."""
    k = re.sub(r"\s+", " ", (keyword or "").strip().lower())
    return k if re.search(r"\bvideos?$", k) else k + QUERY_SUFFIX


def total_pages(html):
    """Search page ke pagination se sabse bada page number (nahi mila to None)."""
    nums = [int(n) for n in re.findall(r"[?&]page=(\d{1,4})", html)]
    return max(nums) if nums else None


def page_videos(html, base, host):
    out, seen = [], set()
    for href in re.findall(r'href="((?:https?://[^"/]+)?/videos/[^"#?]+)"', html):
        u = urllib.parse.urljoin(base + "/", href)
        if urllib.parse.urlparse(u).netloc == host and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def video_pages(keyword, deadline):
    """Yields video page URLs from RANDOM search-result pages (1..total), mixed across pages, no duplicates.
    Pehle 'india popular video' search hota hai; kuch na mile to sirf 'india'."""
    queries = [search_query(keyword)]
    plain = (keyword or "").strip().lower()
    if plain and plain != queries[0]:
        queries.append(plain)
    for query in queries:
        q = urllib.parse.quote_plus(query)
        base, first = None, None
        for d in DOMAINS:  # first domain that answers
            try:
                first = fetch(f"{d}/search/{q}")
                base = d
                break
            except Exception as e:
                log("domain failed:", d, e)
        if not base:
            return
        host = urllib.parse.urlparse(base).netloc
        first_vids = page_videos(first, base, host)
        if not first_vids:
            continue  # is query ka result nahi -> agla query
        total = min(total_pages(first) or DEFAULT_TOTAL_PAGES, PAGE_CAP)
        picks = random.sample(range(1, total + 1), min(PAGES_PER_SEARCH, total))
        log(f'query="{query}" total_pages={total} picked={sorted(picks)}')
        pools, seen = [], set()
        for page in picks:
            if time.time() > deadline:
                break
            try:
                vids = first_vids if page == 1 else page_videos(fetch(f"{base}/search/{q}?page={page}"), base, host)
            except Exception as e:
                log("page", page, "failed:", e)
                continue
            vids = [u for u in vids if u not in seen]
            seen.update(vids)
            random.shuffle(vids)
            if vids:
                pools.append(vids)
            if page != 1:
                time.sleep(PAGE_PAUSE)
        if not pools and first_vids:  # random pages fail hue -> page 1 se kaam chalao
            pools = [random.sample(first_vids, len(first_vids))]
        random.shuffle(pools)
        while any(pools):  # round-robin: har page se ek-ek video, taaki sab pages ke videos mix hon
            for pool in pools:
                if pool:
                    yield pool.pop()
        return


def best_m3u8(info):
    """Picks an HLS (.m3u8) url from a yt-dlp info dict. Master playlist first, else the tallest variant."""
    cands = []
    for f in info.get("formats") or []:
        u = f.get("url") or ""
        proto = str(f.get("protocol") or "")
        if ".m3u8" in u or proto.startswith("m3u8"):
            cands.append((f.get("height") or 0, f.get("manifest_url") or u))
    if cands:
        masters = [c for c in cands if ".m3u8" in c[1] and c[1] != ""]
        pool = masters or cands
        return max(pool, key=lambda c: c[0])[1]
    u = info.get("url") or ""
    return u if ".m3u8" in u else None


def on_search(keyword):
    """Bot request: search=<keyword>. Finds .m3u8 streams and saves each one as soon as it is found."""
    try:
        import yt_dlp
    except ImportError:
        log("yt-dlp is not installed (add `yt-dlp` to requirements.txt)")
        return
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return
    log(f"request: search={keyword}")
    if not needs_links(keyword):
        log("enough fresh links in DB, skipping")
        return

    deadline = time.time() + BUDGET
    opts = {
        "format": "best[protocol^=m3u8]/best",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 15,
        "extractor_retries": 1,
    }
    saved = tried = 0
    with yt_dlp.YoutubeDL(opts) as ydl:
        for page_url in video_pages(keyword, deadline):
            if saved >= TARGET or time.time() > deadline:
                break
            if has_page(keyword, page_url):  # top-up: ye video already fresh hai, yt-dlp time waste nahi
                continue
            tried += 1
            try:
                info = ydl.extract_info(page_url, download=False)
            except Exception as e:
                log("skip", page_url, str(e)[:80])
                continue
            m3u8 = best_m3u8(info or {})
            if m3u8:
                saved += save_links(keyword, [((info.get("title") or ""), m3u8, page_url)])
    log(f"search={keyword}: tried {tried} pages, saved {saved} links")


def allowed_page(url):
    """Sirf apne DOMAINS ke https video pages resolve hote hain (koi bhi random url nahi)."""
    try:
        u = urllib.parse.urlparse(url or "")
    except Exception:
        return False
    if u.scheme != "https" or not u.hostname or "/videos/" not in u.path:
        return False
    host = u.hostname.lower()
    for d in DOMAINS:
        dh = urllib.parse.urlparse(d).hostname
        if dh and (host == dh or host.endswith("." + dh)):
            return True
    return False


def resolve_page(page_url):
    """Video page url -> naya .m3u8 stream url (None agar nahi mila). Bot /api/resolve me thread me chalata hai."""
    if not allowed_page(page_url):
        return None
    try:
        import yt_dlp
    except ImportError:
        log("yt-dlp is not installed")
        return None
    opts = {
        "format": "best[protocol^=m3u8]/best",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 15,
        "extractor_retries": 1,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(page_url, download=False)
        return best_m3u8(info or {})
    except Exception as e:
        log("resolve failed", page_url, str(e)[:80])
        return None
