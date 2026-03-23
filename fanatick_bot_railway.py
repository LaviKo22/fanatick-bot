"""
Fanatick Pass Sheet Bot
All secrets loaded from environment variables — nothing hardcoded.
"""

import logging
import requests
import gspread
import time
import os
import base64
import json
from datetime import datetime
from google.oauth2.service_account import Credentials
from openai import OpenAI
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# ============================================================
#  CONFIG — all from Railway environment variables
# ============================================================

TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TICKETVAULT_API_KEY = os.environ["TICKETVAULT_API_KEY"]
OPENAI_API_KEY      = os.environ["OPENAI_API_KEY"]
WEBHOOK_URL         = os.environ.get("WEBHOOK_URL", "")
ALLOWED_USER_ID     = 1415960837

MEMBERS_SHEET_URL   = "https://docs.google.com/spreadsheets/d/1pzjdhThhRy86Xf4RBdn6PvWu8LeLVh3wLqs1HzLiU6I"
PASSSHEET_SHEET_URL = "https://docs.google.com/spreadsheets/d/1jjWrtwkes8088gelJjmMTOT_uYTlMx4AKiXC28w-M0g"

TV_BASE_URL         = "https://my-tix.net/api/v1"
TV_HEADERS          = {"X-API-Key": TICKETVAULT_API_KEY}
PORT                = int(os.environ.get("PORT", 8080))

# ============================================================
#  LOGGING
# ============================================================

logging.basicConfig(format="%(asctime)s — %(levelname)s — %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ============================================================
#  GOOGLE SHEETS — credentials from env variable
# ============================================================

def get_sheets_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    sa_dict = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(sa_dict, scopes=scopes)
    return gspread.authorize(creds)

def get_all_members():
    client = get_sheets_client()
    sheet = client.open_by_url(MEMBERS_SHEET_URL).sheet1
    records = sheet.get_all_records()
    members = []
    for row in records:
        email = str(row.get("Email", "")).strip()
        password = str(row.get("Password", "")).strip()
        member_number = str(row.get("Member Number", "")).strip()
        if email and password:
            members.append({"email": email, "password": password, "member_number": member_number})
    log.info(f"Loaded {len(members)} members")
    return members

def write_pass_sheet(game_name, passes):
    client = get_sheets_client()
    sheet = client.open_by_url(PASSSHEET_SHEET_URL).sheet1
    sheet.clear()
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    sheet.append_row([f"Pass Sheet — {game_name}", f"Generated: {now}", "", "", "", "", ""])
    sheet.append_row(["Member Number", "Email", "Block", "Row", "Seat", "Apple Wallet Link", "Google Wallet Link", "Status"])
    for p in passes:
        sheet.append_row([
            p.get("member_number", ""),
            p.get("email", ""),
            p.get("block", ""),
            p.get("row", ""),
            p.get("seat", ""),
            p.get("apple_wallet_link", "N/A"),
            p.get("google_wallet_link", "N/A"),
            p.get("status", "")
        ])
    log.info(f"Written {len(passes)} rows to pass sheet")

# ============================================================
#  GPT VISION
# ============================================================

def extract_seats_from_image(image_bytes):
    client = OpenAI(api_key=OPENAI_API_KEY)
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    prompt = """You are analyzing an Arsenal FC ticket cart screenshot.
Extract ALL seats visible in this cart.
Return ONLY a valid JSON array with objects containing:
- block (string)
- row (string)
- seat (string)
- member_number (string if visible, else "")
- game (string, match name if visible, else "Arsenal Match")
No markdown, no explanation. Just the JSON array."""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        ]}],
        max_tokens=1000
    )
    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())

# ============================================================
#  TICKETVAULT
# ============================================================

def check_credits():
    r = requests.get(f"{TV_BASE_URL}/credits", headers=TV_HEADERS)
    return r.json().get("credits", 0)

def generate_passes(credentials):
    r = requests.post(f"{TV_BASE_URL}/passes/generate", headers=TV_HEADERS,
                      json={"club": "arsenal", "credentials": credentials})
    return r.json().get("job_token")

def poll_job(job_token, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{TV_BASE_URL}/jobs/{job_token}/status", headers=TV_HEADERS)
        data = r.json()
        if data.get("status") in ["completed", "failed"]:
            return data
        time.sleep(4)
    raise TimeoutError("Job timed out")

def unlock_passes(identifiers):
    r = requests.post(f"{TV_BASE_URL}/passes/arsenal/unlock", headers=TV_HEADERS,
                      json={"identifiers": identifiers})
    return r.json()

def refresh_wallet_links(identifiers):
    r = requests.post(f"{TV_BASE_URL}/passes/arsenal/refresh-wallet-links", headers=TV_HEADERS,
                      json={"identifiers": identifiers})
    return r.json()

def get_pass_details(identifiers):
    r = requests.post(f"{TV_BASE_URL}/passes/arsenal/details", headers=TV_HEADERS,
                      json={"identifiers": identifiers, "pass_type": "season"})
    return r.json()

# ============================================================
#  SEAT MATCHING
# ============================================================

def match_seats_to_members(seats, members):
    matched = []
    used = set()
    for seat in seats:
        mn = seat.get("member_number", "").strip()
        matched_member = None
        if mn:
            for m in members:
                if m["member_number"] == mn and mn not in used:
                    matched_member = m
                    used.add(mn)
                    break
        if not matched_member:
            for m in members:
                if m["member_number"] not in used:
                    matched_member = m
                    used.add(m["member_number"])
                    break
        if matched_member:
            matched.append({**seat, **matched_member, "status": "matched"})
        else:
            matched.append({**seat, "email": "", "password": "", "member_number": "", "status": "NO MEMBER"})
    return matched

# ============================================================
#  FULL PIPELINE
# ============================================================

async def process_screenshot(image_bytes, status_callback):
    await status_callback("🔍 Reading your screenshot...")
    seats = extract_seats_from_image(image_bytes)
    if not seats:
        return None, "❌ Couldn't extract seats. Try a clearer image."

    game_name = seats[0].get("game", "Arsenal Match")
    await status_callback(f"✅ Found *{len(seats)} seat(s)* for {game_name}\nLoading members...")

    members = get_all_members()
    if not members:
        return None, "❌ No members found in your sheet."

    matched = match_seats_to_members(seats, members)
    available = [m for m in matched if m["status"] == "matched"]

    await status_callback(f"✅ Matched {len(available)}/{len(matched)} seats\nChecking credits...")

    credits = check_credits()
    await status_callback(f"💳 Credits available: *{credits}*\nGenerating passes...")

    if credits < len(available):
        return None, f"❌ Not enough credits. Need {len(available)}, have {credits}."

    credentials = [f"{m['email']},{m['password']}" for m in available]
    job_token = generate_passes(credentials)
    if not job_token:
        return None, "❌ TicketVault failed to start. Check API key."

    await status_callback("⚙️ Processing on TicketVault...")
    job_result = poll_job(job_token)
    if job_result.get("status") == "failed":
        return None, f"❌ Job failed: {job_result.get('errors', '')}"

    await status_callback(f"✅ Passes generated\nUnlocking ({len(available)} credit(s))...")
    identifiers = [m["email"] for m in available]
    unlock_passes(identifiers)

    await status_callback("🔓 Unlocked\nFetching wallet links...")
    refresh_result = refresh_wallet_links(identifiers)
    log.info(f"Refresh result: {json.dumps(refresh_result)}")

    details_result = get_pass_details(identifiers)
    log.info(f"Details result: {json.dumps(details_result)}")

    # Send raw response to Telegram so we can see the exact structure
    await status_callback(f"🔎 Raw response:\n`{json.dumps(details_result)[:800]}`")

    details_map = {}
    if isinstance(details_result, dict) and "results" in details_result:
        for result in details_result["results"]:
            passes = result.get("passes", [])
            for p in passes:
                key = p.get("email") or result.get("identifier", "")
                details_map[key] = p
    elif isinstance(details_result, dict) and "passes" in details_result:
        for p in details_result["passes"]:
            key = p.get("email") or p.get("identifier") or p.get("member_number", "")
            details_map[key] = p
    elif isinstance(details_result, list):
        for p in details_result:
            key = p.get("email") or p.get("identifier") or p.get("member_number", "")
            details_map[key] = p

    passes_with_links = []
    for m in matched:
        detail = details_map.get(m.get("email", ""), {})
        links = detail.get("links") or detail.get("wallet_links") or detail.get("walletLinks") or {}
        apple = links.get("apple") or "N/A"
        google = links.get("google") or "N/A"
        passes_with_links.append({
            "member_number": m.get("member_number", ""),
            "email": m.get("email", ""),
            "block": m.get("block", ""),
            "row": m.get("row", ""),
            "seat": m.get("seat", ""),
            "apple_wallet_link": apple,
            "google_wallet_link": google,
            "status": m.get("status", "")
        })

    await status_callback("📝 Writing to Google Sheets...")
    write_pass_sheet(game_name, passes_with_links)
    return passes_with_links, None

# ============================================================
#  TELEGRAM HANDLERS
# ============================================================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("❌ Unauthorized.")
        return

    await update.message.reply_text("📸 Got it. Starting...")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    async def status_callback(msg):
        await update.message.reply_text(msg, parse_mode="Markdown")

    try:
        passes, error = await process_screenshot(bytes(image_bytes), status_callback)
        if error:
            await update.message.reply_text(error)
            return

        lines = [f"✅ *Pass sheet done — {len(passes)} tickets*\n"]
        for p in passes:
            apple = p.get("apple_wallet_link", "N/A")
            ok = "✅" if apple != "N/A" else "⚠️"
            lines.append(f"{ok} Block {p['block']} · Row {p['row']} · Seat {p['seat']}\nMember: {p['member_number']}\n🍎 {apple}\n")
        lines.append("📊 Full sheet written to Google Sheets.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    text = update.message.text.lower().strip()
    if text in ["/start", "start", "hi", "hello"]:
        await update.message.reply_text(
            "👋 *Fanatick Pass Sheet Bot*\n\nSend me a cart screenshot and I'll generate your full pass sheet automatically.\n\nCommands:\n/credits — check TicketVault balance\n/members — check members loaded",
            parse_mode="Markdown"
        )
    elif text in ["/credits", "credits"]:
        try:
            await update.message.reply_text(f"💳 Credits: *{check_credits()}*", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")
    elif text in ["/members", "members"]:
        try:
            await update.message.reply_text(f"👥 Members: *{len(get_all_members())}*", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")
    else:
        await update.message.reply_text("Send me a cart screenshot 📸")

# ============================================================
#  MAIN
# ============================================================

def main():
    log.info("Starting Fanatick bot...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))

    if WEBHOOK_URL:
        log.info(f"Webhook mode: {WEBHOOK_URL}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=f"{WEBHOOK_URL}/webhook",
            url_path="/webhook"
        )
    else:
        log.info("Polling mode")
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
