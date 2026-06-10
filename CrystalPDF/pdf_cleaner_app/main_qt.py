from __future__ import annotations

import base64
import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import cv2
import fitz
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageOps
from PySide6.QtCore import QObject, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from pdf_cleaner import CleanSettings, clean_page_image, estimate_deskew_angle


APP_NAME = "CrystalPDF"
APP_VERSION = "v2.0.0"
APP_TITLE = f"{APP_NAME} {APP_VERSION}"
DEFAULT_DPI = 300
PREVIEW_MAX_WIDTH = 980
PREVIEW_MAX_HEIGHT = 1320
THUMB_MAX_WIDTH = 120
THUMB_MAX_HEIGHT = 170
INITIAL_THUMB_BEFORE = 4
INITIAL_THUMB_AFTER = 8
THUMB_BATCH_SIZE = 12
THUMB_QUEUE_LIMIT = 96
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".jfif", ".tif", ".tiff", ".bmp", ".webp", ".gif"}
ADD_PAGE_FILE_FILTER = (
    "PDF и изображения (*.pdf *.png *.jpg *.jpeg *.jfif *.tif *.tiff *.bmp *.webp *.gif);;"
    "PDF (*.pdf);;"
    "Изображения (*.png *.jpg *.jpeg *.jfif *.tif *.tiff *.bmp *.webp *.gif);;"
    "Все файлы (*.*)"
)
DESKTOP_SHORTCUT_NAME = APP_TITLE
CLEAN_PROGRESS_RE = re.compile(
    r"Стр\.\s*(\d+)\s*/\s*\d+\s*·\s*(.+)",
    re.IGNORECASE,
)
LEGACY_DESKTOP_SHORTCUT_NAMES = ("CrystalPDF", "Mini_Icon_CrystalPDF")
DESKTOP_SHORTCUT_NEVER_ASK_SETTING = "desktop_shortcut_never_ask"
LEGACY_DESKTOP_SHORTCUT_PROMPT_DISABLED_SETTING = "desktop_shortcut_prompt_disabled"
MAX_OUTPUT_PATH_CHARS = 230
MAX_OUTPUT_STEM_CHARS = 80
GENERATED_STEM_SUFFIXES = (
    re.compile(r"_cut_page_\d+(?:_CrystalPDF)?$", re.IGNORECASE),
    re.compile(r"_merged_pages_\d+_\d+(?:_CrystalPDF)?$", re.IGNORECASE),
    re.compile(r"_without_page_\d+$", re.IGNORECASE),
    re.compile(r"_plus_pages(?:_CrystalPDF)?$", re.IGNORECASE),
    re.compile(r"_rotated(?:_CrystalPDF)?$", re.IGNORECASE),
    re.compile(r"_clean$", re.IGNORECASE),
    re.compile(r"_CrystalPDF(?:_\d+)?$", re.IGNORECASE),
)


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def app_icon_path() -> Path | None:
    for candidate in (resource_path("icon.ico"), Path(__file__).resolve().with_name("icon.ico")):
        if candidate.exists():
            return candidate
    return None


def set_windows_app_user_model_id() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.{APP_VERSION}")
    except Exception:
        pass


def downloads_dir() -> Path:
    downloads = Path.home() / "Downloads"
    return downloads if downloads.exists() else Path.home()


def safe_file_stem(value: object, max_chars: int = MAX_OUTPUT_STEM_CHARS) -> str:
    text = str(value or "CrystalPDF")
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    if not text:
        text = "CrystalPDF"
    return text[:max_chars].rstrip(" ._") or "CrystalPDF"


def base_document_stem(path: Path | None) -> str:
    stem = (path.stem if path else "scan") or "scan"
    previous = None
    while stem and stem != previous:
        previous = stem
        for pattern in GENERATED_STEM_SUFFIXES:
            stem = pattern.sub("", stem)
    return safe_file_stem(stem or (path.stem if path else "scan"))


def bounded_output_path(directory: Path, stem: str, suffix: str) -> Path:
    safe_stem = safe_file_stem(stem)
    safe_suffix = safe_file_stem(suffix, 120)

    def build(candidate_stem: str) -> Path:
        return directory / f"{candidate_stem}_{safe_suffix}.pdf"

    path = build(safe_stem)
    if len(str(path)) <= MAX_OUTPUT_PATH_CHARS:
        return path

    overhead = len(str(directory)) + len(safe_suffix) + len(".pdf") + 2
    keep = max(12, min(len(safe_stem), MAX_OUTPUT_PATH_CHARS - overhead))
    return build(safe_stem[:keep].rstrip(" ._") or "CrystalPDF")


def generated_output_path(input_path: Path | None, suffix: str) -> Path:
    return bounded_output_path(downloads_dir(), base_document_stem(input_path), suffix)


def default_output_path(input_path: Path | None) -> Path:
    return generated_output_path(input_path or Path("scan.pdf"), "CrystalPDF")


def unique_output_path(output_path: Path) -> Path:
    if not output_path.exists():
        return output_path
    for index in range(2, 1000):
        candidate = output_path.with_name(f"{output_path.stem}_{index}{output_path.suffix}")
        if not candidate.exists():
            return candidate
    return output_path.with_name(f"{output_path.stem}_{uuid4().hex[:8]}{output_path.suffix}")


def natural_sort_key(value: Path) -> list[object]:
    import re

    text = str(value).casefold()
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def list_image_files(folder: Path) -> list[Path]:
    try:
        images = [
            path
            for path in folder.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ]
    except Exception:
        return []
    return sorted(images, key=natural_sort_key)


@dataclass
class UiOptions:
    dot_area: int = 25
    denoise: int = 12
    brightness: int = 0
    contrast: int = 100
    edge_margin: int = 0
    edge_threshold: int = 60
    edge_clean: bool = True
    deskew: bool = True
    skip_first: bool = True
    skip_last: bool = False
    clean_ranges: str = ""
    clean_from: int = 1
    clean_to: int = 0
    keep_color: bool = True
    split_pages: bool = False
    compress_pdf: bool = False
    compression_level: str = "medium"
    compression_scope: str = "all"
    mode: str = "standard"

    def clean_settings(self, clean_edges: bool) -> CleanSettings:
        return CleanSettings(
            mode=self.mode,
            dpi=DEFAULT_DPI,
            denoise=int(self.denoise),
            dot_area=int(self.dot_area),
            brightness=int(self.brightness),
            contrast=int(self.contrast),
            clean_edges=bool(clean_edges),
            edge_margin=int(self.edge_margin),
            edge_threshold=int(self.edge_threshold),
            deskew=bool(self.deskew),
            max_angle=10.0,
            preserve_first=False,
            preserve_last=False,
        )

    def jpeg_quality(self) -> int:
        if not self.compress_pdf:
            return 92
        text = self.compression_level.casefold()
        if text in {"strong", "high", "max"}:
            return 62
        if text in {"light", "low"}:
            return 88
        return 78

    def compression_scope_kind(self) -> str:
        scope = self.compression_scope.casefold()
        if scope in {"color", "colour", "colored"}:
            return "color"
        if scope in {"processed", "cleaned"}:
            return "processed"
        return "all"

    def should_compress_page(self, *, color: bool, processed: bool) -> bool:
        if not self.compress_pdf:
            return False

        scope = self.compression_scope_kind()
        if scope == "color":
            return bool(color)
        if scope == "processed":
            return bool(processed)
        return True


def render_page_rgb(page: fitz.Page, dpi: int) -> Image.Image:
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", (pix.w, pix.h), pix.samples)


def adjust_pil_image(image: Image.Image, brightness: int, contrast: int) -> Image.Image:
    original = image.convert("RGB") if image.mode != "RGB" else image
    result = original
    if brightness:
        result = ImageEnhance.Brightness(result).enhance(max(0.1, 1.0 + float(brightness) / 100.0))
    if contrast != 100:
        result = ImageEnhance.Contrast(result).enhance(max(0.1, float(contrast) / 100.0))

    if result.mode == "RGB" and (brightness or contrast != 100):
        mask = color_text_mask(original)
        if mask.any():
            adjusted = np.array(result, dtype=np.uint8)
            adjusted[mask] = np.asarray(original, dtype=np.uint8)[mask]
            result = Image.fromarray(adjusted)
    return result


def color_text_mask(image: Image.Image) -> np.ndarray:
    arr = np.asarray(image.convert("RGB"), dtype=np.uint8)
    arr_i = arr.astype(np.int16)
    channel_delta = arr_i.max(axis=2) - arr_i.min(axis=2)
    not_white = arr_i.min(axis=2) < 245
    saturated = (channel_delta > 18) & not_white
    if not saturated.any():
        return saturated
    kernel = np.ones((2, 2), np.uint8)
    return cv2.dilate(saturated.astype(np.uint8), kernel, iterations=1).astype(bool)


def pil_image_has_color(image: Image.Image, max_pixels: int = 180_000) -> bool:
    rgb = image.convert("RGB")
    arr = np.asarray(rgb)
    pixels = arr.reshape(-1, 3)
    if pixels.shape[0] > max_pixels:
        step = max(1, pixels.shape[0] // max_pixels)
        pixels = pixels[::step]
    pixels_i = pixels.astype(np.int16)
    diffs = pixels_i.max(axis=1) - pixels_i.min(axis=1)
    colored = (diffs > 18) & (pixels_i.min(axis=1) < 245)
    return bool(np.mean(colored) > 0.003 or np.count_nonzero(colored) > 600)


def image_stream(image: Image.Image, quality: int, prefer_jpeg: bool) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if prefer_jpeg:
        image.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue(), "jpeg"
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), "png"


