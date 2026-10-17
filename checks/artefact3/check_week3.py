#!/usr/bin/env python3
"""Самопроверка артефакта недели 3 (реконструкция по practice.md и EXPECTED.md).

Использование:
    python3 check_week3.py [ПРОЕКТ] [--memory ПАПКА] [--paper PDF] [--no-git]

ПРОЕКТ   — корень проекта (по умолчанию текущая папка).
--memory — папка с L1.md, L2.md, L3.md (по умолчанию ПРОЕКТ/memory).
--paper  — PDF статьи (по умолчанию первый файл ПРОЕКТ/paper/*.pdf).
--no-git — не проверять, что всё закоммичено.

Блоки: «Сжатие статьи», «AGENTS.md», «INDEX.md», «git». Ноутбук шага 1
практики этим скриптом не проверяется.
[OK] / [НЕТ] — обязательный пункт выполнен / не выполнен; [СОВЕТ] — не влияет
на итог. Если PDF не найден или не читается, цитаты автоматически не сверяются.
Код возврата 0, если все проверяемые обязательные пункты выполнены, иначе 1.

Цитаты из таблицы сверки ищутся в тексте указанной страницы PDF. Сравнение
без учёта регистра, пробелов, переносов и знаков препинания; пропуск внутри
цитаты обозначается многоточием («…» или «...»).
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import unicodedata

FAILS = []


def ok(msg):
    print(f"[OK]    {msg}")


def bad(msg):
    FAILS.append(msg)
    print(f"[НЕТ]   {msg}")


def tip(msg):
    print(f"[СОВЕТ] {msg}")


def header(title, path):
    print(f"\n== {title}: {os.path.abspath(path)}")


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def words(text):
    return len(re.findall(r"\w+(?:[-–]\w+)*", text))


def is_placeholder(s):
    return bool(re.search(r"<[^>]*>", s))


# ---------- PDF ----------

def pdf_pages(path):
    """Список текстов страниц (индекс 0 = с. 1) или None."""
    if not path or not os.path.isfile(path):
        return None
    if shutil.which("pdftotext"):
        try:
            out = subprocess.run(["pdftotext", "-enc", "UTF-8", path, "-"],
                                 capture_output=True, check=True).stdout
            pages = out.decode("utf-8", "replace").split("\f")
            if pages and not pages[-1].strip():
                pages = pages[:-1]
            if any(p.strip() for p in pages):
                return pages
        except (subprocess.CalledProcessError, OSError):
            pass
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(path)
        pages = [(pg.extract_text() or "") for pg in reader.pages]
        if any(p.strip() for p in pages):
            return pages
    except Exception:
        pass
    return None


def norm(text):
    # /C14 и т. п. — коды глифов, которые pypdf выдаёт вместо символов (°, ×, −)
    text = re.sub(r"/C\d+", "", text)
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("ё", "е")
    # Сохраняем буквы любых письменностей (в том числе греческие символы)
    # и цифры, удаляя только знаки пунктуации и пробелы.
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def fragments(quote):
    parts = re.split(r"…|\.\.\.|\[\.\.\.\]", quote)
    return [norm(p) for p in parts if len(norm(p)) >= 3]


def found_in(frags, text_norm):
    pos = 0
    for fr in frags:
        i = text_norm.find(fr, pos)
        if i < 0:
            return False
        pos = i + len(fr)
    return True


def page_ranges(cell):
    """Диапазоны страниц PDF, явно указанные в ячейке таблицы."""
    ranges = []
    for a, b in re.findall(r"(\d+)\s*(?:[–—-]\s*(\d+))?", cell):
        first = int(a)
        last = int(b) if b else first
        ranges.append((min(first, last), max(first, last)))
    return ranges


# ---------- блоки ----------

def check_memory(mem, paper_path):
    header("Сжатие статьи", mem)
    l1, l2, l3 = (read(os.path.join(mem, f)) for f in ("L1.md", "L2.md", "L3.md"))

    # L1
    if l1 is None:
        bad("L1.md не найден")
    else:
        parts = len(re.findall(r"^##\s", l1, re.M))
        line = re.search(r"^Статья:(.*)$", l1, re.M)
        if parts == 0:
            bad("L1: нет частей (заголовков «## …»)")
        elif not line or not line.group(1).strip() or is_placeholder(line.group(1)):
            bad("L1: не заполнена строка «Статья:» (авторы, название, издание, год, DOI/arXiv)")
        else:
            ok(f"L1: частей — {parts}, слов — {words(l1)}")
        if l1 and not re.search(r"\(с\.\s*\d", l1):
            tip("L1: нет ни одной страницы вида (с. N)")

    # L2
    rows = []
    if l2 is None:
        bad("L2.md не найден")
    else:
        m_sv = re.search(r"^##\s*Сверка.*$", l2, re.M)
        body, table = (l2[:m_sv.start()], l2[m_sv.end():]) if m_sv else (l2, "")
        theses = [t for t in re.findall(r"^\s*(\d+)\.\s+(.+)$", body, re.M)
                  if not is_placeholder(t[1])]
        n = len(theses)
        if 5 <= n <= 10:
            ok(f"L2: тезисов — {n}, слов — {words(body)}")
        elif n == 0:
            bad("L2: нет тезисов (нумерованный список «1. …»)")
        else:
            bad(f"L2: тезисов — {n}, нужно от 5 до 10")
        thesis_text = {}
        for m in re.finditer(r"^\s*(\d+)\.\s+(.+(?:\n(?!\s*\d+\.\s|\s*$|#).+)*)", body, re.M):
            thesis_text[m.group(1)] = m.group(2)
        no_page = [k for k, v in thesis_text.items() if not re.search(r"с\.\s*\d", v)]
        if no_page:
            bad(f"L2: тезисы без страницы: {', '.join(no_page)}")
        for line_no, line in enumerate(table.splitlines(), 1):
            if not re.match(r"^\|\s*\d", line):
                continue
            cells = [c.strip().replace(r"\|", "|")
                     for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
            if len(cells) < 5:
                bad(f"L2: строка таблицы сверки {line_no} содержит меньше 5 столбцов")
                continue
            rows.append(cells)

    # L3
    if l3 is None:
        bad("L3.md не найден")
    else:
        lines = [s for s in l3.splitlines() if s.strip() and not s.lstrip().startswith("#")]
        if len(lines) != 1 or is_placeholder(lines[0]):
            bad(f"L3: нужна одна строка текста, сейчас — {len(lines)}")
        else:
            w = words(lines[0])
            ok(f"L3: одна строка, слов — {w}")
            if w > 30:
                tip("L3: больше 30 слов")

    # статья
    if paper_path is None:
        cands = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(mem)), "paper", "*.pdf")))
        paper_path = cands[0] if cands else None
    pages = pdf_pages(paper_path)
    if pages is None:
        tip("статья не найдена или не читается (paper/*.pdf): цитаты не проверены автоматически")
    elif l1 and l2 and l3:
        total = sum(words(p) for p in pages)
        l2_body = re.split(r"^##\s*Сверка", l2, flags=re.M)[0]
        l3_line = [s for s in l3.splitlines() if s.strip() and not s.startswith("#")]
        print(f"[OK]    сжатие: статья {total} слов → L1 {words(l1)} → L2 {words(l2_body)}"
              f" → L3 {words(l3_line[0]) if l3_line else 0}")

    # сверка
    good = [r for r in rows if not any(is_placeholder(c) for c in r[1:5])
            and page_ranges(r[2]) and len(norm(r[3])) >= 10]
    if len(good) < 3:
        bad(f"сверка: тезисов со страницей и цитатой — {len(good)}, нужно не меньше 3")
        return
    problems = []
    for r in good:
        num, thesis, pg, quote, verdict = r[0], r[1], r[2], r[3], r[4]
        v = verdict.lower()
        if not (v.startswith("подтвержд") or v.startswith("расходится") or v.startswith("нет в тексте")):
            bad(f"сверка, строка {num}: укажите итог — «подтверждено», «расходится: …» или «нет в тексте»")
        elif v.startswith("расходится") and "исправлен" not in v:
            bad(f"сверка, строка {num}: после «расходится: …» укажите, что тезис исправлен")
        if pages is None:
            continue
        frags = fragments(quote.strip().strip('"«»“”„'))
        want = page_ranges(pg)
        hit = False
        for first, last in want:
            span = "".join(pages[first - 1:last]) if 0 < first <= last <= len(pages) else ""
            if found_in(frags, norm(span)):
                hit = True
                break
        if hit:
            continue
        where = [str(i + 1) for i, t in enumerate(pages) if found_in(frags, norm(t))]
        if not where:
            where = [f"{i + 1}–{i + 2}" for i in range(len(pages) - 1)
                     if found_in(frags, norm(pages[i] + pages[i + 1]))]
        if where:
            problems.append(f"строка {num}: цитата найдена на с. {where[0]}, а указана с. {pg}")
        else:
            problems.append(f"строка {num}: цитата не найдена в статье (с. {pg})")
    if pages is None:
        ok(f"сверка: тезисов со страницей и цитатой — {len(good)}")
    elif problems:
        for p in problems:
            bad(f"сверка, {p}")
    else:
        ok(f"сверка: тезисов со страницей и цитатой — {len(good)}, цитаты найдены")


def check_agents(proj):
    header("AGENTS.md", proj)
    path = os.path.join(proj, "AGENTS.md")
    text = read(path)
    if os.path.exists(os.path.join(proj, "AGENT.md")):
        tip("в проекте есть и AGENT.md: файл агента должен быть один — AGENTS.md")
    if text is None:
        bad("AGENTS.md не найден в корне проекта")
        return
    n = len(re.findall(r"^##\s", text, re.M))
    (ok if n >= 5 else bad)(f"разделов — {n}" + ("" if n >= 5 else ", нужно не меньше 5"))
    m = re.search(r"^(#+)\s*[^\n]*не может знать сам[^\n]*$", text, re.M | re.I)
    if not m:
        bad("нет раздела «Чего агент не может знать сам»")
        return
    level = len(m.group(1))
    rest = text[m.end():]
    stop = re.search(rf"^#{{1,{level}}}\s", rest, re.M)
    section = rest[:stop.start()] if stop else rest
    items = [s for s in re.findall(r"^\s*(?:[-*]|\d+\.)\s+(.+)$", section, re.M) if not is_placeholder(s)]
    if items:
        ok(f"раздел «Чего агент не может знать сам»: пунктов — {len(items)}")
    else:
        bad("раздел «Чего агент не может знать сам» пуст")


def check_index(proj):
    header("INDEX.md", proj)
    text = read(os.path.join(proj, "INDEX.md"))
    if text is None:
        bad("INDEX.md не найден в корне проекта")
        return
    paths = []
    for tok in re.findall(r"`([^`]+)`", text):
        tok = tok.strip().split("::")[0]
        if not tok or " " in tok or "(" in tok:
            continue
        if "/" in tok or re.search(r"\.[A-Za-z0-9]{1,5}$", tok):
            paths.append(tok)
    paths = list(dict.fromkeys(paths))
    if not paths:
        bad("INDEX.md: нет путей в обратных кавычках")
        return
    missing = [p for p in paths if not os.path.exists(os.path.join(proj, p))]
    if missing:
        bad(f"в INDEX.md пути, которых нет в проекте: {', '.join(missing)}")
    else:
        ok(f"INDEX.md: путей — {len(paths)}, все существуют")
    if "memory/L2.md" not in paths and os.path.exists(os.path.join(proj, "memory", "L2.md")):
        tip("включите в INDEX.md строку о memory/L2.md — сжатие статьи")


def check_git(proj, mem):
    header("git", proj)
    try:
        subprocess.run(["git", "-C", proj, "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        bad("проект не в git-репозитории")
        return
    result = subprocess.run(["git", "-C", proj, "status", "--short", "--untracked-files=all"],
                            capture_output=True, text=True)
    if result.returncode:
        bad("не удалось получить состояние Git: " + result.stderr.strip())
        return
    out = result.stdout.strip()
    if out:
        bad("в проекте остались незакоммиченные изменения: " + ", ".join(l[3:] for l in out.splitlines()))
    else:
        ok("все изменения проекта закоммичены")


def main():
    ap = argparse.ArgumentParser(description="Самопроверка артефакта недели 3")
    ap.add_argument("project", nargs="?", default=".")
    ap.add_argument("--memory")
    ap.add_argument("--paper")
    ap.add_argument("--no-git", action="store_true")
    a = ap.parse_args()
    proj = a.project
    mem = a.memory or os.path.join(proj, "memory")
    paper = a.paper
    if paper is None:
        cands = sorted(glob.glob(os.path.join(proj, "paper", "*.pdf")))
        paper = cands[0] if cands else None

    check_memory(mem, paper)
    check_agents(proj)
    check_index(proj)
    if not a.no_git:
        check_git(proj, mem)

    print()
    if FAILS:
        print(f"Не выполнены обязательные пункты: {len(FAILS)}.")
        sys.exit(1)
    print("Все обязательные пункты выполнены.")


if __name__ == "__main__":
    main()
