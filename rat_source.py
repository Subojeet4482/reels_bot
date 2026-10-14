"""
rat_source.py  -  rat.xxx source (download-link based)

bot.py calls:
    on_search("rat:<keyword>")      -> SHORT videos: scrape search results, resolve each download page to a direct
                                       file url, save into the SAME `links` table (search = "rat:<keyword>")
    on_search("rat:long:<keyword>") -> LONG videos only (search = "rat:long:<keyword>")
    resolve_page(page_url)        -> fresh direct file url for a saved download page (used by Favorites + Download button)
    allowed_page(page_url)        -> only https://*.rat.xxx/download/... pages

Page url format that is saved in `links.page`:
    https://www.rat.xxx/download/16424

NOTE: selectors are GENERIC (the site could not be inspected). If a search returns nothing, adjust
SEARCH_URLS / parse_results() / pick_file() below.
"""
import html as htmllib
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import link_file  # reuses save_links / needs_links / has_page (same DB, same 1 hr expiry)

PREFIX = "rat:"
LONG_PREFIX = "rat:long:"
LONG_SEC = 300             # >= 5 min = "Long", shorter = "Short"
LONG_BYTES = 60_000_000    # used only when the duration is unknown: >= ~60 MB = "Long"
BASE = "https://www.rat.xxx"
HOST_RE = re.compile(r"(^|\.)rat\.xxx$", re.I)

# {plus} = keyword with "+" for spaces. Tried in this order, the first one that gives results is used.
SEARCH_URLS = [
    BASE + "/search/{plus}/",       # https://www.rat.xxx/search/ret+xxx+full/
    BASE + "/search/?q={plus}",     # https://www.rat.xxx/search/?q=ret+xxx+full
]
TARGET = 20          # max new videos per search
BUDGET = 90          # seconds (must stay below bot.py LINK_TIMEOUT = 120)
PAGES = 3            # result pages to read (page 1, 2, 3 ...)
PAGES_LONG = 6       # Long mode reads more result pages (fewer long videos per page)
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


def is_long(duration, url):
    """True = Long, False = Short, None = could not tell."""
    if duration is not None:
        return duration >= LONG_SEC
    size = file_size(url)
    return None if size is None else size >= LONG_BYTES


def log(*a):
    print("[rat_source]", *a, flush=True)


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
    return bool(u.scheme == "https" and u.hostname and HOST_RE.search(u.hostname) and "/download/" in u.path)


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
    """[(title, download_page_url, duration_sec_or_None), ...] from a search-result page."""
    p = _Anchors()
    try:
        p.feed(html)
    except Exception:
        pass
    out, seen = [], {}
    for it in p.items:
        u = urllib.parse.urljoin(base, it["href"].strip())
        pu = urllib.parse.urlparse(u)
        if not pu.hostname or not HOST_RE.search(pu.hostname) or SKIP_PATH.search(pu.path):
            continue
        m = re.search(r"/download/(\d+)", pu.path) or re.search(r"(\d{3,})", pu.path)  # numeric video id
        if not m:
            continue
        page = f"{BASE}/download/{m.group(1)}"
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
    """Yields (title, download_page_url) from the first search url form that works."""
    for tpl in SEARCH_URLS:
        got_any = False
        for n in range(1, pages + 1):
            if time.time() > deadline:
                return
            url = _page_url(tpl, plus, n)
            try:
                html, final, _ = fetch(url)
            except Exception as e:
                log("search failed:", url, str(e)[:80])
                break
            items = parse_results(html, final)
            log(f"{url} -> {len(items)} results")
            if not items:
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


def resolve_page(page_url):
    """Download page url -> direct file url (None if nothing found)."""
    if not allowed_page(page_url):
        return None
    try:
        html, final, ct = fetch(page_url)
    except Exception as e:
        log("resolve failed", page_url, str(e)[:80])
        return None
    if not html:  # the download url redirected straight to the file
        return final
    url = pick_file(html, final)
    if not url:
        log("no file url found on", page_url)
    return url


# ------------------------------------------------------------------ bot request
def on_search(keyword):
    """Bot request: search=rat:<keyword>. Saves every video as soon as it is resolved."""
    key = (keyword or "").strip().lower()
    if not key.startswith(PREFIX):
        key = PREFIX + key
    want_long = key.startswith(LONG_PREFIX)
    plain = key[len(LONG_PREFIX if want_long else PREFIX):].strip()
    if not plain:
        return
    log("request:", key, "(long)" if want_long else "(short)")
    if not link_file.needs_links(key):
        log("enough fresh links, skipping")
        return
    deadline = time.time() + BUDGET
    plus = urllib.parse.quote_plus(plain)
    saved = tried = 0
    skipped = 0
    for title, page, dur in result_pages(plus, deadline, PAGES_LONG if want_long else PAGES):
        if saved >= TARGET or time.time() > deadline:
            break
        if link_file.has_page(key, page):
            continue
        # duration known from the result card -> decide before resolving (saves requests)
        if dur is not None and (dur >= LONG_SEC) != want_long:
            skipped += 1
            continue
        tried += 1
        url = resolve_page(page)
        if not url:
            continue
        if dur is None and not re.search(r"\.m3u8", url, re.I):
            kind = is_long(None, url)
            if kind is True and not want_long or kind is False and want_long:
                skipped += 1
                continue
            if kind is None and want_long:  # cannot prove it is long -> not in the Long list
                skipped += 1
                continue
        saved += link_file.save_links(key, [(title, url, page)])
        time.sleep(0.3)
    log(f"{key}: tried {tried}, saved {saved}, skipped {skipped} (wrong length)")
