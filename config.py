"""
Конфигурация для слоя Supabase/Postgres + эмбеддинги.
Ничего не завязано жёстко на Supabase — DATABASE_URL это обычная
Postgres-строка подключения (Supabase её просто выдаёт в Project Settings →
Database → Connection string → URI, с pgvector уже доступным как extension).

Всё читается из .env (см. .env.example) через python-dotenv, либо из
переменных окружения напрямую — .env удобнее для локальной разработки.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# Postgres/Supabase connection string, например:
# postgresql://postgres.xxxxxxxx:PASSWORD@aws-0-eu-central-1.pooler.supabase.com:5432/postgres
DATABASE_URL = os.getenv("DATABASE_URL", "")

# "local" — sentence-transformers, бесплатно, считается на этой же машине.
# "openai" — API text-embedding-3-small, нужен OPENAI_API_KEY, копеечно но платно.
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "local").strip().lower()

# Модель для локальных эмбеддингов. По умолчанию — мультиязычная e5-small:
# у неё та же размерность (384), что и у bge-small-en, но она нормально
# понимает украинский текст, а bge-small-en-v1.5 обучена только на английском
# и на украинских описаниях вакансий будет давать посредственные вектора.
# Если всё же нужен буквально bge-small — поставьте EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
# в .env (размерность у него тоже 384, менять EMBEDDING_DIM не придётся).
LOCAL_EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-small")

OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Модель для generate.py (LLM-оценка релевантности вакансии + cover letter) —
# отдельная от OPENAI_EMBEDDING_MODEL, это chat-модель, а не embeddings.
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")

# Размерность вектора в БД. Должна совпадать с реальной моделью:
#   intfloat/multilingual-e5-small   -> 384
#   BAAI/bge-small-en-v1.5           -> 384
#   text-embedding-3-small (OpenAI)  -> 1536
_DEFAULT_DIM = 1536 if EMBEDDING_PROVIDER == "openai" else 384
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", str(_DEFAULT_DIM)))

# Чанкинг длинных описаний перед эмбеддингом (в символах, не токенах — простой
# и достаточно надёжный способ для текстов такой длины).
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1000"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "150"))

# Путь к локальной SQLite-базе, которую наполняет main.py (scraper) —
# sync_supabase.py читает вакансии оттуда и переливает в Postgres.
SQLITE_PATH = os.getenv("SQLITE_PATH", "vacancies.db")

# Для telegram_report.py — отправка отчёта (список вакансий + score + пояснение)
# в Telegram. Токен — от @BotFather. Chat id — свой user id (написать боту
# /start, потом https://api.telegram.org/bot<TOKEN>/getUpdates) или id группы/канала.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
