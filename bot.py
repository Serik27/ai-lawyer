import asyncio
import os
import logging
import re
import sys
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import CommandStart, StateFilter
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest

# --- ЛОКАЛЬНІ ІМПОРТИ ---
from database_setup import (
    SessionLocal, TelegramUser, ChatMessage, LegalCase, SystemConfig, create_db_and_tables
)
import rag_service as rag_service 

# ==========================================
# ⚙️ КОНФІГУРАЦІЯ
# ==========================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Отримуємо ID адмінів з .env (формат: ADMIN_IDS=12345,67890)
raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in raw_admins.split(",") if x.strip().isdigit()]

if not BOT_TOKEN:
    raise ValueError("❌ Не знайдено BOT_TOKEN у файлі .env")

# Налаштування логування для bot.py (комерційний формат)
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
log_file_path = os.path.join(LOG_DIR, "bot.log")
log_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

file_handler = RotatingFileHandler(log_file_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
file_handler.setFormatter(log_formatter)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(log_formatter)

logger = logging.getLogger("bot")
logger.setLevel(logging.INFO)
if logger.hasHandlers():
    logger.handlers.clear()
logger.addHandler(file_handler)
logger.addHandler(stream_handler)

router = Router()

# ==========================================
# 🧠 FSM STATES (Стани бота)
# ==========================================
class BotStates(StatesGroup):
    Menu = State()              # Головне меню
    QuickAnswer = State()       # Режим швидкої відповіді (без історії)
    MyCases = State()           # Список справ
    ActiveCase = State()        # Спілкування в рамках справи
    CreatingCase = State()      # Введення назви нової справи

# ==========================================
# ⌨️ КЛАВІАТУРИ
# ==========================================

def get_main_keyboard(user_id: int):
    """Головне меню. Кнопка адміна з'являється тільки для обраних."""
    buttons = [
        [KeyboardButton(text="⚡ Швидка відповідь"), KeyboardButton(text="📂 Мої справи")]
    ]
    if user_id in ADMIN_IDS:
        buttons.append([KeyboardButton(text="⚙️ Панель Адміністратора")])
    
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, persistent=True)

