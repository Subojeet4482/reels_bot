"""
rat_source.py  -  rat.xxx source (KVS site)

bot.py calls:
    on_search("rat:<keyword>")    -> scrape search results, keep ONLY LONG videos, resolve each video page to a
                                     direct .mp4 url, save into the SAME `links` table (search = "rat:<keyword>")
    resolve_page(page_url)        -> fresh direct .mp4 url for a saved video page (Favorites + Download + stream proxy)
    allowed_page(page_url)        -> only https://*.rat.xxx/videos/... pages
    REFERER                       -> rat.xxx has hotlink protection: every request for the .mp4 file MUST carry
                                     "Referer: https://www.rat.xxx/". bot.py's /api/stream proxy adds it.

How it works (same as the working command-line script):
    search   : https://www.rat.xxx/search/?q=<kw>&page=N        (links that contain /videos/)
    resolve  : read the video page html and take the .mp4 url
               (video_url / iphone_url / file = '...mp4', then <source src>, then any .mp4 link);
               yt-dlp is the fallback when the html has nothing.

Page url format that is saved in `links.page`:
    https://www.rat.xxx/videos/29369/some-title/
"""
import html as htmllib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import link_file  # reuses save_links / needs_links / has_page (same DB, same 1 hr expiry)

PREFIX = "rat:"
LONG_MIN_SEC = 300              # only videos longer than 5 min are kept (everything big comes, no upper limit)
LONG_MIN_BYTES = 60_000_000     # used only when the duration is unknown: >= ~60 MB counts as long
BASE = "https://www.rat.xxx"
REFERER = BASE + "/"
HOST_RE = re.compile(r"(^|\.)rat\.xxx$", re.I)

# {plus} = keyword with "+" for spaces. Tried in this order, the first one that gives results is used.
SEARCH_URLS = [
    BASE + "/search/?q={plus}",     # https://www.rat.xxx/search/?q=ret+xxx+full&page=2   (works, used first)
    BASE + "/search/{plus}/",       # fallback form
]
TARGET = 20          # max new videos per search
BUDGET = 90          # seconds (must stay below bot.py LINK_TIMEOUT = 120)
PAGES = 6            # result pages to read (page 1, 2, 3 ...). Many pages, because only long videos are kept
PAUSE = 0.8          # seconds between requests
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": BASE + "/",
}
SKIP_PATH = re.compile(r"/(search|category|categories|tag|tags|page|login|register|model|models|channel|channels|user|users)(/|$)", re.I)
FILE_RE = re.compile(
    r"""(?:(?:https?:)?//|(?<=["'=(])/)[^\s"'<>\\]+?\.(?:mp4|m3u8|webm)(?:\?[^\s"'<>\\]*)?""", re.I
)


DUR_CLOCK = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])")
DUR_MIN = re.compile(r"(?<!\d)(\d{1,3})\s*(?:min|mins|minutes)\b", re.I)


def parse_duration(text):
    """'12:34' / '1:02:03' / '12 min' found in a result card -> seconds (None if there is no duration)."""
    m = DUR_CLOCK.search(text or "")
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
        return a * 3600 + b * 60 + int(c) if c is not None else a * 60 + b
    m = DUR_MIN.search(text or "")
    return int(m.group(1)) * 60 if m else None


def file_size(url):
    """Size in bytes of a direct file url (HEAD, then a 1-byte range request). None if unknown."""
    for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        try:
            req = urllib.request.Request(url, headers={**HEADERS, **extra}, method=method)
            with urllib.request.urlopen(req, timeout=6) as r:
                cr = r.headers.get("Content-Range") or ""
                if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                    return int(cr.rsplit("/", 1)[1])
                cl = r.headers.get("Content-Length")
                if cl and cl.isdigit() and int(cl) > 1:
                    return int(cl)
        except Exception:
            continue
    return None


def is_long(duration=None, size=None):
    """True = long enough, False = too short, None = unknown."""
    if duration is not None:
        return duration >= LONG_MIN_SEC
    if size is not None:
        return size >= LONG_MIN_BYTES
    return None


def log(*a):
    print("[rat_source]", *a, flush=True)


def err(*a):
    """Errors show in the Render logs as '[rat_source] ERROR ...' so they are easy to find."""
    print("[rat_source] ERROR", *a, flush=True)


