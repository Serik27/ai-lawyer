import subprocess
import sys
import time
import signal
import os
import psutil
import logging
from typing import List, Tuple, Optional
from logging.handlers import RotatingFileHandler

# ==========================================
# ⚙️ КОНФІГУРАЦІЯ ТА ЛОГУВАННЯ
# ==========================================
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

# Налаштування логування для main.py
log_file_path = os.path.join(LOG_DIR, "system_manager.log")
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Використовуємо RotatingFileHandler для безпечного логування
file_handler = RotatingFileHandler(log_file_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
file_handler.setFormatter(log_formatter)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(log_formatter)

logger = logging.getLogger("SystemManager")
logger.setLevel(logging.INFO)
if logger.hasHandlers():
    logger.handlers.clear()
logger.addHandler(file_handler)
logger.addHandler(stream_handler)

ProcessInfo = Tuple[str, subprocess.Popen, float, int]
processes: List[ProcessInfo] = []

# ==========================================
# 🛠️ HELPER FUNCTIONS
# ==========================================

def cleanup_zombies():
    """Вбиває старі node / chrome процеси перед стартом."""
    logger.info("🧹 Глобальне очищення зомбі-процесів (Chrome/Node)...")
    killed_count = 0
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"] in ("node.exe", "node", "chrome.exe", "chrome", "chromedriver.exe"):
                proc.kill()
                killed_count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    logger.info(f"   -> Очищено {killed_count} процесів.")

def run_script(script_name: str, delay: int = 2) -> Optional[subprocess.Popen]:
    """Запускає python-скрипт в окремому процесі та повертає об'єкт Popen."""
    try:
        logger.info(f"🚀 Запуск {script_name}...")
        
        p = subprocess.Popen([sys.executable, "-u", script_name])
        
        time.sleep(delay) 
        return p
    except Exception as e:
        logger.error(f"❌ Не вдалося запустити {script_name}: {e}")
        return None
        
def start_system_process(script_name: str, delay: int = 2):
    """Створює новий процес та додає його до списку процесів."""
    p = run_script(script_name, delay)
    if p:
        processes.append((script_name, p, time.time(), 0))

def cleanup(signum, frame):
    """Обробник SIGINT (Ctrl+C) для коректного завершення роботи."""
    logger.warning("\n\n🛑 Отримано сигнал зупинки. Завершення роботи системи...")
    
    temp_processes = list(processes) 
    
    for name, p, _, _ in temp_processes:
        if p.poll() is None:
            logger.info(f"   -> Terminating: {name}")
            try:
                # Надсилаємо SIGTERM
                p.terminate() 
                # Чекаємо 5 секунд на коректне завершення
                p.wait(timeout=5) 
            except:
                # Якщо не завершився, вбиваємо
                logger.error(f"   -> {name} не завершився. Killing.")
                p.kill()
    
    logger.info("✅ Усі сервіси зупинено.")
    sys.exit(0)

# ==========================================
# 🔄 MAIN EXECUTION BLOCK
# ==========================================

if __name__ == "__main__":
    signal.signal(signal.SIGINT, cleanup)

    logger.info("=========================================")
    logger.info("====== АКТИВАЦІЯ СИСТЕМИ AI-ЮРИСТ =======")
    logger.info("=========================================")

    # 1. ПЕРШИЙ КРОК: ОЧИЩЕННЯ
    cleanup_zombies()
    time.sleep(1)

    # 2. ПЕРЕВІРКА/ІНІЦІАЛІЗАЦІЯ БД (Повинно бути sync)
    logger.info("🛠️  Перевірка та ініціалізація бази даних...")
    try:
        # Використовуємо database_setup для створення таблиць/перевірки
        subprocess.run([sys.executable, "database_setup.py"], check=True)
    except subprocess.CalledProcessError as e:
        logger.critical(f"❌ КРИТИЧНА ПОМИЛКА БД. Код виходу: {e.returncode}")
        logger.critical("💡 Підказка: Сервер PostgreSQL на порту 5433 не запущений (Connection refused).")
        logger.critical("   1. Переконайтеся, що Docker Desktop запущено.")
        logger.critical("   2. Виконайте в терміналі команду: docker compose up -d")
        logger.critical("   3. Або, якщо PostgreSQL встановлено локально у Windows, змініть порт у .env на 5432.")
        sys.exit(1)
    
    # 3. ЗАПУСК СЕРВІСІВ (Критичний порядок)
    
    # 3.1. RAG Worker (має бути готовий до роботи, бо він ресурсоємний)
    start_system_process("worker_rada.py", delay=8) 
    
    # 3.2. Collector (починає додавати завдання для worker)
    start_system_process("collector_rada.py", delay=5)
    
    # 3.3. Telegram Bot (основний інтерфейс)
    start_system_process("bot.py", delay=2) 
    
    # 3.4. Web Server (сайт та онлайн-консультант)
    start_system_process("web_server.py", delay=2) 
    
    logger.info("\n=======================================================")
    logger.info("✅ СИСТЕМА ПРАЦЮЄ! Натисніть Ctrl+C для зупинки.")
    logger.info("=======================================================")
    
    try:
        MAX_RESTARTS = 5 # Ліміт перезапусків для одного процесу
        RESTART_DELAY = 10 # Затримка перед перезапуском
        
        while True:
            time.sleep(5)
            
            # Створюємо копію, щоб можна було безпечно видаляти з processes
            processes_to_check = list(processes)
            
            for i, (name, p, last_start_time, restarts) in enumerate(processes_to_check):
                exit_code = p.poll()
                
                if exit_code is not None:
                    # Процес завершився/впав
                    
                    # 1. Логування причини падіння
                    logger.error(f"🔥 {name} впав! Код виходу: {exit_code}")
                    logger.error(f"   Перевірте логи вище для деталей помилки")
                    
                    # 2. Перевірка ліміту
                    if restarts >= MAX_RESTARTS:
                        logger.critical(f"❌ {name} перевищив ліміт перезапусків ({MAX_RESTARTS}). Зупинка автоматичного перезапуску.")
                        processes.remove((name, p, last_start_time, restarts))
                        continue

                    # 3. Підготовка до перезапуску
                    logger.warning(f"🔄 Перезапуск {name} через {RESTART_DELAY}с. (Спроба {restarts + 1}/{MAX_RESTARTS})")
                    time.sleep(RESTART_DELAY)
                    
                    # 4. Видалення старого процесу та запуск нового
                    processes.remove((name, p, last_start_time, restarts))
                    new_p = run_script(name, delay=2)
                    
                    if new_p:
                        # Додаємо новий процес з оновленими лічильниками
                        processes.append((name, new_p, time.time(), restarts + 1))
                    else:
                        logger.critical(f"❌ Не вдалося перезапустити {name}. Система деградує.")

            
    except KeyboardInterrupt:
        pass
    finally:
        cleanup(None, None)