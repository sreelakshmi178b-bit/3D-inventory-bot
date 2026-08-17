"""
Telegram bot for the 3D printing filament & inventory Google Sheet.

Everyday use is via the button menu shown on /start (Check Stock, Low Stock,
Log Usage, Add Purchase, Summary) - tap a button and answer the follow-up
question. The same slash commands still work underneath for anyone who
prefers typing them directly:

  /start, /help          - show usage and the button menu
  /stock <search>        - remaining weight for a material/color
  /lowstock [kg]         - list spools at or below a threshold (default from
                            LOW_STOCK_THRESHOLD_KG, falls back to 0.2 kg)
  /used <search> <grams> - log filament consumed (updates "Weight Used (kg)")
  /add                   - guided flow to log a new filament purchase
  /summary               - totals from the Summary tab
  /cancel                - abort an in-progress /add flow

Send a photo or PDF of a receipt directly in the chat to have Claude read it
and propose purchase line items to add - you confirm with a button before
anything is written to the sheet.

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
  ANTHROPIC_API_KEY              (needed only for the receipt-photo feature)
  ANTHROPIC_MODEL                (default "claude-sonnet-4-5")
"""

import asyncio
import base64
import json
import logging
import os
import re
import uuid
from datetime import date
from functools import wraps

import gspread
from google.oauth2.service_account import Credentials
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

try:
    import anthropic
except ImportError:  # receipt scanning is optional; rest of the bot still works
    anthropic = None

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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # optional - only for receipt scanning
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

FILAMENT_SHEET = "Filament Inventory"
EQUIPMENT_SHEET = "Equipment & Other Items"
SUMMARY_SHEET = "Summary"
HEADER_ROW = 4  # row 4 in the workbook holds the column headers
WEIGHT_USED_COL = 10  # column J
FORMAT_COL_INDEX = 5  # column F, 0-indexed (used to find the "TOTALS" marker row on the Filament sheet)
EQUIPMENT_ITEM_COL_INDEX = 2  # column C, 0-indexed (used to find "TOTALS" on the Equipment sheet)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# --------------------------------------------------------------------------
# Button menu
# --------------------------------------------------------------------------
BTN_STOCK = "\U0001F4E6 Check Stock"
BTN_LOWSTOCK = "⚠️ Low Stock"
BTN_USED = "\U0001F4DD Log Usage"
BTN_ADD = "➕ Add Purchase"
BTN_SUMMARY = "\U0001F4B0 Summary"

MAIN_MENU = ReplyKeyboardMarkup(
    [[BTN_STOCK, BTN_LOWSTOCK], [BTN_USED, BTN_ADD], [BTN_SUMMARY]],
    resize_keyboard=True,
)

# transient per-chat state for the two button flows that need a follow-up
# answer (Check Stock, Log Usage) — not used by the /add ConversationHandler
AWAITING_STOCK = "awaiting_stock"
AWAITING_USED = "awaiting_used"

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
            logger.warning("Blocked unauthorized user %s", user.id if user else "unknown")
            if update.message:
                await update.message.reply_text("Sorry, this bot is private.")
            elif update.callback_query:
                await update.callback_query.answer("Not authorized.", show_alert=True)
            return
        return await func(update, context)

    return wrapper


def find_totals_row_index(values, marker_col_index, marker_text="TOTALS"):
    """Row number (1-indexed) of the first row whose marker_col_index cell
    equals marker_text - falls back to appending at the end if not found."""
    for i, row in enumerate(values, start=1):
        if len(row) > marker_col_index and row[marker_col_index].strip().upper() == marker_text:
            return i
    return len(values) + 1


# --------------------------------------------------------------------------
# Core logic (shared by both slash commands and the button menu)
# --------------------------------------------------------------------------
def stock_lookup_text(query: str) -> str:
    query = query.strip().lower()
    ws = get_ws(FILAMENT_SHEET)
    rows = read_filament_rows(ws)
    matches = [
        r
        for r in rows
        if query in r.get("Material", "").lower() or query in r.get("Color", "").lower()
    ]
    if not matches:
        return f"No filament matching '{query}'."
    lines = []
    for r in matches:
        remaining = parse_float(r.get("Weight Remaining (kg)"))
        lines.append(
            f"{r.get('Material')} - {r.get('Color')} ({r.get('Format')}): "
            f"{remaining:.2f} kg remaining"
        )
    return "\n".join(lines)


