"""Week 7: generated properties of the actual EXFOR verifier (G1-G8).

Run from the project root:
    python -m pytest checks/artefact7 -q

Inputs are synthetic and in memory. No project CSV, dictionary, raw ENTRY,
network, or Git checkout is read. Only file access and snapshot acquisition
are substituted; parsing, number checks, selection, queue checks and coverage
run through the real verifier. Mutations execute in separate in-memory modules.
"""
from __future__ import annotations

import argparse
import collections
import csv
import importlib.util
import io
from pathlib import Path
import sys
import types
from decimal import Decimal
from unittest import mock

import pytest
from hypothesis import example, given, settings, strategies as st

VERIFIER_PATH = Path(__file__).with_name("verify_exfor.py")
SOURCE = VERIFIER_PATH.read_text(encoding="utf-8")
spec = importlib.util.spec_from_file_location("week7_exfor_verifier", VERIFIER_PATH)
verifier = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verifier
spec.loader.exec_module(verifier)

PROPERTY = settings(max_examples=60, deadline=None, database=None, derandomize=True)
ROOT = Path("/virtual/week7-exfor")
REACTION = "(60-ND-142(G,N)60-ND-141-M/G,,SIG/RAT,,BRA)"
HEADERS = """row_id exfor_entry exfor_subentry exfor_pointer dataset_id
source_data_row source_data_column exfor_revision_date retrieved_at_utc
source_url raw_file source_reference doi source_locator reaction_raw
quantity_code_raw reaction_channel target_nuclide sample_material
sample_composition_raw sample_thickness_raw radiator_material
radiator_thickness_raw irradiation_conditions_raw product_nuclide product_Z
product_A ir_numerator_state ir_denominator_state state_properties_raw
state_properties_source energy_heading_raw energy_unit_raw energy_value_raw
electron_energy_max_MeV photon_energy_max_MeV energy_definition_raw
electron_energy_evidence energy_source_locator ir_definition_raw
ir_format_original ir_value_raw ir_value_numeric ir_bound_numeric
limit_qualifier ir_uncertainty_raw ir_uncertainty_minus ir_uncertainty_plus
ir_uncertainty_unit ir_uncertainty_type ir_basis cross_section_type_raw
cross_section_type partiality_raw yield_definition_raw production_type
decay_correction_raw measurement_method_raw analysis_method_raw
product_Z_filter_pass electron_endpoint_filter_pass selection_status
selection_reason comparability_status comparability_note duplicate_group_id
review_status review_note review_action notes""".split()
QUEUE_HEADERS = """row_id exfor_entry exfor_subentry exfor_pointer product_nuclide
reaction_channel selection_status review_status review_note review_action
source_url source_reference source_locator""".split()
CANDIDATE_HEADERS = ["candidate_id", "resolution", "review_status",
                     "review_note", "review_action"]
SCALES = {"EV": Decimal("0.000001"), "KEV": Decimal("0.001"),
          "MEV": Decimal(1), "GEV": Decimal(1000)}


class MemoryEntry:
    def __init__(self, text=""):
        self.text = text

    def is_file(self):
        return True

    def read_text(self, **kwargs):
        return self.text

    def read_bytes(self):
        return self.text.encode("utf-8")


def arguments(z_min=60, z_max=80, energy_min=10, energy_max=60):
    return argparse.Namespace(root=ROOT, repo=None, max_examples=5,
        regions="X", expected_commit="", z_min=z_min, z_max=z_max,
        energy_min=Decimal(energy_min), energy_max=Decimal(energy_max))


def errors(report):
    return {code for (level, code), count in report.counts.items()
            if level == "ERROR" and count}


