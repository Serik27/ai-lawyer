import time
import random
import os
import atexit
import datetime
import logging
import sys
from urllib.parse import urljoin
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium_stealth import stealth
from logging.handlers import RotatingFileHandler
from database_setup import SessionLocal, Source, Document, SystemConfig

# ==========================================
# ⚙️ КОНФІГУРАЦІЯ ТА ЛОГУВАННЯ
# ==========================================
load_dotenv()

START_URL = "https://zakon.rada.gov.ua/laws/main/a/page"
SOURCE_NAME = "Верховна Рада України (Всі акти)"

# Налаштування логування
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
log_file_path = os.path.join(LOG_DIR, "collector.log")
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Використовуємо RotatingFileHandler
file_handler = RotatingFileHandler(log_file_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
file_handler.setFormatter(log_formatter)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(log_formatter)

logger = logging.getLogger("collector")
logger.setLevel(logging.INFO)
if logger.hasHandlers():
    logger.handlers.clear()
logger.addHandler(file_handler)
logger.addHandler(stream_handler)

# Глобальна змінна для драйвера
driver = None

# Список слів у заголовках, які ми ігноруємо (адміністративні питання)
IGNORE_TITLES = [
    "про призначення", "про звільнення", "про нагородження", 
    "про присвоєння", "про утворення", "про перейменування",
    "про затвердження змін до складу", "про відзначення",
    "про вихід з", "про делегацію", "про грант", 
    "про внесення змін до деяких постанов",
    "про застосування персональних спеціальних", 
    "про схвалення пропозицій", "про перерозподіл видатків"
]

# ==========================================
# 🛠️ РОБОТА З CONFIG (DB)
# ==========================================

def get_config(key: str, default: str) -> str:
    """Отримує налаштування з БД."""
    session = SessionLocal()
    try:
        obj = session.get(SystemConfig, key)
        return obj.value if obj else default
    except Exception as e:
        logger.error(f"DB Read Error: {e}")
        return default
    finally:
        session.close()

def set_config(key: str, value: str):
    """Записує налаштування в БД."""
    session = SessionLocal()
    try:
        obj = session.get(SystemConfig, key)
        if not obj:
            obj = SystemConfig(key=key)
            session.add(obj)
        obj.value = str(value)
        session.commit()
    except Exception as e:
        logger.error(f"DB Write Error: {e}")
    finally:
        session.close()

# ==========================================
# 🌐 SELENIUM DRIVER SETUP
# ==========================================

def setup_driver():
    """Ініціалізація браузера з маскуванням."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")
    
    d = webdriver.Chrome(options=options)
    
    # Використання stealth для обходу захисту від ботів
    stealth(d,
        languages=["uk-UA", "uk"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel Iris OpenGL Engine",
        fix_hairline=True,
    )
    return d

def ensure_driver():
    """Перевіряє, чи активний драйвер, якщо ні - запускає."""
    global driver
    if driver is None:
        try:
            logger.info("🖥️ Запуск Chrome Driver...")
            driver = setup_driver()
        except Exception as e:
            logger.error(f"❌ Критична помилка запуску драйвера: {e}")
            raise e

def close_driver():
    """Безпечне закриття драйвера."""
    global driver
    if driver:
        try:
            driver.quit()
            logger.info("🖥️ Chrome Driver зупинено.")
        except Exception:
            pass
        driver = None

atexit.register(close_driver)

# ==========================================
# 🔍 ПАРСИНГ ТА ЛОГІКА
# ==========================================

def parse_date(date_str):
    """Перетворює рядок дати 'DD.MM.YYYY' у datetime об'єкт."""
    try:
        return datetime.datetime.strptime(date_str.strip(), "%d.%m.%Y")
    except:
        return None

def is_garbage(title: str) -> bool:
    """Перевіряє, чи є документ 'адміністративним сміттям'."""
    t = title.lower()
    return any(x in t for x in IGNORE_TITLES)

def scan_pages_range(start_page: int, end_page: int, stop_on_no_new: bool = False) -> int:
    """
    Сканує діапазон сторінок сайту ВР.
    
    Args:
        start_page: Початкова сторінка.
        end_page: Кінцева сторінка.
        stop_on_no_new: Якщо True, зупиняється, якщо на сторінці немає жодного нового документу.
        
    Returns:
        int: Кількість доданих нових документів, або -1 якщо сторінки закінчились.
    """
    global driver
    ensure_driver()
    
    session = SessionLocal()
    total_added = 0
    
    # Отримуємо або створюємо джерело в БД
    source = session.query(Source).filter_by(name=SOURCE_NAME).first()
    if not source:
        source = Source(name=SOURCE_NAME, base_url="https://zakon.rada.gov.ua")
        session.add(source)
        session.commit()
    source_id = source.id

    try:
        for page_num in range(start_page, end_page + 1):
            # Формуємо URL: показувати по 100 документів на сторінці (/sp:max100)
            url = f"{START_URL}{page_num}/sp:max100"
            logger.info(f"📄 Сканування сторінки {page_num}...")
            
            try:
                driver.get(url)
                # Чекаємо трохи, щоб не отримати бан (імітація людини)
                time.sleep(random.uniform(2.5, 5.0))
            except Exception as e:
                logger.error(f"Помилка завантаження сторінки {url}: {e}")
                # Якщо помилка мережі, пробуємо перезапустити драйвер і продовжити
                close_driver()
                ensure_driver()
                continue

            # Перевірка на кінець списку
            if "нічого не знайдено" in driver.page_source.lower():
                logger.info("🏁 Досягнуто кінця списку документів.")
                return -1

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            doc_items = soup.select("ol.doc-list > li")
            
            if not doc_items:
                logger.warning(f"⚠️ Елементів не знайдено на сторінці {page_num} (можливий бан або помилка верстки).")
                # Якщо це перша сторінка і порожньо - це підозріло, краще вийти
                if page_num == 1: break
                continue

            page_new_count = 0
            
            for item in doc_items:
                link_tag = item.select_one("div.doc > a")
                if not link_tag: continue
                
                href = link_tag.get('href')
                # Фільтруємо посилання на картки (/card/), беремо тільки тексти (/laws/, /go/)
                if not href or "/card/" in href: continue

                # Нормалізація URL
                raw_url = urljoin("https://zakon.rada.gov.ua", href)
                # Відрізаємо параметри сортування та версію для друку
                clean_url = raw_url.split('/sp:')[0].split('?')[0].rstrip('/')
                if clean_url.endswith('/print'): clean_url = clean_url[:-6]
                
                title_text = " ".join(link_tag.get_text(strip=True).split())

                # Фільтр "сміття"
                if is_garbage(title_text):
                    continue

                # Перевіряємо наявність в БД
                exists = session.query(Document).filter_by(url=clean_url).first()
                
                if not exists:
                    # --- ПАРСИНГ МЕТАДАНИХ ---
                    css_classes = link_tag.get("class", [])
                    act_status = "unknown"
                    if "valid" in css_classes: act_status = "active"
                    elif "notforce" in css_classes: act_status = "pending_enactment"
                    elif "invalid" in css_classes: act_status = "repealed"

                    doc_card = item.select_one("div.doc-card")
                    parsed_date = None
                    parsed_number = None
                    
                    if doc_card:
                        # Дата зазвичай в span
                        date_span = doc_card.select_one("span")
                        if date_span: parsed_date = parse_date(date_span.get_text(strip=True))
                        
                        # Номер зазвичай в strong
                        num_strong = doc_card.select_one("strong")
                        if num_strong: parsed_number = num_strong.get_text(strip=True)

                    # Визначаємо чи це законопроект
                    is_bill = "проект" in title_text.lower()
                    
                    # Створення запису. Статус 'pending' означає, що worker.py забере його.
                    new_doc = Document(
                        url=clean_url,
                        title=title_text[:500], # Обрізаємо якщо занадто довгий
                        status="pending", 
                        source_id=source_id,
                        act_status=act_status,
                        adoption_date=parsed_date,
                        registration_number=parsed_number,
                        is_bill=is_bill,
                        last_checked=datetime.datetime.now(datetime.timezone.utc)
                    )
                    session.add(new_doc)
                    page_new_count += 1
            
            session.commit()
            total_added += page_new_count
            logger.info(f"   -> Сторінка {page_num}: знайдено {len(doc_items)}, додано {page_new_count} нових.")

            # Логіка для режиму 'hourly':
            # Якщо ми на сторінці > 3 і не знайшли нічого нового, значить далі тільки старі закони.
            if stop_on_no_new and page_new_count == 0 and page_num > 3:
                logger.info("💤 Нові документи закінчились (smart-stop).")
                break

    except Exception as e:
        logger.error(f"Scan Loop Error: {e}")
        session.rollback()
    finally:
        session.close()
    
    return total_added

# ==========================================
# 🔄 ГОЛОВНИЙ ЦИКЛ (MAIN LOOP)
# ==========================================

def run_collector_loop():
    logger.info("🚀 Collector Service Started (DB-Controlled).")
    
    # Інтервал перевірки команд (секунди)
    CHECK_INTERVAL = 15 
    
    while True:
        try:
            # 1. Зчитуємо команди з БД
            mode = get_config("collector_mode", "hourly")       # Режими: 'hourly', 'full', 'stop'
            force = get_config("force_run", "false") == "true"  # Примусовий запуск
            current_page = int(get_config("current_page", "1")) # Поточна сторінка для 'full'
            last_run_ts = float(get_config("last_run_timestamp", "0"))
            
            now_ts = datetime.datetime.now().timestamp()
            
            # --- РЕЖИМ 1: ЗУПИНЕНО ---
            if mode == "stop":
                if driver: 
                    close_driver()
                    logger.info("🛑 Парсер в режимі STOP. Очікування...")
                time.sleep(CHECK_INTERVAL)
                continue

            # --- РЕЖИМ 2: ПРИМУСОВА ПЕРЕВІРКА ---
            if force:
                logger.info("⚡ Отримано команду Force Run!")
                # Скануємо перші 5 сторінок, щоб знайти найсвіжіші документи
                scan_pages_range(1, 5, stop_on_no_new=True)
                
                # Скидаємо прапорець та оновлюємо час
                set_config("force_run", "false")
                set_config("last_run_timestamp", str(now_ts))
                continue

            # --- РЕЖИМ 3: ПОВНИЙ ПАРСИНГ (FULL) ---
            if mode == "full":
                logger.info(f"📚 Повний парсинг: обробка сторінки {current_page}...")
                
                # Скануємо тільки одну сторінку за ітерацію циклу
                result = scan_pages_range(current_page, current_page, stop_on_no_new=False)
                
                if result == -1: 
                    # Якщо scan_pages_range повернув -1, значить сторінки закінчились
                    logger.info("✅ Повний парсинг завершено! Всі сторінки перевірено.")
                    
                    # Повертаємось в режим погодинного моніторингу
                    set_config("collector_mode", "hourly")
                    # Скидаємо лічильник сторінок на 1 (аналог очищення rada_state.txt)
                    set_config("current_page", "1")
                    set_config("last_run_timestamp", str(now_ts))
                else:
                    # Якщо все ок, записуємо наступну сторінку в БД
                    set_config("current_page", str(current_page + 1))
                    
                    # Коротка пауза між сторінками для безпеки
                    time.sleep(random.uniform(3, 7)) 
                
                continue

            # --- РЕЖИМ 4: ПОГОДИННИЙ МОНІТОРИНГ (HOURLY) ---
            if mode == "hourly":
                # Перевіряємо, чи пройшла година (3600 секунд)
                if (now_ts - last_run_ts) >= 3600:
                    logger.info("⏰ Час для планової погодинної перевірки...")
                    # Перевіряємо перші 5 сторінок (цього достатньо для новин)
                    scan_pages_range(1, 5, stop_on_no_new=True)
                    set_config("last_run_timestamp", str(now_ts))
                else:
                    # Якщо час ще не настав, просто чекаємо
                    # Але ми все одно робимо короткі ітерації, щоб швидко реагувати на зміну режиму
                    pass
                
                time.sleep(CHECK_INTERVAL)
                continue

        except KeyboardInterrupt:
            logger.info("👋 Зупинка користувачем (Ctrl+C).")
            close_driver()
            break
        except Exception as e:
            logger.error(f"🔥 Критична помилка в головному циклі: {e}")
            logger.info("🔄 Перезапуск циклу через 60 секунд...")
            close_driver()
            time.sleep(60)

if __name__ == "__main__":
    run_collector_loop()