def lowstock_text(threshold: float) -> str:
    ws = get_ws(FILAMENT_SHEET)
    rows = read_filament_rows(ws)
    low = [r for r in rows if parse_float(r.get("Weight Remaining (kg)")) <= threshold]
    if not low:
        return f"Nothing at or below {threshold:.2f} kg remaining."
    lines = [f"Low stock (<= {threshold:.2f} kg):"]
    for r in low:
        remaining = parse_float(r.get("Weight Remaining (kg)"))
        lines.append(f"- {r.get('Material')} {r.get('Color')} ({r.get('Format')}): {remaining:.2f} kg")
    return "\n".join(lines)


def used_apply_text(query: str, grams: float) -> str:
    query = query.strip().lower()
    if grams <= 0:
        return "Grams must be a positive number."

    ws = get_ws(FILAMENT_SHEET)
    rows = read_filament_rows(ws)
    matches = [
        r
        for r in rows
        if query in r.get("Material", "").lower() or query in r.get("Color", "").lower()
    ]
    if not matches:
        return f"No filament matching '{query}'."
    if len(matches) > 1:
        lines = [
            f"{i + 1}. {r.get('Material')} - {r.get('Color')} ({r.get('Format')})"
            for i, r in enumerate(matches)
        ]
        return (
            "Multiple matches - be more specific (add material and color), e.g. "
            "'matte charcoal 200':\n" + "\n".join(lines)
        )

    r = matches[0]
    row_idx = r["_row"]
    prev_used = parse_float(r.get("Weight Used (kg)"))
    new_used = prev_used + grams / 1000.0
    ws.update_cell(row_idx, WEIGHT_USED_COL, round(new_used, 3))
    return (
        f"Logged {grams:.0f}g used for {r.get('Material')} - {r.get('Color')}.\n"
        f"Total used so far: {new_used * 1000:.0f}g. Remaining updates automatically in the sheet."
    )


def summary_text() -> str:
    ws = get_ws(SUMMARY_SHEET)
    values = ws.get_all_values()
    lines = ["Purchase summary:"]
    for row in values:
        if len(row) >= 3 and row[0] and row[2] and row[0] != "Category":
            lines.append(f"{row[0]}: {row[2]}")
    return "\n".join(lines)


def split_query_and_grams(text: str):
    """Split 'charcoal matte 200' into ('charcoal matte', 200.0)."""
    parts = text.strip().split()
    if len(parts) < 2:
        return None, None
    *query_parts, grams_str = parts
    grams = parse_float(grams_str, default=None)
    if grams is None:
        return None, None
    return " ".join(query_parts), grams


# --------------------------------------------------------------------------
# Basic commands
# --------------------------------------------------------------------------
@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(AWAITING_STOCK, None)
    context.user_data.pop(AWAITING_USED, None)
    await update.message.reply_text(
        "3D printing inventory bot. Use the buttons below, or these commands:\n\n"
        "/stock <search> - remaining filament, e.g. /stock charcoal\n"
        "/lowstock [kg] - spools running low (default "
        f"{LOW_STOCK_THRESHOLD_KG:.2f} kg)\n"
        "/used <search> <grams> - log filament consumed, e.g. /used charcoal matte 200\n"
        "/add - log a new purchase\n"
        "/summary - spending summary\n"
        "/cancel - cancel an /add in progress\n\n"
        "You can also just send a photo or PDF of a receipt and I'll read it "
        "and propose what to add.",
        reply_markup=MAIN_MENU,
    )


@restricted
async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip().lower()
    if not query:
        await update.message.reply_text("Usage: /stock <material or color>, e.g. /stock charcoal")
        return
    await update.message.reply_text(stock_lookup_text(query), reply_markup=MAIN_MENU)


@restricted
async def lowstock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    threshold = LOW_STOCK_THRESHOLD_KG
    if context.args:
        threshold = parse_float(context.args[0], LOW_STOCK_THRESHOLD_KG)
    await update.message.reply_text(lowstock_text(threshold), reply_markup=MAIN_MENU)


