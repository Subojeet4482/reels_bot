# Reels bot (Telegram POLLING) + website, one server. No Firebase.

  bot.py          Telegram bot (polling) + backend API + serves the website + SQLite (data.db)
  link_file.py    YOUR link-fetch code. Bot calls on_search(keyword) here. Format + DB are documented inside.
  web/index.html  the player, served at /
  requirements.txt, render.yaml

Polling needs an always-on process: Render / Railway / VPS (NOT Vercel).

## Deploy on Render
1. Push this folder to GitHub (bot.py in the repo root).
2. Render > New > Web Service. Build: pip install -r requirements.txt   Start: python bot.py
3. Environment: BOT_TOKEN, ADMIN_ID, KEY_64 (PORT is automatic)
   KEY_64: python -c "import secrets;print(secrets.token_hex(32))"   (do not change it later)
   WEB_URL: optional (Render sets RENDER_EXTERNAL_URL itself) -> "Watch Videos" button in the bot.
4. /health -> ok ; / -> the player. Add a free ping to /health every 5 min (UptimeRobot) on the free plan.
5. IMPORTANT: data.db (links, PIN users, bot users, settings) lives on the server disk.
   Render free plan wipes the disk on every deploy/restart. For permanent data use a Render Disk
   (paid) and set DB_PATH=/var/data/data.db, or a VPS.

## How the website works
- Opens with the 25 built-in videos (DEFAULT_VIDEOS in bot.py) + Admin > Web Start videos, RANDOM order.
- Key button -> user's own 4-digit PIN -> INDIA videos (random) start.
- Search button (top right, after PIN) + menu (Random / India / Japan / Russia / Cute).
- Every search is a request:  search=india | search=japan | search=<what the user typed>
  1. Links for that keyword already in the DB -> bot returns them (random), nothing else happens.
  2. None yet -> bot calls link_file.on_search(keyword) in a thread. Your code finds links and saves them
     into table `links` (link_file.save_links does it). The page asks again every 4 s and plays them.
  Limits: 10 new keywords / hour / web user, 5 keywords at once, same empty keyword retried after 2 min.

## link_file.py
Ready: searches the site (DOMAINS), takes .m3u8 streams with yt-dlp, saves each link as found.
Settings at the top: TARGET (links per keyword), BUDGET (sec), MAX_PAGES. Empty result = "No videos found".
DB: data.db, table links(id, search, title, url, added), UNIQUE(search, url).

## Bot: admin panel (/start as admin)
- Link Remove: pick a search keyword -> last 1 / last 50 / all / by number or link (2x confirm).
  (There is no Link Add any more: links come from link_file.py.)
- Web Start: extra start videos (added to the built-in 25). Start Msg, Broadcast, Force Join,
  Refer System, Domain / Request / Status, User View.

## Bot: users
/start shows welcome (photo + text) and buttons: Watch Videos, Refer & Earn, Balance, Leaderboard.
Refer link: https://t.me/<bot>?start=ref_<id>. Leaderboard = top 10 by refers.

## Website on another domain (optional, e.g. Vercel)
Put web/index.html there with  const API_BASE = 'https://YOUR-SERVICE.onrender.com';
First use -> "New domain request" in Telegram -> Request > ✅ > confirm twice.
