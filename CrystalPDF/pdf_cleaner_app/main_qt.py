from __future__ import annotations

import base64
import copy
import io
import json
import os
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
from PIL import Image, ImageDraw, ImageEnhance
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
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
DESKTOP_SHORTCUT_NAME = APP_TITLE
LEGACY_DESKTOP_SHORTCUT_NAMES = ("CrystalPDF", "Mini_Icon_CrystalPDF")
DESKTOP_SHORTCUT_NEVER_ASK_SETTING = "desktop_shortcut_never_ask"
LEGACY_DESKTOP_SHORTCUT_PROMPT_DISABLED_SETTING = "desktop_shortcut_prompt_disabled"


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


def default_output_path(input_path: Path | None) -> Path:
    source = input_path or Path("scan.pdf")
    return downloads_dir() / f"{source.stem}_CrystalPDF.pdf"


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
    edge_margin: int = 5
    edge_threshold: int = 60
    edge_clean: bool = True
    deskew: bool = True
    skip_first: bool = True
    skip_last: bool = False
    keep_color: bool = True
    split_pages: bool = False
    compress_pdf: bool = False
    compression_level: str = "Среднее"
    compression_scope: str = "Все страницы"
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
        if "силь" in text:
            return 62
        if "лёг" in text or "лег" in text:
            return 88
        return 78


def render_page_rgb(page: fitz.Page, dpi: int) -> Image.Image:
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", (pix.w, pix.h), pix.samples)


def adjust_pil_image(image: Image.Image, brightness: int, contrast: int) -> Image.Image:
    result = image
    if brightness:
        result = ImageEnhance.Brightness(result).enhance(max(0.1, 1.0 + float(brightness) / 100.0))
    if contrast != 100:
        result = ImageEnhance.Contrast(result).enhance(max(0.1, float(contrast) / 100.0))
    return result


