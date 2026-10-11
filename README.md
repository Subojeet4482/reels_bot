# Reels bot (Telegram POLLING) + website, one server

reels-bot/
  bot.py          Telegram bot (polling) + backend API + serves the website
  web/index.html  the player, served at /
  requirements.txt, render.yaml

Polling needs an always-on process, so host it on Render / Railway / a VPS (NOT Vercel).
The website is served by the same server, so it needs no domain approval.

## Deploy on Render
1. Push this folder to GitHub (bot.py in the repo root).
2. Render > New > Web Service > the repo.  Build: pip install -r requirements.txt   Start: python bot.py
3. Environment: BOT_TOKEN, ADMIN_ID, KEY_64, FIREBASE_B64  (PORT is automatic)
   KEY_64:       python -c "import secrets;print(secrets.token_hex(32))"   (do not change it later)
   FIREBASE_B64: base64 -w0 serviceAccount.json
   WEB_URL:      optional. Public https URL of the site (Render sets RENDER_EXTERNAL_URL itself).
                 It gives users a "Watch Videos" button in the bot.
4. Open https://YOUR-SERVICE.onrender.com/health -> ok ; https://YOUR-SERVICE.onrender.com/ -> the player.
5. Free plan sleeps after ~15 min without web traffic, and then the bot stops answering.
   Add a free ping to /health every 5 min (UptimeRobot / cron-job.org).
6. Telegram: /start

## Website
- Favorites: the heart (top right, where the + was) saves / removes the current video. Max 50.
  The "Favs" button (bottom) opens the list: tap = play, trash = remove one, "Remove all" = 2 taps.
  Favorites are stored in the user's browser.
- Videos that play when the site opens are set in the bot: Admin > Web Start.
  (If that list is empty, the demo videos inside index.html are used.)
- Default videos play first. Key button -> the user creates THEIR OWN 4-digit PIN (typed twice).
  The account id is saved in that browser; next time the key button asks for that PIN.
- Key button again = back to default. Forgot PIN -> "Create a new one" (a new account).

## Bot: admin panel (/start as admin)
- Link Add / Link Remove: remove Last 1, Last 50, All, or by link / number. Every removal asks 2 times.
- Web Start: add / remove the videos shown when the website opens.
- Start Msg: welcome text + banner photo. {name} = user's name. Preview button shows the real thing.
- Broadcast: send any message (text, photo, video...) to all bot users. Preview + confirm, Stop button.
- Force Join: add channels (bot must be admin there), turn ON/OFF. Users must join before using the bot.
- Refer System: set points per refer.
- Domain / Request / Status, User View (see the bot like a normal user).

## Bot: users
/start shows the welcome (photo + text) and buttons: Watch Videos, Refer & Earn, Balance, Leaderboard.
Refer link: https://t.me/<bot>?start=ref_<id>. The referrer gets points once per new user
(after that user passes Force Join, if it is ON). Leaderboard = top 10 by refers.

## Website on another domain (optional, e.g. Vercel)
Put web/index.html there with  const API_BASE = 'https://YOUR-SERVICE.onrender.com';
First use -> "New domain request" in Telegram -> Request > ✅ > confirm twice.