def point(module, value=Decimal("0.25"), unit="NO-DIM", heading="DATA",
          energy=Decimal(20), energy_unit="MEV", nominal=False):
    raw_energy = format(energy / SCALES[energy_unit], "f")
    raw_value = format(value, "f")
    expected_ir = value / 100 if unit == "PER-CENT" else value
    qualifier = {"DATA": "none", "DATA-MAX": "<="}[heading]
    row = dict.fromkeys(HEADERS, "")
    row.update(row_id=f"X0000/X0000002/-/1/{heading}@2",
        exfor_entry="X0000", exfor_subentry="X0000002",
        source_data_row="1", source_data_column=f"{heading}; column=2",
        raw_file="raw/X0000.x4", reaction_raw=REACTION,
        quantity_code_raw=",SIG/RAT,,BRA", reaction_channel="(gamma,n)",
        target_nuclide="60-ND-142", product_nuclide="60-ND-141",
        product_Z="60", product_A="141",
        ir_numerator_state="M", ir_denominator_state="G",
        ir_value_raw=f"{raw_value} {unit}", ir_format_original="m/g",
        ir_value_numeric=format(expected_ir, "f") if heading == "DATA" else "",
        ir_bound_numeric=format(expected_ir, "f") if heading != "DATA" else "",
        limit_qualifier=qualifier, ir_basis="cross_section",
        energy_value_raw=f"EN-MAX={raw_energy}",
        electron_energy_max_MeV=str(energy), photon_energy_max_MeV=str(energy),
        electron_energy_evidence="NOMINAL_ENDPOINT: synthetic" if nominal else "synthetic",
        energy_source_locator="synthetic source",
        product_Z_filter_pass="pass", electron_endpoint_filter_pass="pass",
        selection_status="in_scope", selection_reason="Synthetic selection",
        comparability_status="not_assessed", review_status="no_open_issue")
    sub = module.Subentry("X0000002")
    sub.data = module.Block([("EN-MAX", ""), (heading, "")],
                            [energy_unit, unit], [[raw_energy, raw_value]])
    base = module.Subentry("X0000001")
    if nominal:
        base.bib[("INC-SOURCE", "")].append("(BRST)")
    return row, sub, base


def check(module, case, args=None):
    row, sub, base = case
    before = row.copy()
    report = module.Report()
    with mock.patch.object(module, "within_project", return_value=MemoryEntry()):
        module.check_point(row, sub, base, None, ROOT, "", {},
                           args or arguments(), report)
    assert row == before, "Verification must preserve the supplied measurement"
    return report


def csv_bytes(headers, rows):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=headers)
    writer.writeheader()
    writer.writerows({key: row.get(key, "") for key in headers} for row in rows)
    return out.getvalue().encode("utf-8-sig")


def source_entry(rows):
    def marker(name, value=""):
        return f"{name:<11}{value:>11}"
    def fields(values):
        return "".join(f"{v:<11}" for v in values)
    lines = [marker("ENTRY", "X0000"), marker("SUBENT", "X0000001"),
             marker("BIB"), f"{'INC-SOURCE':<11}(BRST)",
             marker("ENDBIB"), marker("ENDSUBENT"),
             marker("SUBENT", "X0000002"), marker("BIB"),
             f"{'REACTION':<11}{REACTION}", marker("ENDBIB"),
             f"{'DATA':<11}{2:>11}{len(rows):>11}",
             fields(["EN-MAX", "DATA"]), fields(["MEV", "NO-DIM"])]
    lines.extend(fields([row["electron_energy_max_MeV"],
                         row["ir_value_numeric"]]) for row in rows)
    lines.extend([marker("ENDDATA"), marker("ENDSUBENT"), marker("ENDENTRY")])
    return "\n".join(lines) + "\n"


def audit_tables(module, main, queue, source_rows):
    """Supply synthetic files, then run the actual full audit logic."""
    dictionary = "\n".join(f"## {i}. schema\n" + "\n".join(
        f"| `{key}` | synthetic |" for key in headers)
        for i, headers in enumerate((HEADERS, QUEUE_HEADERS, CANDIDATE_HEADERS), 1))
    files = {
        "EXFOR_table_with_IR.csv": csv_bytes(HEADERS, main),
        "EXFOR_review_queue.csv": csv_bytes(QUEUE_HEADERS, queue),
        "EXFOR_candidates.csv": csv_bytes(CANDIDATE_HEADERS, []),
    }
    entry = MemoryEntry(source_entry(source_rows))

    def read_csv(path, expected, id_column, report):
        return module.read_csv_bytes(path, files[path.name], expected, id_column, report)

    def read_dictionary(path, *args, **kwargs):
        assert path == ROOT / "EXFOR_data_dictionary.md"
        return dictionary

    with mock.patch.object(Path, "read_text", read_dictionary), \
         mock.patch.object(module, "read_csv", side_effect=read_csv), \
         mock.patch.object(module, "snapshot", return_value=("", {"X0000": entry})), \
         mock.patch.object(module, "within_project", return_value=entry):
        return module.audit(arguments())


def dataset(module, review_flags):
    rows = []
    for number, needs_review in enumerate(review_flags, 1):
        row, _, _ = point(module)
        row.update(row_id=f"X0000/X0000002/-/{number}/DATA@2",
                   source_data_row=str(number))
        if needs_review:
            row.update(review_status="needs_review", review_note="Check source",
                       review_action="Review original definition")
        rows.append(row)
    return rows