def why(e):
    """Short readable reason for an exception (HTTP code, timeout, ...)."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code} {e.reason}"
    return f"{type(e).__name__}: {str(e)[:100]}"


BLOCK_WORDS = ("just a moment", "cf-chl", "captcha", "access denied", "attention required", "verify you are human")


def blocked_hint(html):
    h = (html or "")[:6000].lower()
    return next((w for w in BLOCK_WORDS if w in h), "")


def fetch(url, timeout=15):
    """Returns (html, final_url, content_type)."""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ct = (r.headers.get("Content-Type") or "").lower()
        final = r.geturl()
        if ct.startswith("video/") or "octet-stream" in ct or "mpegurl" in ct:
            return "", final, ct  # the url IS the file, do not download the body
        return r.read(3_000_000).decode("utf-8", errors="ignore"), final, ct


def allowed_page(url):
    try:
        u = urllib.parse.urlparse(url or "")
    except Exception:
        return False
    return bool(u.scheme == "https" and u.hostname and HOST_RE.search(u.hostname) and ("/videos/" in u.path or "/download/" in u.path))


# ------------------------------------------------------------------ search results
class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items, self._cur = [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self._cur = {"href": a["href"], "title": (a.get("title") or "").strip(), "text": ""}
            self.items.append(self._cur)
        elif tag == "img" and self._cur is not None and not self._cur["title"]:
            self._cur["title"] = (a.get("alt") or "").strip()

    def handle_data(self, d):
        if self._cur is not None and len(self._cur["text"]) < 200:
            self._cur["text"] += d

    def handle_endtag(self, tag):
        if tag == "a":
            self._cur = None


def parse_results(html, base):
    """[(title, video_page_url, duration_sec_or_None), ...] from a search-result page (links that contain /videos/)."""
    p = _Anchors()
    try:
        p.feed(html)
    except Exception:
        pass
    out, seen = [], {}
    for it in p.items:
        u = urllib.parse.urljoin(base, it["href"].strip())
        if "&amp;" in u:
            continue
        pu = urllib.parse.urlparse(u)
        if not pu.hostname or not HOST_RE.search(pu.hostname) or "/videos/" not in pu.path:
            continue
        page = f"{BASE}{pu.path}"  # no query / fragment, always https://www.rat.xxx
        text = re.sub(r"\s+", " ", it["text"]).strip()
        dur = parse_duration(text)
        if page in seen:  # same video, another link (thumbnail + title): keep the duration if this one has it
            k = seen[page]
            if out[k][2] is None and dur is not None:
                out[k] = (out[k][0], page, dur)
            continue
        title = it["title"] or DUR_CLOCK.sub("", text).strip()
        seen[page] = len(out)
        out.append((title[:120], page, dur))
    return out


def _page_url(template, plus, n):
    u = template.format(plus=plus)
    if n <= 1:
        return u
    return u + ("&" if "?" in u else "?") + f"page={n}"


def result_pages(plus, deadline, pages=PAGES):
    """Yields (title, video_page_url, duration) from the first search url form that works."""
    for tpl in SEARCH_URLS:
        got_any = False
        first = None
        for n in range(1, pages + 1):
            if time.time() > deadline:
                return
            url = _page_url(tpl, plus, n)
            try:
                html, final, _ = fetch(url)
            except Exception as e:
                err("search request failed:", url, "->", why(e))
                break
            items = parse_results(html, final)
            log(f"{url} -> {len(items)} results")
            if not items:
                hint = blocked_hint(html)
                err(f"search gave 0 videos: {url} (page size {len(html)} bytes"
                    + (f", looks like an anti-bot / block page: '{hint}'" if hint else ", no /videos/ links in the html") + ")")
            if not items:
                break
            sig = {i[1] for i in items[:5]}
            if n == 1:
                first = sig
            elif sig == first:  # server looped back to page 1 = no more results
                break
            got_any = True
            for it in items:
                yield it
            time.sleep(PAUSE)
        if got_any:
            return


# ------------------------------------------------------------------ download page -> direct file
def pick_file(html, base):
    """Best direct file url on a download page: mp4 first, then highest quality number in the url."""
    html = htmllib.unescape(html.replace("\\/", "/"))
    cands = []
    for m in FILE_RE.finditer(html):
        u = urllib.parse.urljoin(base, m.group(0))
        q = re.search(r"(?<!\d)(2160|1440|1080|720|480|360|240)(?!\d)", u)
        is_mp4 = ".mp4" in u.lower()
        cands.append((is_mp4, int(q.group(1)) if q else 0, u))
    return max(cands)[2] if cands else None


KVS_VAR = re.compile(r"""(?:video_url|iphone_url|file)\s*[:=]\s*["'](https?://[^"']+\.mp4[^"']*)["']""")
KVS_SOURCE = re.compile(r"""<source\s+[^>]*src=["'](https?://[^"']+\.mp4[^"']*)["']""")
ANY_MP4 = re.compile(r"""https?://[^\s<>"']+\.mp4[^\s<>"']*""")