def get_admin_keyboard():
    """Інлайн-кнопки керування колектором."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚡ Force Run (Scan 1-5)", callback_data="admin_scan_now"),
            InlineKeyboardButton(text="🔄 Оновити статус", callback_data="admin_status")
        ],
        [InlineKeyboardButton(text="📚 Повний парсинг (Reset All)", callback_data="admin_scan_full")],
        [InlineKeyboardButton(text="⏰ Режим 'Щогодини'", callback_data="admin_scan_hourly")],
        [InlineKeyboardButton(text="🛑 Зупинити Колектор", callback_data="admin_scan_stop")],
        [InlineKeyboardButton(text="❎ Закрити панель", callback_data="admin_close")]
    ])

def get_cases_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Створити справу"), KeyboardButton(text="❌ Видалити справу")],
            [KeyboardButton(text="🔙 Головне меню")]
        ],
        resize_keyboard=True
    )

def get_back_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔙 Назад до справ")]],
        resize_keyboard=True
    )

def get_cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔙 Скасувати")]],
        resize_keyboard=True
    )

# ==========================================
# 🛠️ СИСТЕМНІ ФУНКЦІЇ (DB Helpers)
# ==========================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_system_status_text() -> str:
    """Формує текст статусу системи для адміна."""
    session = SessionLocal()
    try:
        mode = session.get(SystemConfig, "collector_mode")
        page = session.get(SystemConfig, "current_page")
        last_ts = session.get(SystemConfig, "last_run_timestamp")
        
        mode_val = mode.value if mode else "N/A"
        page_val = page.value if page else "N/A"
        
        last_run_str = "Ніколи"
        if last_ts and last_ts.value != "0":
            import datetime
            dt = datetime.datetime.fromtimestamp(float(last_ts.value))
            last_run_str = dt.strftime("%H:%M:%S")

        status_emoji = "🟢" if mode_val == "hourly" else "🟡" if mode_val == "full" else "🔴"

        return (
            f"📊 **СТАТУС СИСТЕМИ**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Режим: {status_emoji} `{mode_val}`\n"
            f"Сторінка парсингу: `{page_val}`\n"
            f"Останній запуск: `{last_run_str}`"
        )
    finally:
        session.close()

def set_system_config(key: str, value: str):
    """Оновлює налаштування в БД."""
    session = SessionLocal()
    try:
        obj = session.get(SystemConfig, key)
        if not obj:
            obj = SystemConfig(key=key)
            session.add(obj)
        obj.value = str(value)
        session.commit()
    finally:
        session.close()

# ==========================================
# 👮 ADMIN HANDLERS (Панель керування)
# ==========================================

@router.message(F.text == "⚙️ Панель Адміністратора")
async def admin_panel_open(message: Message):
    if not is_admin(message.from_user.id):
        return # Ігноруємо не адмінів
    
    text = get_system_status_text()
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_keyboard())

@router.callback_query(F.data.startswith("admin_"))
async def admin_actions(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ заборонено", show_alert=True)
        return

    action = callback.data
    notification = ""

    if action == "admin_scan_now":
        set_system_config("force_run", "true")
        notification = "⚡ Команду Force Run відправлено!"
    
    elif action == "admin_scan_full":
        set_system_config("collector_mode", "full")
        set_system_config("current_page", "1")
        notification = "📚 Запущено повний парсинг (Reset to Page 1)."
    
    elif action == "admin_scan_stop":
        set_system_config("collector_mode", "stop")
        notification = "🛑 Парсер зупиняється..."

    elif action == "admin_scan_hourly":
        set_system_config("collector_mode", "hourly")
        notification = "⏰ Встановлено погодинний режим."

    elif action == "admin_status":
        notification = "Статус оновлено."
    
    elif action == "admin_close":
        await callback.message.delete()
        return

    new_text = get_system_status_text()
    try:
        await callback.message.edit_text(
            new_text, 
            parse_mode=ParseMode.MARKDOWN, 
            reply_markup=get_admin_keyboard()
        )
    except TelegramBadRequest:
        pass
    
    await callback.answer(notification)

# ==========================================
# 👤 USER & CASE HANDLERS (Стандартні функції)
# ==========================================

# --- DB HELPERS FOR USERS ---
def get_or_create_user(telegram_user: types.User):
    session = SessionLocal()
    try:
        user = session.query(TelegramUser).filter_by(telegram_id=telegram_user.id).first()
        if not user:
            user = TelegramUser(
                telegram_id=telegram_user.id,
                full_name=telegram_user.full_name,
                username=telegram_user.username
            )
            session.add(user)
            session.commit()
    finally:
        session.close()

def get_user_cases(telegram_id: int):
    session = SessionLocal()
    try:
        user = session.query(TelegramUser).filter_by(telegram_id=telegram_id).first()
        if user:
            return session.query(LegalCase).filter_by(user_id=user.id).order_by(LegalCase.created_at.desc()).all()
        return []
    finally:
        session.close()

def create_new_case(telegram_id: int, title: str):
    session = SessionLocal()
    try:
        user = session.query(TelegramUser).filter_by(telegram_id=telegram_id).first()
        if user:
            new_case = LegalCase(user_id=user.id, title=title)
            session.add(new_case)
            session.commit()
            return new_case.id
        return None
    finally:
        session.close()

def delete_user_case(case_id: int):
    session = SessionLocal()
    try:
        case = session.get(LegalCase, case_id)
        if case:
            session.delete(case)
            session.commit()
            return True
        return False
    finally:
        session.close()

def save_message_to_case(case_id: int, role: str, content: str):
    session = SessionLocal()
    try:
        msg = ChatMessage(case_id=case_id, role=role, content=content)
        session.add(msg)
        session.commit()
    finally:
        session.close()

def get_case_summary_and_update(case_id: int, new_summary: str = None):
    session = SessionLocal()
    try:
        case = session.get(LegalCase, case_id)
        if not case: return ""
        if new_summary:
            case.summary = new_summary
            session.commit()
        return case.summary or ""
    finally:
        session.close()

# --- FORMATTING HELPERS ---
def md_to_html(text: str) -> str:
    """Конвертує Markdown від AI в HTML для Telegram."""
    if not text: return ""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\[([^\]]+)\]\s*\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    text = re.sub(r'(^|\n)\s*\*\s+', r'\1- ', text)
    text = re.sub(r'^#{1,6}\s*(.+)$', r'\n<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'___\n', '-------------------\n', text)
    return text

async def safe_send_response(message: Message, text: str):
    """Відправляє довгий текст частинами."""
    html = md_to_html(text)
    chunks = [html[i:i+4000] for i in range(0, len(html), 4000)]
    
    for chunk in chunks:
        try:
            await message.answer(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except TelegramBadRequest:
            await message.answer(chunk.replace("<", "").replace(">", ""), parse_mode=None)
        await asyncio.sleep(0.2)

# --- START & MENU ---

@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext):
    get_or_create_user(message.from_user)
    await state.clear()
    await state.set_state(BotStates.Menu)
    await message.answer(
        "🇺🇦 **Вітаю в системі AI-Юрист!**\n\n"
        "Я моніторю сайт Верховної Ради та допомагаю з пошуком законодавства.\n"
        "Оберіть режим роботи:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@router.message(F.text == "🔙 Головне меню")
async def back_to_main(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(BotStates.Menu)
    await message.answer("Ви в головному меню.", reply_markup=get_main_keyboard(message.from_user.id))

# --- QUICK ANSWER ---

@router.message(F.text == "⚡ Швидка відповідь")
async def mode_quick_answer(message: Message, state: FSMContext):
    await state.set_state(BotStates.QuickAnswer)
    await message.answer(
        "**Режим швидкої консультації**\n"
        "Задайте ваше питання. Історія не зберігається.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔙 Головне меню")]], resize_keyboard=True)
    )

@router.message(StateFilter(BotStates.QuickAnswer))
async def handle_quick_qa(message: Message):
    if message.text == "🔙 Головне меню": return # Обробиться хендлером вище
    
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    response_text = await rag_service.get_legal_advice(user_input=message.text, summary="")
    await safe_send_response(message, response_text)

# --- MY CASES (CRUD) ---

@router.message(F.text == "📂 Мої справи")
async def mode_my_cases(message: Message, state: FSMContext):
    get_or_create_user(message.from_user)
    await state.set_state(BotStates.MyCases)
    cases = get_user_cases(message.from_user.id)
    
    if not cases:
        await message.answer("Справ немає. Створіть нову.", reply_markup=get_cases_keyboard())
    else:
        kb_rows = [[InlineKeyboardButton(text=f"📂 {case.title}", callback_data=f"open:{case.id}")] for case in cases]
        await message.answer(
            "Ваші справи (натисніть, щоб відкрити):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
        )
        await message.answer("Меню:", reply_markup=get_cases_keyboard())

# --- CREATE CASE ---
@router.message(F.text == "➕ Створити справу", StateFilter(BotStates.MyCases))
async def start_create_case(message: Message, state: FSMContext):
    await state.set_state(BotStates.CreatingCase)
    await message.answer("Введіть назву для нової справи:", reply_markup=get_cancel_keyboard())

@router.message(StateFilter(BotStates.CreatingCase))
async def finish_create_case(message: Message, state: FSMContext):
    if message.text == "🔙 Скасувати":
        await mode_my_cases(message, state)
        return

    case_id = create_new_case(message.from_user.id, message.text)
    if case_id:
        await state.update_data(active_case_id=case_id)
        await state.set_state(BotStates.ActiveCase)
        await message.answer(
            f"✅ Справа **'{message.text}'** створена!\nПишіть ваше запитання.", 
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_back_keyboard()
        )
    else:
        await message.answer("Помилка створення.")
        await mode_my_cases(message, state)

# --- DELETE CASE ---
@router.message(F.text == "❌ Видалити справу", StateFilter(BotStates.MyCases))
async def list_delete_case(message: Message):
    cases = get_user_cases(message.from_user.id)
    if not cases:
        await message.answer("Немає що видаляти.")
        return
    kb_rows = [[InlineKeyboardButton(text=f"❌ {case.title}", callback_data=f"del:{case.id}")] for case in cases]
    await message.answer("Оберіть справу для видалення:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))

@router.callback_query(F.data.startswith("del:"))
async def confirm_delete(callback: CallbackQuery, state: FSMContext):
    case_id = int(callback.data.split(":")[1])
    if delete_user_case(case_id):
        await callback.message.edit_text("🗑️ Справу видалено.")
        await mode_my_cases(callback.message, state)
    else:
        await callback.answer("Помилка.", show_alert=True)

# --- ACTIVE CASE CHAT ---

@router.callback_query(F.data.startswith("open:"))
async def open_case_callback(callback: CallbackQuery, state: FSMContext):
    case_id = int(callback.data.split(":")[1])
    await state.update_data(active_case_id=case_id)
    await state.set_state(BotStates.ActiveCase)
    await callback.message.answer("📂 Справа відкрита. Я слухаю.", reply_markup=get_back_keyboard())
    await callback.answer()

@router.message(F.text == "🔙 Назад до справ", StateFilter(BotStates.ActiveCase))
async def exit_case(message: Message, state: FSMContext):
    await mode_my_cases(message, state)

@router.message(StateFilter(BotStates.ActiveCase))
async def handle_case_message(message: Message, state: FSMContext):
    data = await state.get_data()
    case_id = data.get("active_case_id")
    
    if not case_id:
        await message.answer("Помилка контексту.")
        await mode_my_cases(message, state)
        return

    save_message_to_case(case_id, "user", message.text)
    
    current_summary = get_case_summary_and_update(case_id)

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    response_text = await rag_service.get_legal_advice(
        user_input=message.text,
        summary=current_summary
    )
    
    await safe_send_response(message, response_text)
    
    save_message_to_case(case_id, "ai", response_text)
    new_summary = await rag_service.summarize_dialogue(current_summary, message.text, response_text)
    get_case_summary_and_update(case_id, new_summary)

# ==========================================
# 🚀 ЗАПУСК
# ==========================================

async def main():
    session = AiohttpSession(timeout=120.0)
    bot = Bot(token=BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=None))
    
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    create_db_and_tables()
    
    print("✅ [Bot] AI-Юрист запущено...")
    print(f"👮 Admin IDs: {ADMIN_IDS}")
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Бот зупинений.")