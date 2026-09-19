import os
import time
import random
import re
import logging
import datetime
import asyncio
import hashlib
import nest_asyncio
from curl_cffi.requests import AsyncSession
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify as md
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler
from typing import List, Dict, Optional

try:
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
except ImportError:
    print("⚠️ Не знайдено langchain бібліотек. Векторизація працюватиме через API, якщо налаштовано.")
    GoogleGenerativeAIEmbeddings = None

# Local imports
from database_setup import SessionLocal, Document, DocumentChunk
from publishers import detect_publisher

nest_asyncio.apply()
load_dotenv()

# ==============================================================================
# ⚙️ CONFIG & LOGGING
# ==============================================================================
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

log_file_path = os.path.join(LOG_DIR, "worker_async.log")
log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

file_handler = RotatingFileHandler(log_file_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
file_handler.setFormatter(log_formatter)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)

logger = logging.getLogger("rada_worker_async")
logger.setLevel(logging.INFO)
if logger.hasHandlers():
    logger.handlers.clear()
logger.addHandler(file_handler)
logger.addHandler(stream_handler)

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# --- TUNING ---
BATCH_SIZE = 50
MAX_CONCURRENT_CURL = 10    
MAX_CONCURRENT_PW = 5       
BROWSER_RESTART_EVERY = 50 
CHUNK_HARD_LIMIT = 15000

# --- MODELS INITIALIZATION ---
embedding_model = None
if GOOGLE_API_KEY and GoogleGenerativeAIEmbeddings:
    try:
        embedding_model = GoogleGenerativeAIEmbeddings(
            model="models/text-embedding-004", 
            google_api_key=GOOGLE_API_KEY
        )
        logger.info("✅ Embedding Model initialized")
    except Exception as e:
        logger.warning(f"❌ Не вдалося ініціалізувати embeddings: {e}")
else:
    logger.warning("⚠️ Google API Key або бібліотеки відсутні.")

# ==============================================================================
# 🛠️ HELPER FUNCTIONS
# ==============================================================================

def get_random_proxy():
    if not os.path.exists("proxies.txt"): return None
    try:
        with open("proxies.txt", "r", encoding="utf-8") as f:
            proxies = [line.strip() for line in f if line.strip()]
        if not proxies: return None
        raw_proxy = random.choice(proxies)
        if "://" not in raw_proxy:
            parts = raw_proxy.split(":")
            if len(parts) == 4:
                return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
            elif len(parts) == 2:
                return f"http://{raw_proxy}"
        return raw_proxy
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        return None

