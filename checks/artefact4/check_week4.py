#!/usr/bin/env python3
"""Самопроверка недели 4, восстановленная по practice.md и EXPECTED.md.

Это самостоятельная реализация, а не оригинальный проверяющий скрипт курса.
Python 3.9+, единственная внешняя зависимость — PyYAML.

    python3 check_week4.py [ПРОЕКТ] [--strict] [--mermaid graph_view.md]

ПРОЕКТ по умолчанию — текущая папка. Относительный путь --mermaid считается
от корня проекта. Экспорт — единственная операция записи; исходники не меняются.
Код 0 — обязательные проверки пройдены; 1 — ошибки; 2 — ошибка запуска.
--strict делает замечания [ГРАФ] и нарушения [ПРАВИЛО] ошибками.
Структурные ошибки, неверные типы связей и минимум 10 объектов обязательны всегда.

Проверяет YAML, наследование, параметры правил, корневое хранилище Obsidian,
заметки классов, объекты, ссылки, типы концов связей, атрибуты, задачи и правила.
Виды check: some, some_inverse, some_inverse_any, all_inverse, defined,
defined_inverse, defined_value, section, disjoint_properties.
Выводимые классы вычисляются до неподвижной точки. Учитываются наследование,
транзитивные и симметричные отношения; проверяется функциональность.

Это локальная проверка записанного графа в закрытом мире, не OWL/DL reasoner.
Строка dl хранится для человека; её соответствие text/check не доказывается.
Правила без check не исполняются и не выдаются за проверенные. Не проверяются
научная достоверность, реальность приёмки человеком, неизменность условий задачи
между сессиями, история двух сессий и чистота Git — их проверяет пользователь.
"""

from __future__ import annotations

import argparse
import collections
from dataclasses import dataclass, field
import html
import itertools
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, Optional
import unicodedata
from urllib.parse import unquote, urlsplit

try:
    import yaml
except ImportError:
    yaml = None


STATUSES = {"открыта", "в работе", "выполнена", "заблокирована", "принята"}
DONE = {"выполнена", "принята"}
CHARACTERISTICS = {
    "транзитивное": "transitive", "transitive": "transitive",
    "симметричное": "symmetric", "symmetric": "symmetric",
    "функциональное": "functional", "functional": "functional",
}
KINDS = {
    "some", "some_inverse", "some_inverse_any", "all_inverse",
    "defined", "defined_inverse", "defined_value", "section",
    "disjoint_properties",
}
DEFINED = {"defined", "defined_inverse", "defined_value"}
SKIP_DIRS = {".git", ".obsidian", ".venv", "venv", "__pycache__", "node_modules"}
WIKI = re.compile(r"!?\[\[([^\]\n]+)\]\]")
LINK_LINE = re.compile(r"^\s*[-*]\s+([^:\n]+):\s*(.*?)\s*$")
HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
PLACEHOLDER = re.compile(r"<[^<>\n]+>")


class Report:
    def __init__(self, strict: bool = False):
        self.strict = strict
        self.errors = 0
        self.graph_issues = 0
        self.rule_issues = 0
        self.messages: list[tuple[str, str]] = []

    def emit(self, tag: str, message: str) -> None:
        self.messages.append((tag, message))
        print(f"[{tag}] {message}")

    def ok(self, message: str) -> None:
        self.emit("OK", message)

    def bad(self, message: str) -> None:
        self.errors += 1
        self.emit("НЕТ", message)

    def tip(self, message: str) -> None:
        self.emit("СОВЕТ", message)

    def graph(self, message: str) -> None:
        self.graph_issues += 1
        self.errors += int(self.strict)
        self.emit("ГРАФ", message)

    def rule(self, message: str) -> None:
        self.rule_issues += 1
        self.errors += int(self.strict)
        self.emit("ПРАВИЛО", message)


def text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\\", "/"))


def scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) and value is not None


