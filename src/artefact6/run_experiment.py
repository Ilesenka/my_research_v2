"""[порождено] Этапы эксперимента из METHOD.md: dry, smoke, sweep, full.

Пример из корня проекта: python src/artefact6/run_experiment.py dry
Для отдельного запуска можно указать --output-dir /tmp/experiment-name.
Файлы результатов не перезаписываются. Журнал и методику обновляет агент
после анализа фактических результатов, а не этот скрипт.
"""

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.artefact6.flux_average import make_model_data, trapezoid_integral, flux_average

CASES = {
    "A": dict(e_th=8.0, e_max=20.0, sigma0=120.0, w0=1.0),
    "B": dict(e_th=10.0, e_max=30.0, sigma0=60.0, w0=5.0),
    "C": dict(e_th=12.0, e_max=60.0, sigma0=240.0, w0=10.0),
}
GRIDS = (10, 20, 40, 80, 160, 320)
FIELDS = (
    "case_id", "e_th_MeV", "e_max_MeV", "sigma0_mb", "w0", "n", "h_MeV",
    "numerator", "denominator", "sigma_avg_mb", "sigma_exact_mb",
    "abs_error_mb", "rel_error", "p_to_next", "elapsed_s",
)
TIMING_FIELDS = ("n", "nodes", "batch", "repeats", "batch_s", "seconds_per_run")
CHECK_FILES = ("test_model_data.py", "test_flux_inputs.py", "test_numerics.py")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def context():
    """Снимок версии: сравнение этапов зависит от содержимого, не даты коммита."""
    method = (ROOT / "METHOD.md").read_text(encoding="utf-8").split("## 8.", 1)[0]
    signature = {
        "model_sha256": digest(ROOT / "src/artefact6/flux_average.py"),
        "runner_sha256": digest(Path(__file__)),
        "method_0_7_sha256": hashlib.sha256(method.encode()).hexdigest(),
        "requirements_sha256": digest(ROOT / "requirements-week6.txt"),
        "checks_sha256": {name: digest(ROOT / "checks/artefact6" / name) for name in CHECK_FILES},
        "parameters": CASES, "grids": list(GRIDS),
        "python": platform.python_version(), "numpy": np.__version__,
    }
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    if revision.returncode:
        raise ValueError("Не удалось определить коммит проекта")
    return {"signature": signature, "git_commit": revision.stdout.strip(),
            "created_utc": datetime.now(timezone.utc).isoformat()}


def ensure_new(out, *names):
    for name in names:
        if (out / name).exists():
            raise ValueError(f"Уже существует {out / name}; для нового опыта укажи другой --output-dir")


def write_json(path, data):
    # allow_nan=False запрещает записывать NaN и Infinity как результаты.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def write_csv(path, rows, fields):
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def require_stage(out, stage, ctx):
    path = out / f"{stage}.json"
    if not path.exists():
        raise ValueError(f"Сначала выполни этап {stage} в том же каталоге результатов")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("stage") != stage or data.get("passed") is not True:
        raise ValueError(f"Этап {stage} не пройден")
    if data.get("context", {}).get("signature") != ctx["signature"]:
        raise ValueError("После предыдущего этапа изменились код, проверки, окружение или методика; начни новый опыт")
    return data


def calculate(case_id, n):
    """Один расчет с метриками; время включает создание модельных массивов."""
    params = CASES[case_id]
    start = time.perf_counter()
    energy, sigma, flux = make_model_data(**params, n=n)
    numerator = trapezoid_integral(energy, sigma * flux)
    denominator = trapezoid_integral(energy, flux)
    value = flux_average(energy, sigma, flux)
    exact = params["sigma0"] / 3.0
    error = abs(value - exact)
    row = dict(case_id=case_id, e_th_MeV=params["e_th"], e_max_MeV=params["e_max"],
               sigma0_mb=params["sigma0"], w0=params["w0"], n=n,
               h_MeV=(params["e_max"] - params["e_th"]) / n,
               numerator=numerator, denominator=denominator, sigma_avg_mb=value,
               sigma_exact_mb=exact, abs_error_mb=error, rel_error=error / exact,
               p_to_next=None, elapsed_s=0.0)
    row["elapsed_s"] = time.perf_counter() - start
    if not all(math.isfinite(v) for v in row.values() if isinstance(v, (int, float))):
        raise ValueError(f"Неконечный результат: {case_id}, n={n}")
    return row


