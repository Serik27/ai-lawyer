import os
import sys
import json
import logging
from logging.handlers import RotatingFileHandler
from aiohttp import web
from dotenv import load_dotenv

# Локальні модулі проекту
import rag_service

load_dotenv()

# ==========================================
# ⚙️ КОНФІГУРАЦІЯ ТА ЛОГУВАННЯ
# ==========================================
PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", "8080")))
HOST = os.getenv("HOST", os.getenv("WEB_HOST", "0.0.0.0"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
INDEX_PATH = os.path.join(WEB_DIR, "index.html")
NOT_FOUND_PATH = os.path.join(WEB_DIR, "404.html")

# Логування
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
log_file_path = os.path.join(LOG_DIR, "web_server.log")
log_formatter = logging.Formatter("%(asctime)s - [Web] - %(levelname)s - %(message)s")

file_handler = RotatingFileHandler(log_file_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
file_handler.setFormatter(log_formatter)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(log_formatter)

logger = logging.getLogger("web_server")
logger.setLevel(logging.INFO)
if logger.hasHandlers():
    logger.handlers.clear()
logger.addHandler(file_handler)
logger.addHandler(stream_handler)

# Сховище сесій для ведення справ на сайті (in-memory з fallback)
case_sessions = {}

# ==========================================
# 🛡️ MIDDLEWARE ДЛЯ ОБРОБКИ ПОМИЛОК ТА 404
# ==========================================
@web.middleware
async def error_middleware(request, handler):
    try:
        response = await handler(request)
        if response.status == 404:
            # Якщо запит не до API, повертаємо стильну сторінку 404
            if not request.path.startswith("/api/"):
                if os.path.exists(NOT_FOUND_PATH):
                    return web.FileResponse(NOT_FOUND_PATH, status=404)
                return web.Response(text="404 — Статтю не знайдено", status=404)
        return response
    except web.HTTPNotFound:
        if not request.path.startswith("/api/"):
            if os.path.exists(NOT_FOUND_PATH):
                return web.FileResponse(NOT_FOUND_PATH, status=404)
            return web.Response(text="404 — Статтю не знайдено", status=404)
        return web.json_response({"status": "error", "error": "Not Found"}, status=404)
    except Exception as e:
        logger.error(f"Server error on {request.path}: {e}", exc_info=True)
        if request.path.startswith("/api/"):
            return web.json_response({"status": "error", "error": "Внутрішня помилка сервера"}, status=500)
        return web.Response(text="500 Internal Server Error", status=500)

# ==========================================
# 🌐 WEB ROUTE HANDLERS
# ==========================================

async def handle_index(request):
    """Головна сторінка сайту."""
    if os.path.exists(INDEX_PATH):
        return web.FileResponse(INDEX_PATH)
    return web.Response(text="Веб-додаток AI-Юрист активний", status=200)

async def handle_404_page(request):
    """Прямий маршрут до сторінки 404."""
    if os.path.exists(NOT_FOUND_PATH):
        return web.FileResponse(NOT_FOUND_PATH, status=404)
    return web.Response(text="404 — Статтю не знайдено", status=404)

async def handle_status(request):
    """API-статус сервісу."""
    return web.json_response({
        "status": "ok",
        "service": "AI-Юрист (Україна)",
        "version": "2.5.0",
        "bot_username": BOT_USERNAME,
        "features": {
            "hybrid_rag": True,
            "flashrank_rerank": True,
            "rada_gov_sync": True,
            "web_and_telegram": True
        }
    })

async def handle_suggestions(request):
    """API популярних правових питань."""
    return web.json_response({
        "status": "ok",
        "categories": [
            {
                "title": "Адміністративне право та ПДР",
                "code": "КУпАП",
                "prompts": [
                    "Який штраф за перевищення швидкості на 20 км/год і коли діє знижка 50%?",
                    "Як правильно оскаржити постанову патрульної поліції?"
                ]
            },
            {
                "title": "Цивільне та договірне право",
                "code": "ЦКУ",
                "prompts": [
                    "Як розірвати договір оренди квартири в односторонньому порядку?",
                    "Який строк позовної давності для повернення боргу за розпискою?"
                ]
            },
            {
                "title": "Трудове право та воєнний стан",
                "code": "КЗпП",
                "prompts": [
                    "Які підстави звільнення працівника за власним бажанням під час воєнного стану?",
                    "Чи має право роботодавець призупинити дію трудового договору?"
                ]
            },
            {
                "title": "Бізнес та оподаткування",
                "code": "ПКУ",
                "prompts": [
                    "Які ліміти доходу та ставки єдиного податку для ФОП 3 групи?",
                    "Як правильно ліквідувати ФОП через Дію?"
                ]
            }
        ]
    })

async def handle_chat(request):
    """
    Головний API-ендпоінт консультації.
    Обробляє запити як у швидкому режимі, так і у режимі ведення справи.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "error": "Некоректний JSON"}, status=400)

    user_message = (data.get("message") or "").strip()
    session_id = data.get("session_id") or "guest"
    mode = data.get("mode") or "quick"
    client_summary = data.get("summary") or ""

    if not user_message:
        return web.json_response({"status": "error", "error": "Повідомлення не може бути порожнім"}, status=400)

    logger.info(f"📨 Новий запит з сайту [mode={mode}, sid={session_id[:12]}]: '{user_message[:80]}...'")

    # Контекст бесіди
    current_summary = ""
    if mode == "case":
        # Якщо клієнт передав summary або ми маємо збережене у пам'яті
        current_summary = client_summary or case_sessions.get(session_id, {}).get("summary", "")

    try:
        # Звернення до загального RAG-сервісу проекту
        response_text = await rag_service.get_legal_advice(
            user_input=user_message,
            summary=current_summary
        )

        updated_summary = current_summary
        if mode == "case":
            try:
                updated_summary = await rag_service.summarize_dialogue(
                    current_summary=current_summary,
                    user_text=user_message,
                    bot_text=response_text
                )
                if session_id not in case_sessions:
                    case_sessions[session_id] = {"messages": [], "summary": ""}
                case_sessions[session_id]["summary"] = updated_summary
                case_sessions[session_id]["messages"].append({"role": "user", "text": user_message})
                case_sessions[session_id]["messages"].append({"role": "ai", "text": response_text})
            except Exception as sum_err:
                logger.warning(f"Summarize warning: {sum_err}")

        logger.info(f"✅ Відповідь згенеровано успішно (довжина: {len(response_text)} символів)")

        return web.json_response({
            "status": "ok",
            "response": response_text,
            "session_id": session_id,
            "summary": updated_summary,
            "mode": mode
        })

    except Exception as e:
        logger.error(f"❌ Помилка RAG-сервісу: {e}", exc_info=True)
        return web.json_response({
            "status": "error",
            "error": "Сталася технічна помилка під час формування консультації. Спробуйте ще раз."
        }, status=500)

# ==========================================
# 🚀 ІНІЦІАЛІЗАЦІЯ ДОДАТКУ
# ==========================================
def create_app():
    app = web.Application(middlewares=[error_middleware])

    # Маршрути API
    app.router.add_post("/api/chat", handle_chat)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/suggestions", handle_suggestions)

    # Веб-сторінки
    app.router.add_get("/", handle_index)
    app.router.add_get("/index.html", handle_index)
    app.router.add_get("/404", handle_404_page)
    app.router.add_get("/404.html", handle_404_page)

    # Статичні файли (CSS, JS)
    css_dir = os.path.join(WEB_DIR, "css")
    js_dir = os.path.join(WEB_DIR, "js")
    if os.path.exists(css_dir):
        app.router.add_static("/css", css_dir, name="css")
    if os.path.exists(js_dir):
        app.router.add_static("/js", js_dir, name="js")

    return app

if __name__ == "__main__":
    logger.info(f"🚀 Запуск веб-сервера AI-Юрист на http://{HOST}:{PORT}...")
    app = create_app()
    try:
        web.run_app(app, host=HOST, port=PORT, print=print)
    except KeyboardInterrupt:
        logger.info("🛑 Веб-сервер зупинено.")

