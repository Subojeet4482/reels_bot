# Reels bot (Stream + Rat source)

Files
- `bot.py`          Telegram admin bot + website backend (one process)
- `link_file.py`    Stream source (HLS, yt-dlp)
- `rat_source.py`   Rat source (rat.xxx download links, mp4)
- `web/index.html`  Website (UI)
- `render.yaml`     Render blueprint
- `requirements.txt`

## Render par deploy
1. Ye folder GitHub repo me push karo.
2. Render > New > Blueprint > repo chuno (render.yaml apne aap settings bhar dega).
   Manual: Web Service, Build `pip install -r requirements.txt`, Start `python bot.py`,
   Health check `/health`, Disk mount `/var/data`, env `DB_PATH=/var/data/data.db`.
3. Environment me daalo: `BOT_TOKEN` (@BotFather), `ADMIN_ID` (@userinfobot), `KEY_64` (random, baad me change mat karna).
4. Deploy ke baad site `https://<name>.onrender.com`. `WEB_URL` apne aap `RENDER_EXTERNAL_URL` se aa jata hai.
5. Telegram me bot ko `/start` karo (admin panel).

Free plan: Disk nahi milta (data.db har deploy par mitega) aur service so jati hai (Telegram polling band ho jata hai).
Bot ke liye `starter` plan chahiye.

## Website
- PIN (4 digit, apna banao) dalo -> menu, search, Favs, Source, Download khulte hain.
- **Source** button: `Stream` (purana HLS) <-> `Rat` (rat.xxx download videos).
- **Rat source** me sirf LAMBE videos aate hain (5 min se zyada, upar koi limit nahi). Random me bhi lambe hi.
- **Download** button: sirf Rat videos par; seedha file ka link kholta hai. 
- Search box me rat.xxx link paste kar sakte ho:
  - `https://www.rat.xxx/download/16424`           -> wo video chalega + download
  - `https://www.rat.xxx/search/?q=ret+xxx+full`   -> "ret xxx full" search (Rat source)
  - `https://www.rat.xxx/search/ret+xxx+full/`     -> same

## Rat scraper tune karna
Site ka HTML mujhe check karne ko nahi mila, isliye `rat_source.py` generic hai.
Search se kuch na aaye to logs me `[rat_source]` lines dekho aur `SEARCH_URLS`, `parse_results()`, `pick_file()` adjust karo.

## Long videos tune karna
`rat_source.py` ke top me `LONG_MIN_SEC` (300 = 5 min) aur duration na mile to `LONG_MIN_BYTES` (file size, 60 MB) badal sakte ho.
Sirf wahi video aati hai jiska length pakka pata chale. Kam aaye to `PAGES` badhao.

## rat.xxx: Referer proxy
rat.xxx par hotlink protection hai: .mp4 file ko `Referer: https://www.rat.xxx/` chahiye, browser ye bhej nahi sakta.
Isliye rat videos `/api/stream` se chalte aur download hote hain: server Referer laga ke file laata hai aur browser ko bhejta hai (seek / Range chalta hai).
Search: `https://www.rat.xxx/search/?q=<keyword>&page=N`, video pages `/videos/...`. mp4 pehle page ke html se, na mile to yt-dlp se.
Note: video ka data aapke server se guzarta hai, isliye Render ka bandwidth use hota hai.

## Like / Dislike, dekhe hue videos, saved (admin ke liye)
**Website (PIN ke baad):** video par neeche-right me 👍 Like, 👎 Dislike aur ⬇️ Download buttons. Dislike dabate hi agla video aata hai.
- Liked video user ke feed me sabse upar, disliked sabse last me.
- Jo video 3 sec se zyada dekha, wo server me save hota hai (`seen`) aur agle din dobara nahi aata. Liked video is rule se alag hai (wo upar aate rehte hain).
- Saved (favorite) videos server me bhi save hote hain, taaki admin sab ke saved videos dekh sake.

**Admin (Telegram) > 👍 Likes / 👎 Dislikes / 💾 Saved:**
- 👍 Likes: kis video me kitne likes / dislikes / saves.
- 💾 Saved: sab users ke saved videos (kitne users ne save kiya).
- 👎 Dislikes: har video par ♻️ (phir se start: dislike hat jata hai) ya 🚫 (poora ban: kisi ko nahi dikhega). Sab ke liye bhi button hai.
- 🚫 Banned: banned videos ki list, yahan se ✅ Unban.
- ☑️ "Purane videos dobara dikhao" tick: ON = dekhe hue videos dobara aayenge, OFF = nahi aayenge.

## Logs me Rat ke errors
Render logs me `[rat_source] ERROR ...` (search 0 result / page nahi khula / mp4 nahi mila / anti-bot page) aur `RAT stream: ...` / `RAT resolve ...` (file ka HTTP status, 403 etc.) dikhte hain. Token logs me nahi aata.

## Video ki length kaise pata chalti hai (Rat)
1) search card, 2) video page ka meta / JSON-LD / "Duration" (ab ye pehle chalta hai), 3) file size (HEAD / Range request). 
Teeno na mile to `rat_source.py` me `KEEP_UNKNOWN = True` hone par video phir bhi save hota hai (log me "length unknown, kept anyway"); `False` karoge to skip hoga.

## Render free aur bandwidth
Render free par mahine ka outbound bandwidth bahut kam hai (docs ke hisaab se 5 GB; purane sources 100 GB likhte hain, apne dashboard ka "Outbound Bandwidth" graph dekho).
Isliye Rat videos pehle SEEDHE rat.xxx se browser me chalte / download hote hain (server se kuch nahi guzarta).
Seedha na chale (file server IP se locked / block) to browser khud server proxy (/api/stream) par aa jata hai aur yaad rakhta hai (2 fail ke baad seedha proxy).
Proxy ka data count hota hai; `PROXY_MAX_MB` (default 3000) ke baad proxy band, taaki free service suspend / bill na ho. Admin > Status me "Server se gaya video data" dikhta hai.
Seedha link dobara try karwana ho to browser me site data clear karo (localStorage `direct`).
