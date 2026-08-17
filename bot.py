"""
Telegram bot for the 3D printing filament & inventory Google Sheet.

Commands:
  /start, /help          - show usage
  /stock <search>        - remaining weight for a material/color
  /lowstock [kg]         - list spools at or below a threshold (default from
                            LOW_STOCK_THRESHOLD_KG, falls back to 0.2 kg)
  /used <search> <grams> - log filament consumed (updates "Weight Used (kg)")
  /add                   - guided flow to log a new filament purchase
  /summary               - totals from the Summary tab
  /cancel                - abort an in-progress /add flow

Reads/writes the same Google Sheet tabs produced by the original workbook:
  "Filament Inventory", "Equipment & Other Items", "Summary"

Required environment variables (see .env.example):
  TELEGRAM_BOT_TOKEN
  SPREADSHEET_ID
  GOOGLE_CREDENTIALS_JSON        (service account key, as one-line JSON)
Optional:
  TELEGRAM_ALLOWED_USER_IDS      (comma-separated Telegram user IDs; if unset,
                                   ANYONE who finds the bot can use it)
  LOW_STOCK_THRESHOLD_KG         (default 0.2)
"""

import json
import logging
import os
from datetime import date
from functools import wraps

import gspread
from google.oauth2.service_account import Credentials
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
logger = logging.getLogger("inventory-bot")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip()
}
LOW_STOCK_THRESHOLD_KG = float(os.environ.get("LOW_STOCK_THRESHOLD_KG", "0.2"))

FILAMENT_SHEET = "Filament Inventory"
EQUIPMENT_SHEET = "Equipment & Other Items"
SUMMARY_SHEET = "Summary"
HEADER_ROW = 4  # row 4 in the workbook holds the column headers
WEIGHT_USED_COL = 10  # column J
FORMAT_COL_INDEX = 5  # column F, 0-indexed (used to find the "TOTALS" marker row)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# --------------------------------------------------------------------------
# Google Sheets helpers
# --------------------------------------------------------------------------
_gc = None


def get_sheet_client():
    global _gc
    if _gc is None:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        _gc = gspread.authorize(creds)
    return _gc


def get_ws(name):
    gc = get_sheet_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(name)


def parse_float(value, default=0.0):
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return default


def read_filament_rows(ws):
    """Return each data row (below the header) as a dict, skipping the
    order-level-discount adjustment row and the TOTALS row."""
    values = ws.get_all_values()
    headers = values[HEADER_ROW - 1]
    rows = []
    for i, row in enumerate(values[HEADER_ROW:], start=HEADER_ROW + 1):
        if not any(cell.strip() for cell in row):
            continue
        material = row[3] if len(row) > 3 else ""
        fmt = row[5] if len(row) > 5 else ""
        if material.strip().lower() == "order-level discount":
            continue
        if fmt.strip().upper() == "TOTALS":
            continue
        record = dict(zip(headers, row))
        record["_row"] = i
        rows.append(record)
    return rows


# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------
def restricted(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if ALLOWED_USER_IDS and (user is None or user.id not in ALLOWED_USER_IDS):
            await update.message.reply_text("Sorry, this bot is private.")
            logger.warning("Blocked unauthorized user %s", user.id if user else "unknown")
            return
        return await func(update, context)

    return wrapper


# --------------------------------------------------------------------------
# Basic commands
# --------------------------------------------------------------------------
@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "3D printing inventory bot.\n\n"
        "/stock <search> - remaining filament, e.g. /stock charcoal\n"
        "/lowstock [kg] - spools running low (default "
        f"{LOW_STOCK_THRESHOLD_KG:.2f} kg)\n"
        "/used <search> <grams> - log filament consumed, e.g. /used charcoal matte 200\n"
        "/add - log a new purchase\n"
        "/summary - spending summary\n"
        "/cancel - cancel an /add in progress"
    )


@restricted
async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip().lower()
    if not query:
        await update.message.reply_text("Usage: /stock <material or color>, e.g. /stock charcoal")
        return
    ws = get_ws(FILAMENT_SHEET)
    rows = read_filament_rows(ws)
    matches = [
        r
        for r in rows
        if query in r.get("Material", "").lower() or query in r.get("Color", "").lower()
    ]
    if not matches:
        await update.message.reply_text(f"No filament matching '{query}'.")
        return
    lines = []
    for r in matches:
        remaining = parse_float(r.get("Weight Remaining (kg)"))
        lines.append(
            f"{r.get('Material')} - {r.get('Color')} ({r.get('Format')}): "
            f"{remaining:.2f} kg remaining"
        )
    await update.message.reply_text("\n".join(lines))


@restricted
async def lowstock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    threshold = LOW_STOCK_THRESHOLD_KG
    if context.args:
        threshold = parse_float(context.args[0], LOW_STOCK_THRESHOLD_KG)
    ws = get_ws(FILAMENT_SHEET)
    rows = read_filament_rows(ws)
    low = [r for r in rows if parse_float(r.get("Weight Remaining (kg)")) <= threshold]
    if not low:
        await update.message.reply_text(f"Nothing at or below {threshold:.2f} kg remaining.")
        return
    lines = [f"Low stock (<= {threshold:.2f} kg):"]
    for r in low:
        remaining = parse_float(r.get("Weight Remaining (kg)"))
        lines.append(f"- {r.get('Material')} {r.get('Color')} ({r.get('Format')}): {remaining:.2f} kg")
    await update.message.reply_text("\n".join(lines))