def pil_image_has_color(image: Image.Image, max_pixels: int = 180_000) -> bool:
    rgb = image.convert("RGB")
    arr = np.asarray(rgb)
    pixels = arr.reshape(-1, 3)
    if pixels.shape[0] > max_pixels:
        step = max(1, pixels.shape[0] // max_pixels)
        pixels = pixels[::step]
    diffs = pixels.max(axis=1).astype(np.int16) - pixels.min(axis=1).astype(np.int16)
    return bool(np.mean(diffs > 18) > 0.015)


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


def document_preview_payload(doc: fitz.Document, preview_page: int = 1) -> tuple[str, list[dict[str, object]]]:
    total = len(doc)
    if total <= 0:
        return "", []

    preview_index = max(0, min(int(preview_page or 1) - 1, total - 1))
    preview = page_png_data_url(doc.load_page(preview_index), 980, 1320)
    thumbs: list[dict[str, object]] = []
    for idx in range(min(total, 24)):
        page = doc.load_page(idx)
        thumbs.append(
            {
                "page": idx + 1,
                "image": page_png_data_url(page, 120, 170),
            }
        )
    return preview, thumbs


def estimate_color_pages_document(doc: fitz.Document) -> int:
    total = len(doc)
    if total <= 0:
        return 0
    count = 0
    limit = min(total, 80)
    for idx in range(limit):
        page = doc.load_page(idx)
        img = render_page_rgb(page, 72)
        if pil_image_has_color(img, max_pixels=60_000):
            count += 1
    if limit < total:
        count = round(count * total / limit)
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


def edited_target_rect(page_rect: fitz.Rect, page_edits: dict[str, object] | None) -> fitz.Rect:
    crop = latest_crop_box(page_edits)
    if not crop:
        return page_rect
    width = max(1.0, page_rect.width * clamp_pct(crop.get("w")) / 100.0)
    height = max(1.0, page_rect.height * clamp_pct(crop.get("h")) / 100.0)
    return fitz.Rect(0, 0, width, height)


def image_page_rect(image: Image.Image) -> fitz.Rect:
    return fitz.Rect(0, 0, image.width * 72.0 / DEFAULT_DPI, image.height * 72.0 / DEFAULT_DPI)


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


def insert_pil_page(target: fitz.Document, rect: fitz.Rect, image: Image.Image, options: UiOptions, color: bool) -> None:
    page = target.new_page(width=rect.width, height=rect.height)
    use_jpeg = color or options.compress_pdf
    stream, _fmt = image_stream(image, options.jpeg_quality(), use_jpeg)
    page.insert_image(page.rect, stream=stream)


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
        for idx in range(total):
            if cancel_requested():
                raise RuntimeError("Обработка отменена")

            page_num = idx + 1
            pct = int((idx / max(1, total)) * 100)
            progress(pct, f"Стр. {page_num}/{total} · рендер страницы")

            page = source.load_page(idx)
            edits = page_edits.get(page_num, {}) if page_edits else {}
            skip = bool(edits.get("skip")) or (idx == 0 and options.skip_first) or (idx == total - 1 and options.skip_last)
            has_page_edits = has_page_visual_edits(edits)
            if skip and not has_page_edits:
                target.insert_pdf(source, from_page=idx, to_page=idx)
                progress(int((page_num / total) * 100), f"Стр. {page_num}/{total} · без очистки")
                continue

            rgb = render_page_rgb(page, DEFAULT_DPI)
            if skip:
                progress(pct, f"Стр. {page_num}/{total} · правки без очистки")
                out_img = apply_page_edits(rgb, rgb, edits)
                insert_pil_page(target, image_page_rect(out_img), out_img, options, color=True)
                processed_count += 1
                progress(int((page_num / total) * 100), f"Стр. {page_num}/{total} · готово")
                continue

            is_color = options.keep_color and pil_image_has_color(rgb)
            if is_color:
                color_count += 1
                progress(pct, f"Стр. {page_num}/{total} · сохранение цвета")
                out_img = adjust_pil_image(rgb, options.brightness, options.contrast)
                out_img = apply_page_edits(out_img, rgb, edits)
                insert_pil_page(target, image_page_rect(out_img), out_img, options, color=True)
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
                insert_pil_page(
                    target,
                    image_page_rect(out_img),
                    out_img,
                    options,
                    color=out_img.mode == "RGB",
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
        self.window.output_path = Path(path).expanduser() if path else None

    @Slot(str, str)
    def setOption(self, name: str, value: str):
        self.window.set_option(name, value)

    @Slot(str)
    def setMode(self, value: str):
        self.window.set_mode(value)

    @Slot(int)
    def renderPage(self, page: int):
        self.window.render_page_preview(page)

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
        self.worker: PdfLoadWorker | CleanWorker | ImageImportWorker | None = None

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

    def mark_result_stale(self) -> None:
        if self.last_output_path is not None:
            self.last_output_path = None
            self.refresh_download_state()

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
                setattr(self.options, name, int(float(value)))
                self.mark_result_stale()
            except ValueError:
                pass
        elif name in bool_names:
            setattr(self.options, name, bool_value)
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
        out = unique_output_path(downloads_dir() / f"{folder_path.name}_CrystalPDF.pdf")
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
                preview = page_png_data_url(doc.load_page(page_number - 1), 980, 1320)
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
        if not self.has_processed_output():
            self.refresh_download_state()
            QMessageBox.information(self, APP_TITLE, "Сначала запустите очистку и дождитесь завершения обработки.")
            return

        target, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить PDF",
            str(self.last_output_path),
            "PDF (*.pdf)",
        )
        if target:
            output = Path(target)
            if output.suffix.lower() != ".pdf":
                output = output.with_suffix(".pdf")
            shutil.copyfile(self.last_output_path, output)
            self.ui_call("setProgress", 100, f"Экспорт готов: {output.name}")

    def export_current_page(self) -> None:
        if self.is_busy():
            return
        if not self.has_processed_output():
            self.refresh_download_state()
            QMessageBox.information(self, APP_TITLE, "Сначала запустите очистку и дождитесь завершения обработки.")
            return

        source_path = self.last_output_path
        default_stem = (self.input_path or source_path or Path("scan")).stem
        default_name = downloads_dir() / f"{default_stem}_page_{self.current_page}.pdf"
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
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Добавить страницы из PDF",
            str(Path.home()),
            "PDF (*.pdf)",
        )
        if not path:
            return
        incoming = Path(path)
        if not self.input_path:
            self.load_pdf(incoming)
            return

        output = unique_output_path(downloads_dir() / f"{self.input_path.stem}_plus_{incoming.stem}_CrystalPDF.pdf")
        try:
            source = fitz.open(str(self.input_path))
            added = fitz.open(str(incoming))
            result = fitz.open()
            try:
                result.insert_pdf(source)
                first_added_page = len(result) + 1
                result.insert_pdf(added)
                result.save(str(output), garbage=4, deflate=True, clean=True)
            finally:
                result.close()
                added.close()
                source.close()
        except Exception as exc:
            QMessageBox.critical(self, APP_TITLE, f"Не удалось добавить страницы: {exc}")
            return

        self.load_pdf(output, first_added_page)
        self.ui_call("setProgress", 100, f"Добавлены страницы: {incoming.name}")

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
        output = unique_output_path(downloads_dir() / f"{self.input_path.stem}_without_page_{deleted_page}.pdf")
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

    def rotate_current_page(self, degrees: int) -> None:
        if self.is_busy():
            return
        if not self.input_path:
            QMessageBox.information(self, APP_TITLE, "Сначала импортируйте PDF.")
            return

        output = unique_output_path(downloads_dir() / f"{self.input_path.stem}_rotated_CrystalPDF.pdf")
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

    def start_cleaning(self, export_after: bool = False) -> None:
        if self.is_busy():
            return
        if not self.input_path:
            QMessageBox.information(self, APP_TITLE, "Сначала импортируйте PDF.")
            return
        output = self.output_path or default_output_path(self.input_path)
        if output.suffix.lower() != ".pdf":
            output = output.with_suffix(".pdf")
        output = unique_output_path(output)
        self.output_path = output
        self.last_output_path = None
        self.ui_call("setOutput", str(output))
        self.refresh_download_state()
        self.ui_call("setProcessing", True, "Идёт очистка")
        self.ui_call("setProgress", 0, "Запуск обработки…")
        self.ui_call("setStatus", "◉ Обработка", "working")

        worker = CleanWorker(self.input_path, output, copy.deepcopy(self.options), copy.deepcopy(self.page_edits))
        self.worker = worker
        worker.progress_changed.connect(lambda pct, text: self.ui_call("setProgress", pct, text))
        worker.finished_ok.connect(self.on_clean_finished)
        worker.failed.connect(self.on_worker_failed)
        worker.start()

    def on_clean_finished(self, output: str, colors: int, processed: int) -> None:
        self.worker = None
        self.last_output_path = Path(output)
        self.ui_call("setProcessing", False)
        self.refresh_download_state()
        self.ui_call("setProgress", 100, f"Готово → {self.last_output_path.name}")
        self.ui_call("setStats", self.page_count, max(self.color_count, colors), processed, 0)
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
