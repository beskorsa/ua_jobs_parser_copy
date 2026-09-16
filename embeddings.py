"""
Чанкинг длинных описаний + получение эмбеддингов.

Два провайдера, выбираются через config.EMBEDDING_PROVIDER:
  - "local"  : sentence-transformers, бесплатно, крутится на этой машине.
               Тяжёлая зависимость (torch) — она НЕ в основном requirements.txt,
               см. requirements-embeddings.txt.
  - "openai" : API text-embedding-3-small. Нужен OPENAI_API_KEY.

Оба реализуют один и тот же интерфейс: embed(list[str]) -> list[list[float]].
"""
import re
from typing import Protocol

import config


def chunk_text(text: str, chunk_size: int = None, overlap: int = None) -> list[str]:
    """
    Режет текст на чанки по chunk_size символов с overlap символами нахлёста,
    стараясь резать по границам предложений/абзацев, а не посередине слова.
    Короткий текст (короче chunk_size) возвращается одним чанком.
    """
    chunk_size = chunk_size or config.CHUNK_SIZE
    overlap = overlap or config.CHUNK_OVERLAP
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    # разбиваем на предложения/абзацы простым способом — без внешних NLP-зависимостей
    sentences = re.split(r"(?<=[.!?…])\s+|\n+", text)
    sentences = [s.strip() for s in sentences if s.strip()]

    chunks = []
    current = ""
    for sent in sentences:
        if current and len(current) + 1 + len(sent) > chunk_size:
            chunks.append(current.strip())
            # нахлёст: переносим хвост текущего чанка в начало следующего
            tail = current[-overlap:] if overlap > 0 else ""
            current = (tail + " " + sent).strip()
        else:
            current = (current + " " + sent).strip() if current else sent

        # отдельное длинное предложение само по себе длиннее chunk_size —
        # режем его жёстко по символам, чтобы не потерять
        while len(current) > chunk_size * 1.5:
            chunks.append(current[:chunk_size].strip())
            current = current[chunk_size - overlap:]

    if current.strip():
        chunks.append(current.strip())

    return chunks


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalEmbedder:
    """sentence-transformers, модель качается один раз и кэшируется на диске."""

    def __init__(self, model_name: str = None):
        from sentence_transformers import SentenceTransformer  # тяжёлый импорт — лениво
        self.model_name = model_name or config.LOCAL_EMBEDDING_MODEL
        self._model = SentenceTransformer(self.model_name)
        # e5-модели специально обучены с префиксами "query: " / "passage: " —
        # без них качество эмбеддингов заметно хуже. bge-* использует похожую конвенцию
        # только для запросов ("query: "), для проиндексированных текстов префикс не нужен.
        self._passage_prefix = "passage: " if "e5" in self.model_name.lower() else ""

    def embed(self, texts: list[str]) -> list[list[float]]:
        prefixed = [self._passage_prefix + t for t in texts]
        vectors = self._model.encode(prefixed, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        prefix = "query: " if "e5" in self.model_name.lower() else ""
        vec = self._model.encode([prefix + text], normalize_embeddings=True, show_progress_bar=False)[0]
        return vec.tolist()


class OpenAIEmbedder:
    def __init__(self, model_name: str = None, api_key: str = None):
        from openai import OpenAI  # лениво — не обязателен, если провайдер local
        self.model_name = model_name or config.OPENAI_EMBEDDING_MODEL
        api_key = api_key or config.OPENAI_API_KEY
        if not api_key:
            raise RuntimeError(
                "EMBEDDING_PROVIDER=openai, но OPENAI_API_KEY не задан (см. .env.example)"
            )
        self._client = OpenAI(api_key=api_key)

    def embed(self, texts: list[str]) -> list[list[float]]:
        # OpenAI сам режет по батчам, но на всякий случай ограничим размер запроса
        out = []
        batch_size = 96
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            resp = self._client.embeddings.create(model=self.model_name, input=batch)
            out.extend([d.embedding for d in resp.data])
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


def embed_as_single_vector(embedder, text: str) -> list[float]:
    """
    Один эмбеддинг на произвольно длинный текст (нужно для резюме целиком —
    "одним эмбеддингом", как в задаче). Модели эмбеддингов имеют лимит по
    токенам (у multilingual-e5-small — 512 токенов, это примерно 1.5-2
    страницы текста): если просто скормить длинный текст, всё, что не
    влезло, будет молча обрезано. Вместо этого режем на чанки (та же
    chunk_text, что и для описаний вакансий), эмбеддим каждый и берём
    усреднённый (mean pooling) вектор — так в него вносит вклад весь текст,
    а не только начало.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Пустой текст — нечего эмбеддить")

    chunks = chunk_text(text, chunk_size=config.CHUNK_SIZE, overlap=config.CHUNK_OVERLAP)
    if len(chunks) == 1:
        return embedder.embed(chunks)[0]

    vectors = embedder.embed(chunks)
    dim = len(vectors[0])
    mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
    norm = sum(x * x for x in mean) ** 0.5
    if norm > 0:
        mean = [x / norm for x in mean]
    return mean


_embedder_instance = None


def get_embedder() -> Embedder:
    """Синглтон — модель (особенно локальная) дорого инициализировать повторно."""
    global _embedder_instance
    if _embedder_instance is not None:
        return _embedder_instance

    if config.EMBEDDING_PROVIDER == "openai":
        _embedder_instance = OpenAIEmbedder()
    elif config.EMBEDDING_PROVIDER == "local":
        _embedder_instance = LocalEmbedder()
    else:
        raise RuntimeError(
            f"Неизвестный EMBEDDING_PROVIDER={config.EMBEDDING_PROVIDER!r}, ожидается 'local' или 'openai'"
        )
    return _embedder_instance