@restricted
async def used(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /used <search term> <grams>, e.g. /used charcoal matte 200")
        return
    *query_parts, grams_str = context.args
    query = " ".join(query_parts).lower()
    grams = parse_float(grams_str)
    if grams <= 0:
        await update.message.reply_text("Grams must be a positive number.")
        return

    ws = get_ws(FILAMENT_SHEET)
    rows = read_filament_rows(ws)
    matches = [
        r
        for r in rows
        if query in r.get("Material", "").lower() or query in r.get("Color", "").lower()
    ]
    if not matches:
        await update.message.reply_text(f"No filament matching '{query}'.")
        return
    if len(matches) > 1:
        lines = [
            f"{i + 1}. {r.get('Material')} - {r.get('Color')} ({r.get('Format')})"
            for i, r in enumerate(matches)
        ]
        await update.message.reply_text(
            "Multiple matches - be more specific (add material and color), e.g. "
            "/used matte charcoal 200:\n" + "\n".join(lines)
        )
        return

    r = matches[0]
    row_idx = r["_row"]
    prev_used = parse_float(r.get("Weight Used (kg)"))
    new_used = prev_used + grams / 1000.0
    ws.update_cell(row_idx, WEIGHT_USED_COL, round(new_used, 3))
    await update.message.reply_text(
        f"Logged {grams:.0f}g used for {r.get('Material')} - {r.get('Color')}.\n"
        f"Total used so far: {new_used * 1000:.0f}g. Remaining updates automatically in the sheet."
    )


@restricted
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ws = get_ws(SUMMARY_SHEET)
    values = ws.get_all_values()
    lines = ["Purchase summary:"]
    for row in values:
        if len(row) >= 3 and row[0] and row[2] and row[0] != "Category":
            lines.append(f"{row[0]}: {row[2]}")
    await update.message.reply_text("\n".join(lines))


# --------------------------------------------------------------------------
# /add conversation
# --------------------------------------------------------------------------
MATERIAL, COLOR, FORMAT, WEIGHT, QTY, PRICE, RETAILER = range(7)


@restricted
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_row"] = {}
    await update.message.reply_text(
        "Adding a new filament purchase. What material? (e.g. PLA Matte)\n"
        "Send /cancel anytime to stop."
    )
    return MATERIAL


async def add_material(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_row"]["material"] = update.message.text.strip()
    await update.message.reply_text("Color?")
    return COLOR


async def add_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_row"]["color"] = update.message.text.strip()
    await update.message.reply_text(
        "Format?",
        reply_markup=ReplyKeyboardMarkup(
            [["Spool", "Refill"]], one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return FORMAT


async def add_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_row"]["format"] = update.message.text.strip()
    await update.message.reply_text(
        "Weight per unit in kg? (usually 1)", reply_markup=ReplyKeyboardRemove()
    )
    return WEIGHT


async def add_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_row"]["weight"] = parse_float(update.message.text, 1.0)
    await update.message.reply_text("Quantity purchased?")
    return QTY


async def add_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_row"]["qty"] = int(parse_float(update.message.text, 1))
    await update.message.reply_text("Unit price paid, in CAD?")
    return PRICE


async def add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_row"]["price"] = parse_float(update.message.text, 0)
    await update.message.reply_text("Retailer? (e.g. Bambu Lab, Amazon.ca)")
    return RETAILER


async def add_retailer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data["new_row"]
    d["retailer"] = update.message.text.strip()

    ws = get_ws(FILAMENT_SHEET)
    values = ws.get_all_values()
    totals_row_idx = None
    for i, row in enumerate(values, start=1):
        if len(row) > FORMAT_COL_INDEX and row[FORMAT_COL_INDEX].strip().upper() == "TOTALS":
            totals_row_idx = i
            break
    if totals_row_idx is None:
        totals_row_idx = len(values) + 1

    r = totals_row_idx  # the new row will land here; everything below shifts down
    row_values = [
        date.today().isoformat(),
        d["retailer"],
        d["retailer"],
        d["material"],
        d["color"],
        d["format"],
        d["weight"],
        d["qty"],
        f"=G{r}*H{r}",
        0,
        f"=I{r}-J{r}",
        "",
        "",
        d["price"],
        0,
        0,
        f"=N{r}-O{r}+P{r}",
    ]
    ws.insert_row(row_values, index=totals_row_idx, value_input_option="USER_ENTERED")

    await update.message.reply_text(
        f"Added: {d['material']} - {d['color']} ({d['format']}), "
        f"{d['qty']} x {d['weight']}kg @ ${d['price']:.2f} from {d['retailer']}.",
        reply_markup=ReplyKeyboardRemove(),
    )
    context.user_data.pop("new_row", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_row", None)
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(CommandHandler("lowstock", lowstock))
    app.add_handler(CommandHandler("used", used))
    app.add_handler(CommandHandler("summary", summary))

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            MATERIAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_material)],
            COLOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_color)],
            FORMAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_format)],
            WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_weight)],
            QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_qty)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_price)],
            RETAILER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_retailer)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(add_conv)

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
