"""Проверки входных массивов и нормировки на поток.

Ожидается flux_average(energy, sigma, flux), возвращающая среднее сечение.
Недопустимые входы и интеграл потока должны вызывать ValueError.
"""

import numpy as np
import pytest

from src.artefact6.flux_average import flux_average


def valid_arrays():
    """Допустимый пример, заданный независимо от генератора модели."""
    energy = np.array([8.0, 14.0, 20.0])
    sigma = np.array([0.0, 60.0, 120.0])
    flux = np.array([1.0, 0.5, 0.0])
    return energy, sigma, flux


@pytest.mark.parametrize("array_index", [0, 1, 2])
@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_nonfinite_arrays_rejected(array_index, bad):
    arrays = list(valid_arrays())
    arrays[array_index][1] = bad
    with pytest.raises(ValueError):
        flux_average(*arrays)


@pytest.mark.parametrize("array_index", [1, 2])
def test_negative_values_rejected(array_index):
    arrays = list(valid_arrays())
    arrays[array_index][1] = -1.0
    with pytest.raises(ValueError):
        flux_average(*arrays)


@pytest.mark.parametrize(
    "energy",
    [
        np.array([20.0, 14.0, 8.0]),  # Убывающая энергия.
        np.array([8.0, 8.0, 20.0]),   # Повторяющиеся узлы.
        np.array([8.0, 20.0, 14.0]),  # Нарушен порядок.
    ],
)
def test_unordered_energy_rejected(energy):
    _, sigma, flux = valid_arrays()
    with pytest.raises(ValueError):
        flux_average(energy, sigma, flux)


def test_mismatched_lengths_rejected():
    energy, sigma, flux = valid_arrays()
    with pytest.raises(ValueError):
        flux_average(energy, sigma[:-1], flux)


@pytest.mark.parametrize("flux_level", [0.0, 1e308])
def test_invalid_flux_integral_rejected(flux_level):
    # Поток нулевой либо его интеграл переполняет float64.
    energy = np.array([8.0, 14.0, 20.0])
    sigma = np.ones(3)
    flux = np.full(3, flux_level)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        with pytest.raises(ValueError):
            flux_average(energy, sigma, flux)


def test_constant_cross_section():
    energy = np.linspace(8.0, 20.0, 11)
    sigma = np.full(11, 40.0)
    flux = np.linspace(1.0, 0.0, 11)
    result = flux_average(energy, sigma, flux)
    assert np.isfinite(result)
    assert result == pytest.approx(40.0, rel=1e-10)
