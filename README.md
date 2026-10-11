# Reels bot (Telegram POLLING) + website, one server

reels-bot/
  bot.py          Telegram bot (polling) + backend API + serves the website
  web/index.html  the player, served at /
  requirements.txt, render.yaml, .env.example

Polling needs an always-on process, so host it on Render / Railway / a VPS (NOT Vercel).
The website is served by the same server, so it needs no domain approval.

## Deploy on Render
1. Push this folder to GitHub (bot.py in the repo root).
2. Render > New > Web Service > the repo.  Build: pip install -r requirements.txt   Start: python bot.py
3. Environment: BOT_TOKEN, ADMIN_ID, KEY_64, FIREBASE_B64  (PORT is automatic)
   KEY_64:       python -c "import secrets;print(secrets.token_hex(32))"   (do not change it later)
   FIREBASE_B64: base64 -w0 serviceAccount.json
4. Open https://YOUR-SERVICE.onrender.com/health -> ok ; https://YOUR-SERVICE.onrender.com/ -> the player.
5. Free plan sleeps after ~15 min without web traffic, and then the bot stops answering.
   Add a free ping to /health every 5 min (UptimeRobot / cron-job.org).
6. Telegram: /start

## Website users
- Default videos play first. Key button -> the user creates THEIR OWN 4-digit PIN (typed twice).
  The account id is saved in that browser; next time the key button asks for that PIN.
- Key button again = back to default. Forgot PIN -> "Create a new one" (a new account).
- The PIN is only a personal lock. Who may use your server is controlled by domain approval.

## Bot
Admin /start:  Link Add | Domain | Request | Status | Start Msg
Normal user /start: sees the message you set under "Start Msg".

## Website on another domain (optional, e.g. Vercel)
Put web/index.html there with  const API_BASE = 'https://YOUR-SERVICE.onrender.com';
First use -> "New domain request" in Telegram -> Request > ✅ > confirm twice.