def same_value(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b


def one_or_many(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and value and all(text(x) for x in value):
        return value
    return []


def has_placeholder(value: Any, seen: Optional[set[int]] = None) -> bool:
    seen = set() if seen is None else seen
    if isinstance(value, str):
        return bool(PLACEHOLDER.search(value))
    if isinstance(value, (dict, list)):
        if id(value) in seen:
            raise ValueError("циклические YAML-ссылки не поддерживаются")
        seen.add(id(value))
        items = itertools.chain(value.keys(), value.values()) if isinstance(value, dict) else value
        result = any(has_placeholder(x, seen) for x in items)
        seen.remove(id(value))
        return result
    return False


def read_yaml(raw: str) -> Any:
    """SafeLoader с явным отказом при повторяющихся ключах."""
    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader: Any, node: Any, deep: bool = False) -> dict:
        loader.flatten_mapping(node)
        result: dict = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                if key in result:
                    raise ValueError(f"повтор ключа {key!r}, строка {key_node.start_mark.line + 1}")
                result[key] = loader.construct_object(value_node, deep=deep)
            except TypeError as exc:
                raise ValueError("ключ YAML должен быть скалярным") from exc
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    return yaml.load(raw, Loader=UniqueLoader)


@dataclass
class Ontology:
    raw: dict
    classes: dict
    properties: dict
    attributes: dict
    rules: list
    ancestors: dict[str, set[str]]
    derived: set[str]

    def is_a(self, actual: str, wanted: str) -> bool:
        return actual == wanted or wanted in self.ancestors.get(actual, set())

    def accepts(self, actual: set[str], wanted: str) -> bool:
        if wanted == "⊤":
            return True
        if wanted == "⊥":
            return False
        return any(self.is_a(a, wanted) for a in actual)


def load_ontology(path: Path, report: Report) -> Optional[Ontology]:
    print(f"\n== Онтология: {path}")
    before = report.errors
    try:
        raw = read_yaml(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("ожидается YAML-словарь")
        if has_placeholder(raw):
            report.bad("в онтологии остались незаполненные места шаблона <…>")
    except (OSError, UnicodeError, ValueError, yaml.YAMLError, RecursionError) as exc:
        report.bad(f"не удалось прочитать ontology.yaml: {exc}")
        return None
    for key in ("ontology", "goal"):
        if not text(raw.get(key)):
            report.bad(f"поле {key}: нужна непустая строка")
    questions = raw.get("competency_questions")
    if not isinstance(questions, list) or not 3 <= len(questions) <= 5 or not all(text(q) for q in questions):
        report.bad("competency_questions: нужны 3–5 непустых вопросов")

    classes = raw.get("classes")
    properties = raw.get("properties")
    attributes = raw.get("attributes", {})
    rules = raw.get("rules")
    for key, value in (("classes", classes), ("properties", properties), ("attributes", attributes)):
        if not isinstance(value, dict):
            report.bad(f"{key}: ожидается словарь")
    if not isinstance(classes, dict) or not isinstance(properties, dict) or not isinstance(attributes, dict):
        return None
    if len(classes) < 3:
        report.bad(f"классов — {len(classes)}, нужно не меньше трёх")
    for required in ("Задача", "Файл"):
        if required not in classes:
            report.bad(f"необходим класс {required}")
    for name, spec in classes.items():
        if not text(name) or re.search(r'[\s/\\:#|\[\]]', str(name)):
            report.bad(f"неверное имя класса: {name!r}")
        if not isinstance(spec, dict):
            report.bad(f"класс {name}: ожидается словарь")
            continue
        if not text(spec.get("definition")):
            report.bad(f"класс {name}: нужно определение definition")
        if "parent" not in spec:
            report.bad(f"класс {name}: нужен parent (или null)")
        parent = spec.get("parent")
        if parent is not None and (not isinstance(parent, str) or parent not in classes):
            report.bad(f"класс {name}: неизвестный parent {parent!r}")
    ancestors: dict[str, set[str]] = {}
    for name in classes:
        seen: set[str] = set()
        current = name
        while current in classes and isinstance(classes[current], dict):
            parent = classes[current].get("parent")
            if not isinstance(parent, str) or parent not in classes:
                break
            if parent == name or parent in seen:
                report.bad(f"цикл наследования у класса {name}")
                break
            seen.add(parent)
            current = parent
        ancestors[name] = seen

    def refs(value: Any, label: str, choices: set[str]) -> None:
        names = one_or_many(value)
        if not names:
            report.bad(f"{label}: нужен класс или непустой список классов")
        for n in names:
            if n not in choices:
                report.bad(f"{label}: неизвестный класс {n}")

    for name, spec in properties.items():
        if not text(name) or re.search(r"[\s:]", str(name)):
            report.bad(f"неверное имя отношения: {name!r}")
        if name in {"тип", "подклассОт"}:
            report.bad(f"отношение {name}: служебное имя зарезервировано")
        if not isinstance(spec, dict):
            report.bad(f"отношение {name}: ожидается словарь")
            continue
        refs(spec.get("domain"), f"{name}.domain", set(classes))
        refs(spec.get("range"), f"{name}.range", set(classes))
        chars = spec.get("characteristics", [])
        if not isinstance(chars, list) or any(not isinstance(c, str) or c not in CHARACTERISTICS for c in chars):
            report.bad(f"{name}.characteristics: допустимы транзитивное, симметричное, функциональное")
    for name, spec in attributes.items():
        if not text(name) or not isinstance(spec, dict):
            report.bad(f"атрибут {name!r}: нужен словарь с domain и values")
            continue
        refs(spec.get("domain"), f"{name}.domain", set(classes))
        values = spec.get("values")
        if not isinstance(values, list) or not values or not all(scalar(v) for v in values):
            report.bad(f"{name}.values: нужен непустой список скалярных значений")
    status = attributes.get("status")
    if not isinstance(status, dict):
        report.bad("необходим атрибут status класса Задача")
    else:
        domains = one_or_many(status.get("domain"))
        if not any(d == "Задача" or d in ancestors.get("Задача", set()) for d in domains):
            report.bad("status.domain должен распространяться на Задача")
        values = status.get("values", [])
        if not isinstance(values, list) or not all(s in values for s in STATUSES):
            report.bad("status.values: нужны открыта, в работе, выполнена, заблокирована, принята")
    subtask = properties.get("подзадачаОт")
    if not isinstance(subtask, dict):
        report.bad("необходимо отношение подзадачаОт: Задача → Задача")
    elif subtask.get("domain") != "Задача" or subtask.get("range") != "Задача":
        report.bad("подзадачаОт должно иметь domain: Задача и range: Задача")

    if not isinstance(rules, list) or len(rules) < 2:
        report.bad("правил должно быть не меньше двух")
        rules = rules if isinstance(rules, list) else []
    derived: set[str] = set()
    for rule in rules:
        if isinstance(rule, dict) and isinstance(rule.get("check"), dict):
            check = rule["check"]
            if isinstance(check.get("kind"), str) and check["kind"] in DEFINED and text(check.get("name")):
                if check["name"] in derived:
                    report.bad(f"повтор определения класса {check['name']}")
                derived.add(check["name"])
    known = set(classes) | derived | {"⊤", "⊥"}
    ids: set[str] = set()
    for i, rule in enumerate(rules, 1):
        label = f"правило {i}"
        if not isinstance(rule, dict):
            report.bad(f"{label}: ожидается словарь")
            continue
        for key in ("id", "text", "dl"):
            if not text(rule.get(key)):
                report.bad(f"{label}: нужна непустая строка {key}")
        rid = rule.get("id")
        if text(rid):
            label = rid
            if rid in ids:
                report.bad(f"повтор id правила {rid}")
            ids.add(rid)
        check = rule.get("check")
        if check is None:
            report.tip(f"{label}: check не задан — правило автоматически не проверяется")
            continue
        if not isinstance(check, dict):
            report.bad(f"{label}.check: ожидается словарь")
            continue
        kind = check.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            report.bad(f"{label}: неизвестный вид check {kind!r}")
            continue
        if kind in DEFINED and not text(check.get("name")):
            report.bad(f"{label}: нужен name выводимого класса")
        if kind != "disjoint_properties":
            if not isinstance(check.get("class"), str) or check["class"] not in known:
                report.bad(f"{label}: неизвестный class {check.get('class')!r}")
        if kind in {"some", "some_inverse", "all_inverse", "defined"}:
            if not isinstance(check.get("property"), str) or check["property"] not in properties:
                report.bad(f"{label}: неизвестное property {check.get('property')!r}")
            key = "source" if kind in {"some_inverse", "all_inverse"} else "target"
            if not isinstance(check.get(key), str) or check[key] not in known:
                report.bad(f"{label}: неизвестный {key} {check.get(key)!r}")
        if kind in {"some_inverse_any", "defined_inverse", "disjoint_properties"}:
            props = one_or_many(check.get("properties"))
            if not props or (kind == "disjoint_properties" and len(set(props)) < 2):
                report.bad(f"{label}: неверный список properties")
            for p in props:
                if p not in properties:
                    report.bad(f"{label}: неизвестное отношение {p}")
            check["properties"] = props
        if kind == "defined_value":
            attribute = attributes.get(check.get("attribute")) if isinstance(check.get("attribute"), str) else None
            vals = check.get("values")
            if not isinstance(attribute, dict):
                report.bad(f"{label}: неизвестный attribute")
            if not isinstance(vals, list) or not vals or not all(scalar(v) for v in vals):
                report.bad(f"{label}: нужен непустой список values")
            elif isinstance(attribute, dict) and isinstance(attribute.get("values"), list):
                for value in vals:
                    if not any(same_value(value, v) for v in attribute["values"]):
                        report.bad(f"{label}: значение {value!r} отсутствует в перечислении атрибута")
        if kind == "section" and not text(check.get("section")):
            report.bad(f"{label}: нужен section — заголовок раздела")
    if report.errors != before:
        return None
    report.ok(f"классов — {len(classes)} (нужно не меньше трёх)")
    report.ok(f"правил — {len(rules)} (нужно не меньше двух)")
    return Ontology(raw, classes, properties, attributes, rules, ancestors, derived)


def prose_only(body: str) -> str:
    """Исключает HTML-комментарии, блоки кода и inline code из ссылочного графа."""
    body = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group().count("\n"), body, flags=re.S)
    result = []
    fence: Optional[tuple[str, int]] = None
    for line in body.splitlines(keepends=True):
        match = re.match(r"^ {0,3}([" + chr(96) + r"~]{3,})(.*)$", line)
        if fence is not None:
            if match and set(match[1]) == {fence[0]} and len(match[1]) >= fence[1] and not match[2].strip():
                fence = None
            result.append("\n")
        elif match and len(set(match[1])) == 1:
            fence = (match[1][0], len(match[1]))
            result.append("\n")
        else:
            result.append(line)
    clean = "".join(result)
    return re.sub(r"(" + chr(96) + r"+)(.*?)\1", lambda m: "\n" * m.group().count("\n"), clean, flags=re.S)


def section(body: str, title: str) -> str:
    lines = body.splitlines()
    start = None
    level = 0
    for i, line in enumerate(lines):
        match = HEAD.match(line)
        if not match:
            continue
        if start is not None and len(match[1]) <= level:
            return "\n".join(lines[start:i]).strip()
        if start is None and match[2].strip() == title:
            start, level = i + 1, len(match[1])
    return "\n".join(lines[start:]).strip() if start is not None else ""


def frontmatter(path: Path, report: Report) -> tuple[dict, str]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
        if not raw.startswith("---\n") and not raw.startswith("---\r\n"):
            report.bad(f"{path.name}: нет YAML front matter с полем type/kind")
            return {}, raw
        lines = raw.splitlines()
        end = next((i for i in range(1, len(lines)) if lines[i].strip() in {"---", "..."}), None)
        if end is None:
            raise ValueError("не закрыт YAML front matter")
        data = read_yaml("\n".join(lines[1:end]))
        if not isinstance(data, dict):
            raise ValueError("YAML front matter должен быть словарём")
        if has_placeholder(data):
            report.graph(f"{path.name}: остались места шаблона в свойствах")
        return data, "\n".join(lines[end + 1:])
    except (OSError, UnicodeError, ValueError, yaml.YAMLError, RecursionError) as exc:
        report.bad(f"{path.name}: {exc}")
        return {}, ""


@dataclass
class Node:
    id: str
    path: Path
    meta: dict
    body: str
    clean: str
    types: set[str]


@dataclass(frozen=True)
class Edge:
    source: str
    relation: str
    target: str
    file_target: bool = False


@dataclass
class Graph:
    root: Path
    ontology: Ontology
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: set[Edge] = field(default_factory=set)
    type_edges: set[tuple[str, str]] = field(default_factory=set)
    file_targets: set[str] = field(default_factory=set)
    classes_present: set[str] = field(default_factory=set)
    types: dict[str, set[str]] = field(default_factory=dict)
    facts: set[tuple[str, str, str]] = field(default_factory=set)
    class_files: set[str] = field(default_factory=set)

    def belongs(self, node_id: str, wanted: str) -> bool:
        return self.ontology.accepts(self.types.get(node_id, set()), wanted)


def check_vault(root: Path, report: Report) -> None:
    before = report.errors
    for filename, required in (("app.json", "showUnsupportedFiles"), ("graph.json", "showAttachments")):
        path = root / ".obsidian" / filename
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict):
                raise ValueError("ожидается объект JSON")
            if config.get(required) is not True:
                report.bad(f".obsidian/{filename}: необходимо {required}: true")
            if filename == "graph.json":
                for key in ("showArrow", "showOrphans"):
                    if config.get(key) is not True:
                        report.tip(f".obsidian/graph.json: включите {key} для просмотра графа")
                if not config.get("colorGroups"):
                    report.tip(".obsidian/graph.json: группы цветов не настроены")
        except (OSError, UnicodeError, ValueError) as exc:
            report.bad(f".obsidian/{filename}: {exc}")
    if report.errors == before:
        report.ok("хранилище Obsidian инициализировано в корне проекта (.obsidian/)")


