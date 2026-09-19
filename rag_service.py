import os
import logging
import sys
import json
import time
import google.generativeai as genai
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from logging.handlers import RotatingFileHandler
try:
    from publishers import DOC_ALIASES
except ImportError:
    logging.warning("⚠️ Could not import DOC_ALIASES from publishers.py. Using empty dict.")
    DOC_ALIASES = {}
try:
    from flashrank import Ranker, RerankRequest
except ImportError:
    print("⚠️ FlashRank not found. Running without reranker.")
    Ranker = None
    RerankRequest = None

# ==========================================
# ⚙️ КОНФІГУРАЦІЯ
# ==========================================
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# Налаштування логування
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
log_file_path = os.path.join(LOG_DIR, "rag_service.log")
log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

file_handler = RotatingFileHandler(log_file_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
file_handler.setFormatter(log_formatter)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(log_formatter)

logger = logging.getLogger("rag_service")
logger.setLevel(logging.INFO)
if logger.hasHandlers():
    logger.handlers.clear()
logger.addHandler(file_handler)
logger.addHandler(stream_handler)

if not GOOGLE_API_KEY:
    raise ValueError("❌ Не знайдено GOOGLE_API_KEY у файлі .env")
if not DATABASE_URL:
    raise ValueError("❌ Не знайдено DATABASE_URL у файлі .env")

# Налаштування Gemini
genai.configure(api_key=GOOGLE_API_KEY)

# Моделі
MODEL_ROUTER = "gemini-2.5-flash-lite"     # Швидка модель
MODEL_ANSWER = "gemini-2.5-flash"          # Основна модель
EMBEDDING_MODEL = "models/text-embedding-004"

# Ініціалізація Reranker
ranker = None
if Ranker:
    try:
        logger.info("⏳ Завантаження моделі FlashRank...")
        ranker = Ranker(model_name="ms-marco-MultiBERT-L-12", cache_dir="./opt")
        logger.info("✅ FlashRank готовий.")
    except Exception as e:
        logger.error(f"❌ Помилка ініціалізації FlashRank: {e}")

# База даних
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# ==========================================
# 🛠️ HELPER FUNCTIONS
# ==========================================

def resolve_doc_filter(ai_filter_suggestion: str) -> str:
    """Нормалізує назву документа з AI у точну назву в БД."""
    if not ai_filter_suggestion:
        return None
    clean = ai_filter_suggestion.lower().strip()
    return DOC_ALIASES.get(clean, clean)

def get_embedding(text: str):
    """Генерує вектор (embeddings)."""
    try:
        safe_text = text[:9000]
        result = genai.embed_content(
            model=EMBEDDING_MODEL,
            content=safe_text,
            task_type="retrieval_query"
        )
        return result['embedding']
    except Exception as e:
        logger.error(f"Embedding error: {e}")
        return None

def analyze_query_intent(user_query: str) -> dict:
    """
    Аналізує запит і формує фільтри.
    """
    try:
        model = genai.GenerativeModel(MODEL_ROUTER)
        prompt = (
            f"Ти досвідчений юрист. Твоє завдання — перетворити запит клієнта на точні параметри пошуку.\n"
            f"Запит: '{user_query}'\n\n"
            f"ІНСТРУКЦІЯ:\n"
            f"1. TRANSLATED_KEYWORDS: Сформулюй юридичний пошуковий запит. Додай синоніми.\n"
            f"2. FILTERS (Критично важливо):\n"
            f"   - Якщо запит стосується конкретних статей, вкажи їх.\n"
            f"   - УВАГА: Якщо злочинів декілька (наприклад, наркотики + вбивство), знайди статті для КОЖНОГО.\n"
            f"   - Приклад: 'виробництво наркотиків і некрофілія' -> doc: 'кку', articles: ['307', '297']\n\n"
            f"Відповідь JSON:\n"
            f"{{\n"
            f"  'translated_keywords': '...', \n"
            f"  'filters': [{{'doc': 'назва_кодексу', 'articles': ['1', '2']}}]\n"
            f"}}\n"
        )
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(response.text)
    except Exception as e:
        logger.warning(f"Intent analysis failed: {e}")
        return {"translated_keywords": user_query, "filters": []}

# ==========================================
# 🔍 SEARCH ENGINE (FULL + OPTIMIZED)
# ==========================================

def search_documents(query_text: str, search_keywords: str, filters: list = None, limit: int = 15):
    """
    Оптимізований пошук:
    1. Batch Precise SQL Lookup (IN clause) з генерацією варіантів ("307" -> "Стаття 307").
    2. Smart Early Exit.
    3. Hybrid Search.
    """
    session = SessionLocal()
    RRF_K = 60
    TRASH_FILTER = "d.title NOT ILIKE 'Про внесення змін%' AND d.title NOT ILIKE 'Про затвердження змін%' AND d.title NOT ILIKE 'Про ратифікацію%'"
    
    final_results = []
    found_ids = set() 
    global_doc_filters = []

    try:
        t_start = time.time()
        
        # ==========================================
        # ЕТАП 1: ТОЧНИЙ ПОШУК (BATCH SQL - ВИПРАВЛЕНИЙ)
        # ==========================================
        if filters:
            logger.info(f"🎯 Filters Detected: {filters}")
            
            for f in filters:
                doc_alias = resolve_doc_filter(f.get('doc'))
                target_articles = f.get('articles', [])
                
                if doc_alias and not target_articles:
                    global_doc_filters.append(doc_alias)
                    continue

                if not doc_alias or not target_articles:
                    continue
                
                expanded_articles = []
                for art in target_articles:
                    clean_art = str(art).strip()
                    expanded_articles.append(clean_art)                
                    expanded_articles.append(f"Стаття {clean_art}")     
                    expanded_articles.append(f"Стаття {clean_art}.")     
                    expanded_articles.append(f"ст. {clean_art}")         

                if expanded_articles:
                    # Створюємо параметри :a0, :a1... для безпечного SQL
                    placeholders = ','.join([f':a{i}' for i in range(len(expanded_articles))])
                    params = {f'a{i}': val for i, val in enumerate(expanded_articles)}
                    params["doc_title"] = f"%{doc_alias}%"
                    
                    sql_precise = text(f"""
                        SELECT dc.id, dc.content, dc.article_number, d.title, d.url, d.act_status
                        FROM document_chunks dc
                        JOIN documents d ON dc.document_id = d.id
                        WHERE d.title ILIKE :doc_title
                          AND d.act_status = 'active'
                          AND dc.article_number IN ({placeholders})
                    """)
                    
                    rows = session.execute(sql_precise, params).fetchall()

                    for row in rows:
                        if row.id not in found_ids:
                            final_results.append({
                                "content": row.content,
                                "source": f"{row.title} (Стаття {row.article_number})",
                                "url": row.url,
                                "status": row.act_status,
                                "score": 10.0
                            })
                            found_ids.add(row.id)
            
            logger.info(f"✅ Precise search found {len(final_results)} docs.")

        # ==========================================
        # ЕТАП 1.5: РАННІЙ ВИХІД (EARLY EXIT)
        # ==========================================

        if filters and len(final_results) >= 1:
             logger.info(f"🚀 Early Exit: Found {len(final_results)} precise matches. Skipping Hybrid.")
             return final_results[:limit]
        
        if len(final_results) >= 5:
             logger.info(f"🚀 Early Exit: Sufficient docs found ({len(final_results)}). Skipping Hybrid.")
             return final_results[:limit]

        # ==========================================
        # ЕТАП 2: ГІБРИДНИЙ ПОШУК (Тільки якщо не знайшли точно)
        # ==========================================
        
        remaining_limit = limit - len(final_results)
        
        if remaining_limit > 0:
            embedding_source = search_keywords if search_keywords else query_text
            query_embedding = get_embedding(embedding_source)
            
            if query_embedding:
                base_query = f"""
                    SELECT dc.id, dc.content, dc.article_number, d.title, d.url, d.doc_type, d.act_status,
                    (dc.embedding <=> :embedding) as distance
                    FROM document_chunks dc
                    JOIN documents d ON dc.document_id = d.id
                    WHERE d.act_status = 'active' 
                      AND d.status != 'unknown'
                      AND {TRASH_FILTER}
                """
                
                params = {
                    "embedding": str(query_embedding), 
                    "q": embedding_source, 
                    "lim": remaining_limit * 2
                }

                if global_doc_filters:
                    doc_filter_clauses = []
                    for i, df in enumerate(global_doc_filters):
                        key = f"doc_filter_{i}"
                        doc_filter_clauses.append(f"d.title ILIKE :{key}")
                        params[key] = f"%{df}%"
                    
                    if doc_filter_clauses:
                        base_query += f" AND ({' OR '.join(doc_filter_clauses)})"

                if found_ids:
                    base_query += f" AND dc.id NOT IN ({','.join(map(str, found_ids))})"

                # 2.1 Векторний пошук
                vector_sql = text(f"""
                    {base_query} 
                    ORDER BY 
                        (dc.embedding <=> :embedding) 
                        - (CASE WHEN d.doc_type = 'code' THEN 0.15 ELSE 0 END) 
                        ASC 
                    LIMIT :lim
                """)
                
                # 2.2 Повнотекстовий пошук
                text_sql = text(f"""
                    {base_query} 
                    AND dc.search_vector @@ websearch_to_tsquery('ukrainian', :q) 
                    ORDER BY 
                        ts_rank_cd(dc.search_vector, websearch_to_tsquery('ukrainian', :q)) 
                        + (CASE WHEN d.doc_type = 'code' THEN 0.5 ELSE 0 END) 
                        DESC 
                    LIMIT :lim
                """)
                
                v_rows = session.execute(vector_sql, params).fetchall()
                t_rows = session.execute(text_sql, params).fetchall()

                # RRF FUSION
                candidates_map = {}
                def process_rows(rows, method_weight=1.0):
                    for rank, row in enumerate(rows):
                        if row.id not in candidates_map:
                            candidates_map[row.id] = {
                                "content": row.content,
                                "source": f"{row.title} ({row.article_number})",
                                "url": row.url,
                                "status": row.act_status,
                                "rrf_score": 0.0
                            }
                        candidates_map[row.id]["rrf_score"] += (1.0 / (RRF_K + rank + 1)) * method_weight

                process_rows(v_rows, method_weight=1.0)
                process_rows(t_rows, method_weight=1.2)

                hybrid_candidates = list(candidates_map.values())
                hybrid_candidates.sort(key=lambda x: x["rrf_score"], reverse=True)
                final_results.extend(hybrid_candidates[:remaining_limit])

        # ==========================================
        # ЕТАП 3: РЕРАНКІНГ (FlashRank)
        # ==========================================
        
        if ranker and final_results:
            try:
                passages = [
                    {"id": i, "text": res["content"], "meta": res} 
                    for i, res in enumerate(final_results)
                ]
                
                rerank_request = RerankRequest(query=query_text, passages=passages)
                ranked_results = ranker.rerank(rerank_request)
                
                reranked_output = []
                for res in ranked_results[:limit]:
                    item = res["meta"]
                    # Зберігаємо пріоритет точних збігів (score 10.0), решту оновлюємо
                    if item.get("score", 0) < 9.0:
                        item["score"] = res["score"]
                    reranked_output.append(item)
                
                final_results = reranked_output
                
            except Exception as e:
                logger.error(f"FlashRank error: {e}")
                final_results.sort(key=lambda x: x["score"], reverse=True)

        logger.info(f"✅ Search finished in {time.time() - t_start:.2f}s. Found {len(final_results)} docs.")
        return final_results[:limit]

    except Exception as e:
        logger.error(f"Search critical error: {e}")
        return []
    finally:
        session.close()

# ==========================================
# 💬 GENERATION & SUMMARY
# ==========================================

async def get_legal_advice(user_input: str, summary: str = "") -> str:
    try:
        # 1. Intent Analysis
        analysis = analyze_query_intent(user_input)
        translated_keywords = analysis.get("translated_keywords", user_input)
        filters = analysis.get("filters", []) 
        
        logger.info(f"🧠 AI Logic: Query='{user_input}' -> Filters={filters}")
        
        # 2. Search
        docs = search_documents(
            query_text=user_input, 
            search_keywords=translated_keywords, 
            filters=filters,
            limit=15
        )
        
        # 3. Context Preparation
        context_text = ""
        links_map = {} 
        MAX_CHARS = 100000 
        current_chars = 0

        if docs:
            for i, doc in enumerate(docs):
                clean_title = doc['source'].split('(')[0].strip()[:100]
                links_map[clean_title] = doc['url']

                content_safe = doc['content'][:6000]
                if current_chars + len(content_safe) > MAX_CHARS: break
                
                status_icon = "🔴 [ВТРАТИВ ЧИННІСТЬ]" if doc['status'] == 'repealed' else ""
                
                context_text += f"\n--- Документ {i+1} ---\n"
                context_text += f"Джерело: {doc['source']} {status_icon}\n"
                context_text += f"Посилання: {doc['url']}\n"
                context_text += f"Зміст:\n{content_safe}\n"
                
                current_chars += len(content_safe)
        else:
            context_text = "В БАЗІ ДАНИХ НЕ ЗНАЙДЕНО ІНФОРМАЦІЇ ПО ЗАПИТУ."

        links_instruction = "\n".join([f"- {title}: {url}" for title, url in links_map.items()])

        # 4. Generation
        model = genai.GenerativeModel(MODEL_ANSWER)
        
        system_prompt = (
            "Ти — професійний юрист-консультант. Твоя мета — надати юридично точну відповідь.\n"
            "Базуй свою відповідь ВИКЛЮЧНО на наданих нижче документах (ВНУТРІШНЯ БАЗА).\n\n"
            
            "🚫 СУВОРІ ЗАБОРОНИ:\n"
            "1. НІКОЛИ не використовуй технічні ідентифікатори типу 'DOCUMENT_ID_X'.\n"
            "2. Не вигадуй закони, яких немає в наданому тексті.\n"
            "3. Не використовуй фрази 'в наданих фрагментах', пиши 'згідно з чинним законодавством'.\n"
            "4. Не виводь URL посилання окремо (крім блоку джерел в самому кінці).\n\n"
            
            "✅ ВИМОГИ ДО ОФОРМЛЕННЯ (КРИТИЧНО ВАЖЛИВО):\n"
            "1. ІНТЕГРАЦІЯ ПОСИЛАНЬ: Назви законів та номери статей мають бути клікабельними гіперпосиланнями.\n"
            "   - НЕПРАВИЛЬНО: 'стаття 307 (https://...)'\n"
            "   - ПРАВИЛЬНО: 'згідно зі [статтею 307 ККУ](https://...)'\n"
            "   - Використовуй стандартний Markdown: [Текст](URL).\n"
            "2. СПИСКИ: Для маркованих списків використовуй ТІЛЬКИ дефіс (-).\n"
            "   - НЕПРАВИЛЬНО: * пункт 1\n"
            "   - ПРАВИЛЬНО: - пункт 1\n"
            "3. Структура: Вступ, Детальний аналіз, Висновок (короткий список через дефіс).\n"
            "4. В самому кінці додай розділ '📚 Використані джерела' зі списком посилань.\n"
            "5. Пиши числа не словами а цифрами. Перераховуй неоподатковуваний мінімум доходів громадян, який становить 17 грн, в зрозумілу суму в гривнях. Не показуй розрахунки для штрафів, давай лише суму/відповідь.\n\n"
            
            "ДОСТУПНІ ПОСИЛАННЯ (використовуй їх для створення гіперлінків [Текст](URL)):\n"
            f"{links_instruction}\n"
        )
        
        user_prompt = (
            f"Контекст бесіди: {summary}\n"
            f"Запитання клієнта: {user_input}\n\n"
            f"ЗНАЙДЕНІ МАТЕРІАЛИ:\n{context_text}\n\n"
            f"Твоя консультація:"
        )

        response = model.generate_content(system_prompt + user_prompt)
        return response.text

    except Exception as e:
        logger.error(f"Gen Error: {e}")
        return "Вибачте, сталася технічна помилка при генерації відповіді."

async def summarize_dialogue(current_summary: str, user_text: str, bot_text: str) -> str:
    try:
        model = genai.GenerativeModel("gemini-2.0-flash-lite")
        prompt = (
            f"Онови коротке резюме юридичного діалогу (до 50 слів).\n"
            f"Попереднє: {current_summary}\n"
            f"Клієнт: {user_text}\n"
            f"Юрист: {bot_text[:300]}...\n"
            f"Нове резюме:"
        )
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Summarize error: {e}")
        return current_summary