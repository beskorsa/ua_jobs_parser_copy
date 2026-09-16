"""
Извлечение текста из резюме (PDF или Markdown/txt) и разбивка на секции
(скиллы, опыт, образование, о себе...) для точечного матчинга с вакансиями.

Секции определяются эвристикой по заголовкам (укр/рос/англ, с учётом что
markdown-резюме использует "## Навички" и т.п.) — это не идеальный парсер
резюме, а прагматичный: если секцию не удалось распознать, весь текст всё
равно доступен целиком под ключом "full", так что матчинг "одним эмбеддингом"
работает независимо от того, распозналась структура или нет.
"""
import re
from pathlib import Path

SECTION_PATTERNS = {
    "skills": r"(технічні навички|навички|hard\s*skills|skills|компетенці[її]|технологі[її])",
    "experience": r"(досвід роботи|досвід|work\s*experience|experience|трудовий досвід)",
    "education": r"(освіта|education)",
    "summary": r"(про мене|коротко про себе|summary|profile|мета|objective|ціль)",
    "projects": r"(проекти|projects|портфоліо|portfolio)",
    "languages": r"(мови|languages|іноземні мови)",
}
_HEADING_RE = {
    key: re.compile(rf"^\s{{0,3}}#{{0,6}}\s*[\*_]{{0,2}}\s*{pattern}\s*[\*_]{{0,2}}\s*:?\s*$", re.IGNORECASE)
    for key, pattern in SECTION_PATTERNS.items()
}


def extract_text(path: str) -> str:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(p)
    if suffix in (".md", ".markdown", ".txt"):
        return p.read_text(encoding="utf-8", errors="ignore")
    raise ValueError(f"Неподдерживаемый формат резюме: {suffix} (ожидается .pdf, .md или .txt)")


def _extract_pdf_text(path: Path) -> str:
    try:
        import pymupdf as fitz  # новое имя пакета/модуля, PyMuPDF >= 1.24
    except ImportError:
        import fitz  # старые версии PyMuPDF

    doc = fitz.open(str(path))
    try:
        pages = [page.get_text("text") for page in doc]
    finally:
        doc.close()
    return "\n".join(pages)


def split_sections(text: str) -> dict[str, str]:
    """
    Возвращает {"full": весь_текст, "skills": ..., "experience": ..., ...} —
    ключи для нераспознанных/отсутствующих секций просто не появляются.
    Текст до первой распознанной секции (обычно шапка + "про себе") идёт
    в "summary", если своего summary-заголовка не было.
    """
    lines = text.splitlines()
    sections: dict[str, list[str]] = {}
    current = None
    preamble: list[str] = []

    for line in lines:
        matched_key = None
        stripped = line.strip()
        if stripped and len(stripped) <= 60:
            for key, rx in _HEADING_RE.items():
                if rx.match(stripped):
                    matched_key = key
                    break
        if matched_key:
            current = matched_key
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
        else:
            preamble.append(line)

    result = {"full": text.strip()}
    for key, content_lines in sections.items():
        content = "\n".join(content_lines).strip()
        if content:
            result[key] = content

    preamble_text = "\n".join(preamble).strip()
    if preamble_text and "summary" not in result:
        result["summary"] = preamble_text

    return result
