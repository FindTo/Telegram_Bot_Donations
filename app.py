import os
import logging
import sqlite3
from dotenv import load_dotenv
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, filters, ContextTypes)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TARGET_AMOUNT = float(os.getenv("TARGET", "1000"))
PHOTO_URL = os.getenv("PHOTO_URL")
DATABASE_NAME = "donations.db"

# === Logging ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Init Flask ===
app = Flask(__name__)
bot = Bot(token=BOT_TOKEN)

application = Application.builder().token(BOT_TOKEN).build()

# === DB ===
def init_db():
    with sqlite3.connect(DATABASE_NAME) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS donations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                status TEXT DEFAULT 'pending',
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()


def save_donation(user_id, amount):
    with sqlite3.connect(DATABASE_NAME) as conn:
        conn.execute("INSERT INTO donations (user_id, amount) VALUES (?, ?)",
                     (user_id, amount))
        conn.commit()
def get_total():
    with sqlite3.connect(DATABASE_NAME) as conn:
        cur = conn.cursor()
        cur.execute("SELECT SUM(amount) FROM donations WHERE status='confirmed'")
        return cur.fetchone()[0] or 0


def get_last_pending_id(user_id):
    with sqlite3.connect(DATABASE_NAME) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM donations WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (user_id,))
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
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = get_total()
    keyboard = [[
        InlineKeyboardButton("🎉 Сделать донат", callback_data="donate")
    ], [InlineKeyboardButton("🔄 Обновить", callback_data="refresh")]]
    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=PHOTO_URL)
    await update.message.reply_text(
        f"<b>Сбор на кондиционер для Каваи Суши!</b>\n\n"
        f"Гио хочет поставить кондиционер в Кавай Суши...\n\n"
        f"Донаты:\n\n"
        f"BOG <code>GE21BG0000000607397845</code> Aleksei Koniaev\n"
        f"TBC <code>GE89TB7056145064400005</code> Artem Proskurin\n\n"
        f"{progress_bar(total, TARGET_AMOUNT)}\n\n"
        f"Нажмите кнопку ниже, чтобы заявить о переводе!",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard))


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "donate":
        context.user_data["awaiting"] = True
        await query.message.reply_text("Введите сумму вашего доната (только цифры):")

    elif query.data == "refresh":
        total = get_total()
        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=PHOTO_URL)
        await query.edit_message_text(
            f"<b>Сбор на кондиционер для Каваи Суши!</b>\n\n"
            f"...\n\n"
            f"{progress_bar(total, TARGET_AMOUNT)}",
            parse_mode='HTML',
            reply_markup=query.message.reply_markup)


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
        reply_markup=confirm_keyboard(donation_id))


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, donation_id = query.data.split("_")
    donation_id = int(donation_id)

    if query.from_user.id != ADMIN_ID:
        await query.message.reply_text("❌ Только админ может подтверждать переводы.")
        return

    with sqlite3.connect(DATABASE_NAME) as conn:
        cur = conn.cursor()

        if action == "confirm":
            cur.execute("UPDATE donations SET status='confirmed' WHERE id=?", (donation_id,))
            status = "подтверждён"
        else:
            cur.execute("UPDATE donations SET status='rejected' WHERE id=?", (donation_id,))
            status = "отклонён"

        cur.execute("SELECT user_id, amount FROM donations WHERE id=?", (donation_id,))
        user_id, amount = cur.fetchone()
        conn.commit()

    await context.bot.send_message(chat_id=user_id, text=f"🎉 Ваш донат на {amount} ₾ был {status}. Спасибо!")
    await query.edit_message_text(f"Заявка #{donation_id} {status}.")


# ==== Flask для Replit ====
# === Flask route for Telegram webhook ===
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), bot)
        application.create_task(application.update_queue.put(update))
        return "ok"

# === Health check ===
@app.route("/")
def index():
    return "Bot is running on Vercel!"

# === Init ===
init_db()
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button, pattern="^(donate|refresh)$"))
application.add_handler(CallbackQueryHandler(confirm, pattern="^(confirm|reject)_\\d+$"))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount))