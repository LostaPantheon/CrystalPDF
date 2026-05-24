from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from uuid import uuid4

import cv2
import fitz
import numpy as np
from PIL import Image, ImageEnhance


ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True)
class CleanSettings:
    mode: str = "standard"
    dpi: int = 300
    denoise: int = 12
    dot_area: int = 25
    threshold_block: int = 25
    threshold_c: int = 12
    brightness: int = 0
    contrast: int = 100
    clean_edges: bool = True
    edge_margin: int = 5
    edge_threshold: int = 60
    deskew: bool = True
    max_angle: float = 8.0
    preserve_first: bool = False
    preserve_last: bool = False
    sharpen_text: bool = True


PRESETS: dict[str, CleanSettings] = {
    "gentle": CleanSettings(
        mode="gentle",
        denoise=6,
        dot_area=10,
        threshold_block=25,
        threshold_c=12,
    ),
    "standard": CleanSettings(),
    "strong": CleanSettings(
        mode="strong",
        denoise=20,
        dot_area=40,
        threshold_block=25,
        threshold_c=12,
    ),
}


def preset(mode: str) -> CleanSettings:
    return PRESETS.get(mode, PRESETS["standard"])


def clean_pdf(
    input_path: str | Path,
    output_path: str | Path,
    settings: CleanSettings,
    progress: ProgressCallback | None = None,
) -> None:
    input_path = Path(input_path)
    output_path = Path(output_path)
    if input_path.resolve() == output_path.resolve():
        raise ValueError("Нельзя сохранять результат поверх исходного PDF")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output_path.with_name(f"{output_path.stem}.tmp-{uuid4().hex}.pdf")

    source = fitz.open(str(input_path))
    target = fitz.open()

    try:
        total = len(source)
        for page_index in range(total):
            page = source.load_page(page_index)
            page_number = page_index + 1
            skip = (
                (page_index == 0 and settings.preserve_first)
                or (page_index == total - 1 and settings.preserve_last)
            )

            if skip:
                target.insert_pdf(source, from_page=page_index, to_page=page_index)
                if progress:
                    progress(page_number, total, f"Страница {page_number}: оставлена без изменений")
                continue

            edge_clean = settings.clean_edges and page_index not in (0, total - 1)
            gray = render_page_gray(page, settings.dpi)
            cleaned = clean_page_image(gray, replace(settings, clean_edges=edge_clean))
            add_image_page(target, page.rect, cleaned)

            if progress:
                progress(page_number, total, f"Страница {page_number} очищена")

        target.save(str(temp_output), garbage=4, deflate=True, clean=True)
        temp_output.replace(output_path)
    finally:
        target.close()
        source.close()
        if temp_output.exists():
            temp_output.unlink()


def render_page_gray(page: fitz.Page, dpi: int) -> np.ndarray:
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)

    if pix.n == 1:
        return arr.copy()
    if pix.n == 3:
        return cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    if pix.n == 4:
        return cv2.cvtColor(arr, cv2.COLOR_RGBA2GRAY)
    return arr[:, :, 0].copy()


def clean_page_image(gray: np.ndarray, settings: CleanSettings) -> np.ndarray:
    gray = _ensure_uint8(gray)
    gray = _adjust_gray(gray, settings.brightness, settings.contrast)

    if settings.deskew:
        gray = _deskew(gray, settings.max_angle)

    if settings.denoise > 0:
        gray = cv2.fastNlMeansDenoising(gray, h=int(settings.denoise))

    block = _odd_at_least(settings.threshold_block, 25)
    binary = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        block,
        int(settings.threshold_c),
    )

    kernel = np.ones((2, 2), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)

    binary = _remove_speckles_balanced(binary, settings.dot_area)

    if settings.clean_edges:
        binary = _clean_edges(binary, settings.edge_margin, settings.edge_threshold)

    cleaned_img = Image.fromarray(binary).convert("L")
    cleaned_img = ImageEnhance.Contrast(cleaned_img).enhance(1.4)

    return np.array(cleaned_img, dtype=np.uint8)


