"""
Сборка валидного ICC RGB display-профиля "Rec2020 Gamut with PQ Transfer"
из чисел (numpy + struct), без сторонних .icc файлов и без интернета.

Помимо classic matrix/TRC тегов (desc, cprt, wtpt, rXYZ, gXYZ, bXYZ, rTRC,
gTRC, bTRC — где TRC-кривая — это сэмплированная PQ/SMPTE ST 2084 EOTF),
профиль несёт два ICC v4 тега, без которых Chromium (Chrome/Edge/Brave),
похоже, не даёт изображению реальный HDR-headroom, а просто прогоняет его
через обычную колориметрию (эффект тогда выглядит как "побелело", а не
"светится"):
  - 'cicp' — явный маркер "BT.2020 + PQ" (те же коды, что в видео/AVIF),
    вместо того чтобы рендерер сам угадывал по форме кривой;
  - 'lumi' — абсолютная яркость белой точки профиля в кд/м² (10000, раз
    PCS Y=1.0 у нас соответствует полному диапазону PQ).
"""
from __future__ import annotations

import struct
import numpy as np

from colorimetry import REC2020_TO_XYZ_D50, ICC_PCS_WHITE_XYZ, pq_eotf

_TAG_COUNT_CURVE_ENTRIES = 1024


def _pad4(data: bytes) -> bytes:
    pad = (-len(data)) % 4
    return data + b"\x00" * pad


def _s15Fixed16(value: float) -> bytes:
    return struct.pack(">i", int(round(value * 65536)))


def _tag_XYZ(xyz: np.ndarray) -> bytes:
    body = b"XYZ " + b"\x00\x00\x00\x00"
    body += _s15Fixed16(xyz[0]) + _s15Fixed16(xyz[1]) + _s15Fixed16(xyz[2])
    return body


def _tag_text(ascii_str: str) -> bytes:
    raw = ascii_str.encode("ascii") + b"\x00"
    body = b"text" + b"\x00\x00\x00\x00" + raw
    return body


def _tag_desc(ascii_str: str) -> bytes:
    raw = ascii_str.encode("ascii") + b"\x00"
    body = b"desc" + b"\x00\x00\x00\x00"
    body += struct.pack(">I", len(raw)) + raw
    body += struct.pack(">I", 0)  # unicode language code
    body += struct.pack(">I", 0)  # unicode count (no unicode string)
    body += struct.pack(">H", 0)  # scriptcode code
    body += struct.pack(">B", 0)  # macintosh description count
    body += b"\x00" * 67  # macintosh description (fixed size, unused)
    return body


def _tag_cicp(colour_primaries: int, transfer_characteristics: int,
              matrix_coefficients: int = 0, full_range: int = 1) -> bytes:
    # ITU-T H.273 code points, как в AVIF/HEVC: 9=BT.2020, 16=SMPTE ST 2084 (PQ)
    body = b"cicp" + b"\x00\x00\x00\x00"
    body += struct.pack(
        ">BBBB", colour_primaries, transfer_characteristics,
        matrix_coefficients, full_range,
    )
    return body


def _tag_curve_pq(n: int = _TAG_COUNT_CURVE_ENTRIES) -> bytes:
    # code (device, 0..1) -> linear light (PCS-relative, 0..1 == 0..10000 nit)
    codes = np.linspace(0.0, 1.0, n)
    linear = pq_eotf(codes)
    samples = np.clip(np.round(linear * 65535.0), 0, 65535).astype(">u2")
    body = b"curv" + b"\x00\x00\x00\x00" + struct.pack(">I", n) + samples.tobytes()
    return body


def build_rec2020_pq_icc_profile(
    description: str = "Rec2020 Gamut with PQ Transfer",
    copyright_str: str = "No rights reserved",
) -> bytes:
    """Возвращает байты готового ICC-профиля."""
    r_xyz = REC2020_TO_XYZ_D50 @ np.array([1.0, 0.0, 0.0])
    g_xyz = REC2020_TO_XYZ_D50 @ np.array([0.0, 1.0, 0.0])
    b_xyz = REC2020_TO_XYZ_D50 @ np.array([0.0, 0.0, 1.0])

    curve = _tag_curve_pq()

    tags: list[tuple[bytes, bytes]] = [
        (b"desc", _tag_desc(description)),
        (b"cprt", _tag_text(copyright_str)),
        (b"wtpt", _tag_XYZ(ICC_PCS_WHITE_XYZ)),
        (b"rXYZ", _tag_XYZ(r_xyz)),
        (b"gXYZ", _tag_XYZ(g_xyz)),
        (b"bXYZ", _tag_XYZ(b_xyz)),
        (b"rTRC", curve),
        (b"gTRC", curve),
        (b"bTRC", curve),
        (b"cicp", _tag_cicp(colour_primaries=9, transfer_characteristics=16)),
        (b"lumi", _tag_XYZ(np.array([0.0, 10000.0, 0.0]))),
    ]

    header_size = 128
    tag_table_size = 4 + 12 * len(tags)
    data_start = header_size + tag_table_size

    tag_table_entries = []
    data_blob = b""
    offset = data_start
    for sig, data in tags:
        padded = _pad4(data)
        tag_table_entries.append((sig, offset, len(data)))
        data_blob += padded
        offset += len(padded)

    total_size = data_start + len(data_blob)

    header = b""
    header += struct.pack(">I", total_size)          # profile size
    header += b"GLOW"                                 # CMM type (informational)
    header += struct.pack(">I", 0x04400000)            # version 4.4.0.0 (cicp requires v4)
    header += b"mntr"                                  # device class: display
    header += b"RGB "                                  # colour space
    header += b"XYZ "                                  # PCS
    header += struct.pack(">HHHHHH", 2026, 1, 1, 0, 0, 0)  # date/time
    header += b"acsp"                                  # signature
    header += b"\x00\x00\x00\x00"                       # primary platform
    header += struct.pack(">I", 0)                     # flags
    header += b"\x00\x00\x00\x00"                       # device manufacturer
    header += b"\x00\x00\x00\x00"                       # device model
    header += struct.pack(">Q", 0)                     # device attributes
    header += struct.pack(">I", 1)                      # rendering intent: relative colorimetric
    header += _s15Fixed16(0.9642) + _s15Fixed16(1.0000) + _s15Fixed16(0.8249)  # PCS illuminant D50
    header += b"\x00\x00\x00\x00"                       # profile creator
    header += b"\x00" * 16                              # profile ID (unset)
    header += b"\x00" * 28                              # reserved
    assert len(header) == 128, len(header)

    tag_table = struct.pack(">I", len(tags))
    for sig, off, size in tag_table_entries:
        tag_table += sig + struct.pack(">II", off, size)

    profile = header + tag_table + data_blob
    assert len(profile) == total_size, (len(profile), total_size)
    return profile


if __name__ == "__main__":
    data = build_rec2020_pq_icc_profile()
    out = "rec2020_pq.icc"
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {out}: {len(data)} bytes")
