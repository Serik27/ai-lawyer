import os
import datetime
from datetime import timezone
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Text, ForeignKey, DateTime, BigInteger, Index, Boolean, Computed, func
from sqlalchemy.orm import sessionmaker, relationship, declarative_base
from sqlalchemy.dialects.postgresql import TSVECTOR
from pgvector.sqlalchemy import Vector

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("❌ Не знайдено DATABASE_URL в .env")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ==========================================
# ЧАСТИНА 1: СИСТЕМНІ НАЛАШТУВАННЯ (НОВЕ)
# ==========================================

class SystemConfig(Base):
    """
    Таблиця для керування станом колектора через Telegram.
    Зберігає налаштування: режим роботи, поточну сторінку, час останнього запуску.
    """
    __tablename__ = "system_config"
    key = Column(String, primary_key=True) # Наприклад: 'collector_mode', 'current_page'
    value = Column(String)                 # Значення: 'hourly', '15', '2023-10-10...'

# ==========================================
# ЧАСТИНА 2: ПАРСИНГ ТА RAG
# ==========================================

class Source(Base):
    __tablename__ = "sources"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    base_url = Column(String)

class Document(Base):
    __tablename__ = "documents"
    
    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, unique=True, nullable=False, index=True) 
    title = Column(String, nullable=False)
    
    doc_type = Column(String, index=True)   
    publisher = Column(String, index=True)  
    act_status = Column(String, index=True) 
    
    adoption_date = Column(DateTime, nullable=True)      
    entry_force_date = Column(DateTime, nullable=True)   
    registration_number = Column(String, nullable=True)  
    
    status = Column(String, default="pending", index=True) 
    last_updated = Column(DateTime, default=datetime.datetime.now(datetime.timezone.utc))
    content_hash = Column(String, index=True, nullable=True)
    last_checked = Column(DateTime, default=datetime.datetime.now(datetime.timezone.utc))
    is_bill = Column(Boolean, default=False, index=True)

    source_id = Column(Integer, ForeignKey("sources.id"))
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (
        Index('idx_doc_status', 'status', 'url'),
        Index('idx_doc_hash', 'content_hash'),
    )

class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    
    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"))
    content = Column(Text, nullable=False)
    
    article_number = Column(String, index=True) 
    section_name = Column(String)               
    chapter_name = Column(String)               
    
    embedding = Column(Vector(768)) 
    search_vector = Column(TSVECTOR, Computed("to_tsvector('ukrainian', content)", persisted=True))
    chunk_index = Column(Integer) 
    
    document = relationship("Document", back_populates="chunks")

    __table_args__ = (
        Index('idx_embedding', 'embedding', postgresql_using='hnsw', postgresql_with={'m': 16, 'ef_construction': 64}, postgresql_ops={'embedding': 'vector_cosine_ops'}),
        Index('idx_search_vector', 'search_vector', postgresql_using='gin'),
    )

# ==========================================
# ЧАСТИНА 3: КОРИСТУВАЧІ ТА СПРАВИ
# ==========================================

class TelegramUser(Base):
    __tablename__ = "telegram_users"
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True)
    full_name = Column(String)
    username = Column(String)
    dialogue_summary = Column(Text, default="") 
    cases = relationship("LegalCase", back_populates="user", cascade="all, delete-orphan")

class LegalCase(Base):
    __tablename__ = "legal_cases"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("telegram_users.id"))
    title = Column(String, nullable=False)
    summary = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.datetime.now(datetime.timezone.utc))
    user = relationship("TelegramUser", back_populates="cases")
    messages = relationship("ChatMessage", back_populates="case", cascade="all, delete-orphan")

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("telegram_users.id"), nullable=True)
    case_id = Column(Integer, ForeignKey("legal_cases.id"), nullable=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.datetime.now(datetime.timezone.utc))
    user = relationship("TelegramUser")
    case = relationship("LegalCase", back_populates="messages")

def create_db_and_tables():
    with engine.connect() as conn:
        try:
            conn.execute(func.text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
            print("✅ Extension 'vector' verified.")
        except Exception as e:
            print(f"⚠️ Warning: Could not create extension 'vector'. Error: {e}")

    Base.metadata.create_all(bind=engine)
    
    # Ініціалізація дефолтних налаштувань
    session = SessionLocal()
    try:
        defaults = {
            "collector_mode": "stop", # 'hourly', 'full', 'stop'
            "current_page": "1",
            "force_run": "false",
            "last_run_timestamp": "0"
        }
        for k, v in defaults.items():
            if not session.query(SystemConfig).filter_by(key=k).first():
                session.add(SystemConfig(key=k, value=v))
        session.commit()
        print("✅ Структура бази даних та конфігурація оновлені.")
    finally:
        session.close()

if __name__ == "__main__":
    create_db_and_tables()