def page_png_data_url(page: fitz.Page, max_width: int, max_height: int) -> str:
    rect = page.rect
    scale = min(
        max_width / max(float(rect.width), 1.0),
        max_height / max(float(rect.height), 1.0),
    )
    scale = max(0.2, min(scale, 2.0))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
    png = pix.tobytes("png")
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def initial_thumb_pages(active_page: int, total: int) -> list[int]:
    if total <= 0:
        return []
    active = max(1, min(int(active_page or 1), total))
    start = max(1, active - INITIAL_THUMB_BEFORE)
    end = min(total, active + INITIAL_THUMB_AFTER)
    pages = set(range(start, end + 1))
    pages.update(range(1, min(total, 8) + 1))
    pages.add(active)
    return sorted(pages)


def thumbnail_payload(doc: fitz.Document, pages: list[int] | set[int] | tuple[int, ...]) -> list[dict[str, object]]:
    total = len(doc)
    thumbs: list[dict[str, object]] = []
    requested_pages: list[int] = []
    seen_pages: set[int] = set()
    for page in pages:
        try:
            page_number = int(page)
        except (TypeError, ValueError):
            continue
        if 1 <= page_number <= total and page_number not in seen_pages:
            requested_pages.append(page_number)
            seen_pages.add(page_number)
    for page_number in requested_pages:
        try:
            thumbs.append(
                {
                    "page": page_number,
                    "image": page_png_data_url(doc.load_page(page_number - 1), THUMB_MAX_WIDTH, THUMB_MAX_HEIGHT),
                }
            )
        except Exception:
            continue
    return thumbs


def document_preview_payload(doc: fitz.Document, preview_page: int = 1) -> tuple[str, list[dict[str, object]]]:
    total = len(doc)
    if total <= 0:
        return "", []

    preview_index = max(0, min(int(preview_page or 1) - 1, total - 1))
    preview = page_png_data_url(doc.load_page(preview_index), PREVIEW_MAX_WIDTH, PREVIEW_MAX_HEIGHT)
    return preview, thumbnail_payload(doc, initial_thumb_pages(preview_page, total))


def parse_clean_page_ranges(text: str, total: int) -> set[int] | None:
    text = (text or "").strip()
    if not text:
        return None

    selected: set[int] = set()
    normalized = text.replace("–", "-").replace("—", "-").replace(";", ",")
    for raw_part in normalized.split(","):
        part = raw_part.strip()
        if not part:
            continue

        if "-" in part:
            bounds = [item.strip() for item in part.split("-")]
            if len(bounds) != 2 or not bounds[0] or not bounds[1]:
                raise ValueError(f"Некорректный диапазон страниц: {part}")
            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError as exc:
                raise ValueError(f"Некорректный диапазон страниц: {part}") from exc
            if start > end:
                start, end = end, start
        else:
            try:
                start = end = int(part)
            except ValueError as exc:
                raise ValueError(f"Некорректный номер страницы: {part}") from exc

        if start < 1:
            start = 1
        if end > total:
            end = total
        if start <= end:
            selected.update(range(start, end + 1))

    if not selected:
        raise ValueError("Диапазон очистки не содержит страниц документа.")
    return selected


def resolve_clean_page_selection(options: UiOptions, total: int) -> tuple[set[int], bool]:
    clean_pages = parse_clean_page_ranges(options.clean_ranges, total)
    if clean_pages is not None:
        return clean_pages, True

    clean_from = max(1, int(options.clean_from or 1))
    clean_to = int(options.clean_to or 0)
    if clean_to <= 0:
        clean_to = total
    clean_from = min(clean_from, max(1, total))
    clean_to = min(max(clean_to, clean_from), max(1, total))
    return set(range(clean_from, clean_to + 1)), False


def page_status_from_progress_text(text: str) -> tuple[int, str] | None:
    match = CLEAN_PROGRESS_RE.search(text or "")
    if not match:
        return None

    page = int(match.group(1))
    stage = match.group(2).casefold()
    if "готово" in stage:
        return page, "ok"
    if "без очистки" in stage or "вне диапазона" in stage:
        return page, "skip"
    if "ошиб" in stage:
        return page, "error"
    return page, "work"


def estimate_color_pages_document(doc: fitz.Document) -> int:
    total = len(doc)
    if total <= 0:
        return 0
    count = 0
    if total <= 40:
        indices = list(range(total))
    else:
        limit = 24 if total <= 250 else 12
        indices = sorted({round(i * (total - 1) / max(1, limit - 1)) for i in range(limit)})
    for idx in indices:
        page = doc.load_page(idx)
        img = render_page_rgb(page, 72)
        if pil_image_has_color(img, max_pixels=60_000):
            count += 1
    if len(indices) < total:
        count = round(count * total / max(1, len(indices)))
    return count


def clamp_pct(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(100.0, number))