def assert_numeric_copy(module, n, unit):
    value = Decimal(n) / 1000
    case = point(module, value=value, unit=unit)
    assert not errors(check(module, case))
    expected = Decimal(case[0]["ir_value_numeric"])
    case[0]["ir_value_numeric"] = str(expected + Decimal("1e-16"))
    assert not errors(check(module, case)), "Rounding within the stated tolerance is allowed"
    case[0]["ir_value_numeric"] = str(expected + Decimal("0.01"))
    assert "NUMERIC_SOURCE" in errors(check(module, case))
    assert module.decimal("0") == 0
    assert module.decimal("") is None


@PROPERTY
@example(n=0, unit="NO-DIM")
@example(n=25000, unit="PER-CENT")
@given(n=st.integers(0, 1000000), unit=st.sampled_from(["NO-DIM", "PER-CENT"]))
def test_numeric_copy(n, unit):
    """G1: nonnegative IR 0..1000, including zero and percentage encoding."""
    assert_numeric_copy(verifier, n, unit)


def assert_energy_units(module, energy, unit):
    case = point(module, energy=energy, energy_unit=unit, nominal=True)
    case[0]["electron_endpoint_filter_pass"] = "pass"
    assert not errors(check(module, case))
    case[0]["electron_energy_max_MeV"] = str(energy + 1)
    assert "NOMINAL_ENDPOINT_SOURCE" in errors(check(module, case))


@PROPERTY
@example(energy=Decimal(10), unit="KEV")
@example(energy=Decimal(60), unit="GEV")
@given(energy=st.integers(10000, 60000).map(lambda n: Decimal(n) / 1000),
       unit=st.sampled_from(list(SCALES)))
def test_energy_units(energy, unit):
    """G2: established bremsstrahlung endpoints, 10..60 MeV in four units."""
    assert_energy_units(verifier, energy, unit)


def set_selection(row, z, energy, args):
    row["product_Z"] = "" if z is None else str(z)
    row["electron_energy_max_MeV"] = "" if energy is None else str(energy)
    z_out = z is not None and not args.z_min <= z <= args.z_max
    e_out = energy is not None and not args.energy_min <= energy <= args.energy_max
    row["product_Z_filter_pass"] = "unknown" if z is None else "fail" if z_out else "pass"
    row["electron_endpoint_filter_pass"] = "unknown" if energy is None else "fail" if e_out else "pass"
    row["selection_status"] = ("excluded" if z_out or e_out else
                               "pending" if z is None or energy is None else "in_scope")
    if energy is None:
        row.update(review_status="needs_review", review_note="Endpoint unknown",
                   review_action="Establish endpoint", electron_energy_evidence="")


def assert_selection_rules(module, z, energy):
    case = point(module)
    set_selection(case[0], z, energy, arguments())
    initial = case[0]["selection_status"]
    assert not errors(check(module, case))
    expanded = arguments(z_min=50, z_max=90, energy_min=0, energy_max=100)
    set_selection(case[0], z, energy, expanded)
    assert not errors(check(module, case, expanded))
    if initial == "in_scope":
        assert case[0]["selection_status"] == "in_scope"
    if z is None:
        assert module.filter_value(None, 50, 90) == "unknown"
    if energy is None:
        assert module.filter_value(None, Decimal(0), Decimal(100)) == "unknown"
    case[0]["selection_status"] = ("excluded" if case[0]["selection_status"] == "in_scope"
                                   else "in_scope")
    assert "FILTER_LOGIC" in errors(check(module, case, expanded))


@PROPERTY
@example(z=60, energy=Decimal(10))
@example(z=80, energy=Decimal(60))
@example(z=60, energy=None)
@example(z=59, energy=None)
@example(z=60, energy=Decimal(0))
@given(z=st.one_of(st.none(), st.integers(50, 90)),
       energy=st.one_of(st.none(), st.integers(0, 100000).map(lambda n: Decimal(n) / 1000)))
def test_selection_rules(z, energy):
    """G3: inclusive boundaries, unknown inputs, and widening the ranges."""
    assert_selection_rules(verifier, z, energy)


def assert_limit_semantics(module, n):
    case = point(module, value=Decimal(n) / 1000000, heading="DATA-MAX")
    assert not errors(check(module, case))
    case[0]["ir_value_numeric"] = case[0]["ir_bound_numeric"]
    case[0]["ir_bound_numeric"] = ""
    case[0]["limit_qualifier"] = "none"
    assert "LIMIT_SOURCE" in errors(check(module, case))


@PROPERTY
@example(n=0)
@example(n=1000)
@given(n=st.integers(0, 1000000))
def test_limit_semantics(n):
    """G4: upper bounds from zero to one are never point estimates."""
    assert_limit_semantics(verifier, n)


