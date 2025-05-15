"""
Ecliptica On-Chain Trader Assistant — Telegram bot for on-chain signal generation

This bot mirrors the structure of Ecliptica Perps Assistant but focuses on on-chain data
and narrative-driven signals. Users complete a setup wizard specifying:
  • Blockchains & assets
  • On-chain metrics and thresholds
  • Wallets to track
  • Alert frequency and risk parameters

Endpoints:
  • REI CORE Chat API: https://api.reisearch.box/v1/chat/completions

Dependencies:
  python-telegram-bot==20.7
  requests
  python-dotenv
"""
from __future__ import annotations
import os, json, sqlite3, logging, textwrap, requests
import asyncio, functools, time
from typing import Final
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, ContextTypes, filters
)

# Load config
load_dotenv()
BOT_TOKEN: Final[str] = os.environ["TELEGRAM_BOT_TOKEN"].strip()
REI_KEY:  Final[str] = os.environ["REICORE_API_KEY"].strip()
DB = "ecliptica_onchain.db"
# Profile questions for on-chain trader
QUESTS: Final[list[tuple[str,str]]] = [
    ("chains",    "Which blockchain(s) to monitor? (Ethereum, BSC, Polygon)"),
    ("assets",    "Which assets to track? (ETH, BTC, USDC, etc.)"),
    ("metrics",   "Which on-chain metrics? (exchange flows, whale transfers, DEX volume)"),
    ("wallets",   "Any wallet addresses to track? (comma-separated, optional)"),
    ("thresholds","Alert thresholds? (e.g., >1000 ETH transfer, >$10M exchange inflow)"),
    ("freq",      "Alert frequency (instant / 5m / 15m / hourly)"),
    ("risk",      "Max exposure per trade? (USD or % of portfolio)"),
]
SETUP, ASK = range(2)

# Database helpers

def init_db() -> None:
    with sqlite3.connect(DB) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS profile (uid INTEGER PRIMARY KEY, data TEXT)"
        )

def save_profile(uid: int, data: dict[str,str]) -> None:
    with sqlite3.connect(DB) as con:
        con.execute(
            "REPLACE INTO profile VALUES (?,?)", (uid, json.dumps(data))
        )

def load_profile(uid: int) -> dict[str,str]:
    with sqlite3.connect(DB) as con:
        cur = con.cursor()
        cur.execute("SELECT data FROM profile WHERE uid=?", (uid,))
        row = cur.fetchone()
    return json.loads(row[0]) if row else {}

# REI CORE API call with retry, timeout, and latency logging
# Serialized via asyncio.Lock if needed

token_lock = asyncio.Lock()

def rei_call(prompt: str, profile: dict[str,str]) -> str:
    headers = {"Authorization": f"Bearer {REI_KEY}", "Content-Type": "application/json"}
    messages = []
    if profile:
        # Proper newline separator
        profile_txt = "\n".join(f"{k}: {v}" for k, v in profile.items())
        messages.append({
            "role": "user",
            "content": f"On-chain Trader Profile:\n{profile_txt}"
        })
    messages.append({"role": "user", "content": prompt})
    body = {"model": "rei-core-chat-001", "temperature": 0.2, "messages": messages}

    # retry up to 2 times on 5xx errors
    for attempt in range(2):
        start_ts = time.time()
        try:
            resp = requests.post(
                "https://api.reisearch.box/v1/chat/completions",
                headers=headers,
                json=body,
                timeout=300
            )
            resp.raise_for_status()
            elapsed = time.time() - start_ts
            logging.info(f"REI call succeeded in {elapsed:.1f}s")
            return resp.json()["choices"][0]["message"]["content"].strip()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response else None
            logging.error(f"REI HTTPError {status} on attempt {attempt+1}")
            if status and 500 <= status < 600 and attempt == 0:
                time.sleep(2)
                continue
            raise
        except Exception:
            logging.exception(f"REI unexpected error on attempt {attempt+1}")
            raise
    raise RuntimeError("REI retry failed")

# Telegram handlers

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome to *Ecliptica On-Chain Trader Assistant*!\n"
        "Use /setup to build your profile, then /ask for signals.",
        parse_mode=ParseMode.MARKDOWN
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/setup – setup on-chain profile\n"
        "/ask <question> – get on-chain insights\n"
        "/cancel – abort setup"
    )

# Setup wizard

async def setup_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data['i'] = 0
    ctx.user_data['ans'] = {}
    await update.message.reply_text(
        "Let's build your on-chain profile – /cancel anytime."
    )
    _, q = QUESTS[0]
    await update.message.reply_text(f"[1/{len(QUESTS)}] {q}")
    return ASK

async def collect(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    i = ctx.user_data['i']
    key, _ = QUESTS[i]
    ctx.user_data['ans'][key] = update.message.text.strip()
    ctx.user_data['i'] += 1
    if ctx.user_data['i'] < len(QUESTS):
        idx = ctx.user_data['i']
        _, q = QUESTS[idx]
        await update.message.reply_text(f"[{idx+1}/{len(QUESTS)}] {q}")
        return ASK
    # save
    save_profile(update.effective_user.id, ctx.user_data['ans'])
    await update.message.reply_text(
        "✅ Profile saved! Now use /ask <question> to get on-chain signals."
    )
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Setup cancelled.")
    return ConversationHandler.END

# /ask command

async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    profile = load_profile(update.effective_user.id)
    if not profile:
        await update.message.reply_text("⚠️ Please run /setup first.")
        return
    query = " ".join(ctx.args) or "Please provide an on-chain trade idea."
    await update.message.reply_text("🔍 Fetching on-chain insights…")
    try:
        # optional serialization
        async with token_lock:
            ans = await ctx.application.run_async(
                functools.partial(rei_call, query, profile)
            )
    except Exception:
        logging.exception("REI call failed")
        await update.message.reply_text(
            "⚠️ REI CORE error — please try again later."
        )
        return
    await update.message.reply_text(ans, parse_mode=ParseMode.MARKDOWN)

# Entrypoint

def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    
    app.add_handler(CommandHandler("ask", ask_cmd))

    wizard = ConversationHandler(
        entry_points=[CommandHandler("setup", setup_start)],
        states={ASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(wizard)
    app.run_polling()

if __name__ == "__main__":
    main()