def add_orders(rows):
    for case_id in CASES:
        group = sorted((r for r in rows if r["case_id"] == case_id), key=lambda r: r["n"])
        for left, right in zip(group, group[1:]):
            if left["abs_error_mb"] <= 0 or right["abs_error_mb"] <= 0:
                raise ValueError("Нельзя оценить порядок: погрешность не положительна")
            left["p_to_next"] = math.log2(left["abs_error_mb"] / right["abs_error_mb"])


def validate_rows(rows):
    """Проверить таблицу независимо от записанных в ней погрешностей."""
    errors = []
    expected_keys = {(c, n) for c in CASES for n in GRIDS}
    keys = [(r["case_id"], r["n"]) for r in rows]
    if len(rows) != 18 or len(set(keys)) != 18 or set(keys) != expected_keys:
        return ["Требуются ровно 18 уникальных комбинаций A-C и заданных сеток"]
    for row in rows:
        case_id, n = row["case_id"], row["n"]
        params = CASES[case_id]
        prefix = f"{case_id}, n={n}: "
        numbers = [v for k, v in row.items() if k != "case_id" and v is not None]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in numbers):
            errors.append(prefix + "неконечные или нечисловые значения")
            continue
        known = dict(e_th_MeV=params["e_th"], e_max_MeV=params["e_max"],
                     sigma0_mb=params["sigma0"], w0=params["w0"],
                     h_MeV=(params["e_max"] - params["e_th"]) / n,
                     sigma_exact_mb=params["sigma0"] / 3.0)
        if any(not math.isclose(row[k], v, rel_tol=1e-12, abs_tol=0.0) for k, v in known.items()):
            errors.append(prefix + "параметры или аналитический ответ не соответствуют плану")
        exact = params["sigma0"] / 3.0
        absolute = abs(row["sigma_avg_mb"] - exact)
        relative = absolute / exact
        if not math.isclose(row["abs_error_mb"], absolute, rel_tol=1e-10, abs_tol=0.0) or not math.isclose(row["rel_error"], relative, rel_tol=1e-10, abs_tol=0.0):
            errors.append(prefix + "погрешности не соответствуют среднему")
        finite_grid = exact * (1.0 - 1.0 / n**2)
        if not math.isclose(row["sigma_avg_mb"], finite_grid, rel_tol=1e-10, abs_tol=0.0):
            errors.append(prefix + "нарушена формула конечной сетки")
        margin = 1e-10 * params["sigma0"]
        if not -margin <= row["sigma_avg_mb"] <= params["sigma0"] + margin:
            errors.append(prefix + "среднее вышло за границы сечения")
        if row["denominator"] <= 0 or row["numerator"] < 0 or row["elapsed_s"] < 0:
            errors.append(prefix + "недопустимый интеграл или время")
        elif not math.isclose(row["sigma_avg_mb"], row["numerator"] / row["denominator"], rel_tol=1e-10, abs_tol=0.0):
            errors.append(prefix + "среднее не равно отношению интегралов")
        if n == 320:
            length = params["e_max"] - params["e_th"]
            num_exact = params["sigma0"] * params["w0"] * length / 6.0
            den_exact = params["w0"] * length / 2.0
            if relative > 1e-4 or abs(row["numerator"] - num_exact) / num_exact > 1e-4 or abs(row["denominator"] - den_exact) / den_exact > 1e-12:
                errors.append(prefix + "не выполнена точность конечной сетки")
            if row["p_to_next"] is not None:
                errors.append(prefix + "после последней сетки порядок не определен")
    for case_id in CASES:
        group = sorted((r for r in rows if r["case_id"] == case_id), key=lambda r: r["n"])
        for left, right in zip(group, group[1:]):
            e1 = abs(left["sigma_avg_mb"] - CASES[case_id]["sigma0"] / 3.0)
            e2 = abs(right["sigma_avg_mb"] - CASES[case_id]["sigma0"] / 3.0)
            if not (math.isfinite(e1) and math.isfinite(e2) and 0 < e2 < e1):
                errors.append(f"{case_id}: погрешность не убывает или порядок не определен")
                continue
            order = math.log2(e1 / e2)
            saved = left["p_to_next"]
            if not 1.9 <= order <= 2.1 or saved is None or not math.isclose(saved, order, rel_tol=1e-10, abs_tol=0.0):
                errors.append(f"{case_id}, n={left['n']}: неверный порядок сходимости")
    return errors