def sanitize_page_edits(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        return {"skip": False, "rotation": 0.0, "deskew": 0.0, "overlays": []}

    overlays: list[dict[str, float | str]] = []
    for overlay in data.get("overlays", []):
        if not isinstance(overlay, dict):
            continue
        overlay_type = str(overlay.get("type", "")).lower()
        if overlay_type == "eraser":
            size = max(0.5, min(30.0, clamp_pct(overlay.get("size"), 7.0)))
            overlays.append(
                {
                    "type": "eraser",
                    "x": clamp_pct(overlay.get("x")),
                    "y": clamp_pct(overlay.get("y")),
                    "size": size,
                }
            )
        elif overlay_type == "split":
            orientation = str(overlay.get("orientation", "")).lower()
            if orientation in {"vertical", "v", "left-right", "leftright"}:
                orientation = "vertical"
            elif orientation in {"horizontal", "h", "top-bottom", "topbottom"}:
                orientation = "horizontal"
            else:
                continue
            position = max(2.0, min(98.0, clamp_pct(overlay.get("pos"), 50.0)))
            overlays.append({"type": "split", "orientation": orientation, "pos": position})
        elif overlay_type in {"crop", "protect"}:
            x = clamp_pct(overlay.get("x"))
            y = clamp_pct(overlay.get("y"))
            w = max(0.0, min(100.0 - x, clamp_pct(overlay.get("w"))))
            h = max(0.0, min(100.0 - y, clamp_pct(overlay.get("h"))))
            if w >= 0.5 and h >= 0.5:
                overlays.append({"type": overlay_type, "x": x, "y": y, "w": w, "h": h})

    try:
        rotation = float(data.get("rotation", 0.0))
    except (TypeError, ValueError):
        rotation = 0.0
    rotation = ((rotation + 180.0) % 360.0) - 180.0

    try:
        deskew = float(data.get("deskew", 0.0))
    except (TypeError, ValueError):
        deskew = 0.0
    deskew = max(-12.0, min(12.0, deskew))

    return {"skip": bool(data.get("skip", False)), "rotation": rotation, "deskew": deskew, "overlays": overlays}


def page_edit_overlays(page_edits: dict[str, object] | None, overlay_type: str) -> list[dict[str, object]]:
    if not page_edits:
        return []
    overlays = page_edits.get("overlays", [])
    if not isinstance(overlays, list):
        return []
    return [item for item in overlays if isinstance(item, dict) and item.get("type") == overlay_type]


def latest_crop_box(page_edits: dict[str, object] | None) -> dict[str, object] | None:
    crops = page_edit_overlays(page_edits, "crop")
    return crops[-1] if crops else None


def page_rotation(page_edits: dict[str, object] | None) -> float:
    if not page_edits:
        return 0.0
    try:
        return float(page_edits.get("rotation", 0.0))
    except (TypeError, ValueError):
        return 0.0


def page_deskew(page_edits: dict[str, object] | None) -> float:
    if not page_edits:
        return 0.0
    try:
        return float(page_edits.get("deskew", 0.0))
    except (TypeError, ValueError):
        return 0.0


def has_page_visual_edits(page_edits: dict[str, object] | None) -> bool:
    return bool(
        page_edit_overlays(page_edits, "eraser")
        or page_edit_overlays(page_edits, "protect")
        or page_edit_overlays(page_edits, "split")
        or latest_crop_box(page_edits)
        or abs(page_rotation(page_edits)) > 0.001
        or abs(page_deskew(page_edits)) > 0.001
    )


def pct_box_to_px(box: dict[str, object], width: int, height: int) -> tuple[int, int, int, int]:
    x = clamp_pct(box.get("x"))
    y = clamp_pct(box.get("y"))
    w = clamp_pct(box.get("w"))
    h = clamp_pct(box.get("h"))
    x0 = int(round(width * x / 100.0))
    y0 = int(round(height * y / 100.0))
    x1 = int(round(width * clamp_pct(x + w) / 100.0))
    y1 = int(round(height * clamp_pct(y + h) / 100.0))
    x0 = max(0, min(width - 1, x0))
    y0 = max(0, min(height - 1, y0))
    x1 = max(x0 + 1, min(width, x1))
    y1 = max(y0 + 1, min(height, y1))
    return x0, y0, x1, y1


def pil_image_has_color_outside_boxes(
    image: Image.Image,
    boxes: list[dict[str, object]],
    max_pixels: int = 180_000,
) -> bool:
    if not boxes:
        return pil_image_has_color(image, max_pixels=max_pixels)

    rgb = image.convert("RGB")
    arr = np.asarray(rgb)
    height, width = arr.shape[:2]
    mask = np.ones((height, width), dtype=bool)
    for box in boxes:
        x0, y0, x1, y1 = pct_box_to_px(box, width, height)
        mask[y0:y1, x0:x1] = False

    pixels = arr[mask].reshape(-1, 3)
    if pixels.shape[0] == 0:
        return False
    if pixels.shape[0] > max_pixels:
        step = max(1, pixels.shape[0] // max_pixels)
        pixels = pixels[::step]

    pixels_i = pixels.astype(np.int16)
    diffs = pixels_i.max(axis=1) - pixels_i.min(axis=1)
    colored = (diffs > 18) & (pixels_i.min(axis=1) < 245)
    return bool(np.mean(colored) > 0.003 or np.count_nonzero(colored) > 600)


def edited_target_rect(page_rect: fitz.Rect, page_edits: dict[str, object] | None) -> fitz.Rect:
    crop = latest_crop_box(page_edits)
    if not crop:
        return page_rect
    width = max(1.0, page_rect.width * clamp_pct(crop.get("w")) / 100.0)
    height = max(1.0, page_rect.height * clamp_pct(crop.get("h")) / 100.0)
    return fitz.Rect(0, 0, width, height)


def image_page_rect(image: Image.Image) -> fitz.Rect:
    return fitz.Rect(0, 0, image.width * 72.0 / DEFAULT_DPI, image.height * 72.0 / DEFAULT_DPI)


def split_image_at(image: Image.Image, orientation: str, position: float = 50.0) -> list[Image.Image]:
    width, height = image.size
    orientation = orientation.lower()
    position = max(2.0, min(98.0, float(position)))

    if orientation == "vertical" and width >= 4:
        x = max(1, min(width - 1, int(round(width * position / 100.0))))
        return [image.crop((0, 0, x, height)), image.crop((x, 0, width, height))]

    if orientation == "horizontal" and height >= 4:
        y = max(1, min(height - 1, int(round(height * position / 100.0))))
        return [image.crop((0, 0, width, y)), image.crop((0, y, width, height))]

    return [image]


def auto_split_page_images(image: Image.Image) -> list[Image.Image]:
    width, height = image.size
    orientation = "vertical" if width >= height else "horizontal"
    return split_image_at(image, orientation, 50.0)


def split_page_images(
    image: Image.Image,
    page_edits: dict[str, object] | None,
    auto_split: bool = False,
) -> list[Image.Image]:
    splits = page_edit_overlays(page_edits, "split")
    if splits:
        split = splits[-1]
        orientation = str(split.get("orientation", "")).lower()
        position = max(2.0, min(98.0, clamp_pct(split.get("pos"), 50.0)))
        return split_image_at(image, orientation, position)

    if auto_split:
        return auto_split_page_images(image)

    return [image]


def apply_page_edits(
    image: Image.Image,
    original_rgb: Image.Image,
    page_edits: dict[str, object] | None,
) -> Image.Image:
    if not page_edits:
        return image

    result = image.copy()
    width, height = result.size

    deskew = page_deskew(page_edits)
    if abs(deskew) > 0.001:
        fill = 255 if result.mode == "L" else (255, 255, 255)
        result = result.rotate(deskew, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=fill)

    erasers = page_edit_overlays(page_edits, "eraser")
    if erasers:
        white = 255 if result.mode == "L" else (255, 255, 255)
        draw = ImageDraw.Draw(result)
        for mark in erasers:
            size_pct = max(0.5, float(mark.get("size", 7.0)))
            radius = max(1, int(round(min(width, height) * size_pct / 200.0)))
            cx = int(round(width * clamp_pct(mark.get("x")) / 100.0))
            cy = int(round(height * clamp_pct(mark.get("y")) / 100.0))
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=white)

    protected_boxes = page_edit_overlays(page_edits, "protect")
    if protected_boxes:
        if result.mode != "RGB":
            result = result.convert("RGB")
        original = original_rgb.convert("RGB")
        for box in protected_boxes:
            px_box = pct_box_to_px(box, width, height)
            result.paste(original.crop(px_box), px_box)

    crop = latest_crop_box(page_edits)
    if crop:
        result = result.crop(pct_box_to_px(crop, width, height))

    rotation = page_rotation(page_edits)
    if abs(rotation) > 0.001:
        fill = 255 if result.mode == "L" else (255, 255, 255)
        result = result.rotate(-rotation, expand=True, fillcolor=fill)

    return result


def insert_pil_page(
    target: fitz.Document,
    rect: fitz.Rect,
    image: Image.Image,
    options: UiOptions,
    color: bool,
    compress: bool | None = None,
) -> None:
    page = target.new_page(width=rect.width, height=rect.height)
    compress_image = options.compress_pdf if compress is None else bool(compress)
    use_jpeg = color or compress_image
    quality = options.jpeg_quality() if compress_image else 92
    stream, _fmt = image_stream(image, quality, use_jpeg)
    page.insert_image(page.rect, stream=stream)


def image_to_rgb_page(src: Image.Image) -> Image.Image:
    if src.mode in {"RGBA", "LA"} or "transparency" in src.info:
        rgba = src.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return src.convert("RGB")


def insert_image_file_page(target: fitz.Document, image_path: Path, options: UiOptions) -> None:
    with Image.open(image_path) as src:
        image = image_to_rgb_page(ImageOps.exif_transpose(src))
    insert_pil_page(
        target,
        image_page_rect(image),
        image,
        options,
        color=pil_image_has_color(image),
        compress=False,
    )


def append_pdf_or_image_pages(target: fitz.Document, path: Path, options: UiOptions) -> int:
    if path.suffix.lower() == ".pdf":
        source = fitz.open(str(path))
        try:
            page_count = len(source)
            if page_count <= 0:
                raise RuntimeError(f"PDF не содержит страниц: {path.name}")
            target.insert_pdf(source)
            return page_count
        finally:
            source.close()

    try:
        insert_image_file_page(target, path, options)
        return 1
    except Exception as exc:
        raise RuntimeError(f"Файл не похож на PDF или изображение: {path.name}") from exc


def merge_page_images(first: Image.Image, second: Image.Image) -> Image.Image:
    first_rgb = first.convert("RGB")
    second_rgb = second.convert("RGB")
    w1, h1 = first_rgb.size
    w2, h2 = second_rgb.size
    height_delta = abs(h1 - h2) / max(h1, h2, 1)
    width_delta = abs(w1 - w2) / max(w1, w2, 1)

    if height_delta <= width_delta:
        canvas = Image.new("RGB", (w1 + w2, max(h1, h2)), (255, 255, 255))
        canvas.paste(first_rgb, (0, 0))
        canvas.paste(second_rgb, (w1, 0))
        return canvas

    canvas = Image.new("RGB", (max(w1, w2), h1 + h2), (255, 255, 255))
    canvas.paste(first_rgb, (0, 0))
    canvas.paste(second_rgb, (0, h1))
    return canvas


def insert_edited_pages(
    target: fitz.Document,
    image: Image.Image,
    edits: dict[str, object],
    options: UiOptions,
    color: bool,
    compress: bool | None = None,
    auto_split: bool = False,
) -> None:
    for page_image in split_page_images(image, edits, auto_split=auto_split):
        insert_pil_page(
            target,
            image_page_rect(page_image),
            page_image,
            options,
            color=color or page_image.mode == "RGB",
            compress=compress,
        )


def clean_pdf_webengine(
    input_path: Path,
    output_path: Path,
    options: UiOptions,
    page_edits: dict[int, dict[str, object]] | None,
    progress,
    cancel_requested,
) -> tuple[int, int]:
    if input_path.resolve() == output_path.resolve():
        output_path = unique_output_path(output_path.with_name(f"{output_path.stem}_clean.pdf"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output_path.with_name(f"{output_path.stem}.tmp-{uuid4().hex}.pdf")

    source = fitz.open(str(input_path))
    target = fitz.open()
    color_count = 0
    processed_count = 0
    try:
        total = len(source)
        clean_pages, explicit_clean_range = resolve_clean_page_selection(options, total)
        for idx in range(total):
            if cancel_requested():
                raise RuntimeError("Обработка отменена")

            page_num = idx + 1
            pct = int((idx / max(1, total)) * 100)

            page = source.load_page(idx)
            edits = page_edits.get(page_num, {}) if page_edits else {}
            in_clean_range = page_num in clean_pages
            if explicit_clean_range and not in_clean_range and not options.split_pages:
                target.insert_pdf(source, from_page=idx, to_page=idx)
                progress(int((page_num / total) * 100), f"Стр. {page_num}/{total} · вне диапазона")
                continue

            skip = (
                not in_clean_range
                or bool(edits.get("skip"))
                or (not explicit_clean_range and idx == 0 and options.skip_first)
                or (not explicit_clean_range and idx == total - 1 and options.skip_last)
            )
            has_page_edits = has_page_visual_edits(edits)
            protected_boxes = page_edit_overlays(edits, "protect")
            if skip and not has_page_edits and not options.split_pages:
                if options.compress_pdf and options.compression_scope_kind() in {"all", "color"}:
                    progress(pct, f"Стр. {page_num}/{total} · сжатие")
                    rgb = render_page_rgb(page, DEFAULT_DPI)
                    is_color = pil_image_has_color(rgb)
                    if options.should_compress_page(color=is_color, processed=False):
                        insert_pil_page(
                            target,
                            image_page_rect(rgb),
                            rgb,
                            options,
                            color=is_color,
                            compress=True,
                        )
                        processed_count += 1
                        progress(int((page_num / total) * 100), f"Стр. {page_num}/{total} · сжатие")
                        progress(int((page_num / total) * 100), f"Стр. {page_num}/{total} · готово")
                        continue
                target.insert_pdf(source, from_page=idx, to_page=idx)
                reason = "вне диапазона" if not in_clean_range else "без очистки"
                progress(int((page_num / total) * 100), f"Стр. {page_num}/{total} · {reason}")
                continue

            progress(pct, f"Стр. {page_num}/{total} · рендер страницы")
            rgb = render_page_rgb(page, DEFAULT_DPI)
            if skip:
                progress(pct, f"Стр. {page_num}/{total} · правки без очистки")
                out_img = apply_page_edits(rgb, rgb, edits)
                is_color = pil_image_has_color(out_img)
                insert_edited_pages(
                    target,
                    out_img,
                    edits,
                    options,
                    color=is_color,
                    compress=options.should_compress_page(color=is_color, processed=False),
                    auto_split=options.split_pages,
                )
                processed_count += 1
                progress(int((page_num / total) * 100), f"Стр. {page_num}/{total} · готово")
                continue

            is_color = options.keep_color and pil_image_has_color_outside_boxes(rgb, protected_boxes)
            if is_color:
                color_count += 1
                progress(pct, f"Стр. {page_num}/{total} · сохранение цвета")
                out_img = adjust_pil_image(rgb, options.brightness, options.contrast)
                out_img = apply_page_edits(out_img, rgb, edits)
                insert_edited_pages(
                    target,
                    out_img,
                    edits,
                    options,
                    color=True,
                    compress=options.should_compress_page(color=True, processed=True),
                    auto_split=options.split_pages,
                )
            else:
                progress(pct, f"Стр. {page_num}/{total} · шумоподавление")
                gray = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2GRAY)
                edge_clean = options.edge_clean and idx not in (0, total - 1)
                settings = options.clean_settings(edge_clean)
                if abs(page_rotation(edits)) > 0.001 or abs(page_deskew(edits)) > 0.001:
                    settings = replace(settings, deskew=False)
                cleaned = clean_page_image(gray, settings)
                out_img = Image.fromarray(cleaned).convert("L")
                out_img = apply_page_edits(out_img, rgb, edits)
                insert_edited_pages(
                    target,
                    out_img,
                    edits,
                    options,
                    color=out_img.mode == "RGB",
                    compress=options.should_compress_page(color=out_img.mode == "RGB", processed=True),
                    auto_split=options.split_pages,
                )
            processed_count += 1
            progress(int((page_num / total) * 100), f"Стр. {page_num}/{total} · готово")

        target.save(str(temp_output), garbage=4, deflate=True, clean=True)
        temp_output.replace(output_path)
        return color_count, processed_count
    finally:
        target.close()
        source.close()
        if temp_output.exists():
            try:
                temp_output.unlink()
            except Exception:
                pass


def images_to_pdf(folder: Path, output_path: Path, progress, cancel_requested) -> int:
    images = list_image_files(folder)
    if not images:
        raise RuntimeError("В папке не найдены изображения")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output_path.with_name(f"{output_path.stem}.tmp-{uuid4().hex}.pdf")
    target = fitz.open()
    try:
        total = len(images)
        for idx, path in enumerate(images):
            if cancel_requested():
                raise RuntimeError("Импорт отменён")
            progress(int((idx / max(1, total)) * 100), f"Изображение {idx + 1}/{total}")
            with Image.open(path) as src:
                img = src.convert("RGB")
                width_pt = img.width * 72.0 / DEFAULT_DPI
                height_pt = img.height * 72.0 / DEFAULT_DPI
                page = target.new_page(width=width_pt, height=height_pt)
                stream, _fmt = image_stream(img, 92, True)
                page.insert_image(page.rect, stream=stream)
        target.save(str(temp_output), garbage=4, deflate=True, clean=True)
        temp_output.replace(output_path)
        progress(100, "PDF из изображений готов")
        return total
    finally:
        target.close()
        if temp_output.exists():
            try:
                temp_output.unlink()
            except Exception:
                pass


class PdfLoadWorker(QThread):
    finished_ok = Signal(str, int, int, str, object, int, bool)
    failed = Signal(str)

    def __init__(self, path: Path, page_number: int = 1, reset_edits: bool = True):
        super().__init__()
        self.path = path
        self.page_number = page_number
        self.reset_edits = reset_edits

    def cancel(self) -> None:
        pass

    def run(self) -> None:
        try:
            preview = ""
            thumbs: list[dict[str, object]] = []
            doc = fitz.open(str(self.path))
            try:
                page_count = len(doc)
                color_count = estimate_color_pages_document(doc)
                page_number = max(1, min(int(self.page_number or 1), max(1, page_count)))
                try:
                    preview, thumbs = document_preview_payload(doc, page_number)
                except Exception:
                    preview, thumbs = "", []
            finally:
                doc.close()
            self.finished_ok.emit(
                str(self.path),
                page_count,
                color_count,
                preview,
                thumbs,
                page_number,
                self.reset_edits,
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class PdfPartialRefreshWorker(QThread):
    finished_ok = Signal(str, int, str, object, int)
    failed = Signal(str)

    def __init__(self, path: Path, page_number: int, thumb_pages: set[int]):
        super().__init__()
        self.path = path
        self.page_number = page_number
        self.thumb_pages = set(thumb_pages)

    def cancel(self) -> None:
        pass

    def run(self) -> None:
        try:
            preview = ""
            thumbs: list[dict[str, object]] = []
            doc = fitz.open(str(self.path))
            try:
                page_count = len(doc)
                page_number = max(1, min(int(self.page_number or 1), max(1, page_count)))
                if page_count > 0:
                    preview = page_png_data_url(
                        doc.load_page(page_number - 1),
                        PREVIEW_MAX_WIDTH,
                        PREVIEW_MAX_HEIGHT,
                    )
                    thumbs = thumbnail_payload(doc, self.thumb_pages)
            finally:
                doc.close()
            self.finished_ok.emit(str(self.path), page_count, preview, thumbs, page_number)
        except Exception as exc:
            self.failed.emit(str(exc))


class PdfThumbWorker(QThread):
    finished_ok = Signal(str, object)
    failed = Signal(str)

    def __init__(self, path: Path, pages: set[int]):
        super().__init__()
        self.path = path
        self.pages = set(pages)

    def cancel(self) -> None:
        pass

    def run(self) -> None:
        try:
            thumbs: list[dict[str, object]] = []
            doc = fitz.open(str(self.path))
            try:
                thumbs = thumbnail_payload(doc, self.pages)
            finally:
                doc.close()
            self.finished_ok.emit(str(self.path), thumbs)
        except Exception as exc:
            self.failed.emit(str(exc))


class CleanWorker(QThread):
    progress_changed = Signal(int, str)
    finished_ok = Signal(str, int, int)
    failed = Signal(str)

    def __init__(
        self,
        input_path: Path,
        output_path: Path,
        options: UiOptions,
        page_edits: dict[int, dict[str, object]] | None = None,
    ):
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.options = options
        self.page_edits = page_edits or {}
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            colors, processed = clean_pdf_webengine(
                self.input_path,
                self.output_path,
                self.options,
                self.page_edits,
                lambda pct, text: self.progress_changed.emit(pct, text),
                lambda: self._cancel,
            )
            self.progress_changed.emit(100, "Готово")
            self.finished_ok.emit(str(self.output_path), colors, processed)
        except Exception as exc:
            self.failed.emit(str(exc))


class ImageImportWorker(QThread):
    progress_changed = Signal(int, str)
    finished_ok = Signal(str, int)
    failed = Signal(str)

    def __init__(self, folder: Path, output_path: Path):
        super().__init__()
        self.folder = folder
        self.output_path = output_path
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            total = images_to_pdf(
                self.folder,
                self.output_path,
                lambda pct, text: self.progress_changed.emit(pct, text),
                lambda: self._cancel,
            )
            self.finished_ok.emit(str(self.output_path), total)
        except Exception as exc:
            self.failed.emit(str(exc))


class Bridge(QObject):
    def __init__(self, window: "CrystalPdfQtApp"):
        super().__init__()
        self.window = window

    @Slot()
    def uiReady(self):
        self.window.on_ui_ready()

    @Slot()
    def importPdf(self):
        self.window.import_pdf()

    @Slot()
    def exportPdf(self):
        self.window.export_pdf()

    @Slot()
    def exportCurrentPage(self):
        self.window.export_current_page()

    @Slot()
    def addPages(self):
        self.window.add_pdf_pages()

    @Slot()
    def deleteCurrentPage(self):
        self.window.delete_current_page()

    @Slot(int, str)
    def applySplitCrop(self, page: int, payload: str):
        self.window.apply_split_crop_current_page(page, payload)

    @Slot()
    def mergeSplitPages(self):
        self.window.merge_split_pages()

    @Slot(int)
    def rotateCurrentPage(self, degrees: int):
        self.window.rotate_current_page(degrees)

    @Slot()
    def importImages(self):
        self.window.import_images()

    @Slot()
    def startCleaning(self):
        self.window.start_cleaning()

    @Slot()
    def cancelProcessing(self):
        self.window.cancel_processing()

    @Slot(str)
    def setOutputPath(self, path: str):
        self.window.set_output_path(path)

    @Slot(str, str)
    def setOption(self, name: str, value: str):
        self.window.set_option(name, value)

    @Slot(str)
    def setMode(self, value: str):
        self.window.set_mode(value)

    @Slot(int)
    def renderPage(self, page: int):
        self.window.render_page_preview(page)

    @Slot(str)
    def renderThumbs(self, payload: str):
        self.window.request_thumbnails(payload)

    @Slot(int)
    def autoDeskewPage(self, page: int):
        self.window.auto_deskew_page(page)

    @Slot(int, str)
    def syncPageEdits(self, page: int, payload: str):
        self.window.sync_page_edits(page, payload)


class CrystalPdfQtApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        icon_path = app_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))

        self.options = UiOptions()
        self.input_path: Path | None = None
        self.output_path: Path | None = None
        self.last_output_path: Path | None = None
        self.page_count = 0
        self.color_count = 0
        self.current_page = 1
        self.page_edits: dict[int, dict[str, object]] = {}
        self.pending_clean_pages: set[int] = set()
        self.pending_clean_range_explicit = False
        self.pending_thumb_pages: list[int] = []
        self.worker: PdfLoadWorker | PdfPartialRefreshWorker | CleanWorker | ImageImportWorker | None = None
        self.thumb_worker: PdfThumbWorker | None = None

        self.view = QWebEngineView(self)
        self.setCentralWidget(self.view)
        self.resize(1320, 860)
        self.setMinimumSize(980, 640)

        self.bridge = Bridge(self)
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)

        html_path = resource_path("ui/CrystalPDF_UI_v2.0.0.html")
        self.view.load(QUrl.fromLocalFile(str(html_path)))
        QTimer.singleShot(650, self.maybe_prompt_desktop_shortcut)

    def js(self, code: str) -> None:
        self.view.page().runJavaScript(code)

    def ui_call(self, name: str, *args) -> None:
        arg_text = ", ".join(json.dumps(arg, ensure_ascii=False) for arg in args)
        self.js(f"window.crystalUI && window.crystalUI.{name}({arg_text});")

    def has_processed_output(self) -> bool:
        return bool(self.last_output_path and self.last_output_path.exists())

    def refresh_download_state(self) -> None:
        self.ui_call("setDownloadReady", self.has_processed_output())

    def reset_thumbnail_queue(self) -> None:
        self.pending_thumb_pages.clear()

    def request_thumbnails(self, payload: str) -> None:
        try:
            raw_pages = json.loads(payload) if payload else []
        except Exception:
            return
        if not isinstance(raw_pages, list):
            return

        pages: list[int] = []
        seen: set[int] = set()
        for item in raw_pages:
            try:
                page = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= page <= self.page_count and page not in seen:
                pages.append(page)
                seen.add(page)
        if not pages:
            return

        existing = [page for page in self.pending_thumb_pages if page not in seen]
        self.pending_thumb_pages = (pages + existing)[:THUMB_QUEUE_LIMIT]
        self.start_next_thumb_worker()

    def start_next_thumb_worker(self) -> None:
        if not self.input_path or self.is_busy():
            return
        if self.thumb_worker and self.thumb_worker.isRunning():
            return
        if not self.pending_thumb_pages:
            return

        pages = self.pending_thumb_pages[:THUMB_BATCH_SIZE]
        del self.pending_thumb_pages[:THUMB_BATCH_SIZE]
        worker = PdfThumbWorker(self.input_path, pages)
        self.thumb_worker = worker
        worker.finished_ok.connect(self.on_thumbnails_loaded)
        worker.failed.connect(self.on_thumbnail_worker_failed)
        worker.start()

    def on_thumbnails_loaded(self, path: str, thumbs: object) -> None:
        self.thumb_worker = None
        if self.input_path and Path(path).resolve() == self.input_path.resolve():
            self.ui_call("updateThumbImages", thumbs if isinstance(thumbs, list) else [])
        self.start_next_thumb_worker()

    def on_thumbnail_worker_failed(self, _message: str) -> None:
        self.thumb_worker = None
        self.start_next_thumb_worker()

    def handle_clean_progress(self, pct: int, text: str) -> None:
        self.ui_call("setProgress", pct, text)
        page_status = page_status_from_progress_text(text)
        if page_status:
            page, status = page_status
            self.ui_call("setPageStatus", page, status)

    def mark_finished_page_statuses(self) -> None:
        if self.pending_clean_range_explicit and self.pending_clean_pages:
            self.ui_call("resetPageStatuses")
            for page in sorted(self.pending_clean_pages):
                self.ui_call("setPageStatus", page, "ok")
        else:
            self.ui_call("setAllPageStatuses", "ok")

    def can_partially_refresh_clean_result(self) -> bool:
        if self.options.split_pages:
            return False
        if not self.pending_clean_range_explicit or not self.pending_clean_pages:
            return False
        if self.page_count <= 0 or len(self.pending_clean_pages) >= self.page_count:
            return False
        for page in self.pending_clean_pages:
            if page_edit_overlays(self.page_edits.get(page), "split"):
                return False
        return True

    def start_processed_full_reload(self, result_path: Path, colors: int, processed: int) -> None:
        worker = PdfLoadWorker(result_path, self.current_page, True)
        self.worker = worker
        worker.finished_ok.connect(
            lambda path, page_count, color_count, preview, thumbs, page_number, reset_edits: self.on_processed_pdf_loaded(
                path,
                page_count,
                color_count,
                preview,
                thumbs,
                page_number,
                reset_edits,
                result_path,
                colors,
                processed,
            )
        )
        worker.failed.connect(self.on_worker_failed)
        worker.start()

    def mark_result_stale(self) -> None:
        if self.last_output_path is not None:
            self.last_output_path = None
            self.refresh_download_state()

    def set_output_path(self, path: str) -> None:
        if self.is_busy():
            return
        self.output_path = Path(path).expanduser() if path else None
        self.mark_result_stale()

    def on_ui_ready(self) -> None:
        self.ui_call("setProgress", 0, "Готов · откройте PDF")
        self.ui_call("setStats", 0, 0, 0, 0)
        self.ui_call("setProcessing", False)
        self.refresh_download_state()
        self.ui_call("setStatus", "● Готов к работе", "ready")

    def settings_path(self) -> Path:
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        return base / APP_NAME / "settings.json"

    def load_user_settings(self) -> dict[str, object]:
        path = self.settings_path()
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as file:
                    data = json.load(file)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}

    def save_user_settings(self, settings: dict[str, object]) -> bool:
        try:
            path = self.settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as file:
                json.dump(settings, file, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def desktop_dir(self) -> Path:
        for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            root = os.environ.get(var)
            if root:
                candidate = Path(root) / "Desktop"
                if candidate.exists():
                    return candidate
        return Path.home() / "Desktop"

    def desktop_shortcut_path(self) -> Path:
        return self.desktop_dir() / f"{DESKTOP_SHORTCUT_NAME}.lnk"

    def normalize_desktop_shortcut_name(self) -> bool:
        shortcut_path = self.desktop_shortcut_path()
        for name in LEGACY_DESKTOP_SHORTCUT_NAMES:
            legacy_path = self.desktop_dir() / f"{name}.lnk"
            if not legacy_path.exists():
                continue
            try:
                if shortcut_path.exists():
                    legacy_path.unlink()
                else:
                    legacy_path.rename(shortcut_path)
            except Exception:
                pass
        return shortcut_path.exists()

    def maybe_prompt_desktop_shortcut(self) -> None:
        if os.name != "nt":
            return
        settings = self.load_user_settings()
        if settings.get(DESKTOP_SHORTCUT_NEVER_ASK_SETTING):
            return
        if LEGACY_DESKTOP_SHORTCUT_PROMPT_DISABLED_SETTING in settings:
            settings.pop(LEGACY_DESKTOP_SHORTCUT_PROMPT_DISABLED_SETTING, None)
            self.save_user_settings(settings)
        if self.normalize_desktop_shortcut_name():
            return
        self.show_desktop_shortcut_prompt(settings)

    def show_desktop_shortcut_prompt(self, settings: dict[str, object]) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Ярлык {APP_TITLE}")
        if not self.windowIcon().isNull():
            dialog.setWindowIcon(self.windowIcon())
        dialog.setModal(True)
        dialog.setObjectName("shortcutDialog")
        dialog.setStyleSheet(
            """
            QDialog#shortcutDialog { background:#060919; color:#edf2ff; }
            QLabel#shortcutTitle { color:#edf2ff; font-size:16px; font-weight:800; }
            QLabel#shortcutText { color:#7a86aa; font-size:12px; line-height:1.35; }
            QCheckBox { color:#7a86aa; font-size:12px; spacing:8px; }
            QCheckBox::indicator { width:16px; height:16px; border-radius:4px; border:1px solid rgba(255,255,255,.16); background:#0d1226; }
            QCheckBox::indicator:checked { background:#00e5ff; border:1px solid #00e5ff; }
            QPushButton { min-width:104px; padding:8px 14px; border-radius:8px; font-weight:700; }
            QPushButton#primaryButton { color:#001018; background:#00e5ff; border:1px solid rgba(0,229,255,.72); }
            QPushButton#secondaryButton { color:#7a86aa; background:rgba(255,255,255,.025); border:1px solid rgba(255,255,255,.09); }
            QPushButton#secondaryButton:hover { color:#edf2ff; border-color:rgba(255,255,255,.18); }
            """
        )

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = QLabel("Создать ярлык на рабочем столе?")
        title.setObjectName("shortcutTitle")
        layout.addWidget(title)

        text = QLabel(f"Будет создан ярлык «{DESKTOP_SHORTCUT_NAME}» для быстрого запуска {APP_TITLE}.")
        text.setObjectName("shortcutText")
        text.setWordWrap(True)
        layout.addWidget(text)

        dont_show = QCheckBox("Больше не показывать")
        layout.addWidget(dont_show)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        later = QPushButton("Не сейчас")
        later.setObjectName("secondaryButton")
        create = QPushButton("Создать ярлык")
        create.setObjectName("primaryButton")
        buttons.addWidget(later)
        buttons.addWidget(create)
        layout.addLayout(buttons)

        def suppress_prompt() -> None:
            settings[DESKTOP_SHORTCUT_NEVER_ASK_SETTING] = True
            settings.pop(LEGACY_DESKTOP_SHORTCUT_PROMPT_DISABLED_SETTING, None)
            self.save_user_settings(settings)

        def close_without_create() -> None:
            if dont_show.isChecked():
                suppress_prompt()
            dialog.reject()

        def create_shortcut() -> None:
            ok, err = self.create_desktop_shortcut()
            if ok:
                settings.pop(LEGACY_DESKTOP_SHORTCUT_PROMPT_DISABLED_SETTING, None)
                self.save_user_settings(settings)
                self.ui_call("setStatus", "● Ярлык создан", "ready")
                dialog.accept()
                return
            QMessageBox.critical(dialog, "Не удалось создать ярлык", err or "Неизвестная ошибка")

        later.clicked.connect(close_without_create)
        create.clicked.connect(create_shortcut)
        dialog.setFixedWidth(430)
        result = dialog.exec()
        if result == QDialog.DialogCode.Rejected and dont_show.isChecked():
            suppress_prompt()

    def create_desktop_shortcut(self) -> tuple[bool, str]:
        try:
            shortcut_path = self.desktop_shortcut_path()
            shortcut_path.parent.mkdir(parents=True, exist_ok=True)

            if getattr(sys, "frozen", False):
                target_path = Path(sys.executable).resolve()
                arguments = ""
                work_dir = target_path.parent
                icon_path = target_path
            else:
                target_path = Path(sys.executable).resolve()
                app_path = Path(__file__).resolve()
                arguments = f'"{app_path}"'
                work_dir = app_path.parent
                icon_path = app_icon_path() or target_path

            def ps_quote(value: object) -> str:
                return "'" + str(value).replace("'", "''") + "'"

            script = [
                "$WshShell = New-Object -ComObject WScript.Shell",
                f"$Shortcut = $WshShell.CreateShortcut({ps_quote(shortcut_path)})",
                f"$Shortcut.TargetPath = {ps_quote(target_path)}",
                f"$Shortcut.WorkingDirectory = {ps_quote(work_dir)}",
                f"$Shortcut.IconLocation = {ps_quote(str(icon_path) + ',0')}",
                f"$Shortcut.Description = {ps_quote(APP_TITLE)}",
            ]
            if arguments:
                script.append(f"$Shortcut.Arguments = {ps_quote(arguments)}")
            script.append("$Shortcut.Save()")

            encoded = base64.b64encode("\r\n".join(script).encode("utf-16le")).decode("ascii")
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=20,
            )
            if result.returncode != 0:
                return False, (result.stderr or result.stdout or "PowerShell завершился с ошибкой").strip()
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def set_option(self, name: str, value: str) -> None:
        if self.is_busy():
            return
        bool_value = value == "1" or value.casefold() in {"true", "yes", "да"}
        int_names = {
            "dot_area",
            "denoise",
            "brightness",
            "contrast",
            "edge_margin",
            "edge_threshold",
            "clean_from",
            "clean_to",
        }
        bool_names = {
            "edge_clean",
            "deskew",
            "skip_first",
            "skip_last",
            "keep_color",
            "split_pages",
            "compress_pdf",
        }
        if name in int_names:
            try:
                text = str(value or "").strip()
                if not text and name == "clean_to":
                    parsed = 0
                elif not text and name == "clean_from":
                    parsed = 1
                else:
                    parsed = int(float(text))
                if name == "clean_from":
                    parsed = max(1, parsed)
                elif name == "clean_to":
                    parsed = max(0, parsed)
                setattr(self.options, name, parsed)
                self.mark_result_stale()
            except ValueError:
                pass
        elif name in bool_names:
            setattr(self.options, name, bool_value)
            self.mark_result_stale()
        elif name == "clean_ranges":
            self.options.clean_ranges = value.strip()
            self.mark_result_stale()
        elif name in {"compression_level", "compression_scope"}:
            setattr(self.options, name, value)
            self.mark_result_stale()

    def set_mode(self, value: str) -> None:
        if self.is_busy():
            return
        text = value.casefold()
        if "лёг" in text or "лег" in text:
            self.options.mode = "gentle"
        elif "агрессив" in text or "максим" in text:
            self.options.mode = "strong"
        else:
            self.options.mode = "standard"
        self.mark_result_stale()

    def import_pdf(self) -> None:
        if self.is_busy():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Открыть PDF",
            str(Path.home()),
            "PDF (*.pdf)",
        )
        if path:
            self.load_pdf(Path(path))

    def import_images(self) -> None:
        if self.is_busy():
            return
        folder = QFileDialog.getExistingDirectory(self, "Папка со сканами", str(Path.home()))
        if not folder:
            return
        folder_path = Path(folder)
        out = unique_output_path(bounded_output_path(downloads_dir(), folder_path.name, "CrystalPDF"))
        self.ui_call("setProcessing", True, "Импорт сканов")
        self.ui_call("setProgress", 0, "Импорт изображений")
        self.ui_call("setStatus", "Импорт изображений", "working")
        worker = ImageImportWorker(folder_path, out)
        self.worker = worker
        worker.progress_changed.connect(lambda pct, text: self.ui_call("setProgress", pct, text))
        worker.finished_ok.connect(self.on_images_imported)
        worker.failed.connect(self.on_worker_failed)
        worker.start()

    def on_images_imported(self, path: str, total: int) -> None:
        self.worker = None
        self.ui_call("setProgress", 100, f"Собран PDF из изображений: {total}")
        self.load_pdf(Path(path))

    def load_pdf(self, path: Path, page_number: int = 1, reset_edits: bool = True) -> None:
        if self.is_busy():
            return
        self.reset_thumbnail_queue()
        self.last_output_path = None
        self.refresh_download_state()
        self.ui_call("setProcessing", True, "Открытие PDF")
        self.ui_call("setProgress", 0, f"Открытие PDF: {path.name}")
        self.ui_call("setStatus", "◉ Открытие PDF", "working")

        worker = PdfLoadWorker(path, page_number, reset_edits)
        self.worker = worker
        worker.finished_ok.connect(self.on_pdf_loaded)
        worker.failed.connect(self.on_worker_failed)
        worker.start()

    def on_pdf_loaded(
        self,
        path: str,
        page_count: int,
        color_count: int,
        preview: str,
        thumbs: object,
        page_number: int,
        reset_edits: bool,
    ) -> None:
        self.worker = None
        self.reset_thumbnail_queue()
        path_obj = Path(path)
        self.input_path = path_obj
        self.page_count = page_count
        self.color_count = color_count
        self.output_path = unique_output_path(default_output_path(path_obj))
        self.last_output_path = None
        self.current_page = page_number
        if reset_edits:
            self.page_edits = {}
        thumb_payload = thumbs if isinstance(thumbs, list) else []
        self.ui_call("setFile", path_obj.name)
        self.ui_call("setOutput", str(self.output_path))
        self.ui_call("setStats", self.page_count, self.color_count, 0, 0)
        self.ui_call("setDocument", path_obj.name, self.page_count, self.color_count, preview, thumb_payload, self.current_page)
        self.ui_call("setProgress", 0, f"Открыт PDF: {path_obj.name}")
        self.ui_call("setProcessing", False)
        self.refresh_download_state()
        self.ui_call("setStatus", "● Готов к работе", "ready")

    def sync_page_edits(self, page_number: int, payload: str) -> None:
        if self.is_busy():
            return
        try:
            page_number = max(1, int(page_number or 1))
            data = json.loads(payload) if payload else {}
            edits = sanitize_page_edits(data)
        except Exception:
            return

        if edits.get("skip") or has_page_visual_edits(edits):
            if self.page_edits.get(page_number) != edits:
                self.page_edits[page_number] = edits
                self.mark_result_stale()
        elif page_number in self.page_edits:
            self.page_edits.pop(page_number, None)
            self.mark_result_stale()

    def render_page_preview(self, page_number: int) -> None:
        if not self.input_path or self.is_busy():
            return
        page_number = max(1, min(int(page_number or 1), max(1, self.page_count)))
        try:
            doc = fitz.open(str(self.input_path))
            try:
                if len(doc) <= 0:
                    return
                page_number = max(1, min(page_number, len(doc)))
                preview = page_png_data_url(
                    doc.load_page(page_number - 1),
                    PREVIEW_MAX_WIDTH,
                    PREVIEW_MAX_HEIGHT,
                )
            finally:
                doc.close()
        except Exception as exc:
            self.ui_call("setProgress", 0, f"Предпросмотр недоступен: {exc}")
            return

        self.current_page = page_number
        self.ui_call(
            "setPagePreview",
            page_number,
            self.page_count,
            self.color_count,
            self.input_path.name,
            preview,
        )

    def auto_deskew_page(self, page_number: int) -> None:
        if not self.input_path or self.is_busy():
            return
        page_number = max(1, min(int(page_number or 1), max(1, self.page_count)))
        try:
            doc = fitz.open(str(self.input_path))
            try:
                if len(doc) <= 0:
                    return
                page = doc.load_page(page_number - 1)
                rgb = render_page_rgb(page, 220)
                gray = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2GRAY)
                angle = estimate_deskew_angle(gray, 12.0, min_abs_angle=0.15)
            finally:
                doc.close()
        except Exception as exc:
            self.ui_call("setProgress", 0, f"Выравнивание недоступно: {exc}")
            return

        self.ui_call("setDeskewAngle", page_number, float(angle))

    def estimate_color_pages(self, doc: fitz.Document) -> int:
        return estimate_color_pages_document(doc)

    def export_pdf(self) -> None:
        if self.is_busy():
            return
        if not self.input_path and not self.has_processed_output():
            QMessageBox.information(self, APP_TITLE, "Сначала импортируйте PDF.")
            return

        default_target = self.last_output_path or self.output_path or default_output_path(self.input_path or Path("scan.pdf"))
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить PDF",
            str(default_target),
            "PDF (*.pdf)",
        )
        if not target:
            return

        output = Path(target)
        if output.suffix.lower() != ".pdf":
            output = output.with_suffix(".pdf")

        if self.has_processed_output():
            try:
                if self.last_output_path.resolve() != output.resolve():
                    shutil.copyfile(self.last_output_path, output)
            except Exception as exc:
                QMessageBox.critical(self, APP_TITLE, f"Не удалось экспортировать PDF: {exc}")
                return
            self.ui_call("setProgress", 100, f"Экспорт готов: {output.name}")
            self.ui_call("setStatus", "● Экспорт готов", "ready")
            return

        self.start_cleaning(output_override=output, uniquify_output=False)

    def export_current_page(self) -> None:
        if self.is_busy():
            return
        if not self.has_processed_output():
            self.refresh_download_state()
            QMessageBox.information(self, APP_TITLE, "Сначала запустите очистку и дождитесь завершения обработки.")
            return

        source_path = self.last_output_path
        default_stem = base_document_stem(self.input_path or source_path or Path("scan"))
        default_name = bounded_output_path(downloads_dir(), default_stem, f"page_{self.current_page}")
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Скачать текущую страницу",
            str(unique_output_path(default_name)),
            "PDF (*.pdf)",
        )
        if not target:
            return

        output = Path(target)
        if output.suffix.lower() != ".pdf":
            output = output.with_suffix(".pdf")

        try:
            source = fitz.open(str(source_path))
            result = fitz.open()
            try:
                page_index = max(0, min(self.current_page - 1, len(source) - 1))
                result.insert_pdf(source, from_page=page_index, to_page=page_index)
                result.save(str(output), garbage=4, deflate=True, clean=True)
            finally:
                result.close()
                source.close()
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"Не удалось сохранить страницу: {exc}")
            return

        self.ui_call("setProgress", 100, f"Страница сохранена: {output.name}")
        self.ui_call("setStatus", "● Страница сохранена", "ready")

    def add_pdf_pages(self) -> None:
        if self.is_busy():
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Добавить страницы из PDF или изображений",
            str(Path.home()),
            ADD_PAGE_FILE_FILTER,
        )
        if not paths:
            return
        incoming_paths = [Path(path) for path in paths]
        first_incoming = incoming_paths[0]

        if not self.input_path and len(incoming_paths) == 1 and first_incoming.suffix.lower() == ".pdf":
            self.load_pdf(first_incoming)
            return

        output = unique_output_path(generated_output_path(self.input_path or first_incoming, "plus_pages_CrystalPDF"))
        try:
            result = fitz.open()
            try:
                if self.input_path:
                    source = fitz.open(str(self.input_path))
                    try:
                        result.insert_pdf(source)
                    finally:
                        source.close()
                first_added_page = len(result) + 1
                added_pages = 0
                for incoming in incoming_paths:
                    added_pages += append_pdf_or_image_pages(result, incoming, self.options)
                if added_pages <= 0:
                    raise RuntimeError("Не выбраны страницы для добавления.")
                result.save(str(output), garbage=4, deflate=True, clean=True)
            finally:
                result.close()
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"Не удалось добавить страницы или изображения: {exc}")
            return

        self.load_pdf(output, first_added_page)
        if len(incoming_paths) == 1:
            label = incoming_paths[0].name
        else:
            label = f"{len(incoming_paths)} файлов"
        self.ui_call("setProgress", 100, f"Добавлены страницы: {label}")

    def delete_current_page(self) -> None:
        if self.is_busy():
            return
        if not self.input_path:
            QMessageBox.information(self, APP_TITLE, "Сначала импортируйте PDF.")
            return
        if self.page_count <= 1:
            QMessageBox.information(self, APP_TITLE, "Нельзя удалить единственную страницу.")
            return

        deleted_page = self.current_page
        output = unique_output_path(generated_output_path(self.input_path, f"without_page_{deleted_page}"))
        try:
            source = fitz.open(str(self.input_path))
            result = fitz.open()
            try:
                remove_index = max(0, min(deleted_page - 1, len(source) - 1))
                for idx in range(len(source)):
                    if idx != remove_index:
                        result.insert_pdf(source, from_page=idx, to_page=idx)
                result.save(str(output), garbage=4, deflate=True, clean=True)
            finally:
                result.close()
                source.close()
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"Не удалось удалить страницу: {exc}")
            return

        next_page = min(deleted_page, self.page_count - 1)
        self.load_pdf(output, next_page)
        self.ui_call("setProgress", 100, f"Удалена страница {deleted_page}")

    def choose_merge_pair(self) -> tuple[int, int] | None:
        if self.page_count < 2:
            QMessageBox.information(self, APP_TITLE, "Для склейки нужны минимум две страницы.")
            return None

        current = max(1, min(self.current_page, self.page_count))
        if current == 1:
            return 1, 2
        if current == self.page_count:
            return self.page_count - 1, self.page_count

        box = QMessageBox(self)
        box.setWindowTitle(APP_TITLE)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("С какой соседней страницей склеить текущую?")
        previous_button = box.addButton("С предыдущей", QMessageBox.ButtonRole.AcceptRole)
        next_button = box.addButton("Со следующей", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked == previous_button:
            return current - 1, current
        if clicked == next_button:
            return current, current + 1
        return None

    def merge_split_pages(self) -> None:
        if self.is_busy():
            return
        if not self.input_path:
            QMessageBox.information(self, APP_TITLE, "Сначала импортируйте PDF.")
            return

        pair = self.choose_merge_pair()
        if not pair:
            return
        first_page, second_page = pair
        output = unique_output_path(
            generated_output_path(self.input_path, f"merged_pages_{first_page}_{second_page}_CrystalPDF")
        )

        self.ui_call("setProcessing", True, "Склейка страниц")
        self.ui_call("setProgress", 0, f"Склейка страниц {first_page} и {second_page}")
        self.ui_call("setStatus", "◉ Склейка страниц", "working")

        try:
            source = fitz.open(str(self.input_path))
            result = fitz.open()
            try:
                if len(source) < 2:
                    raise RuntimeError("В PDF меньше двух страниц.")
                first_index = first_page - 1
                second_index = second_page - 1
                for idx in range(len(source)):
                    if idx == first_index:
                        first_img = render_page_rgb(source.load_page(first_index), DEFAULT_DPI)
                        second_img = render_page_rgb(source.load_page(second_index), DEFAULT_DPI)
                        merged = merge_page_images(first_img, second_img)
                        insert_pil_page(
                            result,
                            image_page_rect(merged),
                            merged,
                            self.options,
                            color=pil_image_has_color(merged),
                            compress=False,
                        )
                    elif idx == second_index:
                        continue
                    else:
                        result.insert_pdf(source, from_page=idx, to_page=idx)
                result.save(str(output), garbage=4, deflate=True, clean=True)
            finally:
                result.close()
                source.close()
        except Exception as exc:
            self.ui_call("setProcessing", False)
            self.ui_call("setStatus", "✗ Ошибка", "error")
            QMessageBox.critical(self, APP_TITLE, f"Не удалось склеить страницы: {exc}")
            return

        self.load_pdf(output, first_page)
        self.ui_call("setProgress", 100, f"Склеены страницы {first_page} и {second_page}")

    def apply_split_crop_current_page(self, page_number: int = 0, payload: str = "") -> None:
        if self.is_busy():
            return
        if not self.input_path:
            QMessageBox.information(self, APP_TITLE, "Сначала импортируйте PDF.")
            return

        page_number = max(1, min(int(page_number or self.current_page or 1), max(1, self.page_count)))
        edits = self.page_edits.get(page_number, {})
        if payload:
            try:
                edits = sanitize_page_edits(json.loads(payload))
            except Exception:
                edits = {}
            if edits.get("skip") or has_page_visual_edits(edits):
                self.page_edits[page_number] = edits
            else:
                self.page_edits.pop(page_number, None)

        if not page_edit_overlays(edits, "split"):
            QMessageBox.information(self, APP_TITLE, "Сначала поставьте линию лев/прав или верх/низ.")
            return

        output = unique_output_path(generated_output_path(self.input_path, f"cut_page_{page_number}_CrystalPDF"))
        self.current_page = page_number
        self.ui_call("setProcessing", True, "Обрезка страницы")
        self.ui_call("setProgress", 0, f"Обрезка страницы {page_number}")
        self.ui_call("setStatus", "◉ Обрезка страницы", "working")

        try:
            source = fitz.open(str(self.input_path))
            result = fitz.open()
            try:
                page_index = max(0, min(page_number - 1, len(source) - 1))
                for idx in range(len(source)):
                    if idx != page_index:
                        result.insert_pdf(source, from_page=idx, to_page=idx)
                        continue

                    page = source.load_page(idx)
                    rgb = render_page_rgb(page, DEFAULT_DPI)
                    edited = apply_page_edits(rgb, rgb, edits)
                    split_images = split_page_images(edited, edits)
                    if len(split_images) < 2:
                        raise RuntimeError("Линия не смогла разделить страницу.")
                    for image in split_images:
                        insert_pil_page(
                            result,
                            image_page_rect(image),
                            image,
                            self.options,
                            color=pil_image_has_color(image),
                            compress=False,
                        )
                result.save(str(output), garbage=4, deflate=True, clean=True)
            finally:
                result.close()
                source.close()
        except Exception as exc:
            self.ui_call("setProcessing", False)
            self.ui_call("setStatus", "✗ Ошибка", "error")
            QMessageBox.critical(self, APP_TITLE, f"Не удалось обрезать страницу: {exc}")
            return

        self.load_pdf(output, page_number)

    def rotate_current_page(self, degrees: int) -> None:
        if self.is_busy():
            return
        if not self.input_path:
            QMessageBox.information(self, APP_TITLE, "Сначала импортируйте PDF.")
            return

        output = unique_output_path(generated_output_path(self.input_path, "rotated_CrystalPDF"))
        try:
            doc = fitz.open(str(self.input_path))
            try:
                page_index = max(0, min(self.current_page - 1, len(doc) - 1))
                page = doc.load_page(page_index)
                page.set_rotation((page.rotation + int(degrees)) % 360)
                doc.save(str(output), garbage=4, deflate=True, clean=True)
            finally:
                doc.close()
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"Не удалось повернуть страницу: {exc}")
            return

        page_number = self.current_page
        self.load_pdf(output, page_number)
        self.ui_call("setProgress", 100, f"Поворот страницы: {degrees:+d}°")

    def start_cleaning(
        self,
        export_after: bool = False,
        output_override: Path | None = None,
        uniquify_output: bool = True,
    ) -> None:
        if self.is_busy():
            return
        if not self.input_path:
            QMessageBox.information(self, APP_TITLE, "Сначала импортируйте PDF.")
            return
        output = output_override or self.output_path or default_output_path(self.input_path)
        if output.suffix.lower() != ".pdf":
            output = output.with_suffix(".pdf")
        if self.input_path and output.resolve() == self.input_path.resolve():
            output = output.with_name(f"{output.stem}_clean.pdf")
            uniquify_output = True
        if uniquify_output:
            output = unique_output_path(output)
        self.output_path = output
        self.last_output_path = None
        self.ui_call("setOutput", str(output))
        self.refresh_download_state()
        self.ui_call("resetPageStatuses")
        self.ui_call("setProcessing", True, "Идёт очистка")
        self.ui_call("setProgress", 0, "Запуск обработки…")
        self.ui_call("setStatus", "◉ Обработка", "working")

        try:
            clean_pages, explicit_range = resolve_clean_page_selection(self.options, self.page_count)
            self.pending_clean_pages = clean_pages
            self.pending_clean_range_explicit = explicit_range
        except Exception:
            self.pending_clean_pages = set()
            self.pending_clean_range_explicit = False

        worker = CleanWorker(self.input_path, output, copy.deepcopy(self.options), copy.deepcopy(self.page_edits))
        self.worker = worker
        worker.progress_changed.connect(self.handle_clean_progress)
        worker.finished_ok.connect(self.on_clean_finished)
        worker.failed.connect(self.on_worker_failed)
        worker.start()

    def on_clean_finished(self, output: str, colors: int, processed: int) -> None:
        self.worker = None
        result_path = Path(output)
        self.last_output_path = result_path
        self.output_path = result_path
        self.ui_call("setProgress", 100, f"Готово → {result_path.name}")
        self.ui_call("setStats", self.page_count, max(self.color_count, colors), processed, 0)
        self.mark_finished_page_statuses()
        self.ui_call("setProcessing", True, "Открытие результата")
        self.ui_call("setStatus", "◉ Открытие результата", "working")

        if not self.can_partially_refresh_clean_result():
            self.start_processed_full_reload(result_path, colors, processed)
            return

        worker = PdfPartialRefreshWorker(result_path, self.current_page, self.pending_clean_pages)
        self.worker = worker
        worker.finished_ok.connect(
            lambda path, page_count, preview, thumbs, page_number: self.on_partial_processed_pdf_loaded(
                path,
                page_count,
                preview,
                thumbs,
                page_number,
                result_path,
                colors,
                processed,
            )
        )
        worker.failed.connect(self.on_worker_failed)
        worker.start()

    def on_partial_processed_pdf_loaded(
        self,
        path: str,
        page_count: int,
        preview: str,
        thumbs: object,
        page_number: int,
        result_path: Path,
        colors: int,
        processed: int,
    ) -> None:
        self.worker = None
        self.reset_thumbnail_queue()
        if page_count != self.page_count:
            self.start_processed_full_reload(result_path, colors, processed)
            return

        path_obj = Path(path)
        self.input_path = path_obj
        self.output_path = result_path
        self.last_output_path = result_path
        self.current_page = page_number
        self.color_count = max(self.color_count, colors)
        self.page_edits = {}

        self.ui_call("setFile", path_obj.name)
        self.ui_call("setOutput", str(result_path))
        self.ui_call(
            "updateProcessedPages",
            path_obj.name,
            self.page_count,
            self.color_count,
            preview,
            thumbs if isinstance(thumbs, list) else [],
            self.current_page,
        )
        self.ui_call("setStats", self.page_count, max(self.color_count, colors), processed, 0)
        self.mark_finished_page_statuses()
        self.ui_call("setProgress", 100, f"Готово → {result_path.name}")
        self.ui_call("setProcessing", False)
        self.refresh_download_state()
        self.ui_call("setStatus", "✓ Готово", "ready")

    def on_processed_pdf_loaded(
        self,
        path: str,
        page_count: int,
        color_count: int,
        preview: str,
        thumbs: object,
        page_number: int,
        reset_edits: bool,
        result_path: Path,
        colors: int,
        processed: int,
    ) -> None:
        self.worker = None
        self.reset_thumbnail_queue()
        path_obj = Path(path)
        self.input_path = path_obj
        self.page_count = page_count
        self.color_count = color_count
        self.output_path = result_path
        self.last_output_path = result_path
        self.current_page = page_number
        if reset_edits:
            self.page_edits = {}

        thumb_payload = thumbs if isinstance(thumbs, list) else []
        self.ui_call("setFile", path_obj.name)
        self.ui_call("setOutput", str(result_path))
        self.ui_call("setDocument", path_obj.name, self.page_count, self.color_count, preview, thumb_payload, self.current_page)
        self.ui_call("setStats", self.page_count, max(self.color_count, colors), processed, 0)
        self.mark_finished_page_statuses()
        self.ui_call("setProgress", 100, f"Готово → {result_path.name}")
        self.ui_call("setProcessing", False)
        self.refresh_download_state()
        self.ui_call("setStatus", "✓ Готово", "ready")

    def on_worker_failed(self, message: str) -> None:
        self.worker = None
        self.ui_call("setProcessing", False)
        self.refresh_download_state()
        self.ui_call("setProgress", 0, message)
        self.ui_call("setStatus", "✗ Ошибка", "error")
        if "отмен" not in message.casefold():
            QMessageBox.critical(self, APP_TITLE, message)

    def cancel_processing(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.ui_call("setProgress", 0, "Отмена обработки…")

    def is_busy(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def closeEvent(self, event) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)
        super().closeEvent(event)


def main() -> int:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    set_windows_app_user_model_id()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    icon_path = app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))
    try:
        window = CrystalPdfQtApp()
        window.show()
        return app.exec()
    except Exception:
        QMessageBox.critical(None, APP_TITLE, traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