def assert_review_queue(module, flags):
    main = dataset(module, flags)
    queue = [row.copy() for row in main if row["review_status"] == "needs_review"]
    assert not errors(audit_tables(module, main, queue, main))
    if queue:
        bad_queue = queue[1:]
        assert "QUEUE_MISSING" in errors(audit_tables(module, main, bad_queue, main))
        stale = [row.copy() for row in queue]
        stale[0]["review_note"] = "Incorrect copied note"
        assert "QUEUE_FIELD_MISMATCH" in errors(audit_tables(module, main, stale, main))
    else:
        assert "QUEUE_EXTRA" in errors(audit_tables(module, main, [main[0]], main))


@PROPERTY
@example(flags=[False])
@example(flags=[True])
@example(flags=[True, False, True])
@given(flags=st.lists(st.booleans(), min_size=1, max_size=6))
def test_review_queue(flags):
    """G5: exact review subset; missing, extra and stale queue rows are rejected."""
    assert_review_queue(verifier, flags)


def assert_exclusion_reason(module, energy, reason):
    case = point(module, energy=energy)
    set_selection(case[0], 60, energy, arguments())
    case[0]["selection_reason"] = "Endpoint outside accepted interval"
    assert not errors(check(module, case))
    case[0]["selection_reason"] = reason
    assert "EXCLUSION_REASON_EMPTY" in errors(check(module, case))
    case[0].pop("selection_reason")
    assert "EXCLUSION_REASON_EMPTY" in errors(check(module, case))


@PROPERTY
@example(energy=Decimal(0), reason="")
@example(energy=Decimal(65), reason=" \t\n")
@given(energy=st.one_of(st.integers(0, 9999), st.integers(60001, 100000)).map(
           lambda n: Decimal(n) / 1000),
       reason=st.text(alphabet=" \t\r\n", max_size=15))
def test_exclusion_reason(energy, reason):
    """G6: rejected energies remain unchanged; absent/blank reasons are errors."""
    assert_exclusion_reason(verifier, energy, reason)


def assert_empty_main(module, count):
    source_rows = dataset(module, [False] * count)
    assert not errors(audit_tables(module, source_rows, [], source_rows))
    report = audit_tables(module, [], [], source_rows)
    assert report.counts[("ERROR", "EXPECTED_POINT_MISSING")] == count
    assert errors(report) == {"EXPECTED_POINT_MISSING"}


@PROPERTY
@given(count=st.integers(1, 8))
def test_empty_main(count):
    """G7: valid CSV headers cannot hide loss of 1..8 expected source points."""
    assert_empty_main(verifier, count)


def test_empty_main_without_expected_points_is_allowed():
    assert not errors(audit_tables(verifier, [], [], []))


MUTATIONS = [
    ("numeric_copy",
     "if target is not None and not close(target, preferred):",
     "if False:",
     lambda m: assert_numeric_copy(m, 250, "NO-DIM")),
    ("energy_units",
     "'KEV': Decimal('.001')",
     "'KEV': Decimal(1)",
     lambda m: assert_energy_units(m, Decimal(20), "KEV")),
    ("selection_rules",
     "low <= value <= high",
     "low < value < high",
     lambda m: assert_selection_rules(m, 60, Decimal(10))),
    ("limit_semantics",
     "if expected_limit and row['limit_qualifier'] != expected_limit:",
     "if False:",
     lambda m: assert_limit_semantics(m, 1000)),
    ("review_queue",
     "expected_queue = {r['row_id'] for r in main if r['review_status'] == 'needs_review'}",
     "expected_queue = {r['row_id'] for r in queue}",
     lambda m: assert_review_queue(m, [True])),
    ("exclusion_reason",
     "if row['selection_status'] == 'excluded' and not row.get('selection_reason', '').strip():",
     "if False:",
     lambda m: assert_exclusion_reason(m, Decimal(65), "")),
    ("empty_main",
     "for key in sorted(expected - actual.keys()):",
     "for key in []:",
     lambda m: assert_empty_main(m, 2)),
]


@pytest.mark.parametrize("name,before,after,check_property", MUTATIONS,
                         ids=[item[0] for item in MUTATIONS])
def test_detects_mutation(name, before, after, check_property):
    """G8: the same property accepts the original and rejects its broken copy."""
    check_property(verifier)
    assert SOURCE.count(before) == 1, "Mutation must target exactly one code fragment"
    module_name = "week7_exfor_mutant_" + name
    mutant = types.ModuleType(module_name)
    mutant.__file__ = str(VERIFIER_PATH)
    sys.modules[module_name] = mutant
    try:
        exec(compile(SOURCE.replace(before, after, 1), str(VERIFIER_PATH), "exec"),
             mutant.__dict__)
        with pytest.raises(AssertionError):
            check_property(mutant)
    finally:
        sys.modules.pop(module_name, None)
