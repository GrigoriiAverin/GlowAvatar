"""
Чистая математика и экспорт: sRGB -> Rec.2020 -> PQ-код, с учётом маски
свечения (0..1 на пиксель, поддерживает градиенты), плюс сохранение
готового Progressive JPEG со встроенным Rec2020-PQ ICC-профилем.

Никакого UI здесь нет — это отдельно тестируемый модуль.
"""
from __future__ import annotations

import io
import numpy as np
from PIL import Image

from colorimetry import (
    SRGB_LINEAR_TO_REC2020_LINEAR,
    SDR_REFERENCE_WHITE_NITS,
    PQ_MAX_NITS,
    srgb_eotf,
    pq_inverse_eotf,
)
from icc_profile import build_rec2020_pq_icc_profile

# Профиль строится один раз при импорте модуля (не зависит от изображения)
REC2020_PQ_ICC_BYTES = build_rec2020_pq_icc_profile()


def load_square_srgb_uint8(path: str, working_size: int) -> np.ndarray:
    """Открывает фото, центрирует по квадрату и уменьшает до working_size."""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((working_size, working_size), Image.LANCZOS)
    return np.asarray(img, dtype=np.uint8)


def compute_pq_rec2020_code(
    srgb_uint8: np.ndarray, mask: np.ndarray, stops: float, whiten: float = 0.0
) -> np.ndarray:
    """
    srgb_uint8: HxWx3 uint8, обычное sRGB-фото.
    mask: HxW float32 в [0,1] — 0 = обычный SDR-белый (203 нит), 1 = пик
          свечения (203 * 2**stops нит); промежуточные значения — градиент.
    whiten: 0..1 — насколько подмешивать белый к цвету в самых "горячих"
          (mask≈1) зонах. 0 = цвет не трогаем (только поднимаем яркость),
          1 = на пике маски цвет полностью уходит в белый. Насыщенный цвет
          физически не может светиться так же ярко, как белый (у HDR-панелей
          ниже пиковая яркость для чистого цвета, чем для белого) — подмешивание
          белого даёт более честный и заметный на экране эффект.
    Возвращает HxWx3 uint8 — код, который надо просто сохранить как пиксели
    JPEG со встроенным Rec2020-PQ ICC-профилем.
    """
    mask = np.clip(mask, 0.0, 1.0)
    srgb_linear = srgb_eotf(srgb_uint8.astype(np.float64) / 255.0)

    if whiten > 0.0:
        blend = (mask * whiten)[..., None]
        srgb_linear = srgb_linear * (1.0 - blend) + blend

    rec2020_linear = srgb_linear @ SRGB_LINEAR_TO_REC2020_LINEAR.T

    scale = 2.0 ** (stops * mask)  # HxW
    boosted = rec2020_linear * scale[..., None]

    abs_nits = boosted * SDR_REFERENCE_WHITE_NITS
    y_rel = np.clip(abs_nits / PQ_MAX_NITS, 0.0, 1.0)

    code = pq_inverse_eotf(y_rel)
    return np.clip(np.round(code * 255.0), 0, 255).astype(np.uint8)


def target_nits_for_mask_value(mask_value: float, stops: float) -> float:
    """Для UI-подсказки: во сколько нит превратится данное значение маски."""
    return SDR_REFERENCE_WHITE_NITS * (2.0 ** (stops * mask_value))


def resize_mask(mask: np.ndarray, size: int) -> np.ndarray:
    if mask.shape[0] == size and mask.shape[1] == size:
        return mask
    img = Image.fromarray(np.clip(mask * 255.0, 0, 255).astype(np.uint8))
    img = img.resize((size, size), Image.BILINEAR)
    return np.asarray(img, dtype=np.float32) / 255.0


def export_glow_jpeg(
    out_path: str,
    srgb_uint8: np.ndarray,
    mask: np.ndarray,
    stops: float,
    output_size: int,
    quality: int = 95,
    whiten: float = 0.0,
) -> None:
    """Ресайзит фото+маску до output_size и сохраняет готовый glow-JPEG."""
    src_img = Image.fromarray(srgb_uint8)
    if src_img.size != (output_size, output_size):
        src_img = src_img.resize((output_size, output_size), Image.LANCZOS)
    resized_srgb = np.asarray(src_img, dtype=np.uint8)
    resized_mask = resize_mask(mask, output_size)

    code = compute_pq_rec2020_code(resized_srgb, resized_mask, stops, whiten)
    out_img = Image.fromarray(code)
    out_img.save(
        out_path,
        format="JPEG",
        quality=quality,
        progressive=True,
        icc_profile=REC2020_PQ_ICC_BYTES,
    )


def render_mask_overlay(
    srgb_uint8: np.ndarray, mask: np.ndarray, color=(0, 200, 255), alpha: float = 0.55
) -> np.ndarray:
    """Для превью в GUI: фото с полупрозрачной подсветкой маски поверх."""
    base = srgb_uint8.astype(np.float32)
    tint = np.array(color, dtype=np.float32)
    a = (np.clip(mask, 0.0, 1.0) * alpha)[..., None]
    out = base * (1 - a) + tint[None, None, :] * a
    return np.clip(out, 0, 255).astype(np.uint8)


def smoothstep(edge0: float, edge1: float, x: np.ndarray) -> np.ndarray:
    if edge0 == edge1:
        return np.where(x < edge0, 0.0, 1.0)
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _flood_fill(within_tolerance: np.ndarray, seed_y: int, seed_x: int) -> np.ndarray:
    """
    4-связный заливочный флуд по булевой маске "похожий цвет" (within_tolerance),
    от точки (seed_y, seed_x). Векторизовано через итеративную дилатацию —
    без ручного Python-цикла по пикселям и без scipy.
    """
    filled = np.zeros_like(within_tolerance)
    filled[seed_y, seed_x] = True
    if not within_tolerance[seed_y, seed_x]:
        return filled
    while True:
        grown = filled.copy()
        grown[1:, :] |= filled[:-1, :]
        grown[:-1, :] |= filled[1:, :]
        grown[:, 1:] |= filled[:, :-1]
        grown[:, :-1] |= filled[:, 1:]
        grown &= within_tolerance
        if np.array_equal(grown, filled):
            return grown
        filled = grown


def magic_wand_mask(
    srgb_uint8: np.ndarray, seed_x: int, seed_y: int, tolerance: float, feather: float
) -> np.ndarray:
    """
    "Волшебная палочка": от клика (seed_x, seed_y) выделяет связную область
    похожего цвета (Евклидово расстояние в sRGB 0..255).
    tolerance — макс. допустимое расстояние (граница выделения).
    feather — доля [0,1] от tolerance, отведённая под мягкий (антиалиасинг) край.
    Возвращает HxW float32 маску 0..1 (внутри области, плавно спадающую к краю;
    везде вне связной области — строго 0, т.е. другие похожие по цвету, но
    не связанные с точкой клика участки не попадают в выделение).
    """
    seed_color = srgb_uint8[seed_y, seed_x].astype(np.float32)
    dist = np.linalg.norm(srgb_uint8.astype(np.float32) - seed_color[None, None, :], axis=-1)

    tol_outer = max(tolerance, 1e-3)
    tol_inner = tol_outer * (1.0 - np.clip(feather, 0.0, 1.0))
    closeness = 1.0 - smoothstep(tol_inner, tol_outer, dist)

    within = dist <= tol_outer
    connected = _flood_fill(within, seed_y, seed_x)

    return np.where(connected, closeness, 0.0).astype(np.float32)