def load_graph(root: Path, ontology: Ontology, report: Report) -> Graph:
    graph = Graph(root, ontology)
    check_vault(root, report)
    for name, spec in ontology.classes.items():
        path = root / "ontology" / (name + ".md")
        if not path.is_file():
            report.graph(f"класс без заметки в ontology/: {name}")
            continue
        graph.classes_present.add(name)
        graph.class_files.add(path.relative_to(root).as_posix())
        meta, body = frontmatter(path, report)
        if meta.get("kind") != "класс":
            report.graph(f"ontology/{name}.md: нужно kind: класс")
        clean = prose_only(body)
        if not re.search(r"^#\s+" + re.escape(name) + r"\s*$", clean, re.M):
            report.graph(f"ontology/{name}.md: заголовок должен совпадать с именем класса")
        if text(spec.get("definition")) and spec["definition"] not in body:
            report.tip(f"ontology/{name}.md: проверьте соответствие определения ontology.yaml")
        parents = []
        for line in section(clean, "Связи").splitlines():
            match = LINK_LINE.match(line)
            if match:
                if match[1].strip() != "подклассОт":
                    report.graph(f"ontology/{name}.md: в связях класса допустимо подклассОт")
                else:
                    parents.extend(v.split("|", 1)[0].split("#", 1)[0].strip() for v in WIKI.findall(match[2]))
        expected = spec.get("parent")
        normalized = [PurePosixPath(p.replace("\\", "/")).stem for p in parents]
        if normalized != ([expected] if expected else []):
            report.graph(f"ontology/{name}.md: подклассОт не соответствует parent: {expected!r}")
    for path in (root / "ontology").glob("*.md"):
        if path.stem not in ontology.classes:
            report.graph(f"заметка класса без определения в ontology.yaml: {path.name}")
    report.ok(f"заметки классов в ontology/ — {len(graph.classes_present)} из {len(ontology.classes)}")
    for folder in ("tasks", "notes", "reports"):
        for path in sorted((root / folder).rglob("*.md")):
            relative = path.relative_to(root).as_posix()
            if path.parent != root / folder:
                report.graph(f"заметка во вложенной папке {folder}/: {relative}")
            if re.search(r'[/\\:#|\[\]]', path.stem):
                report.graph(f"недопустимые символы в названии заметки: {relative}")
            if path.stem in ontology.classes:
                report.bad(f"заметка объекта названа как класс: {relative}")
            if not path.resolve().is_relative_to(root):
                report.bad(f"заметка вне проекта (символическая ссылка): {relative}")
                continue
            meta, body = frontmatter(path, report)
            node_types = one_or_many(meta.get("type"))
            if not node_types:
                report.bad(f"{relative}: отсутствует непустое поле type")
            for node_type in node_types:
                if node_type not in ontology.classes:
                    report.graph(f"{relative}: тип, которого нет в онтологии: {node_type}")
                    if "Новый тип:" not in body:
                        report.tip(f"{relative}: поясните новый тип строкой «Новый тип: …»")
            graph.nodes[relative] = Node(relative, path, meta, body, prose_only(body), set(node_types))
            graph.types[relative] = set(node_types)
    count = sum(bool(n.types) for n in graph.nodes.values())
    if count < 10:
        report.bad(f"объектов с типом — {count}, нужно не меньше десяти")
    else:
        report.ok(f"объектов с типом — {count} (нужно не меньше десяти)")
    return graph


