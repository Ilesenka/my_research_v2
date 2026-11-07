"""Численные проверки усреднения сечения, ветка 2 практики.

Аналитические эталоны и допуски зафиксированы в METHOD.md.
Ожидается модуль src/artefact6/flux_average.py с функциями:
make_model_data(e_th, e_max, sigma0, w0, n),
flux_average(energy, sigma, flux), trapezoid_integral(energy, values).

Здесь нет чтения результатов из CSV: проверяемый код вызывается напрямую,
а эталоны вычисляются независимо по аналитическим формулам.
"""

import numpy as np
import pytest

from src.artefact6.flux_average import (
    flux_average,
    make_model_data,
    trapezoid_integral,
)


CASES = [
    dict(e_th=8.0, e_max=20.0, sigma0=120.0, w0=1.0),
    dict(e_th=10.0, e_max=30.0, sigma0=60.0, w0=5.0),
    dict(e_th=12.0, e_max=60.0, sigma0=240.0, w0=10.0),
]
GRID_SIZES = [10, 20, 40, 80, 160, 320]


@pytest.fixture(params=CASES, ids=["A", "B", "C"])
def case(request):
    """Все проверки выполняются для каждого набора из методики."""
    return request.param.copy()


def average_at(case, n):
    """Запустить всю цепочку: модельные данные, затем усреднение."""
    energy, sigma, flux = make_model_data(**case, n=n)
    result = flux_average(energy, sigma, flux)
    assert np.isfinite(result), "Среднее должно быть конечным"
    return result


@pytest.mark.parametrize("n", GRID_SIZES)
def test_model_matches_analytic_functions(case, n):
    """Монотонности недостаточно: проверяем сами линейные зависимости."""
    energy, sigma, flux = make_model_data(**case, n=n)
    x = (energy - case["e_th"]) / (case["e_max"] - case["e_th"])
    np.testing.assert_allclose(sigma, case["sigma0"] * x, rtol=1e-12, atol=0.0)
    np.testing.assert_allclose(flux, case["w0"] * (1.0 - x), rtol=1e-12, atol=0.0)


def test_fine_grid_accuracy(case):
    """На конечной сетке относительная погрешность не больше 0,01%."""
    exact = case["sigma0"] / 3.0
    result = average_at(case, n=320)
    relative_error = abs(result - exact) / abs(exact)
    assert relative_error <= 1e-4, f"Относительная погрешность: {relative_error}"


def test_second_order_convergence(case):
    """Ошибка убывает, а все пять оценок порядка лежат в [1,9; 2,1]."""
    exact = case["sigma0"] / 3.0
    errors = np.array([abs(average_at(case, n) - exact) for n in GRID_SIZES])
    assert np.isfinite(errors).all()
    assert np.all(errors > 0), "Для оценки порядка нужны положительные ошибки"
    assert np.all(np.diff(errors) < 0), f"Погрешность не убывает: {errors}"
    orders = np.log2(errors[:-1] / errors[1:])
    assert np.isfinite(orders).all()
    assert np.all((orders >= 1.9) & (orders <= 2.1)), f"Порядки: {orders}"


@pytest.mark.parametrize("n", GRID_SIZES)
def test_finite_grid_formula(case, n):
    """Проверяем точную формулу результата именно метода трапеций."""
    expected = case["sigma0"] / 3.0 * (1.0 - 1.0 / n**2)
    assert average_at(case, n) == pytest.approx(expected, rel=1e-10, abs=0.0)


def test_separate_integrals(case):
    """Общая ошибка обоих интегралов не должна скрываться в отношении."""
    energy, sigma, flux = make_model_data(**case, n=320)
    length = case["e_max"] - case["e_th"]
    exact_numerator = case["sigma0"] * case["w0"] * length / 6.0
    exact_denominator = case["w0"] * length / 2.0
    numerator = trapezoid_integral(energy, sigma * flux)
    denominator = trapezoid_integral(energy, flux)
    assert np.isfinite(numerator) and np.isfinite(denominator)
    assert denominator > 0
    assert abs(numerator - exact_numerator) / exact_numerator <= 1e-4
    assert abs(denominator - exact_denominator) / exact_denominator <= 1e-12
    # Отдельно проверяем, что усреднение использует отношение интегралов.
    assert flux_average(energy, sigma, flux) == pytest.approx(
        numerator / denominator, rel=1e-10, abs=0.0
    )


@pytest.mark.parametrize("factor", [0.1, 2.0, 10.0])
def test_flux_scaling(case, factor):
    """Общая интенсивность спектра не меняет усреднённое сечение."""
    energy, sigma, flux = make_model_data(**case, n=320)
    baseline = flux_average(energy, sigma, flux)
    scaled = flux_average(energy, sigma, flux * factor)
    assert np.isfinite(baseline) and np.isfinite(scaled)
    assert scaled == pytest.approx(baseline, rel=1e-10, abs=0.0)


@pytest.mark.parametrize("factor", [0.1, 2.0, 10.0])
def test_cross_section_scaling(case, factor):
    """Масштаб сечения линейно переносится в среднее."""
    energy, sigma, flux = make_model_data(**case, n=320)
    baseline = flux_average(energy, sigma, flux)
    scaled = flux_average(energy, sigma * factor, flux)
    assert np.isfinite(baseline) and np.isfinite(scaled)
    assert scaled == pytest.approx(baseline * factor, rel=1e-10, abs=0.0)


def test_average_bounds(case):
    """Неотрицательные веса сохраняют среднее в пределах сечения."""
    energy, sigma, flux = make_model_data(**case, n=320)
    result = flux_average(energy, sigma, flux)
    tolerance = 1e-10 * case["sigma0"]
    assert np.isfinite(result)
    assert np.min(sigma) - tolerance <= result <= np.max(sigma) + tolerance
