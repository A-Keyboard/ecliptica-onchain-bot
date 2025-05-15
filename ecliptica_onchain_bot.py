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
    ("metrics",   "Which on-chain metrics? (exchange flows, whale transfers, DEX volume)") ,
    ("wallets",   "Any wallet addresses to track? (comma-separated, optional)"),
    ("thresholds","Alert thresholds? (e.g., >1000 ETH transfer, >$10M exchange inflow)"),
    ("freq",      "Alert frequency (instant / 5m / 15m / hourly)"),
    ("risk",      "Max exposure per trade? (USD or % of portfolio)"),
]
SETUP, ASK = range(2)

# Database helpers

def init_db() -> None:
    with sqlite3.connect(DB) as con:
        con.execute("CREATE TABLE IF NOT EXISTS profile (uid INTEGER PRIMARY KEY, data TEXT)")


def save_profile(uid: int, data: dict[str,str]) -> None:
    with sqlite3.connect(DB) as con:
        con.execute("REPLACE INTO profile VALUES (?,?)", (uid, json.dumps(data)))


def load_profile(uid: int) -> dict[str,str]:
    with sqlite3.connect(DB) as con:
        cur = con.cursor()
        cur.execute("SELECT data FROM profile WHERE uid=?", (uid,))
        row = cur.fetchone()
    return json.loads(row[0]) if row else {}

# REI CORE API call

def rei_call(prompt: str, profile: dict[str,str]) -> str:
    headers = {"Authorization": f"Bearer {REI_KEY}", "Content-Type": "application/json"}
    msgs = [{"role":"user","content":"On-chain Trader Profile:\n" + \
             "\n".join(f"{k}: {v}" for k,v in profile.items())}]
    msgs.append({"role":"user","content": prompt})
    body = {"model":"rei-core-chat-001","temperature":0.2,"messages":msgs}
    r = requests.post(
        "https://api.reisearch.box/v1/chat/completions",
        headers=headers, json=body, timeout=90
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

# Telegram handlers
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome to *Ecliptica On-Chain Trader Assistant*!\n" +
        "Use /setup to configure your on-chain profile, then /ask <question>.",
        parse_mode=ParseMode.MARKDOWN
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/setup – on-chain profile wizard\n" +
        "/ask MEV or DEX flow insights? – get on-chain signals\n" +
        "/cancel – abort setup"
    )

# Setup wizard
async def setup_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data['i'] = 0
    ctx.user_data['ans'] = {}
    await update.message.reply_text("Let's build your on-chain profile (type /cancel to abort)")
    return ASK

async def collect(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    i = ctx.user_data['i']
    key, question = QUESTS[i]
    ctx.user_data['ans'][key] = update.message.text.strip()
    ctx.user_data['i'] += 1
    if ctx.user_data['i'] < len(QUESTS):
        _, q = QUESTS[ctx.user_data['i']]
        await update.message.reply_text(f"[{ctx.user_data['i']+1}/{len(QUESTS)}] {q}")
        return ASK
    # done
    save_profile(update.effective_user.id, ctx.user_data['ans'])
    await update.message.reply_text("✅ Profile saved! Now use /ask to get on-chain signals.")
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Setup cancelled.")
    return ConversationHandler.END

# /ask command
async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    profile = load_profile(update.effective_user.id)
    if not profile:
        await update.message.reply_text("⚠️ Please run /setup before asking for signals.")
        return
    q = " ".join(ctx.args) or "Please provide an on-chain trade idea."
    await update.message.reply_text("🔍 Fetching on-chain insights…")
    try:
        ans = await ctx.application.run_async(rei_call, q, profile)
    except Exception:
        await update.message.reply_text("⚠️ REI CORE error — try again later.")
        return
    await update.message.reply_text(ans, parse_mode=ParseMode.MARKDOWN)

# Main

def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setup", setup_start))
    app.add_handler(CommandHandler("ask", ask_cmd))

    wizard = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, collect)],
        states={ASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(wizard)

    app.run_polling()

if __name__ == "__main__":
    main()
