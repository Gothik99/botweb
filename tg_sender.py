import asyncio
from aiogram import Bot
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'vpn_bot.db')

def get_bot_token():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = 'bot_token'")
    row = cur.fetchone()
    conn.close()
    return row[0] if row else ''

async def send_telegram_message(user_id: int, text: str, reply_markup=None, parse_mode="HTML"):
    bot_token = get_bot_token()
    if not bot_token:
        raise RuntimeError('Bot token not found in database!')
    bot = Bot(token=bot_token)
    await bot.send_message(user_id, text, reply_markup=reply_markup, parse_mode=parse_mode) 