class Resolver:
    def __init__(self, graph: Graph, report: Report):
        self.graph = graph
        self.report = report
        self.cache: dict[tuple[str, str], Optional[tuple[str, str, bool]]] = {}
        self.by_name: dict[str, set[str]] = collections.defaultdict(set)
        for node in graph.nodes.values():
            for key in (node.path.stem, node.id, node.id[:-3]):
                self.by_name[name_key(key)].add(node.id)
            aliases = node.meta.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [aliases]
            if isinstance(aliases, list):
                for alias in aliases:
                    if text(alias):
                        self.by_name[name_key(alias)].add(node.id)
        self.files: dict[str, list[str]] = collections.defaultdict(list)
        for path in graph.root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(graph.root)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            self.files[path.name].append(rel.as_posix())
        for key, values in self.by_name.items():
            if "/" not in key and len(values) > 1:
                report.graph(f"неоднозначное имя/псевдоним {key!r}: {', '.join(sorted(values))}")

    def resolve(self, raw: str, source: Node) -> Optional[tuple[str, str, bool]]:
        """Возвращает (id, object/file/class, явно указан файл с расширением)."""
        key = source.id, raw
        if key not in self.cache:
            self.cache[key] = self._resolve(raw, source)
        return self.cache[key]

    def _resolve(self, raw: str, source: Node) -> Optional[tuple[str, str, bool]]:
        value = name_key(raw.split("|", 1)[0].split("#", 1)[0].strip())
        if not value:
            return source.id, "object", False
        if urlsplit(value).scheme or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
            self.report.bad(f"{source.id}: ссылка должна вести внутрь проекта: {raw}")
            return None
        parts = PurePosixPath(value).parts
        if ".." in parts:
            self.report.bad(f"{source.id}: wiki-ссылка должна содержать путь от корня: {raw}")
            return None
        candidate = (self.graph.root / value).resolve()
        if not candidate.is_relative_to(self.graph.root):
            self.report.bad(f"{source.id}: ссылка выходит за пределы проекта: {raw}")
            return None
        explicit_file = bool(PurePosixPath(value).suffix)
        class_name = value.removesuffix(".md").removeprefix("ontology/")
        class_candidate = class_name in self.graph.ontology.classes and (
            "/" not in value or value.startswith("ontology/"))
        objects = self.by_name.get(value, set())
        if class_candidate and objects:
            self.report.bad(f"{source.id}: имя {value} одновременно обозначает класс и объект")
            return None
        if class_candidate:
            return "ontology/" + class_name + ".md", "class", explicit_file
        if len(objects) > 1:
            self.report.bad(f"{source.id}: неоднозначная ссылка [[{raw}]]")
            return None
        if len(objects) == 1:
            return next(iter(objects)), "object", explicit_file
        if candidate.is_file():
            return candidate.relative_to(self.graph.root).as_posix(), "file", True
        matches = self.files.get(value, []) if "/" not in value else []
        if len(matches) == 1:
            self.report.graph(f"{source.id}: укажите полный путь от корня вместо [[{raw}]]: {matches[0]}")
            match = matches[0]
            return match, "object" if match in self.graph.nodes else "file", True
        if len(matches) > 1:
            self.report.bad(f"{source.id}: неоднозначное имя файла [[{raw}]]: {', '.join(matches)}")
        else:
            self.report.graph(f"{source.id}: ссылка не найдена [[{raw}]]")
        return None