def calculate_md5(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()

def clean_markdown(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"^\s*Друкувати.*?mouse\s+wheel", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"(Попередній\s*\|\s*Наступний|Зміст)", "", text, flags=re.IGNORECASE)
    return text.strip()

def preprocess_html(soup: BeautifulSoup) -> BeautifulSoup:
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe", "form", "noindex"]):
        tag.decompose()
    for div in soup.find_all("div"):
        if len(str(div)) > 100000: continue
        text = div.get_text(strip=True)
        if "Відбувається форматування тексту" in text or "Почекайте кілька секунд" in text:
            div.decompose()
    for tag in soup.find_all(class_=["btn", "nav", "social", "warn", "doc-card", "card", "appeal-form", "n-p", "txt-format-msg"]):
        tag.decompose()
    for table in soup.find_all("table"):
        if "class" in table.attrs and any(cls in table["class"] for cls in ["header_table", "footer_table"]):
            table.decompose()
            continue
        try:
            md_table = md(str(table), heading_style="ATX")
            new_node = soup.new_string(f"\n\n{md_table}\n\n")
            table.replace_with(new_node)
        except Exception: table.decompose()
    return soup

def find_text_container(soup: BeautifulSoup) -> Optional[Tag]:
    selectors = [
        lambda s: s.find("div", id="article"),
        lambda s: s.find("div", id="Text"),
        lambda s: s.find("div", class_="txt"),
        lambda s: s.find("div", class_="text"),
        lambda s: s.find("div", id="text_content"),
        lambda s: s.find("div", class_="doc-content"),
        lambda s: s.find("pre"),
        lambda s: s.find("main"),
    ]
    for sel in selectors:
        try:
            found = sel(soup)
            if found:
                if found.name == "pre" and found.parent: return found.parent
                return found
        except Exception: continue
    return None

def extract_metadata_from_soup(soup, raw_text: str) -> Dict:
    meta = {"doc_type": "other", "publisher": "other", "status_override": None}
    text_lower = raw_text[:5000].lower()
    h1 = soup.find("h1")
    title = h1.get_text().lower() if h1 else ""
    if "кодекс" in title: meta["doc_type"] = "code"
    elif "закон" in title: meta["doc_type"] = "law"
    elif "постанова" in title: meta["doc_type"] = "decree"
    elif "наказ" in title: meta["doc_type"] = "order"
    else: meta["doc_type"] = "act"
    meta["publisher"] = detect_publisher(title, text_lower[:1000], meta["doc_type"])
    if "втратив чинність" in text_lower[:500]: meta["status_override"] = "repealed"
    return meta

def extract_dates_fallback(text: str, title: str) -> Dict:
    result = {"adoption_date": None, "number": None}
    search_sources = [title, text[:1500]]
    for source in search_sources:
        if result["adoption_date"]: break
        numeric_match = re.search(r"(?:від\s+)?(\d{1,2})\.(\d{1,2})\.(\d{4})", source)
        if numeric_match:
            try:
                d, m, y = map(int, numeric_match.groups())
                if 1990 <= y <= 2100: result["adoption_date"] = datetime.datetime(y, m, d)
            except: pass
    combined = title + " " + text[:1000]
    num_match = re.search(r"№\s+([\d\-\/IVXА-Яа-яІіЇїЄєA-Za-z\.]+)", combined)
    if num_match: result["number"] = num_match.group(1)
    return result

# ==============================================================================
# 🧠 SMART CHUNKING LOGIC (UPDATED FOR CODES)
# ==============================================================================

def split_text_sliding_window(text: str, context_header: str, chunk_size: int = 2000, overlap: int = 300) -> List[str]:    
    if len(text) <= chunk_size:
        return [f"{context_header}\n\n{text}"]
    
    chunks = []
    start = 0
    part_counter = 1
    
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            lookback = text[end-150:end] 
            split_points = [lookback.rfind('\n\n'), lookback.rfind('\n'), lookback.rfind('. ')]
            best_split = max(split_points)
            if best_split != -1:
                end = (end - 150) + best_split + 1

        chunk_content = text[start:end].strip()
        final_chunk = f"{context_header} (Частина {part_counter})\n\n{chunk_content}"
        chunks.append(final_chunk)
        part_counter += 1
        start += (chunk_size - overlap) 
    return chunks

def hierarchical_chunking(markdown_text: str, doc_title: str = "Документ") -> List[Dict]:
    """
    Розумне розбиття для Кодексів та Законів.
    Зберігає ієрархію: Книга -> Розділ -> Глава -> Стаття.
    """
    chunks_data = []
    lines = markdown_text.split('\n')
    
    current_book = ""
    current_section = ""
    current_chapter = ""
    
    buffer = []
    buffer_start_title = "Вступ/Преамбула"
    
    # Регулярки (враховують можливі markdown-символи *, #)
    re_book = re.compile(r"^[\#\*]*\s*(КНИГА\s+[IVX0-9]+|Книга\s+[а-яА-Яa-zA-Z]+)", re.IGNORECASE)
    re_section = re.compile(r"^[\#\*]*\s*(РОЗДІЛ\s+[IVX0-9]+|Розділ\s+[IVX0-9]+)", re.IGNORECASE)
    re_chapter = re.compile(r"^[\#\*]*\s*(ГЛАВА\s+\d+|Глава\s+\d+)", re.IGNORECASE)
    re_article = re.compile(r"^[\#\*]*\s*(Стаття\s+\d+[\-\d\w]*\.?)", re.IGNORECASE)

    def save_buffer(title, content_lines, section_ctx, chapter_ctx):
        if not content_lines: return
        text = "\n".join(content_lines).strip()
        if not text: return
        
        # Формуємо повний шлях для контексту (для LLM)
        context_parts = [doc_title]
        if section_ctx: context_parts.append(section_ctx)
        if chapter_ctx: context_parts.append(chapter_ctx)
        context_parts.append(title)
        
        full_header = " > ".join(context_parts)
        
        sub_chunks = split_text_sliding_window(text, full_header)
        
        for sub_c in sub_chunks:
            chunks_data.append({
                "article_title": title,
                "section": section_ctx,
                "chapter": chapter_ctx,
                "content": sub_c
            })

    for line in lines:
        stripped = line.strip()
        if not stripped:
            buffer.append(line)
            continue
            
        match_book = re_book.match(stripped)
        match_section = re_section.match(stripped)
        match_chapter = re_chapter.match(stripped)
        match_article = re_article.match(stripped)
        
        if match_book:
            save_buffer(buffer_start_title, buffer, current_book or current_section, current_chapter)
            buffer = []
            current_book = match_book.group(1).replace("*", "").replace("#", "").strip()
            current_section = "" 
            current_chapter = ""
            buffer_start_title = f"Вступ до {current_book}"
            
        elif match_section:
            save_buffer(buffer_start_title, buffer, current_book or current_section, current_chapter)
            buffer = []
            current_section = match_section.group(1).replace("*", "").replace("#", "").strip()
            current_chapter = ""
            buffer_start_title = f"Вступ до {current_section}"
            
        elif match_chapter:
            save_buffer(buffer_start_title, buffer, current_book or current_section, current_chapter)
            buffer = []
            current_chapter = match_chapter.group(1).replace("*", "").replace("#", "").strip()
            buffer_start_title = f"Вступ до {current_chapter}"
            
        elif match_article:
            save_buffer(buffer_start_title, buffer, current_book or current_section, current_chapter)
            buffer = []
            buffer_start_title = match_article.group(1).replace("*", "").replace("#", "").strip()
            
        buffer.append(line)

    # Зберігаємо залишок
    save_buffer(buffer_start_title, buffer, current_book or current_section, current_chapter)
    
    return chunks_data

# ==============================================================================
# 🧠 SYNC PROCESSING (Wrapped for Async Execution)
# ==============================================================================

def sync_process_and_save(doc_id: int, html_content: str, source_type: str):
    session = SessionLocal()
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        raw_text_check = soup.get_text(" ", strip=True)
        STOP_PHRASES = ["Відбувається форматування тексту", "Wait a few seconds", "Почекайте кілька секунд"]
        if any(phrase in raw_text_check for phrase in STOP_PHRASES):
             logger.warning(f"⛔ [ID:{doc_id}] Банер виявлено! Скасування.")
             doc = session.get(Document, doc_id)
             if doc:
                 doc.status = "failed_content_check"
                 session.commit()
             return False 

        text_div = find_text_container(soup)
        if not text_div:
            logger.error(f"❌ [ID:{doc_id}] Не знайдено текстовий контейнер (failed_no_selector).")
            doc = session.get(Document, doc_id)
            if doc: 
                doc.status = "failed_no_selector"
                session.commit()
            return True 

        temp_text = soup.get_text()
        meta = extract_metadata_from_soup(soup, temp_text)
        doc = session.get(Document, doc_id)
        if not doc: return True 

        doc.doc_type = meta.get("doc_type", doc.doc_type)
        doc.publisher = meta.get("publisher", doc.publisher)
        if meta.get("status_override"): doc.act_status = meta["status_override"]
            
        soup = preprocess_html(soup)
        text_div = find_text_container(soup) 
        if not text_div: 
             logger.error(f"❌ [ID:{doc_id}] Не знайдено текстовий контейнер після препроцесингу.")
             return True

        markdown_text = md(str(text_div), heading_style="ATX", strip=["script", "style", "iframe"])
        final_text = clean_markdown(markdown_text)
        if len(final_text) < 50:
             logger.warning(f"⛔ [ID:{doc_id}] Занадто короткий контент (<50 символів).")
             doc.status = "failed_too_short"
             session.commit()
             return False

        new_hash = calculate_md5(final_text)
        if doc.status == "processed" and doc.content_hash == new_hash:
            logger.info(f"⏭️ [ID:{doc_id}] Контент не змінився.")
            doc.last_updated = datetime.datetime.now(datetime.timezone.utc)
            doc.last_checked = datetime.datetime.now(datetime.timezone.utc)
            session.commit()
            return True

        logger.info(f"📝 [ID:{doc_id}] Оновлення контенту & Векторів ({source_type})...")

        if not doc.adoption_date or not doc.registration_number:
            fb = extract_dates_fallback(final_text, doc.title or "")
            if fb.get("adoption_date") and not doc.adoption_date:
                doc.adoption_date = fb["adoption_date"]
            if fb.get("number") and not doc.registration_number:
                doc.registration_number = fb["number"]

        # --- CHUNKING & EMBEDDING ---
        current_doc_title = doc.title.strip() if doc.title else "Документ"
        
        # Використовуємо нову ієрархічну функцію
        chunks_data = hierarchical_chunking(final_text, current_doc_title)
        
        # Fallback, якщо регулярки не спрацювали зовсім
        if not chunks_data:
             chunks_data = [{
                "article_title": "Повний текст",
                "section": "Основний текст",
                "chapter": "",
                "content": final_text[:CHUNK_HARD_LIMIT] 
            }]

        texts_to_embed = [ch['content'] for ch in chunks_data]
        embeddings = []
        
        # Strict Vector Check
        if embedding_model and texts_to_embed:
            try:
                BATCH_EMBED = 50
                for i in range(0, len(texts_to_embed), BATCH_EMBED):
                    batch = texts_to_embed[i:i+BATCH_EMBED]
                    batch_embeddings = embedding_model.embed_documents(batch)
                    embeddings.extend(batch_embeddings)
                    time.sleep(1) # Rate limit guard
                
                # Перевірка цілісності
                if len(embeddings) != len(texts_to_embed):
                    raise ValueError(f"Mismatch: texts {len(texts_to_embed)} != embeddings {len(embeddings)}")

            except Exception as e:
                logger.error(f"🔥 Vector API Critical Fail [ID:{doc_id}]: {e}")
                # Повертаємо False, щоб НЕ зберігати документ без векторів (або частково)
                # Документ залишиться в стані 'processing' або 'pending', або можна поставити 'failed_vector'
                doc.status = "failed_vector_api"
                session.commit()
                return False
        
        # Видаляємо старі тільки якщо нові вектори успішно отримані
        session.query(DocumentChunk).filter_by(document_id=doc_id).delete()

        for i, chunk_map in enumerate(chunks_data):
            emb_vector = embeddings[i] if i < len(embeddings) else None
            
            db_chunk = DocumentChunk(
                document_id=doc_id,
                content=chunk_map["content"],
                article_number=chunk_map["article_title"],
                section_name=chunk_map["section"], # Тепер тут буде реальна Книга/Розділ
                chapter_name=chunk_map["chapter"], # Тепер тут буде Глава
                chunk_index=i,
                embedding=emb_vector
            )
            session.add(db_chunk)
            
        doc.content_hash = new_hash
        doc.status = "processed"
        doc.last_updated = datetime.datetime.now(datetime.timezone.utc)
        doc.last_checked = datetime.datetime.now(datetime.timezone.utc)
        if doc.is_bill and doc.doc_type in ["law", "code"]: doc.is_bill = False
        session.commit()
        logger.info(f"✅ [ID:{doc_id}] Успішно збережено ({len(chunks_data)} чанків).")
        return True

    except Exception as e:
        logger.error(f"Processing Error [ID:{doc_id}]: {e}")
        session.rollback()
        return False
    finally:
        session.close()

# ==============================================================================
# 🌐 ASYNC NETWORK & PLAYWRIGHT MANAGER
# ==============================================================================

class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.request_count = 0
        self.lock = asyncio.Lock()

    async def get_page(self):
        async with self.lock:
            if not self.browser: await self.start_new_browser()
            if self.request_count >= BROWSER_RESTART_EVERY:
                logger.info("♻️ Rolling Restart браузера...")
                await self.rolling_restart()
            self.request_count += 1
            try:
                context = await self.browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                    ignore_https_errors=True
                )
                context.set_default_timeout(45000)
                await context.route("**/*", self._route_intercept)
                return await context.new_page(), context
            except Exception as e:
                logger.error(f"Browser Context Error: {e}")
                await self.start_new_browser()
                context = await self.browser.new_context()
                return await context.new_page(), context

    async def _route_intercept(self, route):
        try:
            rt = route.request.resource_type
            if rt in ["image", "stylesheet", "font", "media", "websocket", "manifest", "other"]:
                await route.abort()
            elif any(x in route.request.url for x in ["google-analytics", "facebook", "twitter"]):
                await route.abort()
            else:
                await route.continue_()
        except Exception: pass

    async def start_new_browser(self):
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions", "--mute-audio"]
            )
            self.request_count = 0
            logger.info("✅ Browser Started.")
        except Exception as e:
            logger.error(f"❌ Browser Start Fail: {e}")
            raise e

    async def rolling_restart(self):
        old_browser = self.browser
        old_pw = self.playwright
        try: await self.start_new_browser()
        except Exception: return
        if old_browser: asyncio.create_task(self._delayed_close(old_browser, old_pw))

    async def _delayed_close(self, browser, pw):
        await asyncio.sleep(40)
        try:
            await browser.close()
            await pw.stop()
        except Exception: pass

