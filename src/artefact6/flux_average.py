"""[порождено] Усреднение сечения по спектру фотонов методом трапеций.

Энергия задается в МэВ, сечение — в мб. В make_model_data параметр
е_max (в коде e_max) соответствует верхней границе E^m из METHOD.md.
Функции не читают и не записывают файлы, не используют случайность
и не изменяют переданные массивы. Недопустимые данные дают ValueError.
"""

from numbers import Integral, Real

import numpy as np


__all__ = ["make_model_data", "trapezoid_integral", "flux_average"]


def _finite_scalar(value, name):
    """Проверить действительный числовой параметр и привести к float."""
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name}: ожидается действительное число")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name}: число не представимо в float64") from exc
    if not np.isfinite(result):
        raise ValueError(f"{name}: NaN и бесконечности недопустимы")
    return result


def _finite_vector(values, name):
    """Проверить одномерный массив, пригодный для интегрирования."""
    try:
        raw = np.asarray(values)
        if np.iscomplexobj(raw):
            raise ValueError("Комплексные значения недопустимы")
        result = np.asarray(raw, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name}: ожидается массив действительных чисел") from exc
    if result.ndim != 1 or result.size < 2:
        raise ValueError(f"{name}: нужен одномерный массив длиной не меньше 2")
    if not np.isfinite(result).all():
        raise ValueError(f"{name}: NaN и бесконечности недопустимы")
    return result


def make_model_data(e_th, e_max, sigma0, w0, n):
    """Построить сетку и линейные модельные сечение и спектр.

    n — число интервалов; возвращаются n+1 узлов energy и массивы
    sigma = sigma0*x, flux = w0*(1-x), где x меняется от 0 до 1.
    """
    e_th = _finite_scalar(e_th, "e_th")
    e_max = _finite_scalar(e_max, "e_max")
    sigma0 = _finite_scalar(sigma0, "sigma0")
    w0 = _finite_scalar(w0, "w0")
    if not 0 <= e_th < e_max:
        raise ValueError("Требуется 0 <= e_th < e_max")
    if sigma0 <= 0 or w0 <= 0:
        raise ValueError("sigma0 и w0 должны быть положительными")
    if isinstance(n, (bool, np.bool_)) or not isinstance(n, Integral) or n < 2:
        raise ValueError("n должно быть целым числом не меньше 2")

    energy = np.linspace(e_th, e_max, int(n) + 1, dtype=np.float64)
    # Слишком близкие энергии могут совпасть после округления float64.
    if not np.isfinite(energy).all() or not np.all(np.diff(energy) > 0):
        raise ValueError("Невозможно построить строго возрастающую сетку float64")
    x = (energy - e_th) / (e_max - e_th)
    sigma = sigma0 * x
    flux = w0 * (1.0 - x)
    if not np.isfinite(sigma).all() or not np.isfinite(flux).all():
        raise ValueError("Модельные значения должны быть конечными")
    if np.any(sigma < 0) or np.any(flux < 0):
        raise ValueError("Модельные значения должны быть неотрицательными")
    return energy, sigma, flux


def trapezoid_integral(energy, values):
    """Вычислить интеграл как сумму площадей трапеций между узлами.

    Поддерживается любая строго возрастающая энергетическая сетка.
    В эксперименте используются равномерные сетки. Значения функции
    могут иметь любой знак; проверка физического смысла выполняется выше.
    """
    energy = _finite_vector(energy, "energy")
    values = _finite_vector(values, "values")
    if energy.shape != values.shape:
        raise ValueError("Массивы энергии и значений должны иметь одинаковую длину")
    if np.any(energy < 0):
        raise ValueError("Энергия фотона не может быть отрицательной")
    try:
        with np.errstate(over="raise", invalid="raise"):
            widths = np.diff(energy)
            if not np.all(widths > 0):
                raise ValueError("Энергетические узлы должны строго возрастать")
            # Полусумма значений на концах, умноженная на ширину интервала.
            # Деление до сложения уменьшает риск промежуточного переполнения.
            heights = 0.5 * values[:-1] + 0.5 * values[1:]
            result = float(np.sum(widths * heights, dtype=np.float64))
    except FloatingPointError as exc:
        raise ValueError("Переполнение при вычислении интеграла") from exc
    if not np.isfinite(result):
        raise ValueError("Интеграл должен быть конечным")
    return result


def flux_average(energy, sigma, flux):
    """Вычислить интеграл sigma*flux, нормированный на интеграл flux.

    Возвращает среднее в единицах входного сечения. Нормировка нужна,
    чтобы результат зависел от формы спектра, а не его общего масштаба.
    """
    energy = _finite_vector(energy, "energy")
    sigma = _finite_vector(sigma, "sigma")
    flux = _finite_vector(flux, "flux")
    if not energy.shape == sigma.shape == flux.shape:
        raise ValueError("Массивы энергии, сечения и спектра должны иметь одинаковую длину")
    if np.any(sigma < 0) or np.any(flux < 0):
        raise ValueError("Сечение и спектр должны быть неотрицательными")

    denominator = trapezoid_integral(energy, flux)
    if denominator <= 0:
        raise ValueError("Интеграл потока должен быть положительным")
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            weighted_sigma = sigma * flux
            numerator = trapezoid_integral(energy, weighted_sigma)
            result = float(np.divide(numerator, denominator))
    except FloatingPointError as exc:
        raise ValueError("Переполнение при вычислении среднего сечения") from exc
    if not np.isfinite(result):
        raise ValueError("Среднее сечение должно быть конечным")
    return result