@restricted
async def used(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /used <search term> <grams>, e.g. /used charcoal matte 200")
        return
    *query_parts, grams_str = context.args
    query = " ".join(query_parts)
    grams = parse_float(grams_str)
    await update.message.reply_text(used_apply_text(query, grams), reply_markup=MAIN_MENU)


@restricted
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(summary_text(), reply_markup=MAIN_MENU)


# --------------------------------------------------------------------------
# Button menu handler (Check Stock / Low Stock / Log Usage / Summary)
# "Add Purchase" is handled separately below, as an entry point into the
# /add ConversationHandler, since it needs a multi-step guided flow.
# --------------------------------------------------------------------------
@restricted
async def menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Follow-up answer to a pending "Check Stock" button tap
    if context.user_data.get(AWAITING_STOCK):
        context.user_data.pop(AWAITING_STOCK, None)
        await update.message.reply_text(stock_lookup_text(text), reply_markup=MAIN_MENU)
        return

    # Follow-up answer to a pending "Log Usage" button tap
    if context.user_data.get(AWAITING_USED):
        context.user_data.pop(AWAITING_USED, None)
        query, grams = split_query_and_grams(text)
        if query is None:
            await update.message.reply_text(
                "Couldn't read that - send it like 'charcoal matte 200' "
                "(search term, then grams).",
                reply_markup=MAIN_MENU,
            )
            return
        await update.message.reply_text(used_apply_text(query, grams), reply_markup=MAIN_MENU)
        return

    # A main-menu button was tapped
    if text == BTN_STOCK:
        context.user_data[AWAITING_STOCK] = True
        await update.message.reply_text(
            "Which material or color? (e.g. charcoal)", reply_markup=ReplyKeyboardRemove()
        )
        return

    if text == BTN_LOWSTOCK:
        await update.message.reply_text(lowstock_text(LOW_STOCK_THRESHOLD_KG), reply_markup=MAIN_MENU)
        return

    if text == BTN_USED:
        context.user_data[AWAITING_USED] = True
        await update.message.reply_text(
            "Which material/color, and how many grams? e.g. 'charcoal matte 200'",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if text == BTN_SUMMARY:
        await update.message.reply_text(summary_text(), reply_markup=MAIN_MENU)
        return

    # Anything else that isn't a recognized button or a pending follow-up
    await update.message.reply_text(
        "Not sure what you mean - use the buttons below, or /help for commands.",
        reply_markup=MAIN_MENU,
    )


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
    totals_row_idx = find_totals_row_index(values, FORMAT_COL_INDEX)

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
        reply_markup=MAIN_MENU,
    )
    context.user_data.pop("new_row", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_row", None)
    await update.message.reply_text("Cancelled.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Receipt scanning (photo/PDF -> Claude -> confirm -> add to sheet)
# --------------------------------------------------------------------------
RECEIPT_TOOL = {
    "name": "record_receipt",
    "description": (
        "Record every purchased line item found on a receipt or invoice for "
        "a 3D printing business."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "retailer": {"type": "string", "description": "Store or seller name, e.g. 'Bambu Lab'."},
            "order_number": {"type": "string", "description": "Order/invoice number if visible, else ''."},
            "date": {"type": "string", "description": "Purchase date as YYYY-MM-DD, best guess if unclear."},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["filament", "equipment"],
                            "description": (
                                "'filament' if this line is a spool/refill of print "
                                "material sold by weight; 'equipment' for anything else "
                                "(printers, tools, accessories, parts)."
                            ),
                        },
                        "material": {"type": "string", "description": "Filament type, e.g. 'PLA Matte'. '' if not filament."},
                        "color": {"type": "string", "description": "Filament color. '' if not filament."},
                        "format": {
                            "type": "string",
                            "enum": ["Spool", "Refill", ""],
                            "description": "Filament packaging. '' if not filament.",
                        },
                        "weight_kg": {"type": "number", "description": "Weight per unit in kg for filament (usually 1). 0 if not filament."},
                        "item_name": {"type": "string", "description": "Descriptive name, especially for equipment items."},
                        "equipment_category": {
                            "type": "string",
                            "description": "For equipment only: Printer, Tool, Accessory, or Other. '' for filament.",
                        },
                        "sku": {"type": "string", "description": "SKU or ASIN if visible, else ''."},
                        "qty": {"type": "integer", "description": "Quantity purchased."},
                        "unit_price": {"type": "number", "description": "Price per unit before tax and discount."},
                        "discount": {"type": "number", "description": "Discount amount for this line, 0 if none."},
                        "tax": {"type": "number", "description": "Tax amount for this line, 0 if none/unknown."},
                    },
                    "required": ["category", "qty", "unit_price"],
                },
            },
        },
        "required": ["items"],
    },
}

RECEIPT_PROMPT = (
    "Read this receipt or invoice for a 3D printing business and call the "
    "record_receipt tool with every purchased line item. Classify each line as "
    "'filament' (spools/refills of print material) or 'equipment' (printers, "
    "tools, accessories, anything else). Skip shipping charges and "
    "subtotal/tax/total summary lines - only include actual purchased items."
)


def extract_receipt(file_bytes: bytes, media_type: str) -> dict:
    """Send a receipt image/PDF to Claude and return the parsed record_receipt
    tool input, or raise on failure."""
    if not anthropic:
        raise RuntimeError("The 'anthropic' package isn't installed.")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
    data_b64 = base64.b64encode(file_bytes).decode("ascii")
    if media_type == "application/pdf":
        content_block = {
            "type": "document",
            "source": {"type": "base64", "media_type": media_type, "data": data_b64},
        }
    else:
        content_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data_b64},
        }

    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2048,
        tools=[RECEIPT_TOOL],
        tool_choice={"type": "tool", "name": "record_receipt"},
        messages=[{"role": "user", "content": [content_block, {"type": "text", "text": RECEIPT_PROMPT}]}],
    )
    for block in message.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("Claude didn't return a structured result.")


def build_filament_row(retailer, order_number, date_str, item, row_idx):
    r = row_idx
    return [
        date_str,
        retailer,
        retailer,
        item.get("material") or item.get("item_name") or "",
        item.get("color") or "",
        item.get("format") or "Spool",
        item.get("weight_kg") or 1.0,
        item.get("qty") or 1,
        f"=G{r}*H{r}",
        0,
        f"=I{r}-J{r}",
        item.get("sku") or "",
        order_number,
        item.get("unit_price") or 0,
        item.get("discount") or 0,
        item.get("tax") or 0,
        f"=N{r}-O{r}+P{r}",
    ]


def build_equipment_row(retailer, order_number, date_str, item, row_idx):
    r = row_idx
    name = item.get("item_name") or f"{item.get('material', '')} {item.get('color', '')}".strip() or "Item"
    return [
        date_str,
        retailer,
        name,
        item.get("equipment_category") or "Other",
        item.get("sku") or "",
        order_number,
        item.get("qty") or 1,
        item.get("unit_price") or 0,
        item.get("discount") or 0,
        item.get("tax") or 0,
        f"=H{r}-I{r}+J{r}",
    ]


def format_item_preview(item) -> str:
    qty = item.get("qty") or 1
    price = item.get("unit_price") or 0
    if item.get("category") == "filament":
        return (
            f"- {item.get('material', '?')} {item.get('color', '')} "
            f"({item.get('format', '')}) x{qty} @ ${price:.2f}"
        )
    return f"- {item.get('item_name', 'Item')} x{qty} @ ${price:.2f}"


async def safe_edit(status_msg, fallback_chat_msg, text, **kwargs):
    """Edit a status message, but never let a failed edit swallow the result.

    Telegram occasionally refuses to edit a message (rate limits, timing,
    client-side quirks) and raises telegram.error.BadRequest. If that
    happens here, send the text as a brand-new message instead of losing
    it - the user should always see the outcome of a receipt scan.
    """
    try:
        await status_msg.edit_text(text, **kwargs)
    except Exception:
        logger.exception("Couldn't edit status message, sending a new one instead")
        await fallback_chat_msg.reply_text(text, **kwargs)


async def safe_edit_query(query, text, **kwargs):
    """Same idea as safe_edit(), for callback-query messages (the Add/Discard buttons)."""
    try:
        await query.edit_message_text(text, **kwargs)
    except Exception:
        logger.exception("Couldn't edit message via callback, sending a new one instead")
        await query.message.reply_text(text, **kwargs)