browser_manager = BrowserManager()

async def fetch_with_curl_async(url: str, doc_id: int) -> Optional[str]:
    proxy = get_random_proxy()
    try:
        async with AsyncSession(impersonate="chrome110", headers={"Accept-Language": "uk-UA"}, proxy=proxy) as s:
            resp = await s.get(url, timeout=20)
            if resp.status_code != 200: return None
            text = resp.text
            if any(phrase in text for phrase in ["Відбувається форматування тексту", "Wait a few seconds"]): return None 
            if len(text) < 500: return None
            return text
    except Exception as e:
        logger.warning(f"⚠️ [ID:{doc_id}] Curl failed: {e}")
        return None

async def fetch_with_playwright_async(url: str, doc_id: int) -> Optional[str]:
    page = None
    context = None
    try:
        page, context = await browser_manager.get_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        for _ in range(10): 
            content = await page.content()
            if any(p in content for p in ["Відбувається форматування тексту", "Wait a few seconds"]):
                await asyncio.sleep(2)
            else:
                try:
                    await page.wait_for_selector("#article, #text_content, .text, .doc-content", state="visible", timeout=5000)
                    break 
                except: await asyncio.sleep(1)
        return await page.content()
    except Exception as e:
        logger.error(f"❌ [ID:{doc_id}] PW Error: {e}")
        return None
    finally:
        if page: await page.close()
        if context: await context.close()

