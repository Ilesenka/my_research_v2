#!/usr/bin/env bash
# Каскадное сжатие статьи учебным агентом: L1 (по частям) -> L2 (тезисы) -> L3 (одна строка).
# Каждая часть сжимается отдельным запуском `ai4science run`, то есть в своём окне контекста;
# следующий уровень читает только файлы предыдущего.
#
# Запуск из корня проекта (macOS, Linux, WSL):
#     bash cascade.sh paper/paper.pdf
# Результат:
#     paper/paper.txt           текст статьи; страницы разделены символом \f
#     paper/parts/part_NN.md    части по WORDS_PER_PART слов (по умолчанию 1500) с метками [с. N];
#                               если есть paper/sections/*.md (статья, разбитая на разделы вручную),
#                               сжимаются они
#     memory/L1/<часть>.md      сжатие каждой части
#     memory/L1.md, L2.md, L3.md
# Повторный запуск пропускает уже сжатые части. Чтобы начать заново: rm -rf paper/parts memory/L1
# Сверку в memory/L2.md заполняет человек.
set -euo pipefail

PDF="${1:?укажите PDF статьи: bash cascade.sh paper/paper.pdf}"
WORDS_PER_PART="${WORDS_PER_PART:-1500}"
export AI4SCIENCE_WEB_DISABLED=1   # поиск в сети не нужен; Lean отключает флаг --no-lean

# 1. Текст статьи по страницам
TXT="${PDF%.pdf}.txt"
if command -v pdftotext >/dev/null 2>&1; then
  pdftotext "$PDF" "$TXT"
else
  python3 - "$PDF" "$TXT" <<'PY'
import sys
from pypdf import PdfReader  # python3 -m pip install pypdf
pages = [p.extract_text() or "" for p in PdfReader(sys.argv[1]).pages]
open(sys.argv[2], "w", encoding="utf-8").write("\f".join(pages))
PY
fi

# 2. Части: разделы из paper/sections/ или подряд идущие страницы, в сумме около WORDS_PER_PART слов
mkdir -p memory/L1
if ls paper/sections/*.md >/dev/null 2>&1; then
  PARTS=paper/sections
else
  PARTS=paper/parts
  mkdir -p "$PARTS"
fi
if [ "$PARTS" = paper/parts ] && ! ls paper/parts/part_*.md >/dev/null 2>&1; then
  python3 - "$TXT" "$WORDS_PER_PART" <<'PY'
import sys
pages = open(sys.argv[1], encoding="utf-8").read().split("\f")
limit = int(sys.argv[2])
parts, cur, n = [], [], 0
for i, text in enumerate(pages, start=1):
    if not text.strip():
        continue
    cur.append((i, text))
    n += len(text.split())
    if n >= limit:
        parts.append(cur)
        cur, n = [], 0
if cur:
    parts.append(cur)
for k, part in enumerate(parts, start=1):
    first, last = part[0][0], part[-1][0]
    with open(f"paper/parts/part_{k:02d}.md", "w", encoding="utf-8") as f:
        f.write(f"# Часть {k}: с. {first}–{last}\n\n")
        for i, text in part:
            f.write(f"[с. {i}]\n{text.strip()}\n\n")
print(f"частей: {len(parts)}")
PY
fi

# 3. L1: каждая часть — в отдельном запуске агента
for f in "$PARTS"/*.md; do
  out="memory/L1/$(basename "$f")"
  if [ -s "$out" ]; then echo "уже есть: $out"; continue; fi
  ai4science --no-lean run "Прочитай файл $f — часть научной статьи; метки [с. N] — номера страниц. \
Сожми эту часть до 100–150 слов по-русски: главное утверждение, метод, результат. \
Числа, формулы и названия — дословно; после каждого числа и утверждения — страница в виде (с. N). \
Первая строка — заголовок '## <тема части> (с. a–b)'. Запиши результат в файл $out и больше ничего не делай."
  [ -s "$out" ] || echo "ВНИМАНИЕ: агент не записал $out — запустите скрипт ещё раз"
done

{
  echo "# L1 — реферат по частям"
  echo
  echo "Статья: <авторы, «название», издание, год; DOI или arXiv с номером версии>."
  echo "Страницы — по файлу $PDF."
  echo
  for f in "$PARTS"/*.md; do cat "memory/L1/$(basename "$f")" 2>/dev/null; echo; done
} > memory/L1.md

# 4. L2: тезисы по L1, в новом окне
ai4science --no-lean run "Прочитай memory/L1.md. Составь от 5 до 10 тезисов всей статьи по-русски. \
Каждый тезис — одно проверяемое утверждение: результат с числом, условие, ограничение; \
в конце тезиса — страница (с. N) из L1. Запиши в memory/L2.md: первая строка '# L2 — тезисы', \
пустая строка, затем нумерованный список. Больше ничего в файл не пиши."

if [ -s memory/L2.md ] && ! grep -q '^## Сверка' memory/L2.md; then
  cat >> memory/L2.md <<'EOF'

## Сверка с оригиналом

Не меньше трёх тезисов сверяются по PDF, не по L1: страница, дословная цитата, итог.
Итог — «подтверждено»; «расходится: …; исправлено» — что именно расходилось;
«нет в тексте; тезис удалён».

| № | Тезис | Стр. | Цитата из оригинала | Итог |
|---|---|---|---|---|
| 1 | <номер тезиса> | <N> | "<дословная цитата>" | <итог> |
| 2 | <номер тезиса> | <N> | "<дословная цитата>" | <итог> |
| 3 | <номер тезиса> | <N> | "<дословная цитата>" | <итог> |
EOF
fi

# 5. L3: одна строка по L2, в новом окне
ai4science --no-lean run "Прочитай memory/L2.md, только нумерованный список тезисов. \
Сформулируй главный результат статьи одной фразой до 30 слов по-русски. \
Запиши в memory/L3.md: первая строка '# L3 — одна строка', пустая строка, затем эта фраза. Больше ничего не пиши."

echo
echo "Слов: статья $(wc -w < "$TXT"), L1 $(wc -w < memory/L1.md), L2 $(wc -w < memory/L2.md), L3 $(wc -w < memory/L3.md)"
echo "Дальше: заполните строку «Статья:» в memory/L1.md и таблицу сверки в memory/L2.md."
