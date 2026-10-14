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
- **Short / Long** switch: top me (title ke neeche), sirf Rat source me dikhta hai. Short = chhote videos, Long = lambe videos (>= 5 min).
- **Download** button: sirf Rat videos par; seedha file ka link kholta hai. Ab top ke Short/Long bar me bhi hai (green icon) aur neeche rail me bhi.
- Search box me rat.xxx link paste kar sakte ho:
  - `https://www.rat.xxx/download/16424`           -> wo video chalega + download
  - `https://www.rat.xxx/search/?q=ret+xxx+full`   -> "ret xxx full" search (Rat source)
  - `https://www.rat.xxx/search/ret+xxx+full/`     -> same

## Rat scraper tune karna
Site ka HTML mujhe check karne ko nahi mila, isliye `rat_source.py` generic hai.
Search se kuch na aaye to logs me `[rat_source]` lines dekho aur `SEARCH_URLS`, `parse_results()`, `pick_file()` adjust karo.

## Short / Long tune karna
`rat_source.py` ke top me `LONG_SEC` (default 300 = 5 min) aur `LONG_BYTES` (duration na mile to file size se, default 60 MB) badal sakte ho.
Long list me sirf wahi video aati hai jiska length pakka pata chale (result card ki duration ya file size). Long me kam results aaye to `PAGES_LONG` badhao.
DB me Long videos `rat:long:<keyword>` key se save hote hain.