# ==============================================================================
# 🎛️ CORE ASYNC LOGIC
# ==============================================================================

def set_failed_status_sync(doc_id):
    with SessionLocal() as s:
        d = s.get(Document, doc_id)
        if d: 
            d.status = "failed_all_attempts"
            s.commit()

async def process_single_document(doc_data, curl_sem: asyncio.Semaphore, pw_sem: asyncio.Semaphore):
    doc_id, url = doc_data
    clean_url = url.split("/sp:")[0].split("?")[0].rstrip("/")
    if clean_url.endswith("/priint"): clean_url = clean_url[:-7]
    if clean_url.endswith("/print"): clean_url = clean_url[:-6]

    html = None
    source = "Curl"
    async with curl_sem:
        html = await fetch_with_curl_async(clean_url, doc_id)
    if not html:
        async with pw_sem:
            html = await fetch_with_playwright_async(clean_url, doc_id)
            source = "Playwright"
    if not html:
        logger.error(f"💀 [ID:{doc_id}] Failed completely.")
        await asyncio.to_thread(set_failed_status_sync, doc_id)
        return

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, sync_process_and_save, doc_id, html, source)

def get_pending_docs_sync(limit):
    with SessionLocal() as session:
        docs = session.query(Document).filter(Document.status == "pending").limit(limit).all()
        return [(d.id, d.url) for d in docs]

