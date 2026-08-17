# 3D Printing Inventory Telegram Bot — Setup Guide

This bot reads and updates your **Filament Inventory** Google Sheet from Telegram:
check remaining stock, log filament used, log new purchases, and see spending
totals. It needs three things before it can run: a Telegram bot token, a Google
Sheet the bot can edit, and somewhere to host it so it stays online. Follow the
steps in order — none of them require coding.

## 1. Put the spreadsheet on Google Sheets

If you haven't already:

1. Go to [drive.google.com](https://drive.google.com) and upload
   `3D Printing - Filament & Inventory.xlsx`.
2. Right-click the uploaded file → **Open with** → **Google Sheets**. This
   converts it to a live Google Sheet (keep the original .xlsx as a backup;
   the bot only talks to the new Google Sheet).
3. Copy the **Spreadsheet ID** from the address bar. It's the long string
   between `/d/` and `/edit`:
   `https://docs.google.com/spreadsheets/d/`**`1AbCDEfGhIjKlMnOpQrStUvWxYz`**`/edit`
   Save this — it goes in `SPREADSHEET_ID` later.

## 2. Create the Telegram bot

1. In Telegram, message **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot`, give it a name and a username (must end in `bot`, e.g.
   `mya3dprintbot`).
3. BotFather replies with a **token** that looks like
   `123456789:AAExample...`. Save it — this goes in `TELEGRAM_BOT_TOKEN`.
4. Message **[@userinfobot](https://t.me/userinfobot)** to get your own
   Telegram numeric user ID. Save it — this goes in `TELEGRAM_ALLOWED_USER_IDS`
   so only you can use the bot (recommended, since it can edit your business
   spreadsheet).

## 3. Give the bot access to your Sheet (Google service account)

The bot needs its own Google credentials — separate from your personal Google
login — so it can edit the sheet unattended.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and
   create a new project (or reuse one), e.g. "3D Printing Bot".
2. In the search bar, find **Google Sheets API** and click **Enable**.
3. Go to **APIs & Services → Credentials → Create Credentials → Service
   account**. Give it any name (e.g. `inventory-bot`), then **Create and
   continue**, skip the optional role/access steps, and click **Done**.
4. Click the new service account → **Keys** tab → **Add Key → Create new
   key → JSON**. This downloads a `.json` file — keep it private, it's a
   credential. Its contents go in `GOOGLE_CREDENTIALS_JSON`.
5. Open that JSON file and copy the `client_email` value (looks like
   `inventory-bot@your-project.iam.gserviceaccount.com`).
6. Open your Google Sheet → **Share** → paste that email address → give it
   **Editor** access → **Send** (uncheck "notify" if it asks, this isn't
   a real inbox). Without this step the bot gets a permissions error.

## 4. Deploy the bot so it stays online 24/7

**Railway** (recommended — free tier, simplest):

1. Create the four files in this folder (`bot.py`, `requirements.txt`,
   `Procfile`, and this README) into a new **GitHub repository** — easiest
   way is to create a repo on github.com, then drag-and-drop-upload these
   files via the GitHub web UI (no git command line needed).
2. Go to [railway.app](https://railway.app) and sign up (GitHub login is
   easiest).
3. **New Project → Deploy from GitHub repo** → pick the repo you just made.
4. Once it's created, open the service → **Variables** tab, and add each
   one from `.env.example`:
   - `TELEGRAM_BOT_TOKEN`
   - `SPREADSHEET_ID`
   - `GOOGLE_CREDENTIALS_JSON` (paste the *entire* JSON file contents as
     one value)
   - `TELEGRAM_ALLOWED_USER_IDS`
   - `LOW_STOCK_THRESHOLD_KG` (optional)
5. Railway auto-detects the `Procfile` and runs `python bot.py` as a
   worker. Check the **Deployments** tab for logs — you should see
   `Bot starting...` with no errors.

**Render** works the same way (New → Background Worker → connect the repo →
same environment variables → start command `python bot.py`); either is fine.

## 5. Test it

Open a chat with your bot in Telegram and try:

```
/start
/stock charcoal
/lowstock
/used matte charcoal 200
/summary
/add
```

## Notes

- Only the **Filament Inventory** and **Summary** tabs are read by the bot
  today; `/add` only logs filament (not equipment/tools) to keep the guided
  flow short — add those manually in the sheet, or ask to extend the bot.
- `/used` updates the "Weight Used (kg)" column; "Weight Remaining (kg)" is
  a live formula in the sheet and recalculates automatically.
- Keep `TELEGRAM_ALLOWED_USER_IDS` set. Without it, anyone who finds your
  bot's username on Telegram can read and edit your inventory.
- Never commit `GOOGLE_CREDENTIALS_JSON` or `TELEGRAM_BOT_TOKEN` to a
  public GitHub repo — use a private repo, or set them only as host
  environment variables (never in code).