def read_rows(path):
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(FIELDS):
            raise ValueError("Столбцы convergence.csv не соответствуют схеме")
        rows = []
        for raw in reader:
            row = {k: (v if k == "case_id" else int(v) if k == "n" else None if k == "p_to_next" and v == "" else float(v)) for k, v in raw.items()}
            rows.append(row)
    return rows


def benchmark():
    """Технические повторы для времени, не дополнительные научные испытания."""
    rows, medians = [], []
    repeats = 1000
    for n in (40, 160):
        for _ in range(5):
            calculate("A", n)
        samples = []
        for batch in range(1, 4):
            start = time.perf_counter()
            for _ in range(repeats):
                calculate("A", n)
            elapsed = time.perf_counter() - start
            per_run = elapsed / repeats
            samples.append(per_run)
            rows.append(dict(n=n, nodes=n + 1, batch=batch, repeats=repeats,
                             batch_s=elapsed, seconds_per_run=per_run))
        medians.append(statistics.median(samples))
    t1, t2 = medians
    if t1 <= 0 or t2 <= 0:
        raise ValueError("Недостаточная точность измерения времени")
    kappa = math.log(t2 / t1) / math.log(161 / 41)
    predict = lambda n: t2 * ((n + 1) / 161) ** kappa
    forecast = dict(n1=40, n2=160, median_t1_s=t1, median_t2_s=t2, kappa=kappa,
                    predicted_n320_s=predict(320),
                    predicted_sweep_s=3 * sum(predict(n) for n in GRIDS),
                    scope="Расчет с метриками, без файлового ввода-вывода, запуска Python и pytest",
                    limitation="Два малых размера: накладные расходы и шум могут преобладать; kappa не подгоняется")
    return rows, forecast


def dry(out, ctx):
    ensure_new(out, "dry.json")
    plan = []
    for case_id, params in CASES.items():
        for n in GRIDS:
            # Генерация проверяет договор; интегралы на этапе dry не вызываются.
            energy, sigma, flux = make_model_data(**params, n=n)
            if not (len(energy) == len(sigma) == len(flux) == n + 1):
                raise ValueError("Некорректная модельная сетка")
            plan.append(dict(case_id=case_id, n=n, **params))
    if len({(r["case_id"], r["n"]) for r in plan}) != 18:
        raise ValueError("План должен содержать 18 уникальных комбинаций")
    write_json(out / "dry.json", dict(stage="dry", passed=True, context=ctx, plan=plan))
    return "план 3 набора x 6 сеток = 18 расчетов, интегрирование не выполнялось"