def mp4_from_html(html):
    """The three patterns of the command-line script. Returns the .mp4 url or None."""
    html = html.replace("\\/", "/")  # json-escaped slashes: https:\/\/cdn... -> https://cdn...
    m = KVS_VAR.search(html) or KVS_SOURCE.search(html)
    if m:
        return m.group(1).replace("\\/", "/")
    links = [l.replace("\\/", "/") for l in ANY_MP4.findall(html)]
    if links:
        good = [l for l in links if "preview" not in l and "trailer" not in l]  # skip thumbnail / preview mp4s
        return (good or links)[0]
    return None


def mp4_from_ytdlp(page_url):
    """Fallback when the page html has no mp4 url."""
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"format": "best", "quiet": True, "no_warnings": True, "skip_download": True,
                               "http_headers": {"Referer": REFERER, "User-Agent": HEADERS["User-Agent"]}}) as ydl:
            info = ydl.extract_info(page_url, download=False)
        u = info.get("url")
        if not u and info.get("formats"):
            u = info["formats"][-1].get("url")
        return u
    except Exception as e:
        err("yt-dlp failed:", page_url, "->", str(e)[:160])
        return None


def resolve_page(page_url):
    """Video page url -> direct .mp4 url (None if nothing found). Fast html patterns first, yt-dlp as fallback."""
    if not allowed_page(page_url):
        return None
    url, html = None, ""
    try:
        html, final, ct = fetch(page_url)
        if not html:  # the url redirected straight to the file
            return final
        url = mp4_from_html(htmllib.unescape(html)) or pick_file(html, final)
    except Exception as e:
        err("video page request failed:", page_url, "->", why(e))
        html = ""
    if not url:
        url = mp4_from_ytdlp(page_url)
    if not url:
        hint = blocked_hint(html)
        err("no .mp4 url found on", page_url, f"(html {len(html)} bytes" + (f", block page: '{hint}'" if hint else "") + ", yt-dlp also found nothing)")
    return url


# ------------------------------------------------------------------ bot request
def on_search(keyword):
    """Bot request: search=rat:<keyword>. Saves every video as soon as it is resolved."""
    key = (keyword or "").strip().lower()
    if not key.startswith(PREFIX):
        key = PREFIX + key
    plain = key[len(PREFIX):].strip()
    if not plain:
        return
    log("request:", key)
    if not link_file.needs_links(key):
        log("enough fresh links, skipping")
        return
    deadline = time.time() + BUDGET
    plus = urllib.parse.quote_plus(plain)
    saved = tried = 0
    skipped = 0
    for title, page, dur in result_pages(plus, deadline):
        if saved >= TARGET or time.time() > deadline:
            break
        if link_file.has_page(key, page):
            continue
        # duration known from the result card -> decide before resolving (saves requests)
        if dur is not None and not is_long(dur):
            skipped += 1
            continue
        tried += 1
        url = resolve_page(page)
        if not url:
            continue
        if dur is None:
            size = None if re.search(r"\.m3u8", url, re.I) else file_size(url)
            if size is None:
                err("cannot read the file size (needed to know if it is long):", page)
            if not is_long(size=size):  # too short, or cannot prove it is long
                skipped += 1
                continue
        saved += link_file.save_links(key, [(title, url, page)])
        time.sleep(0.3)
    log(f"{key}: tried {tried}, saved {saved}, skipped {skipped} (not long)")
    if saved == 0:
        err(f"{key}: 0 videos saved (tried {tried}, skipped {skipped} not long). Look at the ERROR lines above for the reason.")