@restricted
async def receipt_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.photo:
        media_type = "image/jpeg"
        tg_file = await msg.photo[-1].get_file()
    elif msg.document:
        mime = msg.document.mime_type or ""
        if mime not in ("application/pdf", "image/jpeg", "image/png", "image/webp"):
            await msg.reply_text(
                "I can only read PDF or image (jpg/png/webp) receipts.", reply_markup=MAIN_MENU
            )
            return
        media_type = mime
        tg_file = await msg.document.get_file()
    else:
        return

    if not ANTHROPIC_API_KEY:
        await msg.reply_text(
            "Receipt scanning isn't set up yet - ask whoever manages the bot to add "
            "an ANTHROPIC_API_KEY.",
            reply_markup=MAIN_MENU,
        )
        return

    status = await msg.reply_text("Reading receipt...", reply_markup=ReplyKeyboardRemove())
    try:
        raw = await tg_file.download_as_bytearray()
        # extract_receipt() is a blocking (synchronous) network call - run it in a
        # worker thread so it can't freeze the bot's whole event loop while it waits.
        parsed = await asyncio.wait_for(
            asyncio.to_thread(extract_receipt, bytes(raw), media_type), timeout=90
        )
    except asyncio.TimeoutError:
        logger.warning("Receipt extraction timed out")
        await safe_edit(status, msg, "That took too long and timed out - try again, or use /add instead.")
        return
    except Exception:
        logger.exception("Receipt extraction failed")
        await safe_edit(status, msg, "Couldn't read that receipt - try a clearer photo, or use /add instead.")
        return

    items = (parsed or {}).get("items") or []
    if not items:
        await safe_edit(status, msg, "Didn't find any purchase line items on that receipt.")
        return

    rid = uuid.uuid4().hex[:8]
    context.user_data.setdefault("pending_receipts", {})[rid] = parsed

    lines = [f"Found {len(items)} item(s) from {parsed.get('retailer') or 'this receipt'}:"]
    lines.extend(format_item_preview(it) for it in items)
    lines.append("\nAdd these to the inventory sheet?")

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Add all", callback_data=f"receipt_add:{rid}"),
                InlineKeyboardButton("❌ Discard", callback_data=f"receipt_discard:{rid}"),
            ]
        ]
    )
    await safe_edit(status, msg, "\n".join(lines))
    await msg.reply_text("Confirm below:", reply_markup=keyboard)


@restricted
async def receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, _, rid = query.data.partition(":")
    pending = context.user_data.get("pending_receipts", {})
    parsed = pending.pop(rid, None)

    if parsed is None:
        await safe_edit_query(query, "This receipt was already handled (or the bot restarted).")
        return

    if action == "receipt_discard":
        await safe_edit_query(query, "Discarded - nothing added.")
        return

    retailer = parsed.get("retailer") or "Unknown"
    order_number = parsed.get("order_number") or ""
    date_str = parsed.get("date") or date.today().isoformat()
    items = parsed.get("items") or []

    filament_items = [it for it in items if it.get("category") == "filament"]
    equipment_items = [it for it in items if it.get("category") != "filament"]
    added = 0

    if filament_items:
        fws = get_ws(FILAMENT_SHEET)
        f_row = find_totals_row_index(fws.get_all_values(), FORMAT_COL_INDEX)
        for it in filament_items:
            fws.insert_row(
                build_filament_row(retailer, order_number, date_str, it, f_row),
                index=f_row,
                value_input_option="USER_ENTERED",
            )
            f_row += 1
            added += 1

    if equipment_items:
        ews = get_ws(EQUIPMENT_SHEET)
        e_row = find_totals_row_index(ews.get_all_values(), EQUIPMENT_ITEM_COL_INDEX)
        for it in equipment_items:
            ews.insert_row(
                build_equipment_row(retailer, order_number, date_str, it, e_row),
                index=e_row,
                value_input_option="USER_ENTERED",
            )
            e_row += 1
            added += 1

    await safe_edit_query(query, f"Added {added} item(s) to the inventory sheet.")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    """Catch-all so an unexpected bug never fails silently in the logs -
    and, where possible, tells the user something went wrong instead of
    leaving them waiting with no reply at all."""
    logger.exception("Unhandled error while processing an update", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "Something went wrong handling that - try again, or use /add instead."
            )
    except Exception:
        logger.exception("Also failed to notify the user about the error")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(on_error)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(CommandHandler("lowstock", lowstock))
    app.add_handler(CommandHandler("used", used))
    app.add_handler(CommandHandler("summary", summary))

    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_ADD)}$"), add_start),
        ],
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

    # Receipt photos/PDFs -> Claude extraction -> confirm buttons
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.PDF | filters.Document.IMAGE, receipt_file)
    )
    app.add_handler(CallbackQueryHandler(receipt_callback, pattern=r"^receipt_(add|discard):"))

    # Catches button taps and follow-up answers for Check Stock / Low Stock /
    # Log Usage / Summary. Registered after add_conv so an active /add flow
    # (or a fresh "Add Purchase" tap) is handled by add_conv first.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