def mark_processing_sync(doc_ids):
    with SessionLocal() as session:
        session.query(Document).filter(Document.id.in_(doc_ids)).update(
            {Document.status: "processing"}, synchronize_session=False
        )
        session.commit()

async def main_loop():
    logger.info("🚀 Async Worker Started (Updated Hierarchical Chunking).")
    curl_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CURL)
    pw_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PW)

    is_idle = False
    
    while True:
        try:
            doc_batch = await asyncio.to_thread(get_pending_docs_sync, BATCH_SIZE)
            
            if not doc_batch:
                if not is_idle:
                    logger.info("💤 Черга порожня. Перехід в режим очікування (тихий режим)...")
                    is_idle = True

                await asyncio.sleep(10)
                continue
            
            if is_idle:
                logger.info("⚡ Отримано нові завдання! Вихід з режиму очікування.")
                is_idle = False

            doc_ids = [d[0] for d in doc_batch]
            await asyncio.to_thread(mark_processing_sync, doc_ids)
            logger.info(f"📦 Нова партія: {len(doc_batch)} документів.")
            
            tasks = [asyncio.create_task(process_single_document(d, curl_semaphore, pw_semaphore)) for d in doc_batch]
            await asyncio.gather(*tasks)
            logger.info("🏁 Партія завершена.")
            
        except Exception as e:
            logger.error(f"Global Loop Error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("🛑 Зупинка...")