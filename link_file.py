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
keyword -> site ka search page (kuch pages) -> video pages -> yt-dlp se .m3u8 stream -> save_links()
 - sirf .m3u8 save hote hain (mp4 player mein nahi chalta)
 - random / unrelated fallback NAHI: kuch nahi mila to keyword khali rehta hai ("No videos found")
 - time budget: BUDGET sec ke baad apne aap ruk jata hai (bot ke 120 sec timeout se pehle)
Settings niche hain: DOMAINS, TARGET, BUDGET, MAX_PAGES.
"""
import os
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
TARGET = 20  # ek keyword ke max naye links
BUDGET = 100  # seconds (bot.py LINK_TIMEOUT = 120 se kam rakho)
MAX_PAGES = 5  # search pages
PAGE_PAUSE = 1.0  # seconds between page requests (block hone se bachne ke liye)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def log(*a):
    print("[link_file]", *a, flush=True)


def save_links(keyword, links):
    """links = [(title, url), ...]  ->  table `links`, search=keyword. Returns how many NEW links were saved."""
    now, added = time.time(), 0
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        for title, url in links:
            cur = con.execute(
                "INSERT OR IGNORE INTO links(search, title, url, added) VALUES (?, ?, ?, ?)",
                (keyword, (title or "")[:120], url, now),
            )
            added += cur.rowcount
        con.commit()
    finally:
        con.close()
    return added


def count_links(keyword):
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        return con.execute("SELECT COUNT(*) FROM links WHERE search=?", (keyword,)).fetchone()[0]
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


def video_pages(query, deadline):
    """Yields video page URLs from the site's search results (page by page, no duplicates)."""
    q = urllib.parse.quote_plus(query.strip())
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
    seen, prev_sig = set(), None
    for page in range(1, MAX_PAGES + 1):
        if time.time() > deadline:
            return
        try:
            html = first if page == 1 else fetch(f"{base}/search/{q}?page={page}")
        except Exception as e:
            log("page", page, "failed:", e)
            return
        found = []
        for href in re.findall(r'href="((?:https?://[^"/]+)?/videos/[^"#?]+)"', html):
            u = urllib.parse.urljoin(base + "/", href)
            if urllib.parse.urlparse(u).netloc == host and u not in seen:
                seen.add(u)
                found.append(u)
        sig = tuple(found[:5])
        if not found or sig == prev_sig:  # empty page or same as before = end of results
            return
        prev_sig = sig
        for u in found:
            yield u
        time.sleep(PAGE_PAUSE)


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
    if count_links(keyword):
        log("links already in DB, skipping")
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
            tried += 1
            try:
                info = ydl.extract_info(page_url, download=False)
            except Exception as e:
                log("skip", page_url, str(e)[:80])
                continue
            m3u8 = best_m3u8(info or {})
            if m3u8:
                saved += save_links(keyword, [((info.get("title") or ""), m3u8)])
    log(f"search={keyword}: tried {tried} pages, saved {saved} links")