def add_image_page(target: fitz.Document, page_rect: fitz.Rect, image: np.ndarray) -> None:
    ok, encoded = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    if not ok:
        raise RuntimeError("Не удалось подготовить изображение страницы для PDF")

    page = target.new_page(width=page_rect.width, height=page_rect.height)
    page.insert_image(page.rect, stream=encoded.tobytes())


def render_preview(input_path: str | Path, settings: CleanSettings, page_index: int = 0) -> tuple[Image.Image, Image.Image]:
    preview_settings = replace(settings, dpi=min(settings.dpi, 180))
    doc = fitz.open(str(input_path))
    try:
        if len(doc) == 0:
            raise ValueError("В PDF нет страниц")
        page_index = max(0, min(page_index, len(doc) - 1))
        page = doc.load_page(page_index)
        before = render_page_gray(page, preview_settings.dpi)
        edge_clean = preview_settings.clean_edges and page_index not in (0, len(doc) - 1)
        after = clean_page_image(before, replace(preview_settings, clean_edges=edge_clean))
        return Image.fromarray(before).convert("L"), Image.fromarray(after).convert("L")
    finally:
        doc.close()


def make_thumbnail(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    thumb = image.copy()
    thumb.thumbnail(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, "#F3F4F6")
    x = (size[0] - thumb.width) // 2
    y = (size[1] - thumb.height) // 2
    canvas.paste(thumb.convert("RGB"), (x, y))
    return canvas


def _ensure_uint8(gray: np.ndarray) -> np.ndarray:
    if gray.dtype == np.uint8:
        return gray.copy()
    return np.clip(gray, 0, 255).astype(np.uint8)


def _odd_at_least(value: int, minimum: int) -> int:
    value = max(int(value), minimum)
    return value if value % 2 == 1 else value + 1


def _adjust_gray(gray: np.ndarray, brightness: int, contrast: int) -> np.ndarray:
    alpha = max(0.25, float(contrast) / 100.0)
    beta = int(brightness)
    if abs(alpha - 1.0) < 0.001 and beta == 0:
        return gray
    return cv2.convertScaleAbs(gray, alpha=alpha, beta=beta)


def _normalize_background(gray: np.ndarray) -> np.ndarray:
    h, w = gray.shape[:2]
    kernel = max(31, int(min(h, w) / 28))
    kernel = _odd_at_least(kernel, 31)
    background = cv2.GaussianBlur(gray, (kernel, kernel), 0)
    normalized = cv2.divide(gray, background, scale=255)
    normalized = cv2.normalize(normalized, None, 0, 255, cv2.NORM_MINMAX)
    return normalized.astype(np.uint8)


def _repair_text(binary: np.ndarray) -> np.ndarray:
    # Очень лёгкое замыкание соединяет разорванные штрихи машинописного текста,
    # не раздувая грязь на странице.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 2))
    return cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=1)


def _remove_speckles_balanced(binary: np.ndarray, dot_limit: int) -> np.ndarray:
    result = binary.copy()
    inv = cv2.bitwise_not(result)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(inv, connectivity=8)
    area_limit = max(1, int(dot_limit))

    for idx in range(1, num_labels):
        area = int(stats[idx, cv2.CC_STAT_AREA])
        if area < area_limit:
            result[labels == idx] = 255

    return result


