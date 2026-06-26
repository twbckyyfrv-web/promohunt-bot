import logging
import os
import threading
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg2
import psycopg2.extras
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telegram.warnings import PTBUserWarning

warnings.filterwarnings("ignore", category=PTBUserWarning)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "975279289"))

CHOOSE_ROLE = 0
EMPLOYER_MENU = 1
PROMOTER_MENU = 2
POST_VACANCY_TITLE = 3
POST_VACANCY_DESC = 4
POST_VACANCY_PAY = 5
POST_VACANCY_CONTACT = 6

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def get_db():
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor, sslmode="prefer")


def db_init_tables():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vacancies (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    pay TEXT,
                    contact TEXT,
                    employer_id BIGINT,
                    employer_name TEXT,
                    employer_username TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS blacklisted_users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    name TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        conn.commit()


def db_get_all_vacancies():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM vacancies ORDER BY created_at DESC")
            return cur.fetchall()


def db_get_user_vacancies(employer_id: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM vacancies WHERE employer_id = %s ORDER BY created_at DESC",
                (employer_id,)
            )
            return cur.fetchall()


def db_insert_vacancy(title, description, pay, contact, employer_id, employer_name, employer_username):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vacancies (title, description, pay, contact, employer_id, employer_name, employer_username)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (title, description, pay, contact, employer_id, employer_name, employer_username)
            )
        conn.commit()


