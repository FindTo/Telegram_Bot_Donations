import os
import asyncio
import threading
import logging

import psycopg2
from dotenv import load_dotenv
from flask import Flask, request

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# === Load .env ===
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BOG_IBAN = os.getenv("BOG_IBAN")
TBC_IBAN = os.getenv("TBC_IBAN")
TARGET_AMOUNT = float(os.getenv("TARGET", "1000"))
PHOTO_URL = os.getenv("PHOTO_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

# === Logging ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.request").setLevel(logging.WARNING)

# === Flask ===
app = Flask(__name__)

# === Telegram App ===
application = Application.builder().token(BOT_TOKEN).build()


# === DB Setup ===
def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS donations (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    amount REAL,
                    status TEXT DEFAULT 'pending',
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()


def save_donation(user_id, amount):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO donations (user_id, amount) VALUES (%s, %s)", (user_id, amount))
            conn.commit()


def get_total():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT SUM(amount) FROM donations WHERE status='confirmed'")
            result = cur.fetchone()[0]
            return result or 0


def get_last_pending_id(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM donations WHERE user_id=%s AND status='pending' ORDER BY id DESC LIMIT 1",
                (user_id,))
            row = cur.fetchone()
            return row[0] if row else None


# === UI ===
def progress_bar(current, target, length=10):
    pct = min(100, int(current / target * 100))
    filled_len = pct * length // 100
    bar = '▓' * filled_len + '░' * (length - filled_len)
    return f"[{bar}] {pct}%  Собрано: {current:.2f} ₾ из {target} ₾"


def confirm_keyboard(donation_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_{donation_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{donation_id}")
    ]])


# === Handlers ===
async def error_handler(update: Update | None, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = get_total()
    keyboard = [[InlineKeyboardButton("🎉 Сделать донат", callback_data="donate")],
                [InlineKeyboardButton("🔄 Обновить", callback_data="refresh")]]

    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=PHOTO_URL)
    await update.message.reply_text(
        f"<b>Сбор на кондиционер для Каваи Суши!</b>\n\n"
        f"Гио хочет поставить кондиционер в Кавай Суши, чтобы мы могли ещё с большим кайфом собираться там, "
        f"но пока у него не хватает денег, поэтому он попросил выложить пост с просьбой сделать донаты.\n\n"
        f"Донаты:\n"
        f"BOG <code>{BOG_IBAN}</code> Aleksei Koniaev\n"
        f"TBC <code>{TBC_IBAN}</code> Artem Proskurin\n\n"
        f"{progress_bar(total, TARGET_AMOUNT)}\n\n"
        f"Нажмите кнопку ниже, чтобы заявить о переводе!",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "donate":
        context.user_data["awaiting"] = True
        await query.message.reply_text("Введите сумму вашего доната (только цифры):")

    elif query.data == "refresh":
        total = get_total()
        await query.edit_message_text(
            f"<b>Сбор на кондиционер для Каваи Суши!</b>\n\n"
            f"Донаты:\n"
            f"BOG <code>{BOG_IBAN}</code>\n"
            f"TBC <code>{TBC_IBAN}</code>\n\n"
            f"{progress_bar(total, TARGET_AMOUNT)}",
            parse_mode='HTML',
            reply_markup=query.message.reply_markup
        )


async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting"):
        return

    try:
        amount = float(update.message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите корректную сумму.")
        return

    context.user_data["awaiting"] = False
    user_id = update.message.from_user.id
    save_donation(user_id, amount)
    donation_id = get_last_pending_id(user_id)

    await update.message.reply_text("✅ Заявка отправлена. Ожидайте подтверждения!")

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"⚠️ Новая заявка на донат от @{update.message.from_user.username or user_id} на {amount} ₾",
        reply_markup=confirm_keyboard(donation_id)
    )


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, donation_id = query.data.split("_")
    donation_id = int(donation_id)

    if query.from_user.id != ADMIN_ID:
        await query.message.reply_text("❌ Только админ может подтверждать переводы.")
        return

    with get_conn() as conn:
        cur = conn.cursor()
        if action == "confirm":
            cur.execute("UPDATE donations SET status=%s WHERE id=%s", ('confirmed', donation_id))
            status = "подтверждён"
        else:
            cur.execute("UPDATE donations SET status=%s WHERE id=%s", ('rejected', donation_id))
            status = "отклонён"

        cur.execute("SELECT user_id, amount FROM donations WHERE id=%s", (donation_id,))
        user_id, amount = cur.fetchone()
        conn.commit()

    await context.bot.send_message(chat_id=user_id, text=f"🎉 Ваш донат на {amount} ₾ был {status}. Спасибо!")

    try:
        await query.edit_message_text(f"Заявка #{donation_id} {status}.")
    except Exception as e:
        logger.error(f"Failed to edit the message: {e}")


# === Register Handlers ===
application.add_error_handler(error_handler)
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button, pattern="^(donate|refresh)$"))
application.add_handler(CallbackQueryHandler(confirm, pattern="^(confirm|reject)_\\d+$"))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount))

if not application._initialized:
    asyncio.run(application.initialize())

# === Webhook ===
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, application.bot)

    async def handle():
        if not application._running:
            await application.start()
        await application.update_queue.put(update)

    asyncio.run(handle())

    return "ok", 200


@app.route("/")
def index():
    return "Bot is running on Vercel!"


@app.route("/test", methods=["POST"])
def test():
    return "ok", 200


# === Entry Point ===
if __name__ == "__main__":
    mode = os.getenv("MODE", "polling")
    init_db()
    if mode == "polling":
        application.run_polling()