"""Поиск пиков в спектрах data/*.txt.

Для каждого спектра: сглаживание скользящим средним (окно 2) для подавления
шумовых двойных пиков, поиск локальных максимумов выше порога, вычисление
положения (center_nm) и ширины на полувысоте (fwhm_nm) линейной интерполяцией.

Результат записывается в results/peaks.csv.
"""
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "results" / "peaks.csv"

THRESHOLD = 0.3   # порог высоты пика (шум в плоских областях < 0.1)
SMOOTH_W = 2      # полуширина окна сглаживания


def load_spectrum(path):
    rows = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("wavelength"):
            continue
        x, y = line.split()
        rows.append((float(x), float(y)))
    return rows


def smooth(ys, w):
    out = []
    for i in range(len(ys)):
        lo, hi = max(0, i - w), min(len(ys), i + w + 1)
        out.append(sum(ys[lo:hi]) / (hi - lo))
    return out


def find_peaks(xs, ys, thresh):
    peaks = []
    for i in range(1, len(xs) - 1):
        if ys[i] > ys[i - 1] and ys[i] >= ys[i + 1] and ys[i] > thresh:
            peaks.append((i, xs[i], ys[i]))
    return peaks


def fwhm(xs, ys, ip, yp):
    half = yp / 2.0
    li = ri = None
    for i in range(ip, 0, -1):
        if (ys[i] <= half <= ys[i - 1]) or (ys[i - 1] <= half <= ys[i]):
            x0, x1 = xs[i - 1], xs[i]
            y0, y1 = ys[i - 1], ys[i]
            li = x0 + (half - y0) * (x1 - x0) / (y1 - y0)
            break
    for i in range(ip, len(xs) - 1):
        if (ys[i] <= half <= ys[i + 1]) or (ys[i + 1] <= half <= ys[i]):
            x0, x1 = xs[i], xs[i + 1]
            y0, y1 = ys[i], ys[i + 1]
            ri = x0 + (half - y0) * (x1 - x0) / (y1 - y0)
            break
    return (ri - li) if (li is not None and ri is not None) else None


def main():
    results = []
    for path in sorted(DATA.glob("*.txt")):
        rows = load_spectrum(path)
        xs = [x for x, _ in rows]
        ys = smooth([y for _, y in rows], SMOOTH_W)
        for ip, xp, yp in find_peaks(xs, ys, THRESHOLD):
            fw = fwhm(xs, ys, ip, yp)
            if fw is not None:
                results.append((path.name, round(xp, 2), round(fw, 2)))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["file", "center_nm", "fwhm_nm"])
        writer.writerows(results)
    print(f"записано {len(results)} пиков в {OUT}")


if __name__ == "__main__":
    main()