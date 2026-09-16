"""
Резюме как источник запроса: загружаем PDF/MD/txt, режем на секции
(скиллы, опыт, освіта, ...), эмбеддим и подбираем вакансии из Supabase
(таблица vacancy_chunks, заполняется sync_supabase.py).

Резюме хранится ОТДЕЛЬНО от вакансий — свои таблицы resumes/resume_sections
(см. postgres_store.py), к vacancies/vacancy_chunks они никак не подмешиваются.

Использование:
    python resume_match.py upload resume.pdf
        -> загружает резюме, считает эмбеддинги, печатает resume_id

    python resume_match.py match --resume-id 3
        -> подбор вакансий одним эмбеддингом всего резюме (быстро, грубее)

    python resume_match.py match --resume-id 3 --by-section
        -> подбор с раздельным весом по секциям (skills весит больше всего) —
           точнее, но требует, чтобы в резюме распознались секции

    python resume_match.py upload resume.pdf --match
        -> загрузить и сразу показать подбор (mode=full)

    python resume_match.py list
        -> список загруженных резюме
"""
import argparse
import sys
from pathlib import Path

import config
import postgres_store
from embeddings import get_embedder, embed_as_single_vector
from resume_parser import extract_text, split_sections


def cmd_upload(path: str, do_match: bool, by_section: bool, top_k: int,
               city: str = None, min_salary: float = None, currency: str = None, since: str = None) -> int:
    p = Path(path)
    if not p.exists():
        print(f"Файл не найден: {path}", file=sys.stderr)
        sys.exit(1)

    print(f"Читаю {p.name}...")
    text = extract_text(str(p))
    if not text.strip():
        print("Не удалось извлечь текст (пустой результат) — для скан-PDF без текстового слоя "
              "нужен OCR, это отдельная история, здесь не реализовано.", file=sys.stderr)
        sys.exit(1)

    sections = split_sections(text)
    print(f"Распознаны секции: {', '.join(sections.keys())} "
          f"({len(text)} символов всего)")

    print(f"Считаю эмбеддинги ({config.EMBEDDING_PROVIDER})...")
    embedder = get_embedder()
    sections_with_embeddings = {}
    for name, content in sections.items():
        vec = embed_as_single_vector(embedder, content)
        sections_with_embeddings[name] = (content, vec)

    conn = postgres_store.get_connection()
    postgres_store.ensure_schema(conn)
    resume_id = postgres_store.upsert_resume(conn, p.name, text)
    postgres_store.replace_resume_sections(conn, resume_id, sections_with_embeddings)
    print(f"Резюме сохранено, id={resume_id}")

    if do_match:
        _print_matches(conn, resume_id, "sections" if by_section else "full", top_k,
                        city=city, min_salary=min_salary, currency=currency, since=since)
    conn.close()
    return resume_id


def cmd_match(resume_id: int, by_section: bool, top_k: int,
              city: str = None, min_salary: float = None, currency: str = None, since: str = None):
    conn = postgres_store.get_connection()
    _print_matches(conn, resume_id, "sections" if by_section else "full", top_k,
                    city=city, min_salary=min_salary, currency=currency, since=since)
    conn.close()


def _print_matches(conn, resume_id: int, mode: str, top_k: int,
                    city: str = None, min_salary: float = None, currency: str = None, since: str = None):
    print(f"\nПодбор вакансий (mode={mode}) для резюме id={resume_id}:\n")
    results = postgres_store.match_vacancies_for_resume(
        conn, resume_id, top_k=top_k, mode=mode,
        city=city, min_salary=min_salary, salary_currency=currency, published_after=since,
    )
    if not results:
        print("Ничего не найдено — либо база вакансий пуста, либо вакансии ещё не засинканы "
              "(python sync_supabase.py), либо ничего не подошло под фильтры.")
        return
    for i, r in enumerate(results, 1):
        by_section = f"  [{', '.join(f'{k}={v}' for k, v in r['by_section'].items())}]" if "by_section" in r else ""
        salary = ""
        if r.get("salary_min") or r.get("salary_max"):
            salary = f" | {r.get('salary_min') or '?'}–{r.get('salary_max') or '?'} {r.get('salary_currency') or ''}"
        city_s = f" | {r['city']}" if r.get("city") else ""
        print(f"{i}. [{r['distance']:.3f}] {r['title']} — {r['company']} ({r['source']}){city_s}{salary}{by_section}")
        print(f"   {r['url']}")


def cmd_list():
    conn = postgres_store.get_connection()
    resumes = postgres_store.list_resumes(conn)
    conn.close()
    if not resumes:
        print("Резюме ещё не загружались.")
        return
    for r in resumes:
        print(f"id={r['id']}  {r['filename']}  ({r['uploaded_at']})")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_upload = sub.add_parser("upload", help="загрузить резюме (pdf/md/txt) и посчитать эмбеддинги")
    p_upload.add_argument("path")
    p_upload.add_argument("--match", action="store_true", help="сразу показать подбор вакансий")
    p_upload.add_argument("--by-section", action="store_true", help="подбор взвешенно по секциям, не одним эмбеддингом")
    p_upload.add_argument("--top-k", type=int, default=15)
    p_upload.add_argument("--city", help="фильтр: подстрока города (ILIKE)")
    p_upload.add_argument("--min-salary", type=float, help="фильтр: минимальная salary_max вакансии")
    p_upload.add_argument("--currency", help="фильтр: валюта зарплаты (UAH/USD/EUR), вместе с --min-salary")
    p_upload.add_argument("--since", metavar="YYYY-MM-DD", help="фильтр: вакансии опубликованы не раньше этой даты")

    p_match = sub.add_parser("match", help="подобрать вакансии под уже загруженное резюме")
    p_match.add_argument("--resume-id", type=int, required=True)
    p_match.add_argument("--by-section", action="store_true")
    p_match.add_argument("--top-k", type=int, default=15)
    p_match.add_argument("--city", help="фильтр: подстрока города (ILIKE)")
    p_match.add_argument("--min-salary", type=float, help="фильтр: минимальная salary_max вакансии")
    p_match.add_argument("--currency", help="фильтр: валюта зарплаты (UAH/USD/EUR), вместе с --min-salary")
    p_match.add_argument("--since", metavar="YYYY-MM-DD", help="фильтр: вакансии опубликованы не раньше этой даты")

    sub.add_parser("list", help="список загруженных резюме")

    args = parser.parse_args()

    if args.cmd == "upload":
        cmd_upload(args.path, args.match, args.by_section, args.top_k,
                   city=args.city, min_salary=args.min_salary, currency=args.currency, since=args.since)
    elif args.cmd == "match":
        cmd_match(args.resume_id, args.by_section, args.top_k,
                  city=args.city, min_salary=args.min_salary, currency=args.currency, since=args.since)
    elif args.cmd == "list":
        cmd_list()


if __name__ == "__main__":
    main()