def collect_edges(graph: Graph, report: Report) -> None:
    resolver = Resolver(graph, report)
    for node in graph.nodes.values():
        link_section = section(node.clean, "Связи")
        if not link_section:
            report.bad(f"{node.id}: нет непустого раздела «## Связи»")
        formal_targets: set[str] = set()
        for line in link_section.splitlines():
            match = LINK_LINE.match(line)
            if not match:
                if WIKI.search(line):
                    report.graph(f"{node.id}: у связи нет имени отношения: {line.strip()}")
                continue
            relation = match[1].strip()
            links = WIKI.findall(match[2])
            if relation != "тип" and relation not in graph.ontology.properties:
                report.graph(f"{node.id}: неизвестное отношение {relation!r}")
                continue
            if not links:
                report.bad(f"{node.id}: отношение {relation} не содержит wiki-ссылки")
            for raw in links:
                target = resolver.resolve(raw, node)
                if target is None:
                    continue
                target_id, kind, explicit_file = target
                formal_targets.add(target_id)
                if relation == "тип":
                    class_name = PurePosixPath(target_id).stem
                    if kind != "class" or class_name not in node.types:
                        report.bad(f"{node.id}: строка «тип» не соответствует полю type: {raw}")
                    else:
                        graph.type_edges.add((node.id, class_name))
                    continue
                if kind == "class":
                    report.bad(f"{node.id}: связь {relation} ведёт к классу {raw}; нужен экземпляр")
                    continue
                prop = graph.ontology.properties[relation]
                range_classes = one_or_many(prop["range"])
                file_target = kind == "file" or (
                    explicit_file and any(graph.ontology.is_a("Файл", c) for c in range_classes))
                if file_target:
                    if not explicit_file:
                        report.graph(f"{node.id}: для файла нужен путь с расширением: {raw}")
                    graph.file_targets.add(target_id)
                    graph.types.setdefault(target_id, set()).add("Файл")
                graph.edges.add(Edge(node.id, relation, target_id, file_target))
        for node_type in node.types & set(graph.ontology.classes):
            if (node.id, node_type) not in graph.type_edges:
                report.bad(f"{node.id}: нет строки «- тип: [[{node_type}]]»")
        body_targets: set[str] = set()
        for raw in WIKI.findall(node.clean):
            target = resolver.resolve(raw, node)
            if target is None:
                continue
            target_id, kind, _ = target
            if kind != "class" and target_id != node.id:
                body_targets.add(target_id)
        extra = body_targets - formal_targets
        if extra:
            report.tip(f"{node.id}: ссылки в тексте без связи в разделе «Связи»: {', '.join(sorted(extra))}")
        # Обычные Markdown-ссылки (включая изображения) проверяются относительно заметки.
        for raw in re.findall(r"!?\[[^\]\n]*\]\(([^)\n]+)\)", node.clean):
            value = raw.strip().split(' "', 1)[0].strip("<>")
            if not value or value.startswith("#") or urlsplit(value).scheme:
                continue
            target = (node.path.parent / unquote(value.split("#", 1)[0])).resolve()
            if not target.is_relative_to(graph.root):
                report.bad(f"{node.id}: Markdown-ссылка выходит за пределы проекта: {value}")
            elif not target.is_file():
                report.graph(f"{node.id}: файл Markdown-ссылки не найден: {value}")
    report.ok(f"типизированных связей — {len(graph.edges)}; связей «тип» с классами — {len(graph.type_edges)}")
    if not graph.file_targets:
        report.bad("нет файлов проекта, связанных с объектами отношениями онтологии")
    else:
        report.ok(f"файлов проекта, связанных с объектами, — {len(graph.file_targets)}: "
                  + ", ".join(sorted(graph.file_targets)))