def smoke(out, ctx):
    require_stage(out, "dry", ctx)
    ensure_new(out, "smoke.json", "timing.csv")
    row = calculate("A", 10)
    if not 0 <= row["sigma_avg_mb"] <= 120 or row["rel_error"] > 0.02:
        raise ValueError("Короткая проверка A, n=10 не пройдена")
    timings, forecast = benchmark()
    write_csv(out / "timing.csv", timings, TIMING_FIELDS)
    write_json(out / "smoke.json", dict(stage="smoke", passed=True, context=ctx,
               run=row, timing=forecast, timing_csv_sha256=digest(out / "timing.csv")))
    return f"A, n=10: {row['sigma_avg_mb']:.10g} мб; относительная погрешность {row['rel_error']:.6g}; kappa={forecast['kappa']:.3f}"


def sweep(out, ctx):
    previous = require_stage(out, "smoke", ctx)
    ensure_new(out, "convergence.csv", "sweep.json")
    start = time.perf_counter()
    rows = [calculate(case_id, n) for case_id in CASES for n in GRIDS]
    add_orders(rows)
    elapsed = time.perf_counter() - start
    errors = validate_rows(rows)
    write_csv(out / "convergence.csv", rows, FIELDS)
    report = dict(stage="sweep", passed=not errors, context=ctx, rows=len(rows),
                  calculation_elapsed_s=elapsed,
                  predicted_calculation_s=previous["timing"]["predicted_sweep_s"],
                  csv_sha256=digest(out / "convergence.csv"), errors=errors)
    write_json(out / "sweep.json", report)
    if errors:
        raise ValueError("; ".join(errors))
    return f"18 расчетов сохранены в convergence.csv; время расчета {elapsed:.6f} s"


def full(out, ctx):
    previous = require_stage(out, "sweep", ctx)
    ensure_new(out, "full.json", "verifier.txt")
    path = out / "convergence.csv"
    if digest(path) != previous["csv_sha256"]:
        raise ValueError("convergence.csv изменен после этапа sweep")
    errors = validate_rows(read_rows(path))
    if errors:
        write_json(out / "full.json", dict(stage="full", passed=False, context=ctx, errors=errors))
        raise ValueError("; ".join(errors))
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable, "-m", "pytest", "checks/artefact6", "-q", "-p", "no:cacheprovider"]
    start = time.perf_counter()
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    elapsed = time.perf_counter() - start
    output = result.stdout + ("\nSTDERR:\n" + result.stderr if result.stderr else "")
    with (out / "verifier.txt").open("x", encoding="utf-8") as stream:
        stream.write("Command: " + " ".join(command) + "\n" + output + f"\nExit code: {result.returncode}\n")
    skipped = re.search(r"(\d+) skipped", result.stdout)
    passed = result.returncode == 0 and (skipped is None or int(skipped.group(1)) == 0)
    count = re.search(r"(\d+) passed", result.stdout)
    report = dict(stage="full", passed=passed, context=ctx, rows=18,
                  csv_sha256=digest(path), verifier_command=command,
                  verifier_exit_code=result.returncode,
                  verifier_passed_tests=int(count.group(1)) if count else None,
                  verifier_elapsed_s=elapsed, verifier_summary=result.stdout.strip().splitlines()[-1:] )
    write_json(out / "full.json", report)
    if not passed:
        raise ValueError(f"Верификатор не пройден; исходный код процесса {result.returncode}; см. verifier.txt")
    return f"таблица принята; {report['verifier_passed_tests']} тестов пройдено; вердикт: принято"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("dry", "smoke", "sweep", "full"))
    parser.add_argument("--output-dir", default="results/artefact6", help="Каталог одного опыта; существующие результаты не перезаписываются")
    args = parser.parse_args(argv)
    out = Path(args.output_dir)
    if not out.is_absolute():
        out = ROOT / out
    try:
        ctx = context()
        out.mkdir(parents=True, exist_ok=True)
        message = globals()[args.stage](out, ctx)
    except (ValueError, OSError, RuntimeError, csv.Error, KeyError, TypeError, OverflowError) as exc:
        print(f"НЕ ПРОЙДЕН: {args.stage}: {exc}", file=sys.stderr)
        return 1
    print(f"ПРОЙДЕН: {args.stage}: {message}")
    print(f"Результаты: {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
