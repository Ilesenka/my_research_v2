"""Проверка результата: python3 checks/artefact2/check_review.py results/artefact2/. Код 0 — принято, 1 — нет."""

import csv
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
library = root / "paper" / "library_artefact2"
manifest = library / "manifest.csv"
out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root / "results" / "artefact2"
table = out / "table.csv"
draft = out / "draft.md"

table_columns = {
    "doi", "year", "authors", "reaction", "Eᵐ_MeV",
    "isomeric_ratio_as_reported", "uncertainty", "method",
    "main_result", "file", "page",
}
manifest_columns = {"doi", "year", "file"}
errors = []


def norm_doi(value):
    value = value.strip().lower()
    value = re.sub(r"^https?://doi\.org/", "", value)
    value = re.sub(r"^doi:\s*", "", value)
    return value.rstrip(".,;")


pdfs = {
    path.name for path in library.iterdir()
    if path.is_file() and path.suffix.lower() == ".pdf"
} if library.is_dir() else set()

if len(pdfs) != 3:
    errors.append(f"PDF-файлов в library_artefact2/: {len(pdfs)}, ожидалось 3")

if not manifest.is_file():
    print("нет paper/library_artefact2/manifest.csv")
    sys.exit(1)

with manifest.open(encoding="utf-8-sig", newline="") as file:
    reader = csv.DictReader(file)
    manifest_header = set(reader.fieldnames or [])
    manifest_rows = list(reader)

if not manifest_columns <= manifest_header:
    print("в manifest.csv нужны столбцы doi, year, file")
    sys.exit(1)

if len(manifest_rows) != 3:
    errors.append(f"строк в manifest.csv: {len(manifest_rows)}, ожидалось 3")

manifest_by_file = {}
manifest_year_by_file = {}

for row in manifest_rows:
    filename = row["file"].strip()
    doi = norm_doi(row["doi"])
    year = row["year"].strip()

    if not filename or not doi or not year:
        errors.append("в manifest.csv есть пустое поле doi, year или file")
        continue

    if filename in manifest_by_file:
        errors.append(f"повтор файла в manifest.csv: {filename}")

    manifest_by_file[filename] = doi
    manifest_year_by_file[filename] = year

if set(manifest_by_file) != pdfs:
    errors.append("список PDF не совпадает со списком файлов в manifest.csv")

if not table.is_file():
    print("нет файла results/artefact2/table.csv")
    sys.exit(1)

if not draft.is_file():
    print("нет файла results/artefact2/draft.md")
    sys.exit(1)

with table.open(encoding="utf-8-sig", newline="") as file:
    reader = csv.DictReader(file)
    table_header = set(reader.fieldnames or [])
    rows = list(reader)

if not table_columns <= table_header:
    print("в table.csv отсутствуют обязательные столбцы")
    sys.exit(1)

if len(rows) != 3:
    errors.append(f"строк в table.csv: {len(rows)}, ожидалось 3")

seen_files = set()
table_dois = set()

for number, row in enumerate(rows, start=2):
    filename = row["file"].strip()
    doi = norm_doi(row["doi"])
    year = row["year"].strip()

    for column in table_columns:
        if not row[column].strip():
            errors.append(f"строка {number}: пустое поле {column}")

    if filename not in pdfs:
        errors.append(f"строка {number}: PDF не найден: {filename}")

    if filename in seen_files:
        errors.append(f"PDF указан больше одного раза: {filename}")
    seen_files.add(filename)

    if filename in manifest_by_file:
        if doi != manifest_by_file[filename]:
            errors.append(f"строка {number}: DOI не совпадает с manifest.csv для {filename}")

        if year != manifest_year_by_file[filename]:
            errors.append(f"строка {number}: год не совпадает с manifest.csv для {filename}")

    elif filename:
        errors.append(f"файл отсутствует в manifest.csv: {filename}")

    table_dois.add(doi)

if seen_files != pdfs:
    errors.append("в table.csv должна быть ровно одна строка для каждого PDF")

text = draft.read_text(encoding="utf-8")
cited_dois = {
    norm_doi(doi)
    for doi in re.findall(r"\[(10\.\d{4,9}/[^\]\s]+)\]", text)
}

for doi in sorted(table_dois - cited_dois):
    errors.append(f"в draft.md нет ссылки [{doi}]")

for doi in sorted(cited_dois - table_dois):
    errors.append(f"в draft.md есть DOI, которого нет в table.csv: {doi}")

print("\n".join(errors) if errors else "принято: таблица, PDF и DOI-ссылки согласованы")
sys.exit(1 if errors else 0)
