"""Проверки параметров, сетки и синтетических функций.

Ожидается make_model_data(e_th, e_max, sigma0, w0, n),
возвращающая массивы NumPy energy, sigma, flux.
Недопустимые параметры должны вызывать ValueError.
"""

import numpy as np
import pytest

from src.artefact6.flux_average import make_model_data


CASES = [
    dict(e_th=8.0, e_max=20.0, sigma0=120.0, w0=1.0),
    dict(e_th=10.0, e_max=30.0, sigma0=60.0, w0=5.0),
    dict(e_th=12.0, e_max=60.0, sigma0=240.0, w0=10.0),
]
GRID_SIZES = [10, 20, 40, 80, 160, 320]


@pytest.fixture(params=CASES, ids=["A", "B", "C"])
def case(request):
    """Повторить проверку для каждого набора из методики."""
    return request.param.copy()


@pytest.mark.parametrize("name", ["e_th", "e_max", "sigma0", "w0"])
@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_nonfinite_parameters_rejected(name, bad):
    params = {**CASES[0], "n": 10}
    params[name] = bad
    with pytest.raises(ValueError):
        make_model_data(**params)


@pytest.mark.parametrize("e_th, e_max", [(-1.0, 20.0), (8.0, 8.0), (20.0, 8.0)])
def test_invalid_energy_bounds_rejected(e_th, e_max):
    params = {**CASES[0], "n": 10}
    params.update(e_th=e_th, e_max=e_max)
    with pytest.raises(ValueError):
        make_model_data(**params)


@pytest.mark.parametrize("name", ["sigma0", "w0"])
@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_nonpositive_scales_rejected(name, bad):
    params = {**CASES[0], "n": 10}
    params[name] = bad
    with pytest.raises(ValueError):
        make_model_data(**params)


@pytest.mark.parametrize("bad", [0, 1, -10, 10.5, True, np.nan, np.inf])
def test_invalid_grid_size_rejected(bad):
    # Логическое значение True не задаёт допустимое число интервалов.
    with pytest.raises(ValueError):
        make_model_data(**CASES[0], n=bad)


@pytest.mark.parametrize("n", GRID_SIZES)
def test_energy_grid(case, n):
    energy, sigma, flux = make_model_data(**case, n=n)
    assert energy.shape == sigma.shape == flux.shape == (n + 1,)
    assert np.isfinite(energy).all()
    assert np.all(np.diff(energy) > 0)
    assert energy[0] == pytest.approx(case["e_th"])
    assert energy[-1] == pytest.approx(case["e_max"])
    expected_step = (case["e_max"] - case["e_th"]) / n
    np.testing.assert_allclose(
        np.diff(energy), expected_step, rtol=1e-12, atol=1e-12
    )


@pytest.mark.parametrize("n", GRID_SIZES)
def test_model_values_valid(case, n):
    _, sigma, flux = make_model_data(**case, n=n)
    for values in (sigma, flux):
        assert np.isfinite(values).all()
        assert np.all(values >= 0)


@pytest.mark.parametrize("n", GRID_SIZES)
def test_model_monotonicity(case, n):
    _, sigma, flux = make_model_data(**case, n=n)
    # Для нашей модели сечение строго растёт, а спектр строго убывает.
    assert np.all(np.diff(sigma) > 0)
    assert np.all(np.diff(flux) < 0)


@pytest.mark.parametrize("n", GRID_SIZES)
def test_model_endpoints(case, n):
    _, sigma, flux = make_model_data(**case, n=n)
    assert sigma[0] == pytest.approx(0.0, abs=1e-12)
    assert sigma[-1] == pytest.approx(case["sigma0"], rel=1e-12)
    assert flux[0] == pytest.approx(case["w0"], rel=1e-12)
    assert flux[-1] == pytest.approx(0.0, abs=1e-12)