def _remove_speckles(binary: np.ndarray, settings: CleanSettings) -> np.ndarray:
    inv = cv2.bitwise_not(binary)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(inv, connectivity=8)
    result = binary.copy()
    h, w = binary.shape[:2]

    area_limit = max(4, int(settings.dot_area))
    max_dim = max(5, int(np.sqrt(area_limit) * 3.0))
    margin = min(max(settings.edge_margin, 0), h // 5, w // 5)
    edge_dot_margin = min(max(12, (margin * 2) // 3), 32)
    text_bands = _detect_text_bands(inv)

    for idx in range(1, num_labels):
        x = int(stats[idx, cv2.CC_STAT_LEFT])
        y = int(stats[idx, cv2.CC_STAT_TOP])
        bw = int(stats[idx, cv2.CC_STAT_WIDTH])
        bh = int(stats[idx, cv2.CC_STAT_HEIGHT])
        area = int(stats[idx, cv2.CC_STAT_AREA])

        if area <= 2:
            result[labels == idx] = 255
            continue

        near_edge = (
            x < edge_dot_margin
            or y < edge_dot_margin
            or (x + bw) > (w - edge_dot_margin)
            or (y + bh) > (h - edge_dot_margin)
        )
        small_shape = area <= area_limit and bw <= max_dim and bh <= max_dim

        if not small_shape:
            continue

        if near_edge:
            result[labels == idx] = 255
            continue

        if _looks_like_text_punctuation(inv, labels, idx, x, y, bw, bh, area, text_bands, max_dim):
            continue

        result[labels == idx] = 255

    return result


def _detect_text_bands(inv: np.ndarray) -> list[tuple[int, int, int, int]]:
    h, w = inv.shape[:2]
    kernel_w = max(18, min(90, w // 45))
    kernel_h = max(2, min(8, h // 420))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, kernel_h))
    grouped = cv2.dilate(inv, kernel, iterations=1)
    num, _, stats, _ = cv2.connectedComponentsWithStats(grouped, connectivity=8)
    bands: list[tuple[int, int, int, int]] = []

    for idx in range(1, num):
        x = int(stats[idx, cv2.CC_STAT_LEFT])
        y = int(stats[idx, cv2.CC_STAT_TOP])
        bw = int(stats[idx, cv2.CC_STAT_WIDTH])
        bh = int(stats[idx, cv2.CC_STAT_HEIGHT])
        area = int(stats[idx, cv2.CC_STAT_AREA])

        if bw < w * 0.08 or area < 80:
            continue
        if bh > h * 0.12:
            continue

        pad_y = max(3, bh // 3)
        pad_x = max(12, kernel_w)
        bands.append((
            max(0, x - pad_x),
            max(0, y - pad_y),
            min(w, x + bw + pad_x),
            min(h, y + bh + pad_y),
        ))

    bands.sort(key=lambda b: (b[1], b[0]))
    return _merge_text_bands(bands)


def _merge_text_bands(bands: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    merged: list[tuple[int, int, int, int]] = []
    for band in bands:
        if not merged:
            merged.append(band)
            continue
        px0, py0, px1, py1 = merged[-1]
        x0, y0, x1, y1 = band
        if y0 <= py1:
            merged[-1] = (min(px0, x0), min(py0, y0), max(px1, x1), max(py1, y1))
        else:
            merged.append(band)
    return merged


def _looks_like_text_punctuation(
    inv: np.ndarray,
    labels: np.ndarray,
    label_id: int,
    x: int,
    y: int,
    width: int,
    height: int,
    area: int,
    bands: list[tuple[int, int, int, int]],
    max_dim: int,
) -> bool:
    cx = x + width / 2.0
    cy = y + height / 2.0

    for bx0, by0, bx1, by1 in bands:
        if not (bx0 <= cx <= bx1 and by0 <= cy <= by1):
            continue

        radius = max(24, int(max_dim * 3.5))
        rx0 = max(bx0, x - radius)
        rx1 = min(bx1, x + width + radius)
        ry0 = by0
        ry1 = by1

        context = inv[ry0:ry1, rx0:rx1]
        nearby_black = int(np.count_nonzero(context))
        nearby_other = nearby_black - area

        left = inv[ry0:ry1, max(rx0, x - radius):x]
        right = inv[ry0:ry1, x + width:min(rx1, x + width + radius)]
        has_left_or_right_text = int(np.count_nonzero(left)) > 8 or int(np.count_nonzero(right)) > 8

        return nearby_other >= max(14, area) and has_left_or_right_text

    return False


def edge_artifact_mask(dark_mask: np.ndarray, margin: int, sensitivity: int = 60) -> np.ndarray:
    dark = np.asarray(dark_mask, dtype=bool)
    h, w = dark.shape[:2]
    remove = np.zeros((h, w), dtype=bool)
    if h <= 0 or w <= 0:
        return remove

    margin = min(max(1, int(margin)), max(1, min(h, w) // 3))
    sensitivity = max(1, min(240, int(sensitivity)))
    density_limit = max(0.006, min(0.12, 0.035 - (sensitivity - 60) / 6000.0))

    def wipe_dense_groups(values: np.ndarray, offset: int, axis: str) -> None:
        dense = values >= density_limit
        start = None
        max_group = max(3, min(24, margin // 2))
        expand = max(1, min(4, margin // 24))
        for pos, is_dense in enumerate(np.r_[dense, False]):
            if is_dense and start is None:
                start = pos
            elif not is_dense and start is not None:
                end = pos
                if 1 <= end - start <= max_group:
                    a = max(0, offset + start - expand)
                    b = min((w if axis == "x" else h), offset + end + expand)
                    if axis == "x":
                        remove[:, a:b] = True
                    else:
                        remove[a:b, :] = True
                start = None

    wipe_dense_groups(np.mean(dark[:, :margin], axis=0), 0, "x")
    wipe_dense_groups(np.mean(dark[:, w - margin :], axis=0), w - margin, "x")
    wipe_dense_groups(np.mean(dark[:margin, :], axis=1), 0, "y")
    wipe_dense_groups(np.mean(dark[h - margin :, :], axis=1), h - margin, "y")

    dark_u8 = dark.astype(np.uint8)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(dark_u8, connectivity=8)
    min_v_len = max(45, int(h * 0.018))
    min_h_len = max(45, int(w * 0.018))
    max_line_thick = max(8, min(26, margin // 3))

    for idx in range(1, num_labels):
        x = int(stats[idx, cv2.CC_STAT_LEFT])
        y = int(stats[idx, cv2.CC_STAT_TOP])
        bw = int(stats[idx, cv2.CC_STAT_WIDTH])
        bh = int(stats[idx, cv2.CC_STAT_HEIGHT])
        area = int(stats[idx, cv2.CC_STAT_AREA])
        inside_margin = x < margin or y < margin or (x + bw) > w - margin or (y + bh) > h - margin
        if not inside_margin:
            continue

        touches_edge = x <= 2 or y <= 2 or (x + bw) >= w - 2 or (y + bh) >= h - 2
        vertical_line = bw <= max_line_thick and bh >= min_v_len and bh >= bw * 6
        horizontal_line = bh <= max_line_thick and bw >= min_h_len and bw >= bh * 6
        border_artifact = touches_edge and (
            bw > w * 0.035 or bh > h * 0.035 or area > max(80, margin * margin * 0.10)
        )

        if vertical_line or horizontal_line or border_artifact:
            remove[labels == idx] = True

    return remove


def _clean_edges(binary: np.ndarray, margin: int, dark_threshold: int = 60) -> np.ndarray:
    h, w = binary.shape[:2]
    result = binary.copy()
    margin = min(max(1, int(margin)), max(1, min(h, w) // 3))

    black = binary == 0

    for row in range(margin):
        if np.mean(black[row, :]) > 0.35:
            result[row, :] = 255
    for row in range(h - margin, h):
        if np.mean(black[row, :]) > 0.35:
            result[row, :] = 255
    for col in range(margin):
        if np.mean(black[:, col]) > 0.35:
            result[:, col] = 255
    for col in range(w - margin, w):
        if np.mean(black[:, col]) > 0.35:
            result[:, col] = 255

    remove = edge_artifact_mask(result == 0, margin, dark_threshold)
    result[remove] = 255

    return result


def _deskew(gray: np.ndarray, max_angle_deg: float) -> np.ndarray:
    h, w = gray.shape[:2]
    crop_x = max(1, int(w * 0.04))
    crop_y = max(1, int(h * 0.04))
    work = gray[crop_y : h - crop_y, crop_x : w - crop_x]

    _, thresh = cv2.threshold(work, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel_width = max(25, int(w / 55))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, 1))
    dilated = cv2.dilate(thresh, kernel, iterations=1)
    coords = np.column_stack(np.where(dilated > 0))

    if len(coords) < 80:
        return gray

    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle

    if abs(angle) > max_angle_deg or abs(angle) < 0.25:
        return gray

    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(
        gray,
        matrix,
        (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )
