"""Проверка результата: python checks/check_peaks.py. Код 0 — принято, 1 — нет."""
import csv, sys
from pathlib import Path
root = Path(__file__).resolve().parents[1]
res, exp = root / "results" / "peaks.csv", root / "checks" / "expected_peaks.csv"
errors = []
if not (root / "src" / "peaks.py").is_file():
    errors.append("нет скрипта src/peaks.py")
if not res.is_file():
    print("нет файла results/peaks.csv"); sys.exit(1)
got = list(csv.DictReader(open(res, encoding="utf-8")))
want = list(csv.DictReader(open(exp, encoding="utf-8")))
need = {"file", "center_nm", "fwhm_nm"}
if not got or not need <= set(got[0]):
    print("в results/peaks.csv нужны столбцы file, center_nm, fwhm_nm"); sys.exit(1)
for w in want:
    c, fw = float(w["center_nm"]), float(w["fwhm_nm"])
    hit = [g for g in got if g["file"] == w["file"] and abs(float(g["center_nm"]) - c) <= 1.0]
    if not hit:
        errors.append(f"{w['file']}: не найден пик около {c} нм")
    elif abs(float(hit[0]["fwhm_nm"]) - fw) > 0.2 * fw:
        errors.append(f"{w['file']}: ширина пика {c} нм вне допуска 20 %")
if len(got) != len(want):
    errors.append(f"пиков в результате {len(got)}, ожидалось {len(want)}")
print("\n".join(errors) if errors else "принято: все пики найдены")
sys.exit(1 if errors else 0)
