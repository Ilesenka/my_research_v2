"""Independent, offline EXFOR/CSV audit; Python standard library only.

Run: python -B _support/scripts/verify_exfor.py --append-log
     python -B _support/scripts/verify_exfor.py --self-test

Does not import the collector, modify CSVs, fetch Git objects or adjudicate
scientific ambiguities. Default output is stdout; --append-log appends to the
existing project log. Exit 0: no detected technical errors; 1: errors; 2: audit
could not complete (or --strict and remaining warnings). Unsupported source
syntax is reported explicitly and prevents a claim of complete selection.
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import unittest
from unittest import mock


class SourceError(ValueError):
    pass


def decimal(value):
    value = str(value).strip().upper().replace('D', 'E')
    if not value:
        return None
    if 'E' not in value:
        value = re.sub(r'(?<=\d)([+-]\d+)$', r'E\1', value)
    try:
        result = Decimal(value)
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def close(a, b):
    if a is None or b is None:
        return False
    return abs(a - b) <= max(Decimal('1e-14'), abs(b) * Decimal('1e-10'))


def canonical(data):
    return data.replace(b'\r\n', b'\n')


def blob_hash(data, digest_length=40):
    data = canonical(data)
    algorithm = hashlib.sha256 if digest_length == 64 else hashlib.sha1
    return algorithm(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


@dataclass
class Report:
    limit: int = 5
    counts: collections.Counter = field(default_factory=collections.Counter)
    examples: dict = field(default_factory=lambda: collections.defaultdict(list))
    metrics: dict = field(default_factory=dict)

    def add(self, level, code, message):
        key = (level, code)
        self.counts[key] += 1
        if len(self.examples[key]) < self.limit:
            self.examples[key].append(str(message))

    def count(self, level):
        return sum(n for (kind, _), n in self.counts.items() if kind == level)

    def render(self):
        lines = ['INDEPENDENT EXFOR AUDIT ' + datetime.now(timezone.utc).isoformat(),
                 'Offline; CSVs unchanged; no collector code imported.',
                 'METRICS ' + json.dumps(self.metrics, ensure_ascii=False, sort_keys=True)]
        for level in ('ERROR', 'WARNING', 'INFO'):
            for (kind, code), count in sorted(self.counts.items()):
                if kind == level:
                    lines.append(f'{level} {code}: {count}')
                    lines.extend('  ' + x for x in self.examples[(kind, code)])
                    if count > len(self.examples[(kind, code)]):
                        lines.append('  Further occurrences omitted from display; count includes all.')
        lines.append(f'TECHNICAL RESULT: {self.count("ERROR")} errors; '
                     f'{self.count("WARNING")} warning occurrences.')
        lines.append('LIMIT: source-cell consistency does not prove scientific validity. '
                     'Selection coverage applies only to supported explicit IR syntax in '
                     'the checked snapshot; article/literature completeness is not tested. '
                     'Energy evidence text/locators are checked for presence, not truth.')
        return '\n'.join(lines) + '\n'


@dataclass
class Block:
    headings: list
    units: list
    rows: list


@dataclass
class Subentry:
    sid: str
    bib: dict = field(default_factory=lambda: collections.defaultdict(list))
    common: Block | None = None
    data: Block | None = None
    empty_stub: bool = False

    def text(self, keyword, pointer=''):
        keys = [(keyword, '')] + ([(keyword, pointer)] if pointer else [])
        return '\n'.join(line for key in keys for line in self.bib.get(key, []))


def parse_block(lines, start):
    """Read logical 11-character fields, including tables wider than 6 columns."""
    marker = lines[start][:10].strip()
    try:
        columns = int(lines[start][11:22])
        row_count = int(lines[start][22:33]) if marker == 'DATA' else 1
    except ValueError as exc:
        raise SourceError(f'Invalid {marker} size at line {start + 1}') from exc
    if columns < 1 or row_count < 0:
        raise SourceError('Invalid table dimensions')
    stride = math.ceil(columns / 6)
    stop = start + 1 + stride * (2 + row_count)
    if stop >= len(lines) or lines[stop][:10].strip() != 'END' + marker:
        raise SourceError(f'Incomplete {marker} at line {start + 1}')

    def fields(offset):
        result = []
        for line in lines[offset:offset + stride]:
            result.extend(line[:66].ljust(66)[i:i + 11] for i in range(0, 66, 11))
        return result[:columns]

    names = [(x[:10].strip(), x[10:11].strip()) for x in fields(start + 1)]
    units = [x.strip() for x in fields(start + 1 + stride)]
    rows = [[x.strip() for x in fields(start + 1 + (2 + r) * stride)]
            for r in range(row_count)]
    return Block(names, units, rows), stop


def parse_entry(text):
    lines = text.splitlines()
    if not lines or not lines[0].startswith('ENTRY ') or not any(
            line.startswith('ENDENTRY') for line in lines):
        raise SourceError('Missing ENTRY/ENDENTRY')
    result, current, in_bib, keyword, pointer = [], None, False, '', ''
    i = 0
    while i < len(lines):
        line = lines[i]
        marker = line[:10].strip()
        if marker == 'SUBENT':
            if current is not None:
                if current.bib or current.common or current.data:
                    raise SourceError('Nested or unclosed nonempty SUBENT')
                # Some master snapshots represent a removed subentry by a
                # bare SUBENT line. Preserve and report the stub, then continue
                # checking the following real data instead of dropping ENTRY.
                current.empty_stub = True
                result.append(current)
            current = Subentry(line[11:22].strip())
        elif marker == 'ENDENTRY' and current is not None:
            if current.bib or current.common or current.data:
                raise SourceError('Unclosed nonempty SUBENT at ENDENTRY')
            current.empty_stub = True
            result.append(current)
            current = None
        elif marker == 'ENDSUBENT':
            if current is None or in_bib:
                raise SourceError('Unexpected ENDSUBENT')
            result.append(current)
            current = None
        elif current is not None:
            if marker == 'BIB':
                in_bib = True
                keyword, pointer = '', ''
            elif marker == 'ENDBIB':
                in_bib = False
            elif in_bib:
                new_key, new_pointer = line[:10].strip(), line[10:11].strip()
                if new_key:
                    keyword, pointer = new_key, new_pointer
                elif new_pointer:
                    pointer = new_pointer
                if not keyword:
                    raise SourceError('BIB continuation without keyword')
                current.bib[(keyword, pointer)].append(line[11:66].rstrip())
            elif marker in ('DATA', 'COMMON'):
                block, i = parse_block(lines, i)
                if getattr(current, marker.lower()) is not None:
                    raise SourceError('Repeated table in a subentry')
                setattr(current, marker.lower(), block)
        i += 1
    if current is not None or not result:
        raise SourceError('Unclosed or absent SUBENT')
    if len({s.sid for s in result}) != len(result):
        raise SourceError('Duplicate SUBENT')
    return result


def matching_close(expression, start):
    depth = 0
    for i in range(start, len(expression)):
        depth += (expression[i] == '(') - (expression[i] == ')')
        if depth == 0:
            return i
    raise SourceError('Unbalanced reaction parentheses')


def reaction_atoms(expression):
    """Locate atomic reactions by balanced parentheses, not the collector regex."""
    expression = expression.strip()
    if not expression.startswith('('):
        raise SourceError('Reaction does not start with a coded expression')
    # REACTION may end in explanatory prose containing more strings '(g,n)'.
    # Only the balanced initial coded expression defines this dataset.
    coded_end = matching_close(expression, 0)
    compact = re.sub(r'\s+', '', expression[:coded_end + 1]).upper()
    atoms = []
    for match in re.finditer(r'\(G,', compact):
        projectile = match.start()
        opening = compact.rfind('(', 0, projectile)
        if opening < 0:
            raise SourceError('Atomic reaction lacks opening parenthesis')
        channel_end = matching_close(compact, projectile)
        end = matching_close(compact, opening)
        target = compact[opening + 1:projectile]
        tail = compact[channel_end + 1:end]
        product, comma, codes = tail.partition(',')
        if not comma or not target:
            raise SourceError('Cannot separate target/product/quantity')
        m = re.fullmatch(r'(\d+)-([A-Z]+)-(\d+)(?:-(.+))?', product)
        atoms.append(dict(start=opening, end=end + 1, target=target,
                          channel=compact[projectile + 3:channel_end],
                          product=product, codes=codes,
                          Z=int(m[1]) if m else None,
                          A=int(m[3]) if m else None,
                          nuclide='-'.join(m.group(1, 2, 3)) if m else None,
                          states=m[4] if m else None))
    return compact, atoms


def classify_ir(expression):
    compact, atoms = reaction_atoms(expression)
    if not atoms:
        return None, None
    if len(atoms) == 1:
        atom = atoms[0]
        has_rat = bool(re.search(r'(^|[,/])RAT($|[,/])', atom['codes']))
        states = atom['states']
        if has_rat and states and '/' in states:
            numerator, denominator = states.split('/', 1)
            return dict(atom, numerator=numerator, denominator=denominator), None
        if has_rat:
            return None, 'RAT without independently resolved product-state ratio'
        return None, None
    # Replace whole atomic expressions: operators inside quantity/state codes
    # cannot be mistaken for the division between reactions.
    reduced = compact
    for i, atom in reversed(list(enumerate(atoms))):
        reduced = reduced[:atom['start']] + f'@{i}' + reduced[atom['end']:]
    if '/' not in reduced:
        if any('RAT' in a['codes'] for a in atoms):
            return None, 'Compound RAT expression requires manual classification'
        return None, None
    identities = {(a['target'], a['channel'], a['nuclide']) for a in atoms}
    if len(identities) != 1 or atoms[0]['nuclide'] is None:
        if any(re.search(r'(^|[,/])RAT($|[,/])', a['codes']) for a in atoms):
            return None, 'Compound RAT with different or unresolved products/channels'
        # Cross-reaction/reference-yield ratios without a state ratio are not
        # isomeric ratios. Variable product/state RATs remain unresolved above.
        return None, None
    if len({a['states'] or 'T' for a in atoms}) == 1 and not any('RAT' in a['codes'] for a in atoms):
        # Identical product states (e.g. an angular/energy ratio with //) do
        # not define an isomeric ratio, however complex the outer expression.
        return None, None
    if len(atoms) == 2 and re.fullmatch(r'\(*@0\)*/\(*@1\)*', reduced):
        states = [a['states'] or 'T' for a in atoms]
        if states[0] != states[1]:
            return dict(atoms[0], numerator=states[0], denominator=states[1]), None
        return None, None
    return None, 'Multi-atom or non-simple quotient needs manual algebra verification'


def applicable_fields(base, sub, pointer, row):
    """Keep field candidates with explicit scope priority and pointer specificity."""
    values = collections.defaultdict(list)
    blocks = [(0, base.common), (1, sub.common), (2, sub.data)]
    for level, block in blocks:
        if block is None:
            continue
        data = block.rows[row if level == 2 else 0]
        for column, ((heading, p), unit, value) in enumerate(
                zip(block.headings, block.units, data), 1):
            if p in ('', pointer) and value:
                values[heading].append(((bool(p), level), value, unit, column))
    return {h: max(v, key=lambda x: x[0])[1:] for h, v in values.items()}


def read_csv(path, expected, id_column, report):
    if not path.is_file():
        report.add('ERROR', 'CSV_MISSING', path.name)
        return []
    return read_csv_bytes(path, path.read_bytes(), expected, id_column, report)


def read_csv_bytes(path, data, expected, id_column, report):
    """The same CSV checks can run entirely in memory for self-tests."""
    if not data.startswith(b'\xef\xbb\xbf'):
        report.add('ERROR', 'CSV_BOM', path.name)
    try:
        text = data.decode('utf-8-sig')
        records = list(csv.reader(io.StringIO(text, newline=''), strict=True))
    except (UnicodeError, csv.Error) as exc:
        report.add('ERROR', 'CSV_PARSE', f'{path.name}: {exc}')
        return []
    if not records or records[0] != expected:
        report.add('ERROR', 'CSV_SCHEMA', path.name)
    if not records:
        return []
    header = records[0]
    report.metrics[path.name] = dict(records=len(records) - 1, columns=len(header),
                                     physical_lines=len(text.splitlines()))
    rows = []
    for number, cells in enumerate(records[1:], 2):
        if len(cells) != len(header):
            report.add('ERROR', 'CSV_WIDTH', f'{path.name} logical row {number}')
            continue
        rows.append(dict(zip(header, cells)))
    ids = [r.get(id_column, '') for r in rows]
    if '' in ids or len(set(ids)) != len(ids):
        report.add('ERROR', 'CSV_IDS', path.name)
    return rows


def snapshot(root, repo, regions, wanted_commit, report):
    """Read local Git metadata with lazy fetching disabled; never contact network."""
    expected = {}
    commit = ''
    git = shutil.which('git')
    if git and (repo / '.git').exists():
        environment = dict(os.environ, GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0')
        try:
            def query(*args):
                return subprocess.check_output([git, '-C', str(repo), *args],
                                               env=environment, text=True,
                                               stderr=subprocess.PIPE, timeout=30).strip()
            commit = query('rev-parse', 'HEAD')
            for line in query('ls-tree', '-r', 'HEAD').splitlines():
                meta, path = line.split('\t', 1)
                if meta.split()[1] == 'blob':
                    expected[path] = meta.split()[2]
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            report.add('WARNING', 'GIT_INVENTORY_UNAVAILABLE', str(exc))
    if not expected:
        tree_path = root / '_support/raw/exfor_master_tree.json'
        commit_path = root / '_support/raw/exfor_master_commit.json'
        tree = json.loads(tree_path.read_text(encoding='utf-8-sig'))
        metadata = json.loads(commit_path.read_text(encoding='utf-8-sig'))
        if tree.get('truncated'):
            raise SourceError('Cached Git tree is truncated')
        if tree.get('sha') != metadata.get('commit', {}).get('tree', {}).get('sha'):
            raise SourceError('Cached tree is not tied to the cached commit')
        expected = {x['path']: x['sha'] for x in tree['tree'] if x['type'] == 'blob'}
        commit = metadata['sha']
        report.add('WARNING', 'CACHED_INVENTORY', 'Used cached commit/tree, not Git HEAD')
    if wanted_commit and commit != wanted_commit:
        report.add('ERROR', 'SNAPSHOT_COMMIT', f'Expected {wanted_commit}, got {commit}')
    if not wanted_commit:
        cached = root / '_support/raw/exfor_master_commit.json'
        if cached.exists():
            pin = json.loads(cached.read_text(encoding='utf-8-sig')).get('sha')
            if pin and pin != commit:
                report.add('ERROR', 'SNAPSHOT_PIN', f'Cached pin {pin}, HEAD {commit}')
    pattern = re.compile(r'exforall/[' + re.escape(regions) + r'][^/]*/[^/]+\.x4$')
    expected = {p: h for p, h in expected.items() if pattern.fullmatch(p)}
    if not expected:
        raise SourceError('No ENTRY files found in the requested snapshot regions')
    found = {}
    for relative, digest in sorted(expected.items()):
        path = repo / relative
        if not path.is_file():
            report.add('ERROR', 'SNAPSHOT_MISSING', relative)
            continue
        data = path.read_bytes()
        if blob_hash(data, len(digest)) != digest:
            report.add('ERROR', 'SNAPSHOT_HASH', relative)
        found[path.stem] = path
    report.metrics['snapshot'] = dict(commit=commit, regions=regions,
                                      expected_entries=len(expected), present_entries=len(found))
    return commit, found


def source_key(row):
    position = re.search(r'column=(\d+)', row.get('source_data_column', ''))
    if not position:
        raise SourceError('source_data_column lacks column=')
    return (row['exfor_subentry'], row['exfor_pointer'],
            int(row['source_data_row']), int(position[1]))


def within_project(root, raw_file):
    if not raw_file:
        raise SourceError('raw_file is empty')
    path = (root / raw_file).resolve()
    if not path.is_relative_to(root.resolve()):
        raise SourceError('raw_file escapes project root')
    return path


def filter_value(value, low, high):
    return 'unknown' if value is None else 'pass' if low <= value <= high else 'fail'


# Explicit primary-paper decisions, separately reviewed against full PDFs.
# The audit still compares every raw scalar/reaction/state code to EXFOR.
# These hashes restrict physical-basis overrides to the actual reviewed sources;
# neither a free-text note nor an arbitrary article link can bypass validation.
PRIMARY_PAPERS = {
    'M0670': ('zheltonozhsky2004', 'af75f01b5955d067849a75e88246eb1ae3facc20a2dfadbc7a6317537863b28d'),
    'M0743': ('palvanov2007', 'ccd491b2058c5143cf5b3b276d5b59b09641455cad12764e80e3a8d1214dfad3'),
    'M0812': ('palvanov2011', '0dc5ec6dd6b4eff590e4ffe78a9f9060d43bf82dbe10e09e4b7524a27da84df0'),
    'M0848': ('thiep2011', '4632920b0750256ae3ff8cfdb6b687fea06ed894a0ca960d3420e150d4c186a3'),
    'M0909': ('thiep2014', '350fc57bc5253927495f2d83ad22d525fd69565d1b060b03a0589af16bf087db'),
    'M0979': ('thiep2019', '347562e241b43fa90e05139b99cc970348e5c5cf1d1c40fa7b642c868ef5316d'),
    'M0986': ('thiep2019', '347562e241b43fa90e05139b99cc970348e5c5cf1d1c40fa7b642c868ef5316d'),
    'G4117': ('derechkey2025', 'cf50f58103fb4fa564875af02f46047c02dc88432a0756886ceac80c0446fe98'),
    'K2027': ('sarkar1991', '0525aac5936a2dc12d7f7a8d4d2f047002d61f4f3e43e6541dbb834ea4d2c045'),
    'M0061': ('dzhilavyan1981', 'ec9ad4d6da32cd9a2ac78a519a02b13ce3b3697a8a24679cf4ddd3d32702c79f'),
    'G3118': ('yang2014', 'c72e3e5a00072b44105c7a71571fb124d3d02085088299a36d76cc496f6b3528'),
    'M0679': ('gangrsky2004', '379d42038024d06c8be42b241c70de4d9a952a9679dcc4a01ab8dc04a2922a18'),
    'G3101': ('rahman2009', '4fd7266b07e729d1b75dd6a030e7bf7f10f2fb7ee2f91d1e4dfae3a8aad4c3d2'),
    'G3120': ('yang2014 (1)', 'e9262b4ad97226b7981858bd47535cd839c5c8ef20f1e13829fa9767525be8e4'),
    'M0766': ('palvanov1999', '72a7fc237a2421198abdc3cafabd9ecaa20f978dcc71b7f5c24a809a0c59c714'),
    'G3122': ('naik2016', '0d2840a5f1c27c2087229ef786055753a584781a57d886ece113dad2d9772afb'),
    'G3123': ('yang2014 (2)', 'e6a1aebb10c17398ab42e78a23a3458dffda166c30ff375be2026ad1fadd989f'),
    'G4086': ('vishnevsky2016', 'd6656d7cbdc3b61838f2371159a08d217a07034a5bf42454e3a58543893994f4'),
    'G4087': ('zheltonozhsky2019', 'd2bf1e827a9ad35042560d15247b16e112261335baabb84d7ba39cf5102a33eb'),
    'M0629': ('belov2001', 'a315f875fb0854c1a58ee59df9cf06a92a849dd3ab38998733872c04c25e2ced'),
    'L0145': ('vishnevsky2009', 'fab0c266a69d85b8ab519b1858e9d22d4e3ebf52a3be16a12e331a8fc1313754'),
    'M0786': ('tonchev1998', '1c1136dcec2310db7cbd368540b8c95c0cbc9e06b853fe376716b243eb0a4a02'),
    'G4094': ('vodin2021', 'ddc1a8076e282f2c91d2522c5cfd05266b765c3414a50e2a1f8e560959ba5669'),
    'M0671': ('demekhina2002', '2e3b10bf118e1f5babda07d33947032747e1457d310ce427eddfc88862f58bb5'),
    'M0794': ('kolev1995', '74848b0658b48170c3a25c52b7e536d83590f1c22c7b5fd836aeff7f95233d82'),
    'M0936': ('karamian1996', 'aa5760b37f054f5b17d37cf30bb07f064962f59f02c956af61780cc29b3af601'),
    'G4027': ('vishnevsky2008', 'efe2d404a460fe0db4584da1335118de3c674458994ec9a6b8def93d5fe358b9'),
}

PRIMARY_MARKERS = ('PRIMARY_REVIEW_2026-11-07_R3', 'PRIMARY_REVIEW_2026-11-07_R6',
                   'PRIMARY_REVIEW_2026-11-07_R8', 'PRIMARY_REVIEW_2026-11-07_R9',
                   'PRIMARY_REVIEW_2026-11-07_R10')
PRIMARY_LOCAL_PAPERS = {
    'M0763': ('_support/raw/thiep2009_M0763.pdf', '5948c712435ba8720dd1e1e360675c02c14e4fe49b3b6a01e802ca837e4d0099'),
    'M0897': ('_support/raw/thiep2007_M0897.pdf', '3f1554525e349b111ff240b645be5c24701c16ab5c10ba8c38406b1a1b0cf69d'),
    'M1039': ('_support/raw/article_M1039_primary_web.txt', 'e6e98db0b65d7ed7b628cf5c8cece48aa2c2dab828ede358dd6bd3432e4cfb10'),
}
PRIMARY_WEB_PAPERS = {
    'G4017': ('_support/raw/article_G4017_primary_web.txt', '00b1d2f1fa9972915a78a3e9f2df52c830dc4c3ed6f86e72091599b22722fcfb'),
    'M0896': ('_support/raw/article_M0896_primary_web.txt', '6d2874f1b76494a2312ccd95201677b2c9c5e9ecb0428723505dcddf3f2d765e'),
}


def primary_paper_verified(row, root):
    local_spec = PRIMARY_LOCAL_PAPERS.get(row.get('exfor_entry'))
    if local_spec and 'PRIMARY_REVIEW_2026-11-07_R9' in row.get('notes', ''):
        name, digest = local_spec
        path = root / name
        return (name in row.get('source_locator', '') and
                'PRIMARY_PAPER_RESOLUTION:' in row.get('ir_definition_raw', '') and
                path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest)
    web_spec = PRIMARY_WEB_PAPERS.get(row.get('exfor_entry'))
    if web_spec and 'PRIMARY_REVIEW_2026-11-07_R7' in row.get('notes', ''):
        name, digest = web_spec
        path = root / name
        return (name in row.get('source_locator', '') and
                'PRIMARY_PAPER_RESOLUTION:' in row.get('ir_definition_raw', '') and
                path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest)
    spec = PRIMARY_PAPERS.get(row.get('exfor_entry'))
    if not spec or not any(marker in row.get('notes', '') for marker in PRIMARY_MARKERS):
        return False
    name, digest = spec
    path = root / 'Статьи от пользователя' / (name + '.pdf')
    return (name + '.pdf' in row.get('source_locator', '') and
            'PRIMARY_PAPER_RESOLUTION:' in row.get('ir_definition_raw', '') and
            path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest)


def documented_scalar(row, original, normalized, verified):
    # One literal decimal-copy error, not a general permission to alter numbers.
    if (verified and row.get('row_id') == 'M0670/M0670004/-/2/DATA@2'
            and original == '.0014' and 'PRIMARY_SCALAR_OVERRIDE_PT10' in row.get('notes', '')):
        return Decimal('.014')
    return normalized


def nominal_endpoint_matches(row, fields, bremsstrahlung, paper_verified):
    """Independent source-cell check for the user-authorized nominal convention."""
    if not bremsstrahlung or 'EN-MIN' in fields:
        return False
    key = 'EN-MAX'
    if row.get('exfor_entry') == 'M0670' and paper_verified:
        key = 'EN'  # Full primary Fig.3/Table2 establishes this yield abscissa.
    if key not in fields:
        return False
    value, unit, _ = fields[key]
    factor = {'MEV': Decimal(1), 'KEV': Decimal('.001'),
              'EV': Decimal('.000001'), 'GEV': Decimal(1000)}.get(unit)
    source = decimal(value)
    return (factor is not None and source is not None and
            close(decimal(row.get('electron_energy_max_MeV', '')), source * factor) and
            close(decimal(row.get('photon_energy_max_MeV', '')), source * factor))


def check_coverage(expected, actual, report):
    for key in sorted(expected - actual.keys()):
        report.add('ERROR', 'EXPECTED_POINT_MISSING', '/'.join(map(str, key)))
    for key in sorted(actual.keys() - expected):
        report.add('WARNING', 'POINT_OUTSIDE_SUPPORTED_SCAN', actual[key])


def check_point(row, sub, base, descriptor, root, commit, repo_files, args, report):
    rid = row['row_id']
    paper_verified = primary_paper_verified(row, root)
    if 'PRIMARY_REVIEW_2026-11-07_R9' in row.get('notes', '') and row.get('exfor_entry') in PRIMARY_LOCAL_PAPERS:
        if not paper_verified:
            report.add('ERROR', 'PRIMARY_LOCAL_EVIDENCE', rid)
    if 'PRIMARY_REVIEW_2026-11-07_R7' in row.get('notes', '') and row.get('exfor_entry') in PRIMARY_WEB_PAPERS:
        if not paper_verified:
            report.add('ERROR', 'PRIMARY_WEB_EVIDENCE', rid)
    if any(marker in row.get('notes', '') for marker in PRIMARY_MARKERS) and row.get('exfor_entry') in PRIMARY_PAPERS:
        if not paper_verified:
            report.add('ERROR', 'PRIMARY_PAPER_EVIDENCE', rid)
    try:
        sid, pointer, ri, ci = source_key(row)
        block = sub.data
        if block is None or not (1 <= ri <= len(block.rows)) or not (1 <= ci <= len(block.headings)):
            raise SourceError('DATA position outside source table')
        heading, field_pointer = block.headings[ci - 1]
        original = block.rows[ri - 1][ci - 1]
        unit = block.units[ci - 1]
        if not original or field_pointer not in ('', pointer):
            raise SourceError('Empty or mismatched source DATA/pointer')
        head_label = row['source_data_column'].split(';', 1)[0].strip()
        if head_label != heading:
            report.add('ERROR', 'SOURCE_HEADING', rid)
        tokens = row['ir_value_raw'].split()
        if tokens and tokens[0] in ('<', '<=', '>', '>=', '~', '≈'):
            tokens = tokens[1:]
        if not tokens or tokens[0] != original or ' '.join(tokens[1:]) != unit:
            report.add('ERROR', 'SOURCE_SCALAR', f'{rid}: expected {original} {unit}')
        expected_id = f'{row["exfor_entry"]}/{sid}/{pointer or "-"}/{ri}/{heading}@{ci}'
        if rid != expected_id:
            report.add('ERROR', 'ROW_ID_SOURCE', rid)
        raw_path = within_project(root, row['raw_file'])
        snapshot_path = repo_files.get(row['exfor_entry'])
        if not raw_path.is_file():
            raise SourceError('Missing saved raw ENTRY')
        if snapshot_path and canonical(raw_path.read_bytes()) != canonical(snapshot_path.read_bytes()):
            report.add('ERROR', 'RAW_COPY_HASH', rid)
        hash_in_url = re.search(r'/exfor_master/([0-9a-f]{40,64})/', row['source_url'])
        if hash_in_url and hash_in_url[1] != commit:
            report.add('ERROR', 'SOURCE_URL_COMMIT', rid)
        if descriptor:
            if row['product_nuclide'] != descriptor['nuclide']:
                report.add('ERROR', 'PRODUCT_SOURCE', rid)
            if descriptor['Z'] is not None and decimal(row['product_Z']) != descriptor['Z']:
                report.add('ERROR', 'PRODUCT_Z_SOURCE', rid)
            if descriptor['A'] is not None and decimal(row['product_A']) != descriptor['A']:
                report.add('ERROR', 'PRODUCT_A_SOURCE', rid)
            if row['target_nuclide'] != descriptor['target']:
                report.add('ERROR', 'TARGET_SOURCE', rid)
            if (row['ir_numerator_state'], row['ir_denominator_state']) != (
                    descriptor['numerator'], descriptor['denominator']):
                report.add('ERROR', 'STATE_SOURCE', rid)
            codes = descriptor['codes']
            if '/' in (descriptor['states'] or '') and re.sub(r'\s+', '', row['quantity_code_raw']) != codes:
                report.add('ERROR', 'QUANTITY_SOURCE', rid)
            if 'SIG/RAT' in codes and row['ir_basis'] not in ('cross_section', 'mixed') and not (
                    paper_verified and row['ir_basis'] == 'yield' and row['yield_definition_raw']):
                report.add('ERROR', 'BASIS_SOURCE', rid)
            if 'FY/RAT' in codes and row['ir_basis'] not in ('yield', 'mixed'):
                report.add('ERROR', 'BASIS_SOURCE', rid)
        if not sub.sid.startswith(row['exfor_entry']):
            report.add('ERROR', 'ENTRY_SUBENTRY_MISMATCH', rid)
        expected_limit = {'DATA': 'none', 'DATA-MAX': '<=', 'DATA-MIN': '>=',
                          'DATA-APRX': 'approx'}.get(heading)
        if expected_limit and row['limit_qualifier'] != expected_limit:
            report.add('ERROR', 'LIMIT_SOURCE', rid)
        numeric = decimal(row['ir_value_numeric'])
        bound = decimal(row['ir_bound_numeric'])
        limit = row['limit_qualifier'] in ('<', '<=', '>', '>=')
        raw_number = decimal(original)
        normalized = (raw_number * (Decimal('.01') if unit == 'PER-CENT' else 1)
                      if raw_number is not None and unit in ('NO-DIM', 'PER-CENT') else None)
        preferred = documented_scalar(row, original, normalized, paper_verified)
        if limit and (row['ir_value_numeric'] or bound is None):
            report.add('ERROR', 'BOUND_AS_ESTIMATE', rid)
        if not limit and row['ir_bound_numeric']:
            report.add('ERROR', 'UNEXPECTED_BOUND', rid)
        target = bound if limit else numeric
        if target is not None and not close(target, preferred):
            report.add('ERROR', 'NUMERIC_SOURCE', rid)
        if preferred != normalized:
            report.add('INFO', 'DOCUMENTED_PRIMARY_SCALAR', rid + ': raw EXFOR .0014 retained; primary Table2 .014 preferred')
        if not limit and numeric is None:
            if row['review_status'] != 'needs_review' or not row['review_note']:
                report.add('ERROR', 'WITHHELD_WITHOUT_REVIEW', rid)
            report.add('WARNING', 'WITHHELD_NUMERIC', rid)
        fields = applicable_fields(base, sub, pointer, ri - 1)
        for column in ('ir_uncertainty_minus', 'ir_uncertainty_plus'):
            if row[column]:
                value = decimal(row[column])
                if value is None or value < 0:
                    report.add('ERROR', 'UNCERTAINTY_NUMBER', rid)
                source_errors = [decimal(v) for h, (v, u, c) in fields.items()
                                 if 'ERR' in h and not h.startswith('EN')]
                if value is not None and not any(close(value, x) for x in source_errors):
                    report.add('WARNING', 'UNCERTAINTY_NOT_DIRECT_SOURCE', rid)
        expected_energies = [(h, v, u) for h, (v, u, c) in fields.items()
                             if h.startswith('EN') or h in ('KT', 'KT-MAX')]
        for h, v, u in expected_energies:
            if h + '=' + v not in row['energy_value_raw']:
                report.add('ERROR', 'ENERGY_RAW_OMITTED', f'{rid}: {h}={v} {u}')
        if row.get('electron_energy_evidence', '').startswith('NOMINAL_ENDPOINT:'):
            brst = '(BRST)' in (base.text('INC-SOURCE') + sub.text('INC-SOURCE'))
            if not nominal_endpoint_matches(row, fields, brst, paper_verified):
                report.add('ERROR', 'NOMINAL_ENDPOINT_SOURCE', rid)
    except (SourceError, KeyError, ValueError, IndexError, OSError) as exc:
        report.add('ERROR', 'POINT_SOURCE_CHECK', f'{rid}: {exc}')

    z, ee = decimal(row['product_Z']), decimal(row['electron_energy_max_MeV'])
    for name in ('product_Z', 'electron_energy_max_MeV', 'photon_energy_max_MeV',
                 'ir_value_numeric', 'ir_bound_numeric'):
        if row[name] and decimal(row[name]) is None:
            report.add('ERROR', 'INVALID_NUMBER', f'{rid}: {name}')
    zflag = filter_value(z, args.z_min, args.z_max)
    eflag = filter_value(ee, args.energy_min, args.energy_max)
    selection = 'excluded' if 'fail' in (zflag, eflag) else (
        'in_scope' if (zflag, eflag) == ('pass', 'pass') else 'pending')
    if (row['product_Z_filter_pass'], row['electron_endpoint_filter_pass'], row['selection_status']) != (
            zflag, eflag, selection):
        report.add('ERROR', 'FILTER_LOGIC', rid)
    if row['selection_status'] == 'excluded' and not row.get('selection_reason', '').strip():
        report.add('ERROR', 'EXCLUSION_REASON_EMPTY', rid)
    if ee is None:
        report.add('WARNING', 'ELECTRON_ENDPOINT_UNKNOWN', rid)
        if row['review_status'] != 'needs_review':
            report.add('ERROR', 'ENERGY_NOT_IN_REVIEW', rid)
    elif not row['electron_energy_evidence'] or not row['energy_source_locator']:
        report.add('ERROR', 'ENERGY_EVIDENCE_EMPTY', rid)
    if row['review_status'] == 'needs_review' and not (row['review_note'] and row['review_action']):
        report.add('ERROR', 'REVIEW_DETAILS_EMPTY', rid)
    if row['review_status'] not in ('needs_review', 'no_open_issue'):
        report.add('ERROR', 'REVIEW_STATUS', rid)
    if row['ir_basis'] not in ('yield', 'cross_section', 'mixed', 'unknown'):
        report.add('ERROR', 'IR_BASIS', rid)
    if row['comparability_status'] != 'not_assessed' and not row['comparability_note']:
        report.add('ERROR', 'COMPARABILITY_EVIDENCE_EMPTY', rid)
    if re.search(r'\b(DERIV|THEOR|CALC|EVAL)\b', row['quantity_code_raw']):
        report.add('WARNING', 'DERIVED_OR_MODEL_SOURCE', rid)
    if row['duplicate_group_id']:
        report.add('WARNING', 'POSSIBLE_DUPLICATE', rid + ': ' + row['duplicate_group_id'])


def audit(args):
    root = args.root.resolve()
    report = Report(args.max_examples)
    dictionary = (root / 'EXFOR_data_dictionary.md').read_text(encoding='utf-8-sig')
    sections = re.split(r'^## [123]\. ', dictionary, flags=re.M)[1:]
    if len(sections) != 3:
        raise SourceError('Expected three schemas in EXFOR_data_dictionary.md')
    schemas = [re.findall(r'^\| `([^`]+)` \|', s, re.M) for s in sections]
    names = ['EXFOR_table_with_IR.csv', 'EXFOR_review_queue.csv', 'EXFOR_candidates.csv']
    main, queue, candidates = [read_csv(root / n, schema, 'candidate_id' if i == 2 else 'row_id', report)
                               for i, (n, schema) in enumerate(zip(names, schemas))]
    if any(code in ('CSV_MISSING', 'CSV_SCHEMA', 'CSV_PARSE', 'CSV_WIDTH', 'CSV_IDS')
           for (_, code) in report.counts):
        report.add('ERROR', 'AUDIT_INCOMPLETE', 'Fix table structure before source checks')
        return report
    by_id = {r['row_id']: r for r in main}
    expected_queue = {r['row_id'] for r in main if r['review_status'] == 'needs_review'}
    actual_queue = {r['row_id'] for r in queue}
    for rid in sorted(expected_queue - actual_queue):
        report.add('ERROR', 'QUEUE_MISSING', rid)
    for rid in sorted(actual_queue - expected_queue):
        report.add('ERROR', 'QUEUE_EXTRA', rid)
    for row in queue:
        original = by_id.get(row['row_id'], {})
        if any(original.get(k) != v for k, v in row.items()):
            report.add('ERROR', 'QUEUE_FIELD_MISMATCH', row['row_id'])
    for row in candidates:
        if row['resolution'] not in ('pending', 'linked_to_main', 'dismissed'):
            report.add('ERROR', 'CANDIDATE_RESOLUTION', row['candidate_id'])
        if row['resolution'] == 'pending' and (row['review_status'] != 'needs_review' or
                not row['review_note'] or not row['review_action']):
            report.add('ERROR', 'CANDIDATE_REVIEW', row['candidate_id'])
    repo = args.repo.resolve() if args.repo else root / '_support/raw/exfor_master'
    commit, files = snapshot(root, repo, args.regions, args.expected_commit, report)
    subentries, bases, descriptors = {}, {}, {}
    expected_points = set()
    seen_reactions, unsupported, nodata, outside = 0, 0, 0, 0
    text_mentions = set()
    for entry, path in sorted(files.items()):
        try:
            subs = parse_entry(path.read_text(encoding='utf-8-sig'))
        except (SourceError, UnicodeError, ValueError) as exc:
            report.add('ERROR', 'ENTRY_PARSE', f'{entry}: {exc}')
            continue
        base = subs[0]
        for sub in subs:
            if sub.empty_stub:
                report.add('WARNING', 'EMPTY_SUBENTRY_STUB',
                           sub.sid + ': bare SUBENT without BIB/DATA/ENDSUBENT; '
                           'retained as an empty source placeholder, not a numeric dataset')
            subentries[sub.sid], bases[sub.sid] = sub, base
            for (keyword, pointer), fragments in sub.bib.items():
                if keyword != 'REACTION':
                    continue
                reaction = ''.join(fragments)
                if not re.search(r'\(\s*G\s*,', reaction):
                    continue
                seen_reactions += 1
                try:
                    descriptor, limitation = classify_ir(reaction)
                except SourceError as exc:
                    descriptor, limitation = None, str(exc)
                if limitation:
                    unsupported += 1
                    report.add('WARNING', 'UNRESOLVED_REACTION', f'{sub.sid}/{pointer}: {limitation}; {reaction}')
                if descriptor is None:
                    text = base.text('TITLE') + base.text('ADD-RES') + sub.text('COMMENT') + sub.text('ADD-RES')
                    if re.search(r'isomer.{0,40}ratio|ratio.{0,40}isomer', re.sub(r'\s+', ' ', text), re.I):
                        text_mentions.add(entry)
                    continue
                descriptors[(sub.sid, pointer)] = descriptor
                if descriptor['Z'] is not None and not args.z_min <= descriptor['Z'] <= args.z_max:
                    outside += 1
                    continue
                if sub.data is None:
                    nodata += 1
                    report.add('INFO', 'IR_NODATA', f'{sub.sid}/{pointer}: {sub.text("STATUS", pointer)}')
                    continue
                for ri, row in enumerate(sub.data.rows, 1):
                    fields = applicable_fields(base, sub, pointer, ri - 1)
                    z = descriptor['Z']
                    if z is None:
                        z = decimal(fields.get('ELEMENT', ('', '', 0))[0])
                        if z is None:
                            unsupported += 1
                            report.add('WARNING', 'VARIABLE_PRODUCT_UNRESOLVED', f'{sub.sid}/{pointer}/{ri}')
                            continue
                    if not args.z_min <= z <= args.z_max:
                        continue
                    for ci, ((heading, p), value) in enumerate(zip(sub.data.headings, row), 1):
                        if p not in ('', pointer) or not value:
                            continue
                        if heading in ('DATA', 'DATA-MIN', 'DATA-MAX', 'DATA-APRX'):
                            expected_points.add((sub.sid, pointer, ri, ci))
                        elif heading.startswith('DATA') and 'ERR' not in heading:
                            unsupported += 1
                            report.add('WARNING', 'UNSUPPORTED_DATA_HEADING', f'{sub.sid}/{pointer}/{ri}: {heading}')
    actual_points = {}
    for row in main:
        rid = row['row_id']
        try:
            key = source_key(row)
            if key in actual_points:
                report.add('ERROR', 'SOURCE_POINT_DUPLICATED', rid)
            actual_points[key] = rid
        except (KeyError, ValueError, SourceError) as exc:
            report.add('ERROR', 'SOURCE_POINT_KEY', f'{rid}: {exc}')
            continue
        sub = subentries.get(row['exfor_subentry'])
        if sub is None:
            report.add('ERROR', 'SOURCE_SUBENTRY_ABSENT', rid)
            continue
        source_reaction = sub.text('REACTION', row['exfor_pointer'])
        if re.sub(r'\s+', '', source_reaction) != re.sub(r'\s+', '', row['reaction_raw']):
            report.add('ERROR', 'REACTION_SOURCE_MISMATCH', rid)
        check_point(row, sub, bases[sub.sid], descriptors.get((sub.sid, row['exfor_pointer'])),
                    root, commit, files, args, report)
    check_coverage(expected_points, actual_points, report)
    report.metrics['selection_scan'] = dict(photon_reactions=seen_reactions,
        recognized_ir_datasets=len(descriptors), outside_Z_datasets=outside,
        nodata_ir_datasets=nodata, expected_points=len(expected_points),
        missing_points=len(expected_points - actual_points.keys()), unresolved=unsupported,
        broad_text_mention_entries=len(text_mentions),
        coverage='incomplete_or_unresolved' if unsupported or report.count('ERROR') else 'matched_supported_syntax')
    report.metrics['selection_status'] = dict(collections.Counter(r['selection_status'] for r in main))
    report.metrics['review_status'] = dict(collections.Counter(r['review_status'] for r in main))
    report.metrics['electron_evidence_entries'] = len({r['exfor_entry'] for r in main if r['electron_energy_max_MeV']})
    report.add('INFO', 'CANDIDATE_SEARCH_LIMIT',
               f'{len(text_mentions)} ENTRY have broad text mentions outside recognized IR. '
               'These may refer to another reaction or product already present; '
               'the article-only candidate list is not certified exhaustive.')
    return report


class AuditTests(unittest.TestCase):
    def test_nominal_endpoint_needs_source_boundary_not_mean_or_interval(self):
        row = {'exfor_entry': 'G0004', 'electron_energy_max_MeV': '50',
               'photon_energy_max_MeV': '50'}
        fields = {'EN-MAX': ('50000', 'KEV', '')}
        self.assertTrue(nominal_endpoint_matches(row, fields, True, False))
        self.assertFalse(nominal_endpoint_matches(row, fields, False, False))
        self.assertFalse(nominal_endpoint_matches(row, {'EN': ('50', 'MEV', '')}, True, False))
        self.assertFalse(nominal_endpoint_matches(row, dict(fields, **{'EN-MIN': ('13', 'MEV', '')}), True, False))
        row['electron_energy_max_MeV'] = '55'
        self.assertFalse(nominal_endpoint_matches(row, fields, True, False))

    def test_primary_override_is_narrow_and_requires_evidence(self):
        row = {'row_id': 'M0670/M0670004/-/2/DATA@2', 'notes': 'PRIMARY_SCALAR_OVERRIDE_PT10'}
        self.assertEqual(documented_scalar(row, '.0014', Decimal('.0014'), False), Decimal('.0014'))
        self.assertEqual(documented_scalar(row, '.0014', Decimal('.0014'), True), Decimal('.014'))
        row['row_id'] = 'M0670/M0670004/-/3/DATA@2'
        self.assertEqual(documented_scalar(row, '.0014', Decimal('.0014'), True), Decimal('.0014'))
        self.assertFalse(primary_paper_verified({'exfor_entry': 'M0670', 'notes': 'unverified link'}, Path('.')))

    def test_number_notation(self):
        self.assertEqual(decimal('1.2-3'), Decimal('.0012'))
        self.assertEqual(decimal('2D-2'), Decimal('.02'))
        self.assertIsNone(decimal('NaN'))

    def test_reaction_state_and_quantity_slashes(self):
        value, error = classify_ir('(60-ND-142(G,N)60-ND-141-M/G,,SIG/RAT,,BRA)')
        self.assertIsNone(error)
        self.assertEqual((value['Z'], value['numerator'], value['denominator']), (60, 'M', 'G'))
        value, error = classify_ir('(73-TA-180-M(G,INL)73-TA-180-G/M,,SIG/RAT)')
        self.assertEqual(value['target'], '73-TA-180-M')
        value, error = classify_ir('(60-ND-142(G,N)60-ND-141-M/G,,SIG/RAT) Text about (g,n) and (g,f).')
        self.assertIsNone(error)
        self.assertEqual(value['Z'], 60)

    def test_empty_deleted_placeholder_does_not_hide_later_data(self):
        def marker(key, identifier):
            return f'{key:<11}{identifier:>11}'
        text = '\n'.join([marker('ENTRY', 'X0000'), marker('SUBENT', 'X0000001'),
                          marker('ENDSUBENT', '0'), marker('SUBENT', 'X0000002'),
                          marker('SUBENT', 'X0000003'), marker('ENDSUBENT', '0'),
                          marker('SUBENT', 'X0000004'), marker('ENDENTRY', '4')])
        subs = parse_entry(text)
        self.assertEqual(len(subs), 4)
        self.assertEqual(sum(s.empty_stub for s in subs), 2)

    def test_explicit_quotient_and_unsupported(self):
        value, error = classify_ir('((60-ND-142(G,N)60-ND-141-M,,SIG)/(60-ND-142(G,N)60-ND-141-G,,SIG))')
        self.assertIsNone(error)
        self.assertEqual(value['denominator'], 'G')
        value, error = classify_ir('(60-ND-142(G,N)ELEM/MASS,,SIG/RAT)')
        self.assertIsNone(value)
        self.assertTrue(error)
        value, error = classify_ir('((60-ND-142(G,EL)60-ND-142,,DA)//(60-ND-142(G,EL)60-ND-142,,DA))')
        self.assertIsNone(value)
        self.assertIsNone(error)

    def test_wrapped_table_and_truncation(self):
        def header(name, n, rows):
            return f'{name:<11}{n:>11}{rows:>11}'
        def pack(values):
            return [''.join(f'{v:<11}' for v in values[i:i + 6]) for i in range(0, len(values), 6)]
        names = ['EN', 'DATA', 'ERR-T', 'ERR-1', 'ERR-2', 'ERR-3', 'ERR-4']
        lines = [header('DATA', 7, 1)] + pack(names) + pack(['MEV'] + ['NO-DIM'] * 6) + pack(['20', '.5', '.07', '1', '2', '3', '4']) + ['ENDDATA']
        block, end = parse_block(lines, 0)
        self.assertEqual(block.rows[0][-1], '4')
        self.assertEqual(block.headings[6][0], 'ERR-4')
        with self.assertRaises(SourceError):
            parse_block(lines[:-1], 0)

    def test_csv_multiline_and_damage(self):
        path = Path('in-memory-test.csv')
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerows([['row_id', 'notes'], ['a', 'line1\nline2, quoted "text"']])
        report = Report()
        rows = read_csv_bytes(path, stream.getvalue().encode('utf-8-sig'), ['row_id', 'notes'], 'row_id', report)
        self.assertEqual(len(rows), 1)
        self.assertEqual(report.count('ERROR'), 0)
        report = Report()
        read_csv_bytes(path, 'row_id,notes\na,b,c\n'.encode('utf-8-sig'), ['row_id', 'notes'], 'row_id', report)
        self.assertEqual(report.counts[('ERROR', 'CSV_WIDTH')], 1)

    def test_priority_and_corruption(self):
        base, sub = Subentry('X0000001'), Subentry('X0000002')
        base.common = Block([('EN-MAX', '')], ['MEV'], [['20']])
        sub.data = Block([('EN-MAX', ''), ('EN-MAX', '1')], ['MEV', 'MEV'], [['21', '22']])
        self.assertEqual(applicable_fields(base, sub, '1', 0)['EN-MAX'][0], '22')
        self.assertNotEqual(blob_hash(b'original'), blob_hash(b'changed'))
        expected = {('X', '', 1, 2), ('X', '', 2, 2)}
        actual = {('X', '', 1, 2): 'row1'}
        report = Report()
        check_coverage(expected, actual, report)
        self.assertEqual(report.counts[('ERROR', 'EXPECTED_POINT_MISSING')], 1)

    def test_excluded_rows_require_nonempty_reason(self):
        sub = Subentry('X0000002')
        sub.data = Block([('DATA', '')], ['NO-DIM'], [['.25']])
        row = collections.defaultdict(str, row_id='X0000/X0000002/-/1/DATA@1',
            exfor_entry='X0000', exfor_subentry=sub.sid, source_data_row='1',
            source_data_column='DATA; column=1', ir_value_raw='.25 NO-DIM',
            ir_value_numeric='.25', limit_qualifier='none', product_Z='60',
            electron_energy_max_MeV='65', electron_energy_evidence='fixture',
            energy_source_locator='fixture', product_Z_filter_pass='pass',
            electron_endpoint_filter_pass='fail', selection_status='excluded',
            review_status='no_open_issue', ir_basis='cross_section',
            comparability_status='not_assessed')
        args = argparse.Namespace(z_min=60, z_max=80, energy_min=Decimal(10), energy_max=Decimal(60))
        fake_path = mock.Mock()
        fake_path.is_file.return_value = True
        cases = [(None, 1), ('', 1), (' \t\r\n', 1),
                 ('Energy outside 10-60 MeV.', 0)]
        with mock.patch(__name__ + '.within_project', return_value=fake_path):
            for reason, expected_errors in cases:
                with self.subTest(reason=reason):
                    candidate = row.copy()
                    if reason is not None:
                        candidate['selection_reason'] = reason
                    original = dict(candidate)
                    report = Report()
                    check_point(candidate, sub, Subentry('X0000001'), None,
                                Path('.'), '', {}, args, report)
                    self.assertEqual(report.counts[('ERROR', 'EXCLUSION_REASON_EMPTY')],
                                     expected_errors)
                    self.assertEqual(report.count('ERROR'), expected_errors)
                    self.assertEqual({key: candidate[key] for key in original}, original)
            row['electron_energy_max_MeV'] = '20'
            row['electron_endpoint_filter_pass'] = 'pass'
            row['selection_status'] = 'in_scope'
            report = Report()
            check_point(row, sub, Subentry('X0000001'), None, Path('.'), '', {}, args, report)
            self.assertEqual(report.count('ERROR'), 0)

    def test_corrupted_number_and_false_filter_are_detected(self):
        # Exercise the actual point auditor against synthetic in-memory source
        # data; no access to project files or a temporary directory is needed.
        sub = Subentry('X0000002')
        sub.data = Block([('DATA', '')], ['NO-DIM'], [['.25']])
        row = collections.defaultdict(str, row_id='X0000/X0000002/-/1/DATA@1',
            exfor_entry='X0000', exfor_subentry=sub.sid, source_data_row='1',
            source_data_column='DATA; column=1', ir_value_raw='.25 NO-DIM',
            ir_value_numeric='.25', limit_qualifier='none', product_Z='60',
            electron_energy_max_MeV='20', electron_energy_evidence='fixture',
            energy_source_locator='fixture', product_Z_filter_pass='pass',
            electron_endpoint_filter_pass='pass', selection_status='in_scope',
            review_status='no_open_issue', ir_basis='cross_section',
            comparability_status='not_assessed')
        args = argparse.Namespace(z_min=60, z_max=80, energy_min=Decimal(10), energy_max=Decimal(60))
        fake_path = mock.Mock()
        fake_path.is_file.return_value = True
        with mock.patch(__name__ + '.within_project', return_value=fake_path):
            report = Report()
            check_point(row, sub, Subentry('X0000001'), None, Path('.'), '', {}, args, report)
            self.assertEqual(report.count('ERROR'), 0)
            row['ir_value_numeric'] = '.5'
            row['electron_energy_max_MeV'] = '65'
            report = Report()
            check_point(row, sub, Subentry('X0000001'), None, Path('.'), '', {}, args, report)
            self.assertEqual(report.counts[('ERROR', 'NUMERIC_SOURCE')], 1)
            self.assertEqual(report.counts[('ERROR', 'FILTER_LOGIC')], 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--repo', type=Path, help='Local EXFOR snapshot checkout')
    parser.add_argument('--regions', default='GKLM')
    parser.add_argument('--expected-commit', default='')
    parser.add_argument('--z-min', type=int, default=60)
    parser.add_argument('--z-max', type=int, default=80)
    parser.add_argument('--energy-min', type=Decimal, default=Decimal('10'))
    parser.add_argument('--energy-max', type=Decimal, default=Decimal('60'))
    parser.add_argument('--max-examples', type=int, default=5)
    parser.add_argument('--append-log', action='store_true')
    parser.add_argument('--strict', action='store_true', help='Exit 2 also on remaining warnings')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(AuditTests))
        return 0 if result.wasSuccessful() else 1
    if not args.energy_min.is_finite() or not args.energy_max.is_finite():
        parser.error('Energy bounds must be finite')
    if args.z_min > args.z_max or args.energy_min > args.energy_max or args.max_examples < 1 or not re.fullmatch('[A-Z]+', args.regions):
        parser.error('Invalid filter bounds, regions or max-examples')
    try:
        report = audit(args)
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        report = Report(args.max_examples)
        report.add('ERROR', 'AUDIT_INCOMPLETE', f'{type(exc).__name__}: {exc}')
    output = report.render()
    print(output, end='')
    if args.append_log:
        log_path = args.root / 'EXFOR_extraction_log.txt'
        if not log_path.is_file():
            print('Existing extraction log not found; refused to create a separate report.', file=sys.stderr)
            return 2
        with log_path.open('a', encoding='utf-8', newline='') as f:
            f.write('\n' + output)
    if report.counts[('ERROR', 'AUDIT_INCOMPLETE')]:
        return 2
    if report.count('ERROR'):
        return 1
    return 2 if args.strict and report.count('WARNING') else 0


if __name__ == '__main__':
    raise SystemExit(main())
