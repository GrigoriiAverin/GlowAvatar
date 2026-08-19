"""
Общая цветовая математика: хроматичности -> XYZ, матрицы RGB<->XYZ,
адаптация Бредфорда D65->D50, и PQ (SMPTE ST 2084) OETF/EOTF.

Все константы стандартизованы (ITU-R BT.709/BT.2020, ICC PCS illuminant D50,
SMPTE ST 2084) — ничего не скачивается из интернета.
"""
from __future__ import annotations

import numpy as np

# --- Хроматичности (x, y) стандартных первичных цветов и белых точек ---

SRGB_PRIMARIES = {
    "R": (0.6400, 0.3300),
    "G": (0.3000, 0.6000),
    "B": (0.1500, 0.0600),
}
REC2020_PRIMARIES = {
    "R": (0.7080, 0.2920),
    "G": (0.1700, 0.7970),
    "B": (0.1310, 0.0460),
}
WHITE_D65 = (0.3127, 0.3290)
WHITE_D50 = (0.3457, 0.3585)  # для справки/проверки; ICC PCS white ниже как XYZ

# Официальная PCS-точка белого ICC (D50), s15Fixed16 0x0000F6D6, 0x00010000, 0x0000D32D
ICC_PCS_WHITE_XYZ = np.array([0.9642, 1.0000, 0.8249])

# Матрица адаптации Бредфорда (стандартная, используется во всех сколько-нибудь
# распространённых построителях ICC-профилей)
_BRADFORD_M = np.array([
    [0.8951, 0.2664, -0.1614],
    [-0.7502, 1.7135, 0.0367],
    [0.0389, -0.0685, 1.0296],
])
_BRADFORD_M_INV = np.linalg.inv(_BRADFORD_M)


def xy_to_XYZ(xy: tuple[float, float], Y: float = 1.0) -> np.ndarray:
    """Хроматичность (x, y) -> XYZ при заданной яркости Y."""
    x, y = xy
    X = (x / y) * Y
    Z = ((1 - x - y) / y) * Y
    return np.array([X, Y, Z])


def bradford_adaptation_matrix(src_white_xy, dst_white_xy) -> np.ndarray:
    """3x3 матрица хроматической адаптации Бредфорда src -> dst."""
    src_XYZ = xy_to_XYZ(src_white_xy)
    dst_XYZ = xy_to_XYZ(dst_white_xy)
    src_cone = _BRADFORD_M @ src_XYZ
    dst_cone = _BRADFORD_M @ dst_XYZ
    ratio = dst_cone / src_cone
    return _BRADFORD_M_INV @ np.diag(ratio) @ _BRADFORD_M


def rgb_to_xyz_matrix(primaries: dict, white_xy) -> np.ndarray:
    """
    Строит матрицу "линейный RGB -> XYZ" для заданных первичных цветов и белой
    точки (тот же метод, которым построены sRGB.icc/AdobeRGB.icc и т.п.):
    P = [Xr Xg Xb; Yr Yg Yb; Zr Zg Zb] (при Y=1 на каждый первичный),
    S = P^-1 * W, M = P * diag(S).
    """
    P = np.column_stack([xy_to_XYZ(primaries[c], 1.0) for c in ("R", "G", "B")])
    W = xy_to_XYZ(white_xy, 1.0)
    S = np.linalg.solve(P, W)
    return P * S  # эквивалент P @ diag(S)


# --- Готовые матрицы, выведенные из первичных цветов (не из "магических" чисел) ---

SRGB_TO_XYZ_D65 = rgb_to_xyz_matrix(SRGB_PRIMARIES, WHITE_D65)
REC2020_TO_XYZ_D65 = rgb_to_xyz_matrix(REC2020_PRIMARIES, WHITE_D65)

# Линейный sRGB -> линейный Rec.2020 (обе цветовые системы D65, доп. адаптация
# белой точки не нужна)
SRGB_LINEAR_TO_REC2020_LINEAR = np.linalg.inv(REC2020_TO_XYZ_D65) @ SRGB_TO_XYZ_D65

# Матрица Rec.2020(D65) -> XYZ(D50), нужна для ICC-профиля (PCS = D50)
_D65_TO_D50 = bradford_adaptation_matrix(WHITE_D65, WHITE_D50)
REC2020_TO_XYZ_D50 = _D65_TO_D50 @ REC2020_TO_XYZ_D65

# Сверка: адаптированная белая точка Rec.2020 должна совпасть с официальной
# PCS-точкой ICC (0.9642, 1.0000, 0.8249)
_white_check = REC2020_TO_XYZ_D50 @ np.array([1.0, 1.0, 1.0])
assert np.allclose(_white_check, ICC_PCS_WHITE_XYZ, atol=2e-4), _white_check


# --- sRGB EOTF (декодирование гаммы) ---

def srgb_eotf(encoded: np.ndarray) -> np.ndarray:
    """sRGB (0..1) -> линейный свет (0..1)."""
    a = 0.055
    return np.where(
        encoded <= 0.04045,
        encoded / 12.92,
        ((encoded + a) / (1 + a)) ** 2.4,
    )


# --- PQ / SMPTE ST 2084 (ITU-R BT.2100 Table 4), точные стандартные константы ---

_PQ_M1 = 2610.0 / 16384.0
_PQ_M2 = 2523.0 / 4096.0 * 128.0
_PQ_C1 = 3424.0 / 4096.0
_PQ_C2 = 2413.0 / 4096.0 * 32.0
_PQ_C3 = 2392.0 / 4096.0 * 32.0


def pq_eotf(code: np.ndarray) -> np.ndarray:
    """PQ код (0..1) -> линейная яркость, относительно 10000 нит (0..1)."""
    code = np.clip(code, 0.0, 1.0)
    e_pow = code ** (1.0 / _PQ_M2)
    num = np.maximum(e_pow - _PQ_C1, 0.0)
    den = _PQ_C2 - _PQ_C3 * e_pow
    return (num / den) ** (1.0 / _PQ_M1)


def pq_inverse_eotf(linear_rel_10000: np.ndarray) -> np.ndarray:
    """Линейная яркость относительно 10000 нит (0..1) -> PQ код (0..1)."""
    y = np.clip(linear_rel_10000, 0.0, 1.0)
    y_pow = y ** _PQ_M1
    num = _PQ_C1 + _PQ_C2 * y_pow
    den = 1.0 + _PQ_C3 * y_pow
    return (num / den) ** _PQ_M2


# Опорная яркость SDR-белого при показе SDR-контента в HDR-контейнере
# (ITU-R BT.2408-4, раздел про "SDR reference white in HDR")
SDR_REFERENCE_WHITE_NITS = 203.0
PQ_MAX_NITS = 10000.0