def db_delete_vacancy(vacancy_id: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM vacancies WHERE id = %s", (vacancy_id,))
        conn.commit()


def db_init_blacklist():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS blacklisted_users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    name TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        conn.commit()


def db_is_blacklisted(user_id: int) -> bool:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM blacklisted_users WHERE user_id = %s", (user_id,))
            return cur.fetchone() is not None


def db_add_to_blacklist(user_id: int, username: str, name: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO blacklisted_users (user_id, username, name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, username, name)
            )
        conn.commit()


def db_remove_from_blacklist(user_id: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM blacklisted_users WHERE user_id = %s", (user_id,))
        conn.commit()


def db_get_blacklist():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM blacklisted_users ORDER BY created_at DESC")
            return cur.fetchall()


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Failed to notify admin: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user = update.effective_user
    username = f"@{user.username}" if user.username else "без username"
    if user.id != ADMIN_ID:
        await notify_admin(
            context,
            f"👤 *Новый пользователь*\n\n"
            f"Имя: {user.first_name}\n"
            f"Username: {username}\n"
            f"ID: `{user.id}`"
        )

    keyboard = [
        [InlineKeyboardButton("👔 Я Работодатель", callback_data="role_employer")],
        [InlineKeyboardButton("🎯 Я Промоутер", callback_data="role_promoter")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 Добро пожаловать в PromoJob Bot!\n\n"
        "Это платформа для промо-работы и ивентов.\n\n"
        "❓ Кто вы?",
        reply_markup=reply_markup
    )
    return CHOOSE_ROLE


async def choose_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "role_employer":
        context.user_data["role"] = "employer"
        keyboard = [
            [InlineKeyboardButton("➕ Разместить вакансию", callback_data="post_vacancy")],
            [InlineKeyboardButton("📋 Мои вакансии", callback_data="my_vacancies")],
        ]
        await query.edit_message_text(
            "💼 Вы вошли как *Работодатель*\n\nЧто хотите сделать?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return EMPLOYER_MENU

    elif query.data == "role_promoter":
        context.user_data["role"] = "promoter"
        keyboard = [
            [InlineKeyboardButton("🔍 Смотреть вакансии", callback_data="view_vacancies")],
        ]
        await query.edit_message_text(
            "🎯 Вы вошли как *Промоутер*\n\nЧто хотите сделать?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return PROMOTER_MENU


async def employer_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "post_vacancy":
        await query.edit_message_text(
            "📝 *Создание вакансии*\n\n"
            "Шаг 1 из 4\n"
            "Введите *название* вакансии:\n"
            "(например: Промоутер на ивент, Раздача листовок)",
            parse_mode="Markdown"
        )
        return POST_VACANCY_TITLE

    elif query.data == "my_vacancies":
        user_id = update.effective_user.id
        user_vacancies = db_get_user_vacancies(user_id)

        if not user_vacancies:
            keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_employer")]]
            await query.edit_message_text(
                "📭 У вас пока нет вакансий.\n\nРазместите первую!",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            text = "📋 *Ваши вакансии:*\n\n"
            for i, v in enumerate(user_vacancies, 1):
                desc_preview = v["description"][:50]
                text += f"{i}. *{v['title']}*\n💰 {v['pay']}\n📍 {desc_preview}...\n\n"
            keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_employer")]]
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return EMPLOYER_MENU

    elif query.data == "back_employer":
        keyboard = [
            [InlineKeyboardButton("➕ Разместить вакансию", callback_data="post_vacancy")],
            [InlineKeyboardButton("📋 Мои вакансии", callback_data="my_vacancies")],
        ]
        await query.edit_message_text(
            "💼 Меню работодателя\n\nЧто хотите сделать?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return EMPLOYER_MENU


async def vacancy_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["vacancy"] = {"title": update.message.text}
    await update.message.reply_text(
        "📝 *Создание вакансии*\n\nШаг 2 из 4\nОпишите *обязанности* и место работы:",
        parse_mode="Markdown"
    )
    return POST_VACANCY_DESC


async def vacancy_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["vacancy"]["description"] = update.message.text
    await update.message.reply_text(
        "📝 *Создание вакансии*\n\nШаг 3 из 4\nУкажите *оплату* (например: 2000₽/день, 500₽/час):",
        parse_mode="Markdown"
    )
    return POST_VACANCY_PAY


async def vacancy_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["vacancy"]["pay"] = update.message.text
    await update.message.reply_text(
        "📝 *Создание вакансии*\n\nШаг 4 из 4\nУкажите *контакт* для связи (телефон или @username):",
        parse_mode="Markdown"
    )
    return POST_VACANCY_CONTACT


async def vacancy_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = context.user_data["vacancy"]
    v["contact"] = update.message.text
    employer_id = update.effective_user.id
    employer_name = update.effective_user.first_name
    employer_username = update.effective_user.username or "нет"

    if db_is_blacklisted(employer_id):
        await update.message.reply_text(
            "🚫 *Ваш аккаунт заблокирован.*\n\nВы не можете публиковать вакансии.\n"
            "Если вы считаете это ошибкой — напишите в /support",
            parse_mode="Markdown"
        )
        return EMPLOYER_MENU

    db_insert_vacancy(
        title=v["title"],
        description=v["description"],
        pay=v["pay"],
        contact=v["contact"],
        employer_id=employer_id,
        employer_name=employer_name,
        employer_username=employer_username,
    )

    await notify_admin(
        context,
        f"📋 *Новая вакансия!*\n\n"
        f"📌 *{v['title']}*\n"
        f"📝 {v['description']}\n"
        f"💰 {v['pay']}\n"
        f"📞 {v['contact']}\n\n"
        f"👤 Работодатель: {employer_name} (@{employer_username})\n"
        f"🆔 ID: `{employer_id}`"
    )

    keyboard = [
        [InlineKeyboardButton("➕ Ещё вакансию", callback_data="post_vacancy")],
        [InlineKeyboardButton("📋 Мои вакансии", callback_data="my_vacancies")],
    ]
    await update.message.reply_text(
        f"✅ *Вакансия опубликована!*\n\n"
        f"📌 *{v['title']}*\n"
        f"📋 {v['description']}\n"
        f"💰 {v['pay']}\n"
        f"📞 {v['contact']}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return EMPLOYER_MENU


async def promoter_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "view_vacancies":
        vacancies = db_get_all_vacancies()
        if not vacancies:
            keyboard = [[InlineKeyboardButton("🔄 Обновить", callback_data="view_vacancies")]]
            await query.edit_message_text(
                "😔 Пока вакансий нет.\n\nПроверьте позже!",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            context.user_data["vacancy_ids"] = [v["id"] for v in vacancies]
            context.user_data["vacancy_index"] = 0
            await show_vacancy(query, context)
        return PROMOTER_MENU

    elif query.data == "next_vacancy":
        ids = context.user_data.get("vacancy_ids", [])
        if ids:
            context.user_data["vacancy_index"] = (context.user_data.get("vacancy_index", 0) + 1) % len(ids)
        await show_vacancy(query, context)
        return PROMOTER_MENU

    elif query.data == "prev_vacancy":
        ids = context.user_data.get("vacancy_ids", [])
        if ids:
            context.user_data["vacancy_index"] = (context.user_data.get("vacancy_index", 0) - 1) % len(ids)
        await show_vacancy(query, context)
        return PROMOTER_MENU


async def show_vacancy(query, context):
    ids = context.user_data.get("vacancy_ids", [])
    idx = context.user_data.get("vacancy_index", 0)
    total = len(ids)

    if not ids:
        await query.edit_message_text("😔 Вакансий не найдено.")
        return

    vacancy_id = ids[idx]
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM vacancies WHERE id = %s", (vacancy_id,))
            v = cur.fetchone()

    if not v:
        await query.edit_message_text("😔 Вакансия не найдена.")
        return

    keyboard = []
    if total > 1:
        keyboard.append([
            InlineKeyboardButton("⬅️", callback_data="prev_vacancy"),
            InlineKeyboardButton("➡️", callback_data="next_vacancy"),
        ])
    keyboard.append([InlineKeyboardButton("✅ Откликнуться", callback_data=f"apply_{vacancy_id}")])
    keyboard.append([InlineKeyboardButton("🔄 Обновить список", callback_data="view_vacancies")])

    await query.edit_message_text(
        f"🎯 *Вакансия {idx + 1} из {total}*\n\n"
        f"📌 *{v['title']}*\n\n"
        f"📋 *Описание:*\n{v['description']}\n\n"
        f"💰 *Оплата:* {v['pay']}\n\n"
        f"📞 *Контакт:* {v['contact']}\n\n"
        f"👤 Работодатель: {v['employer_name']}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def apply_vacancy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    vacancy_id = int(query.data.split("_")[1])
    promoter = query.from_user
    promoter_name = promoter.first_name or "Промоутер"
    promoter_username = promoter.username or ""
    promoter_id = promoter.id

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM vacancies WHERE id = %s", (vacancy_id,))
            v = cur.fetchone()

    if not v:
        await query.answer("❌ Вакансия не найдена.", show_alert=True)
        return

    employer_id = v["employer_id"]
    username_text = f"@{promoter_username}" if promoter_username else "нет username"

    # Notify employer
    try:
        await context.bot.send_message(
            chat_id=employer_id,
            text=(
                f"🔔 *Новый отклик на вакансию!*\n\n"
                f"📌 Вакансия: *{v['title']}*\n\n"
                f"👤 Промоутер: {promoter_name}\n"
                f"📱 Username: {username_text}\n"
                f"🆔 ID: `{promoter_id}`\n\n"
                f"Напишите ему напрямую в Telegram!"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Could not notify employer {employer_id}: {e}")

    # Notify admin
    await notify_admin(
        context,
        f"📨 *Отклик на вакансию*\n\n"
        f"📌 {v['title']}\n"
        f"👤 Промоутер: {promoter_name} ({username_text})\n"
        f"🏢 Работодатель: {v['employer_name']}"
    )

    # Confirm to promoter
    await query.edit_message_text(
        f"✅ *Отклик отправлен!*\n\n"
        f"Работодатель получил ваши контакты и скоро свяжется с вами.\n\n"
        f"📌 Вакансия: *{v['title']}*\n"
        f"👤 Работодатель: {v['employer_name']}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Смотреть другие вакансии", callback_data="view_vacancies")]
        ])
    )
    return PROMOTER_MENU


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("💬 Написать в поддержку", url="https://t.me/promohuntsupport")]]
    await update.message.reply_text(
        "🆘 *Поддержка PromoHunt*\n\n"
        "Если у вас есть вопросы или проблемы — напишите нам!\n\n"
        "👇 Нажмите кнопку ниже:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет доступа к этой команде.")
        return

    all_vacancies = db_get_all_vacancies()
    blacklist = db_get_blacklist()

    text = (
        f"👑 *Админ панель*\n\n"
        f"📋 Вакансий: {len(all_vacancies)}\n"
        f"🚫 В блэклисте: {len(blacklist)}\n"
    )

    keyboard = [
        [InlineKeyboardButton("🗑 Удалить вакансию", callback_data="admin_delete_menu")],
        [InlineKeyboardButton("🚫 Блэклист пользователей", callback_data="admin_blacklist_menu")],
        [InlineKeyboardButton("👥 Заблокировать работодателя", callback_data="admin_ban_menu")],
    ]
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    if query.data == "admin_delete_menu":
        all_vacancies = db_get_all_vacancies()
        if not all_vacancies:
            await query.edit_message_text("📭 Нет вакансий для удаления.")
            return
        keyboard = [
            [InlineKeyboardButton(f"🗑 {i+1}. {v['title']}", callback_data=f"admin_del_{v['id']}")]
            for i, v in enumerate(all_vacancies)
        ]
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_cancel")])
        await query.edit_message_text("Выберите вакансию для удаления:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith("admin_del_"):
        vacancy_id = int(query.data.split("_")[2])
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT title FROM vacancies WHERE id = %s", (vacancy_id,))
                row = cur.fetchone()
        if row:
            db_delete_vacancy(vacancy_id)
            await query.edit_message_text(f"✅ Вакансия *{row['title']}* удалена!", parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Вакансия не найдена.")

    elif query.data == "admin_blacklist_menu":
        blacklist = db_get_blacklist()
        if not blacklist:
            await query.edit_message_text(
                "🚫 *Блэклист пуст*\n\nЗаблокированных пользователей нет.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_cancel")]])
            )
            return
        text = "🚫 *Заблокированные пользователи:*\n\n"
        for u in blacklist:
            uname = f"@{u['username']}" if u['username'] else "без username"
            text += f"• {u['name']} ({uname}) — ID: `{u['user_id']}`\n"
        keyboard = [
            [InlineKeyboardButton(f"✅ Разблокировать {u['name']}", callback_data=f"admin_unban_{u['user_id']}")]
            for u in blacklist
        ]
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_cancel")])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith("admin_unban_"):
        user_id = int(query.data.split("_")[2])
        db_remove_from_blacklist(user_id)
        await query.edit_message_text("✅ Пользователь разблокирован.", parse_mode="Markdown")

    elif query.data == "admin_ban_menu":
        all_vacancies = db_get_all_vacancies()
        if not all_vacancies:
            await query.edit_message_text(
                "📭 Нет работодателей для блокировки.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_cancel")]])
            )
            return
        # Get unique employers
        seen = set()
        employers = []
        for v in all_vacancies:
            if v["employer_id"] not in seen:
                seen.add(v["employer_id"])
                employers.append(v)
        keyboard = [
            [InlineKeyboardButton(
                f"🚫 {v['employer_name']} (@{v['employer_username']})",
                callback_data=f"admin_ban_{v['employer_id']}"
            )]
            for v in employers
        ]
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_cancel")])
        await query.edit_message_text("Выберите кого заблокировать:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith("admin_ban_"):
        parts = query.data.split("_")
        user_id = int(parts[2])
        # Look up user details from vacancies
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT employer_name, employer_username FROM vacancies WHERE employer_id = %s LIMIT 1",
                    (user_id,)
                )
                row = cur.fetchone()
        if row:
            db_add_to_blacklist(user_id, row["employer_username"], row["employer_name"])
            await query.edit_message_text(
                f"🚫 *{row['employer_name']}* заблокирован.\n\nОн больше не сможет публиковать вакансии.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("❌ Пользователь не найден.")

    elif query.data == "admin_cancel":
        await query.edit_message_text("Панель закрыта. Напишите /admin чтобы открыть снова.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Отменено. Напишите /start чтобы начать заново."
    )
    return ConversationHandler.END


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🔄 Состояние сброшено. Напишите /start чтобы начать заново."
    )
    return ConversationHandler.END


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        logger.info(f"Health server listening on port {port}")
        server.serve_forever()
    except OSError:
        logger.warning(f"Port {port} already in use — health server skipped (OK in dev)")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_ROLE: [CallbackQueryHandler(choose_role, pattern="^role_")],
            EMPLOYER_MENU: [CallbackQueryHandler(employer_menu, pattern="^(post_vacancy|my_vacancies|back_employer)$")],
            PROMOTER_MENU: [
                CallbackQueryHandler(promoter_menu, pattern="^(view_vacancies|next_vacancy|prev_vacancy)$"),
                CallbackQueryHandler(apply_vacancy, pattern="^apply_\\d+$"),
            ],
            POST_VACANCY_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, vacancy_title)],
            POST_VACANCY_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, vacancy_desc)],
            POST_VACANCY_PAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, vacancy_pay)],
            POST_VACANCY_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, vacancy_contact)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("reset", reset),
            CommandHandler("start", start),
            CommandHandler("admin", admin),
            CommandHandler("support", support),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("support", support))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))

    db_init_tables()
    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info("PromoJob Bot started with PostgreSQL persistence")
    app.run_polling()


if __name__ == "__main__":
    main()