def infer(graph: Graph) -> None:
    graph.facts = {(edge.source, edge.relation, edge.target) for edge in graph.edges}
    changed = True
    while changed:
        changed = False
        for relation, spec in graph.ontology.properties.items():
            chars = {CHARACTERISTICS[c] for c in spec.get("characteristics", [])}
            pairs = {(a, b) for a, p, b in graph.facts if p == relation}
            additions: set[tuple[str, str, str]] = set()
            if "symmetric" in chars:
                additions.update((b, relation, a) for a, b in pairs)
            if "transitive" in chars:
                outgoing: dict[str, set[str]] = collections.defaultdict(set)
                for a, b in pairs:
                    outgoing[a].add(b)
                additions.update((a, relation, c) for a, b in pairs for c in outgoing.get(b, set()))
            new = additions - graph.facts
            if new:
                graph.facts.update(new)
                changed = True
        for rule in graph.ontology.rules:
            check = rule.get("check")
            if not isinstance(check, dict) or check["kind"] not in DEFINED:
                continue
            kind = check["kind"]
            for node_id in list(graph.types):
                node = graph.nodes.get(node_id)
                if not graph.belongs(node_id, check["class"]):
                    continue
                satisfied = False
                if kind == "defined":
                    satisfied = any(a == node_id and p == check["property"] and graph.belongs(b, check["target"])
                                    for a, p, b in graph.facts)
                elif kind == "defined_inverse":
                    satisfied = any(b == node_id and p in check["properties"] for a, p, b in graph.facts)
                elif kind == "defined_value":
                    value = node.meta.get(check["attribute"]) if node else None
                    satisfied = any(same_value(value, v) for v in check["values"])
                if satisfied and check["name"] not in graph.types[node_id]:
                    graph.types[node_id].add(check["name"])
                    changed = True


def validate_edges(graph: Graph, report: Report) -> None:
    before = report.errors
    for edge in sorted(graph.edges, key=lambda e: (e.source, e.relation, e.target)):
        spec = graph.ontology.properties[edge.relation]
        source_types = graph.types.get(edge.source, set())
        target_types = {"Файл"} if edge.file_target else graph.types.get(edge.target, set())
        domain = one_or_many(spec["domain"])
        ranges = one_or_many(spec["range"])
        if not any(graph.ontology.accepts(source_types, c) for c in domain):
            report.bad(f"несогласованная связь: {edge.source} — {edge.relation} → {edge.target}; "
                       f"тип начала {sorted(source_types)} не входит в domain {domain}")
        if not any(graph.ontology.accepts(target_types, c) for c in ranges):
            report.bad(f"несогласованная связь: {edge.source} — {edge.relation} → {edge.target}; "
                       f"тип конца {sorted(target_types)} не входит в range {ranges}")
    for prop, spec in graph.ontology.properties.items():
        if "functional" not in {CHARACTERISTICS[c] for c in spec.get("characteristics", [])}:
            continue
        values: dict[str, set[str]] = collections.defaultdict(set)
        for a, p, b in graph.facts:
            if p == prop:
                values[a].add(b)
        for source, targets in values.items():
            if len(targets) > 1:
                report.bad(f"функциональное отношение {prop}: у {source} несколько целей: {sorted(targets)}")
    if report.errors == before:
        report.ok("типы концов всех известных связей согласованы с онтологией (локальная согласованность)")


def field_value(body: str, label: str) -> str:
    match = re.search(r"^[ \t]*" + re.escape(label) + r":[ \t]*(.*)$", body, re.M)
    return match[1].strip() if match else ""


def validate_tasks(graph: Graph, report: Report) -> None:
    task_ids = {nid for nid in graph.nodes if graph.belongs(nid, "Задача")}
    parents: dict[str, set[str]] = collections.defaultdict(set)
    for edge in graph.edges:
        if edge.relation == "подзадачаОт":
            parents[edge.source].add(edge.target)
    counts: collections.Counter = collections.Counter()
    for node in graph.nodes.values():
        for attr, spec in graph.ontology.attributes.items():
            if attr not in node.meta:
                continue
            if not any(graph.belongs(node.id, c) for c in one_or_many(spec["domain"])):
                report.bad(f"{node.id}: атрибут {attr} не применим к типу объекта")
            if not any(same_value(node.meta[attr], v) for v in spec["values"]):
                report.bad(f"{node.id}: недопустимое значение {attr}: {node.meta[attr]!r}")
        if node.id.startswith("tasks/") and node.id not in task_ids:
            report.bad(f"{node.id}: заметка в tasks/ должна иметь тип Задача или его подкласс")
        if node.id not in task_ids:
            continue
        if not node.id.startswith("tasks/"):
            report.graph(f"{node.id}: заметки задач следует хранить в tasks/")
        status = node.meta.get("status")
        if not isinstance(status, str) or status not in STATUSES:
            report.bad(f"{node.id}: необходимо поле status с допустимым статусом задачи")
        else:
            counts[status] += 1
        for label in ("Готово, когда", "Не входит"):
            value = field_value(node.clean, label)
            if not value or PLACEHOLDER.search(value):
                report.bad(f"{node.id}: заполните «{label}:»")
        if not parents[node.id] and not field_value(node.clean, "Запрос пользователя"):
            report.bad(f"{node.id}: у задачи верхнего уровня нужен «Запрос пользователя:»")
        outcome = section(node.clean, "Итог")
        if status in DONE | {"заблокирована"} and not outcome:
            report.bad(f"{node.id}: статус {status!r}, но нет непустого раздела «## Итог»")
        if status == "принята":
            acceptance = field_value(outcome, "Принято пользователем")
            if not re.search(r"\w", acceptance) or PLACEHOLDER.search(acceptance):
                report.bad(f"{node.id}: статус «принята» без записи «Принято пользователем:» с подтверждением")
        if status in DONE and outcome:
            if node.clean.find("## Итог") > node.clean.find("## Связи") >= 0:
                report.graph(f"{node.id}: раздел «Итог» должен стоять перед «Связями»")
    # Циклы не являются корректной декомпозицией задачи; проверяем итеративно.
    for origin in sorted(parents):
        pending = list(parents[origin])
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == origin:
                report.bad(f"цикл подзадачаОт с участием {origin}")
                break
            if current not in seen:
                seen.add(current)
                pending.extend(parents.get(current, set()))
    if not task_ids:
        report.bad("нет заметок задач с типом Задача")
    else:
        report.ok("Задача по полю «status»: " + "; ".join(f"{s} — {counts[s]}" for s in sorted(counts)))


def validate_rules(graph: Graph, report: Report) -> None:
    for rule in graph.ontology.rules:
        check = rule.get("check")
        if not isinstance(check, dict):
            continue
        rid, kind = rule["id"], check["kind"]
        if kind in DEFINED:
            selected = sorted(nid for nid in graph.types if check["name"] in graph.types[nid])
            report.ok(f"вывод по {rid}: {check['name']} = " + "{" + ", ".join(selected) + "}")
            continue
        before = report.rule_issues
        if kind == "disjoint_properties":
            pairs: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
            for a, p, b in graph.facts:
                if p in check["properties"]:
                    pairs[(a, b)].add(p)
            for (a, b), props in sorted(pairs.items()):
                if len(props) > 1:
                    report.rule(f"{rid}: {a} и {b} соединены несовместимыми отношениями {sorted(props)}")
        else:
            selected = sorted(nid for nid in graph.types if graph.belongs(nid, check["class"]))
            for nid in selected:
                satisfied = True
                if kind == "some":
                    satisfied = any(a == nid and p == check["property"] and graph.belongs(b, check["target"])
                                    for a, p, b in graph.facts)
                elif kind == "some_inverse":
                    satisfied = any(b == nid and p == check["property"] and graph.belongs(a, check["source"])
                                    for a, p, b in graph.facts)
                elif kind == "some_inverse_any":
                    satisfied = any(b == nid and p in check["properties"] for a, p, b in graph.facts)
                elif kind == "all_inverse":
                    satisfied = all(graph.belongs(a, check["source"]) for a, p, b in graph.facts
                                    if b == nid and p == check["property"])
                elif kind == "section":
                    node = graph.nodes.get(nid)
                    satisfied = bool(node and section(node.clean, check["section"]))
                if not satisfied:
                    report.rule(f"{rid}: {nid} — {rule['text']}")
            if not selected:
                report.tip(f"{rid}: объектов класса {check['class']} нет — правило выполнено вакуумно")
        if report.rule_issues == before:
            report.ok(f"правило {rid} выполнено на записанном графе")


def export_mermaid(graph: Graph, destination: Path, report: Report) -> None:
    destination = destination.resolve()
    inputs = {n.path.resolve() for n in graph.nodes.values()}
    inputs.update((graph.root / p).resolve() for p in graph.class_files | graph.file_targets)
    inputs.add((graph.root / "ontology.yaml").resolve())
    if destination in inputs or destination == Path(__file__).resolve():
        report.bad("--mermaid не может перезаписывать входной файл")
        return
    if destination.exists():
        try:
            prefix = "# Граф проекта\n\n" + chr(96) * 3 + "mermaid\nflowchart LR\n"
            if not destination.is_file() or not destination.read_text(encoding="utf-8").startswith(prefix):
                report.bad("--mermaid: существующий файл не является ранее созданной схемой; выберите другой путь")
                return
        except (OSError, UnicodeError) as exc:
            report.bad(f"--mermaid: нельзя перезаписать существующий путь: {exc}")
            return
    labels = {n.id: n.path.stem for n in graph.nodes.values()}
    for name in graph.ontology.classes:
        labels["ontology/" + name + ".md"] = name
    labels.update((p, p) for p in graph.file_targets)
    ids = {key: f"n{i}" for i, key in enumerate(sorted(labels))}

    def safe(value: str) -> str:
        return html.escape(re.sub(r"[\x00-\x1f]", " ", value), quote=True).replace("|", "&#124;")

    lines = ["# Граф проекта", "", chr(96) * 3 + "mermaid", "flowchart LR"]
    for key in sorted(labels):
        lines.append(f'  {ids[key]}["{safe(labels[key])}"]')
    for name, spec in graph.ontology.classes.items():
        if spec["parent"]:
            a, b = "ontology/" + name + ".md", "ontology/" + spec["parent"] + ".md"
            lines.append(f'  {ids[a]} -->|"подклассОт"| {ids[b]}')
    for nid, cls in sorted(graph.type_edges):
        lines.append(f'  {ids[nid]} -.->|"тип"| {ids["ontology/" + cls + ".md"]}')
    for edge in sorted(graph.edges, key=lambda e: (e.source, e.relation, e.target)):
        if edge.source in ids and edge.target in ids:
            lines.append(f'  {ids[edge.source]} -->|"{safe(edge.relation)}"| {ids[edge.target]}')
    lines.extend([chr(96) * 3, "",
                  "Схема показывает записанные связи; выводимые связи и классы в неё не добавляются.", ""])
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(lines), encoding="utf-8")
        report.ok(f"схема Mermaid сохранена: {destination}")
    except OSError as exc:
        report.bad(f"не удалось сохранить Mermaid: {exc}")


def check_project(root: Path, strict: bool = False, mermaid: Optional[str] = None) -> Report:
    root = root.resolve()
    report = Report(strict)
    if not root.is_dir():
        report.bad(f"папка проекта не найдена: {root}")
        return report
    ontology = load_ontology(root / "ontology.yaml", report)
    if ontology is None:
        return report
    print(f"\n== Граф проекта: {root}")
    graph = load_graph(root, ontology, report)
    collect_edges(graph, report)
    infer(graph)
    validate_edges(graph, report)
    validate_tasks(graph, report)
    validate_rules(graph, report)
    if mermaid:
        output = Path(mermaid)
        export_mermaid(graph, output if output.is_absolute() else root / output, report)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project", nargs="?", default=".", help="корень проекта (по умолчанию текущая папка)")
    parser.add_argument("--strict", action="store_true", help="считать [ГРАФ] и [ПРАВИЛО] ошибками")
    parser.add_argument("--mermaid", metavar="ФАЙЛ", help="экспортировать схему, путь относительно проекта")
    args = parser.parse_args(argv)
    if yaml is None:
        print("[НЕТ] Нужен пакет PyYAML: python3 -m pip install pyyaml")
        return 2
    try:
        report = check_project(Path(args.project), args.strict, args.mermaid)
    except (OSError, UnicodeError, RecursionError) as exc:
        print(f"[НЕТ] Не удалось завершить проверку: {exc}")
        return 1
    print()
    if report.errors:
        print(f"Не выполнены обязательные пункты: {report.errors}.")
        return 1
    print("Все обязательные пункты выполнены.")
    if report.graph_issues or report.rule_issues:
        print("Есть замечания [ГРАФ]/[ПРАВИЛО]; для полной проверки запустите --strict.")
    print("Научную достоверность, приёмку человеком и коммиты проверяет пользователь.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
