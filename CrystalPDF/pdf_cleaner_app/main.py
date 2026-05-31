"""
CrystalPDF v1.1.0 — настольное приложение
============================================
Полный рефакторинг под новый интерфейс:
  • Двухпанельная компоновка (сайдбар + предпросмотр)
  • Просмотр страниц с масштабированием
  • Инструмент «Ластик» с рисованием прямо на странице
  • Поворот страниц (±90°) с сохранением в PDF
  • Полоса миниатюр с цветными индикаторами статуса
  • Сохранение цветных страниц без бинаризации
  • Статусы: серый=ожидание, синий=обработка, зелёный=готово, красный=ошибка
  • Все алгоритмы v1.1.0: выравнивание наклона, NL-Means, очистка краёв, удаление точек
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import os
import queue
import math
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
import base64
import json
import subprocess
import sys

from pdf_cleaner import CleanSettings, clean_page_image, edge_artifact_mask, estimate_deskew_angle


# ──────────────────────────────────────────────────────────────────────────────
#  Поддержка HiDPI — ОБЯЗАТЕЛЬНО до создания tk.Tk()
#  Без этого Windows растягивает окно как картинку → размытый текст на 4K
# ──────────────────────────────────────────────────────────────────────────────
def _enable_dpi_awareness():
    """DPI-aware режим для каждого монитора на Windows. На macOS/Linux ничего не делает."""
    try:
        from ctypes import windll
        for fn, arg in (
            (lambda: windll.shcore.SetProcessDpiAwareness(2), None),   # режим на монитор, версия 2
            (lambda: windll.shcore.SetProcessDpiAwareness(1), None),   # режим на монитор, версия 1
            (lambda: windll.user32.SetProcessDPIAware(), None),        # системный режим
        ):
            try:
                fn()
                return
            except Exception:
                continue
    except Exception:
        pass


_enable_dpi_awareness()

DEFAULT_BRIGHTNESS = 0
DEFAULT_CONTRAST = 100
DEFAULT_EDGE_MARGIN = 5
APP_NAME = "CrystalPDF"
APP_VERSION = "v1.1.0"
APP_TITLE = f"{APP_NAME} {APP_VERSION}"
MAX_PROTECTED_BOXES_PER_PAGE = 15
LARGE_DOCUMENT_PAGE_LIMIT = 300
AUTO_COLOR_DETECT_PAGE_LIMIT = 250
ASYNC_PAGE_RENDER_PAGE_LIMIT = 40
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
PROJECT_SCAN_EXCLUDE_DIRS = {
    ".git",
    ".idea",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}
COMPRESSION_LEVELS = {
    "light": {"label": "Лёгкое", "dpi": 240, "quality": 88},
    "medium": {"label": "Среднее", "dpi": 200, "quality": 78},
    "strong": {"label": "Сильное", "dpi": 150, "quality": 62},
}
COMPRESSION_SCOPES = {
    "all": "Все страницы",
    "color": "Только цветные",
    "processed": "Только очищаемые",
}
DESKTOP_SHORTCUT_NAME = APP_TITLE
LEGACY_DESKTOP_SHORTCUT_NAMES = ("CrystalPDF", "Mini_Icon_CrystalPDF")
DESKTOP_SHORTCUT_NEVER_ASK_SETTING = "desktop_shortcut_never_ask"
LEGACY_DESKTOP_SHORTCUT_PROMPT_DISABLED_SETTING = "desktop_shortcut_prompt_disabled"


def _edge_cleanup_allowed(page_idx, page_count, edge_clean, color_pages):
    if not edge_clean:
        return False
    if page_idx <= 0 or page_idx >= page_count - 1:
        return False
    return not bool(color_pages.get(page_idx, False))


# Глобальная ссылка на UIScale — чтобы помощники уровня модуля
# (styled_btn, make_slider, sep) могли масштабировать отступы.
_UI = None


def _set_global_ui(ui):
    global _UI
    _UI = ui


def _spx(n):
    """Безопасный масштаб пикселя: умножает на текущий масштаб интерфейса, если он задан."""
    return _UI.px(n) if _UI is not None else n

# ──────────────────────────────────────────────────────────────────────────────
#  Цветовая палитра
# ──────────────────────────────────────────────────────────────────────────────
BG0       = "#0b0d14"
BG1       = "#10131d"
BG2       = "#141826"
BG3       = "#1a2035"
BDR       = "#252d45"
BDR2      = "#2e3756"
TXT0      = "#e2e6f8"
TXT1      = "#9aa0c0"
TXT2      = "#5a6180"
TXT3      = "#363d58"
BLUE      = "#4f7ff7"
BLUE2     = "#2d5ae0"
BLUE_BG   = "#111d3a"
BLUE_BDR  = "#1e3060"
GREEN     = "#3ac97a"
GREEN_BG  = "#0d2018"
GREEN_BDR = "#1a4030"
RED       = "#f55252"
RED_BG    = "#22100d"
RED_BDR   = "#401818"
AMBER     = "#f7c84e"
AMBER_BG  = "#211900"
AMBER_BDR = "#3d3000"
CYAN      = "#34d2cf"
CYAN_BG   = "#082023"
CYAN_BDR  = "#14595c"
WHITE_PAGE = "#ffffff"

# Статусные цвета
STATUS_COLORS = {
    "idle":    TXT3,
    "waiting": TXT3,
    "working": BLUE,
    "done":    GREEN,
    "error":   RED,
    "skipped": AMBER,
    "cancelled": AMBER,
}


# ──────────────────────────────────────────────────────────────────────────────
#  Вспомогательные виджеты
# ──────────────────────────────────────────────────────────────────────────────

def styled_btn(parent, text, command, fg=TXT1, bg=BG2, active_bg=BG3,
               width=None, pady=6, padx=10, font_size=10, bold=False):
    weight = "bold" if bold else "normal"
    pady_px = _spx(pady)
    padx_px = _spx(padx)
    kw = {"width": width} if width else {}
    btn = tk.Button(
        parent, text=text, command=command,
        font=("Segoe UI", font_size, weight),
        fg=fg, bg=bg, activebackground=active_bg, activeforeground=TXT0,
        relief="flat", bd=0, cursor="hand2",
        padx=padx_px, pady=pady_px,
        highlightthickness=1, highlightbackground=BDR,
        **kw
    )
    return btn


def make_slider(parent, label, variable, from_, to, tip="", bg=BG1):
    """Строка: подпись + значение + ползунок + подсказка."""
    row = tk.Frame(parent, bg=bg)
    row.pack(fill="x", pady=(0, _spx(10)))

    hdr = tk.Frame(row, bg=bg)
    hdr.pack(fill="x")
    tk.Label(
        hdr, text=label, font=("Segoe UI", 9), fg=TXT2, bg=bg,
        anchor="w", justify="left", wraplength=_spx(205)
    ).pack(side="left", fill="x", expand=True)

    val_lbl = tk.Label(hdr, text=str(variable.get()),
                       font=("Courier New", 9, "bold"),
                       fg=BLUE, bg=bg, width=5, anchor="e")
    val_lbl.pack(side="right")

    def on_change(v):
        val_lbl.config(text=str(int(float(v))))

    def on_var_change(*_):
        try:
            val_lbl.config(text=str(int(variable.get())))
        except tk.TclError:
            pass

    variable.trace_add("write", on_var_change)

    style = ttk.Style()
    style.configure("Sl.Horizontal.TScale", background=bg,
                    troughcolor=BDR2, sliderlength=_spx(16))
    ttk.Scale(row, from_=from_, to=to, variable=variable,
              orient="horizontal", command=on_change,
              style="Sl.Horizontal.TScale").pack(fill="x", pady=(_spx(3), 0))

    if tip:
        tk.Label(row, text=tip, font=("Segoe UI", 8), fg=TXT3, bg=bg, anchor="w",
                 wraplength=_spx(190)).pack(fill="x", pady=(_spx(1), 0))
    return row


def sep(parent, bg=BG1, orient="h"):
    if orient == "h":
        tk.Frame(parent, bg=BDR, height=1).pack(fill="x")
    else:
        tk.Frame(parent, bg=BDR, width=1).pack(fill="y", side="left")


# ──────────────────────────────────────────────────────────────────────────────
#  UIScale — масштабирование интерфейса «как игровые текстуры»
#
#  Идея: при смене разрешения окна или DPI экрана пересчитываем три величины:
#    • dpi_scale   — физическая плотность пикселей (Retina/4K/8K)
#    • win_scale   — масштаб относительно эталона 1280×720
#    • render_dpr  — множитель для PDF-рендера (эквивалент уровня детализации mipmap)
#
#  Применяется через `tk scaling` — это меняет конверсию пунктов в пиксели в Tk,
#  и ВСЕ шрифты в пунктах пересчитываются автоматом.
#  Пиксельные паддинги/размеры приходится масштабировать вручную через _spx().
# ──────────────────────────────────────────────────────────────────────────────

class UIScale:
    REF_W, REF_H = 1280, 720          # эталонное «1.0x» разрешение
    MIN_SCALE, MAX_SCALE = 0.85, 2.2  # защита от крайностей

    def __init__(self, app):
        self.app = app
        # Что Tk сам выставил после _enable_dpi_awareness — это база.
        self._base_tk_scaling = float(app.tk.call("tk", "scaling"))
        # Физический DPI экрана (для mipmap-рендера PDF и render_dpr)
        self.dpi = float(app.winfo_fpixels("1i"))
        self.dpi_scale = max(1.0, round(self.dpi / 96.0, 2))

        self.win_scale = 1.0
        self.scale = 1.0
        self.render_dpr = self.dpi_scale  # для FitZ matrix

        self.recompute(*app._current_resolution, apply=True)

    def recompute(self, win_w, win_h, apply=True):
        """Пересчитать масштаб под текущий размер окна."""
        size = min(win_w / self.REF_W, win_h / self.REF_H)
        self.win_scale = max(self.MIN_SCALE, min(self.MAX_SCALE, round(size, 2)))
        self.scale = self.win_scale
        # render_dpr учитывает И физический DPI, И масштаб окна —
        # чтобы на 4K-мониторе с большим окном PDF был особенно чётким
        self.render_dpr = self.dpi_scale * max(1.0, self.win_scale)
        if apply:
            # Главная магия: меняем tk scaling, и все шрифты в пунктах пересчитаются
            self.app.tk.call("tk", "scaling",
                             self._base_tk_scaling * self.win_scale)

    def px(self, n):
        """Масштабирует пиксельную величину (padx/pady/width/height)."""
        if n <= 0:
            return 0
        return max(1, int(round(n * self.scale)))


# ──────────────────────────────────────────────────────────────────────────────
#  Главное приложение
# ──────────────────────────────────────────────────────────────────────────────

class PdfCleanerApp(tk.Tk):
    # ── ИНИЦИАЛИЗАЦИЯ ─────────────────────────────────────────────────────────
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        win_w = max(980, min(1320, sw - 90))
        win_h = max(680, min(860, sh - 120))
        self._current_resolution = (win_w, win_h)
        self.geometry(f"{win_w}x{win_h}")
        self.minsize(min(980, sw - 40), min(640, sh - 80))
        self.configure(bg=BG0)
        self.resizable(True, True)

        # --- Система масштабирования интерфейса (DPI + размер окна) ---
        self._ui = UIScale(self)
        _set_global_ui(self._ui)

        # --- Состояние ---
        self._queue       = queue.Queue()
        self._processing  = False
        self._importing   = False
        self._import_generation = 0
        self._cancel_requested = threading.Event()
        self._processing_before_state = None
        self._doc         = None          # fitz.Document
        self._input_path  = None
        self._page_count  = 0
        self._current_page = 0            # 0-based
        self._zoom_steps = [
            0.15, 0.25, 0.33, 0.50, 0.67, 0.75, 0.90, 1.00,
            1.10, 1.25, 1.50, 1.75, 2.00, 2.50, 3.00, 4.00, 5.00,
        ]
        self._zoom        = 1.0
        self._tool        = "pan"         # "view" | "pan" | "eraser" | "crop" | "protect"
        self._sidebar_collapsed = False
        self._sidebar_width = self._ui.px(326)
        self._sidebar_resize_start = None
        self._page_photo  = None          # ImageTk.PhotoImage текущей страницы
        self._pan_x       = 0
        self._pan_y       = 0
        self._pan_anchor  = None

        # Поворот страниц {индекс_страницы: градусы}
        self._rotations   = {}

        # Маски ластика {индекс_страницы: [(доля_x, доля_y, доля_радиуса), ...]}
        self._eraser_masks = {}
        self._stroke_before = None

        # Статусы страниц: ожидание, обработка, готово, ошибка
        self._page_status = {}

        # Цветные страницы {индекс_страницы: да/нет}
        self._color_pages = {}
        self._color_detect_generation = 0
        self._color_detect_job = None
        self._thumb_containers = {}
        self._thumb_rendered = set()
        self._thumb_render_queue = []
        self._thumb_render_job = None
        self._thumb_visible_job = None
        self._thumb_build_job = None
        self._thumb_build_generation = 0
        self._thumb_build_next = 0
        self._thumb_build_done_callback = None
        self._page_render_generation = 0
        self._page_render_after_job = None
        self._page_render_active = False
        self._page_render_pending = None
        self._render_source_path_usable = False
        self._render_source_path = None
        self._temporary_import_pdf = None
        self._color_detecting = False
        self._color_detect_scanned = 0
        self._color_detect_total = 0
        self._project_scan_watch_job = None
        self._skip_pages = {}
        self._page_adjustments = {}
        self._adjustment_controls_page = None
        self._loading_page_adjustments = False
        self._crop_boxes = {}
        self._crop_start = None
        self._crop_preview_id = None
        self._protected_boxes = {}
        self._protect_start = None
        self._display_box = (0.0, 0.0, 1.0, 1.0)
        self._undo_stack = []
        self._redo_stack = []
        self._preview_adjust_job = None
        self._show_edge_zone = True

        # Счётчик правок
        self._edit_count  = 0

        # Масштаб: пиксели на холсте → доля страницы (обновляется при рендеринге)
        self._page_render_scale = 1.0   # px/px
        self._page_render_ox = 0        # offset canvas X
        self._page_render_oy = 0        # offset canvas Y
        self._page_render_w  = 0
        self._page_render_h  = 0
        self._page_full_render_w = 0
        self._page_full_render_h = 0
        self._page_view_w_pt = 0
        self._page_view_h_pt = 0

        self._build_ui()
        self._set_tool("pan")
        self._update_page_flags_ui()
        self.bind_all("<Control-z>", lambda _e: self._undo())
        self.bind_all("<Control-y>", lambda _e: self._redo())
        self.bind_all("<Control-Z>", lambda _e: self._redo())
        self._poll_queue()
        self._update_history_buttons()
        self._update_status_bar()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(250, self._handle_startup_source)
        self._project_scan_watch_job = self.after(3000, self._watch_project_scan_folder)
        self.after(650, self._maybe_prompt_desktop_shortcut)

    # ── ОКНО СОЗДАНИЯ ЯРЛЫКА ──────────────────────────────────────────────────
    def _settings_path(self):
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        return base / "CrystalPDF" / "settings.json"

    def _load_user_settings(self):
        path = self._settings_path()
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}

    def _save_user_settings(self, settings):
        try:
            path = self._settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def _desktop_dir(self):
        for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            root = os.environ.get(var)
            if root:
                candidate = Path(root) / "Desktop"
                if candidate.exists():
                    return candidate
        desktop = Path.home() / "Desktop"
        return desktop

    def _desktop_shortcut_path(self):
        return self._desktop_dir() / f"{DESKTOP_SHORTCUT_NAME}.lnk"

    def _normalize_desktop_shortcut_name(self):
        shortcut_path = self._desktop_shortcut_path()
        for name in LEGACY_DESKTOP_SHORTCUT_NAMES:
            legacy_path = self._desktop_dir() / f"{name}.lnk"
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

    def _maybe_prompt_desktop_shortcut(self):
        if os.name != "nt":
            return
        settings = self._load_user_settings()
        if settings.get(DESKTOP_SHORTCUT_NEVER_ASK_SETTING):
            return
        if LEGACY_DESKTOP_SHORTCUT_PROMPT_DISABLED_SETTING in settings:
            settings.pop(LEGACY_DESKTOP_SHORTCUT_PROMPT_DISABLED_SETTING, None)
            self._save_user_settings(settings)
        if self._normalize_desktop_shortcut_name():
            return
        self._show_desktop_shortcut_prompt(settings)

    def _show_desktop_shortcut_prompt(self, settings):
        dialog = tk.Toplevel(self)
        dialog.title(f"Ярлык {APP_TITLE}")
        dialog.configure(bg=BG1)
        dialog.resizable(False, False)
        dialog.transient(self)

        try:
            dialog.iconbitmap(str(Path(__file__).with_name("icon.ico")))
        except Exception:
            pass

        frame = tk.Frame(dialog, bg=BG1, padx=18, pady=16)
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame, text="Создать ярлык на рабочем столе?",
            font=("Segoe UI", 12, "bold"), fg=TXT0, bg=BG1
        ).pack(anchor="w")
        tk.Label(
            frame,
            text=f"Будет создан ярлык «{DESKTOP_SHORTCUT_NAME}» для быстрого запуска {APP_TITLE}.",
            font=("Segoe UI", 9), fg=TXT2, bg=BG1,
            wraplength=360, justify="left"
        ).pack(anchor="w", pady=(8, 10))

        dont_show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            frame, variable=dont_show_var,
            text="Больше не показывать",
            font=("Segoe UI", 9),
            bg=BG1, activebackground=BG1,
            fg=TXT1, activeforeground=TXT0,
            selectcolor=BG0
        ).pack(anchor="w", pady=(0, 14))

        buttons = tk.Frame(frame, bg=BG1)
        buttons.pack(fill="x")

        def suppress_prompt():
            settings[DESKTOP_SHORTCUT_NEVER_ASK_SETTING] = True
            settings.pop(LEGACY_DESKTOP_SHORTCUT_PROMPT_DISABLED_SETTING, None)
            self._save_user_settings(settings)

        def close_without_create():
            if dont_show_var.get():
                suppress_prompt()
            dialog.destroy()

        def create_shortcut():
            ok, err = self._create_desktop_shortcut()
            if ok:
                settings.pop(LEGACY_DESKTOP_SHORTCUT_PROMPT_DISABLED_SETTING, None)
                self._save_user_settings(settings)
                dialog.destroy()
                self._sb_status_var.set(f"Ярлык создан: {DESKTOP_SHORTCUT_NAME}")
            else:
                messagebox.showerror("Не удалось создать ярлык", err or "Неизвестная ошибка")

        styled_btn(
            buttons, "Создать ярлык", create_shortcut,
            fg="white", bg=BLUE, active_bg=BLUE2,
            pady=5, padx=10
        ).pack(side="right", padx=(8, 0))
        styled_btn(
            buttons, "Не сейчас", close_without_create,
            fg=TXT1, bg=BG2, pady=5, padx=10
        ).pack(side="right")

        dialog.protocol("WM_DELETE_WINDOW", close_without_create)
        dialog.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - dialog.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()
        dialog.focus_force()

    def _create_desktop_shortcut(self):
        try:
            shortcut_path = self._desktop_shortcut_path()
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
                icon_path = Path(__file__).with_name("icon.ico")
                if not icon_path.exists():
                    icon_path = target_path

            def ps_quote(value):
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
                capture_output=True, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=20,
            )
            if result.returncode != 0:
                return False, (result.stderr or result.stdout or "PowerShell завершился с ошибкой").strip()
            return True, ""
        except Exception as e:
            return False, str(e)

    def _on_close(self):
        self._cancel_requested.set()
        self._cancel_thumb_render_job()
        self._cancel_thumb_build_job()
        self._cancel_page_render_jobs()
        if self._project_scan_watch_job is not None:
            try:
                self.after_cancel(self._project_scan_watch_job)
            except Exception:
                pass
            self._project_scan_watch_job = None
        self._color_detect_generation += 1
        old_doc = self._doc
        self._doc = None
        if old_doc is not None:
            try:
                old_doc.close()
            except Exception:
                pass
        temp_pdf = self._temporary_import_pdf
        self._temporary_import_pdf = None
        if temp_pdf:
            try:
                Path(temp_pdf).unlink(missing_ok=True)
            except Exception:
                pass
        self.destroy()

    def _handle_startup_source(self):
        if self._doc is not None or self._importing or self._processing:
            return

        source = self._startup_source_from_argv()
        if source is None:
            source = self._find_project_scan_folder()
        if source is None:
            return

        if source.is_file() and source.suffix.lower() == ".pdf":
            self.output_var.set(str(self._default_output_path(source)))
            self._load_pdf(str(source))
            return

        if source.is_dir():
            output_path = self._project_scan_output_path(source)
            if output_path is None:
                existing_pdf = source.with_suffix(".pdf")
                if existing_pdf.exists():
                    self.output_var.set(str(self._default_output_path(existing_pdf)))
                    self._load_pdf(str(existing_pdf))
                return
            self.output_var.set(str(output_path))
            self._load_image_folder(str(source), save_path=output_path)

    def _watch_project_scan_folder(self):
        self._project_scan_watch_job = None
        try:
            if self._doc is None and not self._importing and not self._processing:
                source = self._find_project_scan_folder()
                if source is not None:
                    output_path = self._project_scan_output_path(source)
                    if output_path is not None:
                        self.output_var.set(str(output_path))
                        self._load_image_folder(str(source), save_path=output_path)
                        return
        finally:
            if self._doc is None and not self._importing and not self._processing:
                self._project_scan_watch_job = self.after(3000, self._watch_project_scan_folder)

    def _startup_source_from_argv(self):
        for raw_arg in sys.argv[1:]:
            if not raw_arg:
                continue
            path = Path(raw_arg.strip('"')).expanduser()
            if path.exists():
                return path
        return None

    def _find_project_scan_folder(self):
        roots = []
        for root in (Path(__file__).resolve().parent, Path.cwd()):
            if root.exists() and root not in roots:
                roots.append(root)

        candidates = []
        for root in roots:
            try:
                children = list(root.iterdir())
            except Exception:
                continue
            for child in children:
                if not child.is_dir() or child.name in PROJECT_SCAN_EXCLUDE_DIRS:
                    continue
                if child.name.startswith("."):
                    continue
                if not _list_image_files(child, recursive=False):
                    continue
                output_path = self._project_scan_output_path(child)
                if output_path is None:
                    continue
                candidates.append(child)

        unique = []
        seen = set()
        for candidate in candidates:
            try:
                key = candidate.resolve()
            except Exception:
                key = candidate
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)

        return unique[0] if len(unique) == 1 else None

    def _project_scan_output_path(self, folder):
        folder = Path(folder)
        images = _list_image_files(folder, recursive=True)
        if not images:
            return None
        output_path = folder.with_suffix(".pdf")
        if output_path.exists():
            try:
                newest_image = max(path.stat().st_mtime for path in images)
                if output_path.stat().st_mtime >= newest_image:
                    return None
            except Exception:
                pass
        return output_path

    # ── СБОРКА ИНТЕРФЕЙСА ─────────────────────────────────────────────────────
    def _build_ui(self):
        self._build_titlebar()

        body = tk.Frame(self, bg=BG0)
        body.pack(fill="both", expand=True)
        self._body = body

        self._sidebar = tk.Frame(body, bg=BG1, width=self._sidebar_width)
        if not self._sidebar_collapsed:
            self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)

        self._sidebar_sash = tk.Frame(
            body, bg=BDR2, width=self._ui.px(8), cursor="sb_h_double_arrow")
        if not self._sidebar_collapsed:
            self._sidebar_sash.pack(side="left", fill="y")
        self._sidebar_sash.bind("<Button-1>", self._sidebar_resize_begin)
        self._sidebar_sash.bind("<B1-Motion>", self._sidebar_resize_drag)
        self._sidebar_sash.bind("<ButtonRelease-1>", self._sidebar_resize_end)

        main_area = tk.Frame(body, bg=BG0)
        self._main_area = main_area
        main_area.pack(side="left", fill="both", expand=True)

        self._build_sidebar()
        self._build_main(main_area)
        self._build_statusbar()

    # ── ВЕРХНЯЯ ПАНЕЛЬ ────────────────────────────────────────────────────────
    def _build_titlebar(self):
        tb = tk.Frame(self, bg="#0a0c12", height=self._ui.px(38))
        tb.pack(fill="x")
        tb.pack_propagate(False)

        for color in ("#f76b6b", "#f7c94f", "#34c97b"):
            tk.Frame(tb, bg=color,
                     width=self._ui.px(12), height=self._ui.px(12),
                     cursor="hand2").pack(
                side="left",
                padx=(self._ui.px(10) if color == "#f76b6b" else self._ui.px(5), 0),
                pady=self._ui.px(13))

        self._btn_sidebar = styled_btn(
            tb, "☰", self._toggle_sidebar,
            fg=TXT1, bg="#0a0c12", active_bg=BG2,
            pady=3, padx=7, width=2)
        self._btn_sidebar.pack(side="left", padx=(12, 0), pady=6)

        self._title_label = tk.Label(
            tb, text=f"✦  {APP_TITLE}  ·  нет файла",
            font=("Courier New", 10), fg=TXT2, bg="#0a0c12")
        self._title_label.pack(side="left", padx=(14, 0))

        self._badge_label = tk.Label(
            tb, text="● Готов к работе",
            font=("Segoe UI", 9, "bold"), fg=GREEN, bg="#0a0c12")
        self._badge_label.pack(side="right", padx=16)

    def _toggle_sidebar(self):
        if not hasattr(self, "_sidebar"):
            return

        if self._sidebar_collapsed:
            self._sidebar.pack(side="left", fill="y", before=self._main_area)
            self._sidebar_sash.pack(side="left", fill="y", before=self._main_area)
            self._sidebar_collapsed = False
            self._btn_sidebar.config(fg=TXT1, bg="#0a0c12")
        else:
            self._sidebar.pack_forget()
            self._sidebar_sash.pack_forget()
            self._sidebar_collapsed = True
            self._btn_sidebar.config(fg=BLUE, bg=BLUE_BG)

        self.after(80, self._render_page)

    def _set_sidebar_width(self, width, render=True):
        win_w = max(1, self.winfo_width())
        min_w = self._ui.px(270)
        max_w = max(min_w, min(self._ui.px(470), win_w - self._ui.px(64)))
        previous_width = self._sidebar_width
        self._sidebar_width = max(min_w, min(int(width), max_w))
        if hasattr(self, "_sidebar"):
            self._sidebar.config(width=self._sidebar_width)
        if (
            hasattr(self, "_mode_hint_lbl")
            and abs(self._sidebar_width - previous_width) >= self._ui.px(12)
        ):
            hint_width = max(self._ui.px(190), self._sidebar_width - self._ui.px(56))
            for widget in (self._mode_hint_lbl, self._color_info_lbl, self._clean_limit_hint_lbl):
                try:
                    widget.config(wraplength=hint_width)
                except tk.TclError:
                    pass
        if render:
            self.after(80, self._render_page)

    def _sidebar_resize_begin(self, event):
        self._sidebar_resize_start = (event.x_root, self._sidebar_width)
        self._sidebar_sash.config(bg=BLUE)

    def _sidebar_resize_drag(self, event):
        if not self._sidebar_resize_start:
            return
        start_x, start_width = self._sidebar_resize_start
        self._set_sidebar_width(start_width + (event.x_root - start_x), render=False)

    def _sidebar_resize_end(self, _event):
        self._sidebar_resize_start = None
        if hasattr(self, "_sidebar_sash"):
            self._sidebar_sash.config(bg=BDR2)
        self.after(80, self._render_page)

    def _is_child_of(self, widget, parent):
        while widget is not None:
            if widget == parent:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _sidebar_mousewheel(self, event):
        if self._sidebar_collapsed:
            return None
        sidebar = getattr(self, "_sidebar", None)
        sc = getattr(self, "_sidebar_scroll_canvas", None)
        if sidebar is None or sc is None:
            return None
        if not self._is_child_of(event.widget, sidebar):
            return None

        if hasattr(event, "num") and event.num in (4, 5):
            units = -3 if event.num == 4 else 3
        else:
            delta = event.delta or 0
            units = -1 * int(delta / 120) if delta else 0
            if units == 0 and delta:
                units = -1 if delta > 0 else 1
        if units:
            sc.yview_scroll(units, "units")
        return "break"

    # ── БОКОВАЯ ПАНЕЛЬ ────────────────────────────────────────────────────────
    def _build_sidebar(self):
        sb = self._sidebar

        # Логотип
        logo = tk.Frame(sb, bg=BG1)
        logo.pack(fill="x")
        tk.Label(logo, text=f"✦  {APP_NAME}",
                 font=("Segoe UI", 13, "bold"), fg=TXT0, bg=BG1,
                 anchor="w").pack(fill="x", padx=14, pady=(14, 2))
        tk.Label(logo, text=f"{APP_VERSION} — очистка сканов",
                 font=("Courier New", 9), fg=TXT3, bg=BG1,
                 anchor="w").pack(fill="x", padx=14, pady=(0, 12))
        sep(sb)

        # Область прокрутки
        sc = tk.Canvas(sb, bg=BG1, highlightthickness=0)
        vsb = tk.Scrollbar(sb, orient="vertical", command=sc.yview)
        sc.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        sc.pack(fill="both", expand=True)

        inner = tk.Frame(sc, bg=BG1)
        sc_win = sc.create_window((0, 0), window=inner, anchor="nw")

        def _cfg_inner(e): sc.configure(scrollregion=sc.bbox("all"))
        def _cfg_canvas(e): sc.itemconfig(sc_win, width=e.width)
        inner.bind("<Configure>", _cfg_inner)
        sc.bind("<Configure>", _cfg_canvas)
        self._sidebar_scroll_canvas = sc
        sc.bind_all("<MouseWheel>", self._sidebar_mousewheel)
        sc.bind_all("<Button-4>", self._sidebar_mousewheel)
        sc.bind_all("<Button-5>", self._sidebar_mousewheel)

        # ── Файл ──────────────────────────────────────────────────────────────
        file_sec = self._sidebar_card(
            inner, "Документ",
            "Основной PDF, статус выбранного файла и путь сохранения.",
            BLUE)

        self._import_btn = styled_btn(file_sec, "⬆  Импорт PDF",
                                      self._browse_input, fg=BLUE, bg=BLUE_BG,
                                      padx=12, pady=8, font_size=10)
        self._import_btn.pack(fill="x", pady=(0, 5))
        self._import_btn.config(highlightbackground=BLUE_BDR)

        self._export_btn = styled_btn(
            file_sec, "⬇  Экспорт PDF",
            self._export_session,
            fg=GREEN, bg=GREEN_BG,
            padx=12, pady=8, font_size=10)
        self._export_btn.pack(fill="x", pady=(0, 5))
        self._export_btn.config(highlightbackground=GREEN_BDR)

        # Чип выбранного файла
        self._file_chip = tk.Label(
            file_sec, text="нет файла",
            font=("Courier New", 8), fg=TXT3, bg=BG0,
            anchor="w", padx=8, pady=5,
            relief="flat",
            highlightthickness=1, highlightbackground=BDR)
        self._file_chip.pack(fill="x", pady=(0, 8))

        # ── Выходной файл ──────────────────────────────────────────────────────
        self._sidebar_field_label(file_sec, "Выходной файл")

        self.output_var = tk.StringVar()
        out_row = tk.Frame(file_sec, bg=BG1)
        out_row.pack(fill="x")
        out_entry = tk.Entry(out_row, textvariable=self.output_var,
                             font=("Segoe UI", 9), fg=TXT0, bg=BG0,
                             insertbackground=TXT0, relief="flat", bd=0,
                             highlightthickness=1, highlightbackground=BDR)
        out_entry.pack(side="left", fill="x", expand=True, ipady=5, ipadx=6)
        styled_btn(out_row, "…", self._browse_output_dlg,
                   fg=TXT1, bg=BG3, pady=5, padx=8, width=3
                   ).pack(side="left", padx=(4, 0))

        # ── Импорт изображений ────────────────────────────────────────────────
        image_sec = self._sidebar_card(
            inner, "Импорт изображений",
            "Соберите PDF из отдельных сканов: PNG, JPG, TIFF, BMP или WEBP.",
            CYAN)
        self._import_folder_btn = styled_btn(
            image_sec, "Папка сканов -> PDF",
            self._browse_image_folder,
            fg=CYAN, bg=CYAN_BG,
            padx=12, pady=8, font_size=10)
        self._import_folder_btn.pack(fill="x")
        self._import_folder_btn.config(highlightbackground=CYAN_BDR)

        self.edge_clean_var  = tk.BooleanVar(value=True)
        self.deskew_var      = tk.BooleanVar(value=True)
        self.skip_first_var  = tk.BooleanVar(value=True)
        self.skip_last_var   = tk.BooleanVar(value=True)
        self.keep_color_var  = tk.BooleanVar(value=True)
        self.split_pages_var = tk.BooleanVar(value=False)
        self.clean_limit_var = tk.BooleanVar(value=False)
        self.clean_count_var = tk.IntVar(value=1)
        self.compress_pdf_var = tk.BooleanVar(value=False)
        self.compression_level_var = tk.StringVar(value=COMPRESSION_LEVELS["medium"]["label"])
        self.compression_scope_var = tk.StringVar(value=COMPRESSION_SCOPES["all"])
        self.edge_clean_var.trace_add("write", self._on_edge_zone_change)
        self.clean_limit_var.trace_add("write", self._on_clean_limit_change)
        self.clean_count_var.trace_add("write", self._on_clean_limit_change)

        # ── Режим очистки ─────────────────────────────────────────────────────
        mode_sec = self._sidebar_card(
            inner, "Очистка",
            "Режим, базовые правила обработки и сохранение цветных страниц.",
            BLUE)

        self._mode_var = tk.StringVar(value="⚡  Стандартная")
        self._mode_hints = {
            "✨  Лёгкая":       "Минимальная обработка, сохранение деталей",
            "⚡  Стандартная":  "Оптимально для большинства сканов",
            "🔥  Агрессивная":  "Глубокая очистка, возможна потеря деталей",
            "💥  Максимальная": "Максимальный контраст, ч/б вывод",
            "🔧  Ручная":       "Параметры ниже — ручной контроль",
        }
        self._mode_presets = {
            "✨  Лёгкая":       dict(dot=10, h=6,  thresh=40, angle=5),
            "⚡  Стандартная":  dict(dot=25, h=12, thresh=60, angle=10),
            "🔥  Агрессивная":  dict(dot=40, h=20, thresh=80, angle=15),
            "💥  Максимальная": dict(dot=80, h=30, thresh=100, angle=20),
            "🔧  Ручная":       None,
        }
        mode_cb = ttk.Combobox(mode_sec, textvariable=self._mode_var,
                               values=list(self._mode_hints.keys()),
                               state="readonly", font=("Segoe UI", 10))
        mode_cb.pack(fill="x")
        mode_cb.bind("<<ComboboxSelected>>", self._on_mode_change)

        self._mode_hint_lbl = tk.Label(
            mode_sec, text=self._mode_hints["⚡  Стандартная"],
            font=("Segoe UI", 8), fg=TXT3, bg=BG1, anchor="w",
            wraplength=max(self._ui.px(210), self._sidebar_width - self._ui.px(58)),
            justify="left")
        self._mode_hint_lbl.pack(fill="x", pady=(4, 0))

        self._sidebar_field_label(mode_sec, "Правила очистки")
        self._chk(mode_sec, "Очистка краёв (линии сканера)", self.edge_clean_var)
        self._chk(mode_sec, "Выравнивание наклона (deskew)", self.deskew_var)
        self._chk(mode_sec, "Пропустить первую стр.", self.skip_first_var)
        self._chk(mode_sec, "Пропустить последнюю стр.", self.skip_last_var)

        color_row = tk.Frame(mode_sec, bg=BG1)
        color_row.pack(fill="x", pady=(6, 0))
        self._color_chk_var = tk.BooleanVar(value=True)
        color_chk = tk.Checkbutton(
            color_row, variable=self.keep_color_var,
            bg=BG1, activebackground=BG1,
            fg=TXT1, activeforeground=TXT0,
            selectcolor=BG0,
            font=("Segoe UI", 9),
            text="Сохранить цветные стр.",
            anchor="w", command=self._on_color_toggle)
        color_chk.pack(side="left")
        tk.Label(color_row, text="RGB", font=("Courier New", 8, "bold"),
                 fg=AMBER, bg=AMBER_BG,
                 padx=5, pady=1,
                 relief="flat",
                 highlightthickness=1,
                 highlightbackground=AMBER_BDR).pack(side="left", padx=(4, 0))

        self._color_info_lbl = tk.Label(
            mode_sec, text="",
            font=("Segoe UI", 8), fg=AMBER, bg=AMBER_BG,
            anchor="w", padx=6, pady=4,
            wraplength=max(self._ui.px(210), self._sidebar_width - self._ui.px(58)),
            justify="left",
            relief="flat",
            highlightthickness=1, highlightbackground=AMBER_BDR)
        self._color_info_lbl.pack(fill="x", pady=(5, 0))

        # ── Параметры ─────────────────────────────────────────────────────────
        par_sec = self._sidebar_card(
            inner, "Тонкая настройка",
            "Ручные параметры для сложных сканов. Оставьте стандартные, если результат нормальный.",
            AMBER)

        self.dot_var     = tk.IntVar(value=25)
        self.denoise_var = tk.IntVar(value=12)
        self.margin_var  = tk.IntVar(value=DEFAULT_EDGE_MARGIN)
        self.thresh_var  = tk.IntVar(value=60)
        self.angle_var   = tk.IntVar(value=10)
        self.brightness_var = tk.IntVar(value=DEFAULT_BRIGHTNESS)
        self.contrast_var   = tk.IntVar(value=DEFAULT_CONTRAST)

        make_slider(par_sec, "Размер точки (px)",
                    self.dot_var, 1, 100,
                    "Точки меньше этого размера удаляются по всей странице", BG1)
        make_slider(par_sec, "Шумоподавление (NL-Means)",
                    self.denoise_var, 1, 50,
                    "Выше — сильнее, но медленнее. Рек: 8–18", BG1)
        make_slider(par_sec, "Яркость",
                    self.brightness_var, -80, 80,
                    "Сдвигает фон и текст перед очисткой", BG1)
        make_slider(par_sec, "Контраст (%)",
                    self.contrast_var, 50, 200,
                    "100 — без изменения, выше — контрастнее", BG1)
        self.brightness_var.trace_add("write", self._on_preview_adjust_change)
        self.contrast_var.trace_add("write", self._on_preview_adjust_change)

        adjust_reset = tk.Frame(par_sec, bg=BG1)
        adjust_reset.pack(fill="x", pady=(0, _spx(10)))
        styled_btn(adjust_reset, "Яркость 0",
                   self._reset_brightness,
                   fg=TXT1, bg=BG2, pady=3, padx=5, font_size=8
                   ).pack(side="left", fill="x", expand=True, padx=(0, 4))
        styled_btn(adjust_reset, "Контраст 100",
                   self._reset_contrast,
                   fg=TXT1, bg=BG2, pady=3, padx=5, font_size=8
                   ).pack(side="left", fill="x", expand=True, padx=(4, 0))

        make_slider(par_sec, "Зона краёв (px)",
                    self.margin_var, 5, 150,
                    "Ширина зоны очистки краёв; не меняется режимами очистки", BG1)
        edge_zone_row = tk.Frame(par_sec, bg=BG1)
        edge_zone_row.pack(fill="x", pady=(0, _spx(10)))
        styled_btn(edge_zone_row, f"Зона {DEFAULT_EDGE_MARGIN}",
                   self._reset_edge_margin,
                   fg=TXT1, bg=BG2, pady=3, padx=5, font_size=8
                   ).pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._edge_zone_btn = styled_btn(
            edge_zone_row, "Скрыть зону",
            self._toggle_edge_zone,
            fg=AMBER, bg=AMBER_BG, pady=3, padx=5, font_size=8)
        self._edge_zone_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.margin_var.trace_add("write", self._on_edge_zone_change)
        make_slider(par_sec, "Порог тёмной линии",
                    self.thresh_var, 20, 200,
                    "Строка считается линией при яркости < порога", BG1)
        make_slider(par_sec, "Макс. угол наклона (°)",
                    self.angle_var, 1, 30,
                    "Страницы с бо́льшим наклоном не выравниваются", BG1)

        # ── Страницы ──────────────────────────────────────────────────────────
        pg_sec = self._sidebar_card(
            inner, "Диапазон очистки",
            "Ограничьте обработку первыми страницами, если часть документа трогать не нужно.",
            GREEN)

        clean_count_row = tk.Frame(pg_sec, bg=BG1)
        clean_count_row.pack(fill="x", pady=(8, 0))
        tk.Checkbutton(
            clean_count_row, variable=self.clean_limit_var,
            bg=BG1, activebackground=BG1,
            fg=TXT1, activeforeground=TXT0,
            selectcolor=BG0,
            font=("Segoe UI", 9),
            text="Очистить только",
            anchor="w").pack(side="left")
        self._clean_count_spin = tk.Spinbox(
            clean_count_row, from_=1, to=1, width=4,
            textvariable=self.clean_count_var,
            font=("Courier New", 9, "bold"),
            fg=TXT0, bg=BG0,
            buttonbackground=BG2,
            insertbackground=TXT0,
            relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BDR,
            command=self._on_clean_limit_change)
        self._clean_count_spin.pack(side="left", padx=(6, 4), ipady=2)
        tk.Label(clean_count_row, text="стр. с начала",
                 font=("Segoe UI", 8), fg=TXT2, bg=BG1
                 ).pack(side="left")
        self._clean_limit_hint_lbl = tk.Label(
            pg_sec, text="",
            font=("Segoe UI", 8), fg=TXT3, bg=BG1,
            anchor="w",
            wraplength=max(self._ui.px(210), self._sidebar_width - self._ui.px(58)),
            justify="left")
        self._clean_limit_hint_lbl.pack(fill="x", pady=(3, 0))
        self._sync_clean_count_controls()

        compress_sec = self._sidebar_card(
            inner, "Экспорт",
            "Формат сохранения результата и сжатие PDF.",
            GREEN)

        self._chk(compress_sec, "Разбить результат на страницы", self.split_pages_var)

        tk.Checkbutton(
            compress_sec, variable=self.compress_pdf_var,
            bg=BG1, activebackground=BG1,
            fg=TXT1, activeforeground=TXT0,
            selectcolor=BG0,
            font=("Segoe UI", 9),
            text="Сжимать PDF",
            anchor="w").pack(fill="x")

        tk.Label(
            compress_sec, text="Уровень",
            font=("Segoe UI", 8), fg=TXT2, bg=BG1,
            anchor="w").pack(fill="x", pady=(6, 2))
        ttk.Combobox(
            compress_sec,
            textvariable=self.compression_level_var,
            values=[item["label"] for item in COMPRESSION_LEVELS.values()],
            state="readonly",
            font=("Segoe UI", 9),
        ).pack(fill="x")

        tk.Label(
            compress_sec, text="Применять",
            font=("Segoe UI", 8), fg=TXT2, bg=BG1,
            anchor="w").pack(fill="x", pady=(6, 2))
        ttk.Combobox(
            compress_sec,
            textvariable=self.compression_scope_var,
            values=list(COMPRESSION_SCOPES.values()),
            state="readonly",
            font=("Segoe UI", 9),
        ).pack(fill="x")

        page_ops = self._sidebar_card(
            inner, "Текущая страница",
            "Действия только с выбранной страницей: добавить, разрезать, защитить или удалить.",
            RED)

        styled_btn(page_ops, "+  Добавить страницу",
                   self._add_pages_after_current,
                   fg=TXT1, bg=BG2, pady=7, padx=8
                   ).pack(fill="x", pady=(0, 5))
        styled_btn(page_ops, "↔  Линия лев/прав",
                   lambda: self._begin_split("vertical"),
                   fg=AMBER, bg=AMBER_BG, pady=7, padx=8
                   ).pack(fill="x", pady=(0, 5))
        styled_btn(page_ops, "↕  Линия верх/низ",
                   lambda: self._begin_split("horizontal"),
                   fg=AMBER, bg=AMBER_BG, pady=7, padx=8
                   ).pack(fill="x", pady=(0, 5))
        self._skip_page_btn = styled_btn(
            page_ops, "☐  Не чистить стр.",
            self._toggle_skip_page,
            fg=TXT1, bg=BG2, pady=7, padx=8)
        self._skip_page_btn.pack(fill="x", pady=(0, 5))
        self._clear_protect_btn = styled_btn(
            page_ops, "Снять защиту области",
            self._clear_protected_area,
            fg=TXT1, bg=BG2, pady=7, padx=8)
        self._clear_protect_btn.pack(fill="x", pady=(0, 5))
        styled_btn(page_ops, "↓  Скачать текущую",
                   self._download_current_page,
                   fg=GREEN, bg=GREEN_BG, pady=7, padx=8
                   ).pack(fill="x", pady=(0, 5))
        styled_btn(page_ops, "−  Удалить текущую",
                   self._delete_current_page,
                   fg=RED, bg=RED_BG, pady=7, padx=8
                   ).pack(fill="x")

        # Нижняя часть: кнопка запуска и прогресс
        sep(sb)
        foot = tk.Frame(sb, bg=BG1)
        foot.pack(fill="x", side="bottom")
        sep(foot)

        self._run_btn = styled_btn(
            foot, "▶  Запустить очистку", self._start_processing,
            fg="white", bg=BLUE, active_bg=BLUE2,
            padx=0, pady=10, font_size=11, bold=True)
        self._run_btn.pack(fill="x", padx=12, pady=(10, 6))
        self._run_btn.config(highlightbackground=BLUE_BDR)

        self._cancel_btn = styled_btn(
            foot, "Отмена обработки", self._cancel_processing,
            fg=TXT2, bg=BG2, active_bg=BG3,
            padx=0, pady=7, font_size=10)
        self._cancel_btn.pack(fill="x", padx=12, pady=(0, 6))
        self._cancel_btn.config(state="disabled")

        self._prog_var = tk.DoubleVar(value=0)
        self._prog_pct_var = tk.StringVar(value="0%")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("P.Horizontal.TProgressbar",
                        troughcolor=BDR2, background=BLUE,
                        bordercolor=BG1, lightcolor=BLUE, darkcolor=BLUE,
                        thickness=4)
        prog_row = tk.Frame(foot, bg=BG1)
        prog_row.pack(fill="x", padx=12, pady=(0, 4))
        ttk.Progressbar(prog_row, variable=self._prog_var, maximum=100,
                        style="P.Horizontal.TProgressbar"
                        ).pack(side="left", fill="x", expand=True)
        tk.Label(prog_row, textvariable=self._prog_pct_var,
                 font=("Courier New", 8, "bold"), fg=BLUE, bg=BG1,
                 width=4, anchor="e").pack(side="right", padx=(8, 0))

        self._sb_status_var = tk.StringVar(value="Готов · откройте PDF")
        tk.Label(foot, textvariable=self._sb_status_var,
                 font=("Courier New", 8), fg=TXT3, bg=BG1,
                 anchor="w").pack(fill="x", padx=12, pady=(0, 10))

    # ── ОСНОВНАЯ ОБЛАСТЬ ──────────────────────────────────────────────────────
    def _build_main(self, parent):
        self._build_toolbar(parent)

        # Область холста
        canvas_frame = tk.Frame(parent, bg="#080a10")
        canvas_frame.pack(fill="both", expand=True)

        self._canvas = tk.Canvas(canvas_frame, bg="#080a10",
                                 highlightthickness=0, cursor="crosshair")
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Motion>",        self._canvas_motion)
        self._canvas.bind("<Leave>",         self._canvas_leave)
        self._canvas.bind("<Button-1>",      self._canvas_click)
        self._canvas.bind("<B1-Motion>",     self._canvas_drag)
        self._canvas.bind("<ButtonRelease-1>", self._canvas_release)
        self._canvas.bind("<MouseWheel>",    self._canvas_wheel_zoom)
        self._canvas.bind("<Control-MouseWheel>", self._canvas_wheel_zoom)
        self._canvas.bind("<Button-4>",      self._canvas_wheel_zoom)
        self._canvas.bind("<Button-5>",      self._canvas_wheel_zoom)
        self._canvas.bind("<Configure>",     lambda e: self._render_page())

        # Миниатюры
        self._build_thumbs(parent)

        # Навигация по страницам
        self._build_page_nav(parent)

    def _build_toolbar(self, parent):
        tb = tk.Frame(parent, bg=BG1, height=self._ui.px(40))
        tb.pack(fill="x")
        tb.pack_propagate(False)
        sep_v = lambda: tk.Frame(tb, bg=BDR, width=1).pack(
            side="left", fill="y", padx=self._ui.px(4), pady=self._ui.px(6))

        self._btn_undo = styled_btn(
            tb, "↶  Отмена", self._undo,
            fg=TXT2, bg=BG1, active_bg=BG3, pady=5, padx=8)
        self._btn_undo.pack(side="left", padx=(8, 2), pady=4)
        self._btn_redo = styled_btn(
            tb, "↷  Повтор", self._redo,
            fg=TXT2, bg=BG1, active_bg=BG3, pady=5, padx=8)
        self._btn_redo.pack(side="left", padx=2, pady=4)

        sep_v()

        # Кнопки инструментов
        self._btn_view = styled_btn(
            tb, "↖  Просмотр", lambda: self._set_tool("view"),
            fg=TXT1, bg=BG1, active_bg=BG3, pady=5, padx=8)
        self._btn_view.pack(side="left", padx=2, pady=4)

        self._btn_pan = styled_btn(
            tb, "✋  Рука", lambda: self._set_tool("pan"),
            fg=BLUE, bg=BLUE_BG, active_bg=BG3, pady=5, padx=8)
        self._btn_pan.pack(side="left", padx=2, pady=4)
        self._btn_pan.config(highlightbackground=BLUE_BDR)

        self._btn_eraser = styled_btn(
            tb, "◯  Ластик", lambda: self._set_tool("eraser"),
            fg=TXT1, bg=BG1, active_bg=BG3, pady=5, padx=8)
        self._btn_eraser.pack(side="left", padx=2, pady=4)

        self._btn_crop = styled_btn(
            tb, "▣  Обрезка", lambda: self._set_tool("crop"),
            fg=TXT1, bg=BG1, active_bg=BG3, pady=5, padx=8)
        self._btn_crop.pack(side="left", padx=2, pady=4)

        self._btn_protect = styled_btn(
            tb, "□  Защита", lambda: self._set_tool("protect"),
            fg=TXT1, bg=BG1, active_bg=BG3, pady=5, padx=8)
        self._btn_protect.pack(side="left", padx=2, pady=4)

        sep_v()

        # Поворот
        styled_btn(tb, "↺  −90°", lambda: self._rotate_page(-90),
                   fg=AMBER, bg=AMBER_BG, pady=5, padx=8
                   ).pack(side="left", padx=2, pady=4)
        styled_btn(tb, "+90°  ↻", lambda: self._rotate_page(90),
                   fg=AMBER, bg=AMBER_BG, pady=5, padx=8
                   ).pack(side="left", padx=2, pady=4)
        styled_btn(tb, "⇄  Выровнять", self._deskew_current_page,
                   fg=CYAN, bg=CYAN_BG, pady=5, padx=8
                   ).pack(side="left", padx=2, pady=4)

        sep_v()

        # Масштаб
        styled_btn(tb, "−", self._zoom_out,
                   fg=TXT1, bg=BG2, pady=5, padx=8, width=2
                   ).pack(side="left", padx=2, pady=4)
        self._zoom_lbl = tk.Label(
            tb, text="100%",
            font=("Courier New", 9), fg=TXT1, bg=BG1, width=5)
        self._zoom_lbl.pack(side="left")
        styled_btn(tb, "+", self._zoom_in,
                   fg=TXT1, bg=BG2, pady=5, padx=8, width=2
                   ).pack(side="left", padx=2, pady=4)

        sep_v()

        # Размер ластика
        tk.Label(tb, text="Ластик:", font=("Segoe UI", 9),
                 fg=TXT2, bg=BG1).pack(side="left", padx=(4, 4))
        self._eraser_sz_var = tk.IntVar(value=18)
        self._esz_lbl = tk.Label(tb, text="18",
                                 font=("Courier New", 9, "bold"),
                                 fg=BLUE, bg=BG1, width=3)
        eraser_scale = ttk.Scale(tb, from_=5, to=80,
                                 variable=self._eraser_sz_var,
                                 orient="horizontal",
                                 command=lambda v: self._esz_lbl.config(
                                     text=str(int(float(v)))),
                                 style="Sl.Horizontal.TScale")
        eraser_scale.pack(side="left", ipadx=30, pady=4)
        self._esz_lbl.pack(side="left", padx=(2, 0))

        # Сброс страницы справа
        styled_btn(tb, "✕  Сброс стр.", self._clear_page,
                   fg=RED, bg=RED_BG, pady=5, padx=9,
                   ).pack(side="right", padx=(4, 10), pady=4)

    def _build_thumbs(self, parent):
        frame = tk.Frame(parent, bg=BG1, height=self._ui.px(80))
        frame.pack(fill="x")
        frame.pack_propagate(False)
        sep_frame = tk.Frame(frame, bg=BDR, height=1)
        sep_frame.pack(fill="x")

        # Внутренняя прокрутка
        sc = tk.Canvas(frame, bg=BG1, highlightthickness=0, height=self._ui.px(76))
        hsb = tk.Scrollbar(frame, orient="horizontal", command=self._thumb_xview)

        def _xscroll(first, last):
            hsb.set(first, last)
            self._schedule_visible_thumb_render()

        sc.configure(xscrollcommand=_xscroll)
        # Полоса прокрутки появляется только при необходимости
        self._thumb_canvas = sc
        self._thumb_inner  = tk.Frame(sc, bg=BG1)
        sc_win = sc.create_window((0, 0), window=self._thumb_inner, anchor="nw")

        def _cfg(e):
            sc.configure(scrollregion=sc.bbox("all"))
            self._schedule_visible_thumb_render()

        self._thumb_inner.bind("<Configure>", _cfg)
        sc.bind("<Configure>", lambda _e: self._schedule_visible_thumb_render())
        hsb.pack(side="bottom", fill="x")
        sc.pack(fill="both", expand=True)

        self._thumb_frames = []   # список (frame, dot_label, rot_label, num_label)
        self._thumb_containers = {}
        self._thumb_rendered = set()
        self._thumb_render_queue = []
        self._thumb_render_job = None
        self._thumb_visible_job = None

    def _build_page_nav(self, parent):
        nav = tk.Frame(parent, bg=BG1, height=self._ui.px(36))
        nav.pack(fill="x")
        nav.pack_propagate(False)
        sep_frame = tk.Frame(nav, bg=BDR, height=1)
        sep_frame.pack(fill="x")

        inner = tk.Frame(nav, bg=BG1)
        inner.pack(fill="both", expand=True)

        styled_btn(inner, "‹", lambda: self._go_page(self._current_page - 1),
                   fg=TXT1, bg=BG1, pady=4, padx=10
                   ).pack(side="left", padx=(10, 2))

        self._nav_dots_frame = tk.Frame(inner, bg=BG1)
        self._nav_dots_frame.pack(side="left", padx=6)

        styled_btn(inner, "›", lambda: self._go_page(self._current_page + 1),
                   fg=TXT1, bg=BG1, pady=4, padx=10
                   ).pack(side="left", padx=(2, 10))

        self._nav_pages_lbl = tk.Label(inner, text="—",
                                       font=("Courier New", 9),
                                       fg=TXT1, bg=BG1)
        self._nav_pages_lbl.pack(side="left", padx=10)

        # Легенда статусов
        legend = tk.Frame(inner, bg=BG1)
        legend.pack(side="right", padx=16)
        for color, text in [(GREEN, "Готово"), (BLUE, "Обработка"),
                            (RED, "Ошибка"), (AMBER, "Не чистить"),
                            (TXT3, "Ожидание")]:
            dot = tk.Frame(legend, bg=color,
                           width=self._ui.px(7), height=self._ui.px(7))
            dot.pack(side="left", padx=(6, 2))
            tk.Label(legend, text=text, font=("Segoe UI", 8),
                     fg=TXT2, bg=BG1).pack(side="left", padx=(0, 4))

    # ── СТРОКА СОСТОЯНИЯ ──────────────────────────────────────────────────────
    def _build_statusbar(self):
        sb = tk.Frame(self, bg="#0a0c12", height=self._ui.px(24))
        sb.pack(fill="x", side="bottom")
        sb.pack_propagate(False)
        tk.Frame(sb, bg=BDR, height=1).pack(fill="x")

        inner = tk.Frame(sb, bg="#0a0c12")
        inner.pack(fill="both", expand=True, padx=12)

        self._st_main = tk.Label(inner, text="", font=("Segoe UI", 8),
                                 fg=TXT3, bg="#0a0c12", anchor="w")
        self._st_main.pack(side="left")

        self._st_edits = tk.Label(inner, text="",
                                  font=("Courier New", 8), fg=AMBER,
                                  bg=AMBER_BG, padx=6, pady=1)
        self._st_edits.pack(side="left", padx=8)

        self._st_color = tk.Label(inner, text="",
                                  font=("Courier New", 8), fg=GREEN,
                                  bg=GREEN_BG, padx=6, pady=1)
        self._st_color.pack(side="left", padx=2)

        self._st_right = tk.Label(inner, text="",
                                  font=("Courier New", 8), fg=TXT3,
                                  bg="#0a0c12", anchor="e")
        self._st_right.pack(side="right")

        self._st_skip = tk.Label(inner, text="",
                                 font=("Courier New", 8), fg=AMBER,
                                 bg="#0a0c12", padx=0, pady=1)
        self._st_skip.pack(side="right", padx=(0, 8))

    # ── ПОМОЩНИКИ ВИДЖЕТОВ ────────────────────────────────────────────────────
    def _section(self, parent, text):
        f = tk.Frame(parent, bg=BG2)
        f.pack(fill="x", pady=(10, 0))
        tk.Label(f, text=text.upper(),
                 font=("Segoe UI", 8, "bold"), fg=TXT3, bg=BG2,
                 anchor="w").pack(fill="x", padx=12, pady=5)

    def _sidebar_card(self, parent, title, subtitle=None, accent=BLUE):
        outer = tk.Frame(parent, bg=BG1)
        outer.pack(fill="x", padx=10, pady=(10, 0))

        card = tk.Frame(
            outer, bg=BG1,
            highlightthickness=1,
            highlightbackground=BDR,
        )
        card.pack(fill="x")

        header = tk.Frame(card, bg=BG2)
        header.pack(fill="x")
        tk.Frame(header, bg=accent, width=self._ui.px(3), height=self._ui.px(18)).pack(
            side="left", padx=(10, 8), pady=9)
        tk.Label(
            header, text=title.upper(),
            font=("Segoe UI", 8, "bold"),
            fg=TXT1, bg=BG2,
            anchor="w",
        ).pack(side="left", fill="x", expand=True, pady=8)

        body = tk.Frame(card, bg=BG1)
        body.pack(fill="x", padx=10, pady=(8, 10))
        if subtitle:
            tk.Label(
                body, text=subtitle,
                font=("Segoe UI", 8),
                fg=TXT3, bg=BG1,
                anchor="w", justify="left",
                wraplength=max(self._ui.px(210), self._sidebar_width - self._ui.px(58)),
            ).pack(fill="x", pady=(0, 8))
        return body

    def _sidebar_field_label(self, parent, text):
        tk.Label(
            parent, text=text,
            font=("Segoe UI", 8, "bold"),
            fg=TXT2, bg=BG1,
            anchor="w",
        ).pack(fill="x", pady=(8, 3))

    def _sidebar_hint(self, parent, text, fg=TXT3, bg=BG1, pady=(4, 0)):
        tk.Label(
            parent, text=text,
            font=("Segoe UI", 8),
            fg=fg, bg=bg,
            anchor="w", justify="left",
            wraplength=max(self._ui.px(210), self._sidebar_width - self._ui.px(58)),
        ).pack(fill="x", pady=pady)

    def _chk(self, parent, text, variable):
        f = tk.Frame(parent, bg=BG1)
        f.pack(fill="x", pady=(4, 0))
        style = ttk.Style()
        style.configure("Dark.TCheckbutton", background=BG1,
                        foreground=TXT1, font=("Segoe UI", 9))
        ttk.Checkbutton(f, text=text, variable=variable,
                        style="Dark.TCheckbutton").pack(side="left")

    # ── ОПЕРАЦИИ С ФАЙЛАМИ ────────────────────────────────────────────────────
    def _downloads_dir(self):
        downloads = Path.home() / "Downloads"
        if downloads.exists():
            return downloads
        return Path.home()

    def _default_output_path(self, input_path=None):
        source = Path(input_path or self._input_path or "CrystalPDF.pdf")
        return self._downloads_dir() / f"{source.stem}_CrystalPDF.pdf"

    def _unique_output_path(self, output_path, split_pages=False):
        output_path = Path(output_path)

        def occupied(path):
            if path.exists():
                return True
            if split_pages:
                folder = path.with_suffix("")
                folder = folder.with_name(folder.name + "_pages")
                return folder.exists()
            return False

        if not occupied(output_path):
            return output_path

        for index in range(2, 1000):
            candidate = output_path.with_name(
                f"{output_path.stem}_{index}{output_path.suffix}")
            if not occupied(candidate):
                return candidate
        return output_path.with_name(f"{output_path.stem}_{uuid4().hex[:8]}{output_path.suffix}")

    def _browse_input(self):
        if self._importing:
            return
        if self._processing:
            messagebox.showwarning(
                "Подождите",
                "Сначала дождитесь окончания обработки PDF.")
            return
        path = filedialog.askopenfilename(
            title="Выберите входной PDF",
            filetypes=[("PDF", "*.pdf"), ("Все файлы", "*.*")])
        if not path:
            return
        self.output_var.set(str(self._default_output_path(path)))
        self._load_pdf(path)

    def _browse_image_folder(self):
        if self._importing:
            return
        if self._processing:
            messagebox.showwarning(
                "Подождите",
                "Сначала дождитесь окончания обработки PDF.")
            return
        folder = filedialog.askdirectory(title="Выберите папку со сканами")
        if not folder:
            return
        image_paths = _list_image_files(folder)
        if not image_paths:
            messagebox.showwarning(
                "Нет изображений",
                "В выбранной папке не найдены PNG/JPG/TIFF/BMP/WEBP файлы.")
            return
        output_path = self._unique_output_path(self._default_output_path(folder))
        self.output_var.set(str(output_path))
        self._load_image_folder(folder, save_path=output_path)

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Сохранить очищенный PDF",
            initialdir=str(self._downloads_dir()),
            initialfile=self._default_output_path().name,
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf")])
        if path:
            self.output_var.set(path)

    def _browse_output_dlg(self):
        path = filedialog.asksaveasfilename(
            title="Сохранить как",
            initialdir=str(self._downloads_dir()),
            initialfile=self._default_output_path().name,
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf")])
        if path:
            self.output_var.set(path)

    def _resolved_output_path(self):
        out = self.output_var.get().strip() if hasattr(self, "output_var") else ""
        if not out:
            out_path = self._default_output_path(self._input_path)
        else:
            out_path = Path(out)
            if not out_path.suffix:
                out_path = out_path.with_suffix(".pdf")
            elif out_path.suffix.lower() != ".pdf":
                out_path = out_path.with_suffix(out_path.suffix + ".pdf")

        if not out_path.is_absolute():
            out_path = self._downloads_dir() / out_path.name

        if self._input_path and Path(self._input_path).resolve() == out_path.resolve():
            out_path = self._default_output_path(self._input_path)

        return self._unique_output_path(out_path, self.split_pages_var.get())

    def _compression_options(self):
        return _compression_options_from_labels(
            bool(getattr(self, "compress_pdf_var", tk.BooleanVar(value=False)).get()),
            getattr(self, "compression_level_var", tk.StringVar(value=COMPRESSION_LEVELS["medium"]["label"])).get(),
            getattr(self, "compression_scope_var", tk.StringVar(value=COMPRESSION_SCOPES["all"])).get(),
        )

    def _export_session(self):
        if not self._ensure_document_ready("экспорта"):
            return
        self._store_current_page_adjustment()
        out_path = self._resolved_output_path()
        self.output_var.set(str(out_path))
        compression = self._compression_options()

        try:
            images = []
            compression_flags = []
            for idx in range(self._page_count):
                image = self._render_session_page_image(idx, 300)
                if image.mode != "RGB":
                    image = image.convert("RGB")
                images.append(image)
                is_color = bool(self._color_pages.get(idx, False))
                if (
                    compression
                    and compression.get("enabled")
                    and compression.get("scope") == "color"
                    and idx not in self._color_pages
                ):
                    is_color = _pil_image_has_color(image)
                    self._color_pages[idx] = is_color
                compression_flags.append(_compression_applies(
                    compression,
                    is_color=is_color,
                    is_processed=not self._skip_pages.get(idx, False)
                    and not self._is_page_outside_clean_limit(idx),
                ))
                pct = (idx + 1) / max(1, self._page_count) * 100
                self._prog_var.set(pct)
                self._prog_pct_var.set(f"{int(round(pct))}%")
                self._sb_status_var.set(f"Экспорт: стр. {idx + 1}/{self._page_count}")
                self.update_idletasks()

            saved_path = _save_pdf_images(
                images,
                out_path,
                300,
                self.split_pages_var.get(),
                compression=compression,
                page_compression_flags=compression_flags,
            )
            self._prog_var.set(100)
            self._prog_pct_var.set("100%")
            self._sb_status_var.set(f"Экспорт готов: {Path(saved_path).name}")
            messagebox.showinfo("Экспорт готов", f"PDF сохранён:\n\n{saved_path}")
        except Exception as e:
            messagebox.showerror("Ошибка экспорта", str(e))

    def _ensure_document_ready(self, action_text="операции"):
        if self._importing:
            messagebox.showwarning(
                "Подождите",
                "Сначала дождитесь окончания импортирования PDF.")
            return False
        if self._processing:
            messagebox.showwarning(
                "Подождите",
                "Сначала дождитесь окончания обработки PDF.")
            return False
        if self._doc is None:
            messagebox.showwarning(
                "Нет PDF",
                f"Сначала откройте PDF для {action_text}.")
            return False
        return True

    def _snapshot_document_state(self):
        if self._doc is None:
            return None
        try:
            pdf_bytes = self._doc.tobytes(garbage=4, deflate=True)
        except TypeError:
            pdf_bytes = self._doc.tobytes()
        return {
            "pdf": pdf_bytes,
            "page": self._current_page,
            "rotations": dict(self._rotations),
            "eraser_masks": {k: list(v) for k, v in self._eraser_masks.items()},
            "crop_boxes": dict(self._crop_boxes),
            "protected_boxes": {
                k: _sanitize_norm_boxes(v)
                for k, v in self._protected_boxes.items()
                if _protected_box_count(v)
            },
            "page_status": dict(self._page_status),
            "color_pages": dict(self._color_pages),
            "skip_pages": dict(self._skip_pages),
            "page_adjustments": {k: dict(v) for k, v in self._page_adjustments.items()},
        }

    def _restore_document_state(self, state):
        if not state:
            return
        try:
            import fitz
            new_doc = fitz.open(stream=state["pdf"], filetype="pdf")
        except Exception as e:
            messagebox.showerror("Ошибка отката", str(e))
            return

        old_doc = self._doc
        self._cancel_page_render_jobs()
        self._doc = new_doc
        self._render_source_path_usable = False
        self._render_source_path = None
        if old_doc is not None:
            try:
                old_doc.close()
            except Exception:
                pass

        self._page_count = len(new_doc)
        self._current_page = max(0, min(int(state.get("page", 0)), self._page_count - 1))
        self._rotations = dict(state.get("rotations", {}))
        self._eraser_masks = {k: list(v) for k, v in state.get("eraser_masks", {}).items()}
        self._crop_boxes = dict(state.get("crop_boxes", {}))
        self._protected_boxes = {
            k: _sanitize_norm_boxes(v)
            for k, v in state.get("protected_boxes", {}).items()
            if _protected_box_count(v)
        }
        self._page_status = dict(state.get("page_status", {}))
        self._color_pages = dict(state.get("color_pages", {}))
        self._skip_pages = dict(state.get("skip_pages", {}))
        self._page_adjustments = {
            k: dict(v) for k, v in state.get("page_adjustments", {}).items()
        }
        self._pan_x = 0
        self._pan_y = 0
        self._pan_anchor = None
        self._display_box = (0.0, 0.0, 1.0, 1.0)

        self._build_thumb_widgets()
        self._go_page(self._current_page)
        self._update_page_flags_ui()
        self._sync_clean_count_controls()
        self._recount_edits()
        self._update_status_bar()

    def _clear_baked_page_state(self, idx):
        self._rotations.pop(idx, None)
        self._eraser_masks.pop(idx, None)
        self._crop_boxes.pop(idx, None)
        self._protected_boxes.pop(idx, None)
        self._skip_pages.pop(idx, None)
        self._page_adjustments.pop(idx, None)

    def _apply_processed_session(self, pdf_bytes, page_status=None, before_state=None):
        try:
            import fitz
            new_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception as e:
            messagebox.showerror("Ошибка применения очистки", str(e))
            return False

        old_doc = self._doc
        self._cancel_page_render_jobs()
        self._doc = new_doc
        self._render_source_path_usable = False
        self._render_source_path = None
        if old_doc is not None:
            try:
                old_doc.close()
            except Exception:
                pass

        self._page_count = len(new_doc)
        self._current_page = max(0, min(self._current_page, self._page_count - 1))
        self._pan_x = 0
        self._pan_y = 0
        self._pan_anchor = None
        self._display_box = (0.0, 0.0, 1.0, 1.0)
        self._rotations.clear()
        self._eraser_masks.clear()
        self._crop_boxes.clear()
        self._protected_boxes.clear()
        self._skip_pages.clear()
        self._page_adjustments.clear()
        self._adjustment_controls_page = None
        self._page_status = dict(page_status or {i: "waiting" for i in range(self._page_count)})
        self._color_pages = {}

        self._sync_adjustment_controls(self._current_page)
        self._build_thumb_widgets()
        self._go_page(self._current_page)
        self._update_page_flags_ui()
        self._sync_clean_count_controls()

        if before_state:
            after_state = self._snapshot_document_state()
            self._push_action({
                "type": "document_state",
                "before": before_state,
                "after": after_state,
            })
        else:
            self._recount_edits()

        self._update_status_bar()
        self._schedule_color_detection(delay=900, pdf_bytes=pdf_bytes)
        return True

    def _replace_current_page_with_image(self, image, dpi=300):
        if self._doc is None:
            return False
        idx = self._current_page
        before_state = self._snapshot_document_state()
        try:
            import fitz
            pdf_bytes = _pdf_images_to_bytes([image.convert("RGB")], dpi)
            page_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            self._doc.delete_page(idx)
            self._doc.insert_pdf(page_doc, from_page=0, to_page=0, start_at=idx)
            page_doc.close()
            self._render_source_path_usable = False
            self._render_source_path = None
            self._cancel_page_render_jobs()
        except Exception as e:
            messagebox.showerror("Не удалось заменить страницу", str(e))
            return False

        self._clear_baked_page_state(idx)
        self._page_status[idx] = "waiting"
        self._color_pages[idx] = _pil_image_has_color(image)
        self._pan_x = 0
        self._pan_y = 0
        self._display_box = (0.0, 0.0, 1.0, 1.0)
        self._sync_adjustment_controls(idx)
        self._build_thumb_widgets()
        self._go_page(idx)
        after_state = self._snapshot_document_state()
        self._push_action({
            "type": "document_state",
            "before": before_state,
            "after": after_state,
        })
        self._update_page_flags_ui()
        self._update_status_bar()
        return True

    def _shift_page_state_for_insert(self, start, count):
        def shifted(mapping):
            return {
                (idx + count if idx >= start else idx): value
                for idx, value in mapping.items()
            }

        self._rotations = shifted(self._rotations)
        self._eraser_masks = shifted(self._eraser_masks)
        self._crop_boxes = shifted(self._crop_boxes)
        self._protected_boxes = shifted(self._protected_boxes)
        self._page_status = shifted(self._page_status)
        self._color_pages = shifted(self._color_pages)
        self._skip_pages = shifted(self._skip_pages)
        self._page_adjustments = shifted(self._page_adjustments)
        for idx in range(start, start + count):
            self._page_status[idx] = "waiting"
            self._color_pages[idx] = False

    def _shift_page_state_for_delete(self, start, count=1):
        end = start + count

        def shifted(mapping):
            out = {}
            for idx, value in mapping.items():
                if start <= idx < end:
                    continue
                out[idx - count if idx >= end else idx] = value
            return out

        self._rotations = shifted(self._rotations)
        self._eraser_masks = shifted(self._eraser_masks)
        self._crop_boxes = shifted(self._crop_boxes)
        self._protected_boxes = shifted(self._protected_boxes)
        self._page_status = shifted(self._page_status)
        self._color_pages = shifted(self._color_pages)
        self._skip_pages = shifted(self._skip_pages)
        self._page_adjustments = shifted(self._page_adjustments)

    def _shift_page_state_for_replace(self, start, new_count):
        delta = new_count - 1
        old_color = self._color_pages.get(start, False)
        old_adjustment = dict(self._page_adjustments.get(start, {}))
        old_skip = bool(self._skip_pages.get(start, False))

        def shifted(mapping):
            out = {}
            for idx, value in mapping.items():
                if idx == start:
                    continue
                out[idx + delta if idx > start else idx] = value
            return out

        self._rotations = shifted(self._rotations)
        self._eraser_masks = shifted(self._eraser_masks)
        self._crop_boxes = shifted(self._crop_boxes)
        self._protected_boxes = shifted(self._protected_boxes)
        self._page_status = shifted(self._page_status)
        self._color_pages = shifted(self._color_pages)
        self._skip_pages = shifted(self._skip_pages)
        self._page_adjustments = shifted(self._page_adjustments)
        for idx in range(start, start + new_count):
            self._page_status[idx] = "waiting"
            self._color_pages[idx] = old_color
            if old_skip:
                self._skip_pages[idx] = True
            if old_adjustment:
                self._page_adjustments[idx] = dict(old_adjustment)

    def _after_document_structure_change(self, current_page):
        self._render_source_path_usable = False
        self._render_source_path = None
        self._cancel_page_render_jobs()
        self._page_count = len(self._doc) if self._doc is not None else 0
        if self._page_count <= 0:
            return
        self._current_page = max(0, min(int(current_page), self._page_count - 1))
        self._pan_x = 0
        self._pan_y = 0
        self._pan_anchor = None
        self._display_box = (0.0, 0.0, 1.0, 1.0)
        self._page_status = {
            idx: self._page_status.get(idx, "waiting")
            for idx in range(self._page_count)
        }
        self._color_pages = {
            idx: self._color_pages.get(idx, False)
            for idx in range(self._page_count)
        }
        self._skip_pages = {
            idx: True
            for idx in range(self._page_count)
            if self._skip_pages.get(idx, False)
        }
        self._protected_boxes = {
            idx: boxes
            for idx in range(self._page_count)
            for boxes in [_sanitize_norm_boxes(self._protected_boxes.get(idx))]
            if boxes
        }
        self._page_adjustments = {
            idx: dict(self._page_adjustments[idx])
            for idx in range(self._page_count)
            if idx in self._page_adjustments
        }
        self._build_thumb_widgets()
        self._go_page(self._current_page)
        self._update_page_flags_ui()
        if hasattr(self, "clean_limit_var") and not self.clean_limit_var.get():
            self.clean_count_var.set(max(1, self._page_count))
        self._sync_clean_count_controls()
        self._update_status_bar()
        if self._page_count <= 200:
            try:
                pdf_bytes = self._doc.tobytes(garbage=4, deflate=True)
            except TypeError:
                pdf_bytes = self._doc.tobytes()
            except Exception:
                pdf_bytes = None
            if pdf_bytes:
                self._schedule_color_detection(delay=900, pdf_bytes=pdf_bytes)
        else:
            self._color_detect_generation += 1

    def _add_pages_after_current(self):
        if not self._ensure_document_ready("добавления страниц"):
            return
        try:
            import fitz
        except ImportError:
            messagebox.showerror("Ошибка", "Установите pymupdf:\n\npip install pymupdf")
            return

        paths = filedialog.askopenfilenames(
            title="Добавить страницу из PDF или картинки",
            filetypes=[
                ("PDF и изображения", "*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp"),
                ("PDF", "*.pdf"),
                ("Изображения", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp"),
                ("Все файлы", "*.*"),
            ])
        if not paths:
            return

        before = self._snapshot_document_state()
        insert_at = self._current_page + 1
        added_total = 0
        opened_docs = []

        try:
            for raw_path in paths:
                path = Path(raw_path)
                target = insert_at + added_total
                if path.suffix.lower() == ".pdf":
                    src = fitz.open(str(path))
                    opened_docs.append(src)
                    added = len(src)
                    self._doc.insert_pdf(src, start_at=target)
                else:
                    src = fitz.open(str(path))
                    opened_docs.append(src)
                    pdf_bytes = src.convert_to_pdf()
                    img_pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
                    opened_docs.append(img_pdf)
                    added = len(img_pdf)
                    self._doc.insert_pdf(img_pdf, start_at=target)
                added_total += added

            if added_total <= 0:
                return
            self._shift_page_state_for_insert(insert_at, added_total)
            self._after_document_structure_change(insert_at)
            after = self._snapshot_document_state()
            self._push_action({
                "type": "document_state",
                "before": before,
                "after": after,
            })
            self._sb_status_var.set(f"Добавлено страниц: {added_total}")
        except Exception as e:
            self._restore_document_state(before)
            messagebox.showerror("Не удалось добавить страницу", str(e))
        finally:
            for doc in opened_docs:
                try:
                    doc.close()
                except Exception:
                    pass

    def _begin_split(self, orientation):
        if not self._ensure_document_ready("разделения страницы"):
            return
        tool = "split_horizontal" if orientation == "horizontal" else "split_vertical"
        self._set_tool(tool)
        dx0, dy0, dx1, dy1 = self._sanitize_box(getattr(self, "_display_box", None))
        fraction = (dy0 + dy1) / 2 if orientation == "horizontal" else (dx0 + dx1) / 2
        self._draw_split_line(orientation, fraction)
        if orientation == "horizontal":
            self._sb_status_var.set("Кликните по месту горизонтального разреза.")
        else:
            self._sb_status_var.set("Кликните по месту вертикального разреза.")

    def _draw_split_line(self, orientation, fraction):
        self._canvas.delete("split_preview")
        ox, oy = self._page_render_ox, self._page_render_oy
        pw, ph = self._page_render_w, self._page_render_h
        if pw <= 0 or ph <= 0:
            return

        dx0, dy0, dx1, dy1 = self._sanitize_box(getattr(self, "_display_box", None))
        if orientation == "horizontal":
            span = max(0.001, dy1 - dy0)
            local = (fraction - dy0) / span
            if local < 0.0 or local > 1.0:
                return
            y = oy + local * ph
            self._canvas.create_line(
                ox, y, ox + pw, y,
                fill=AMBER, width=3, dash=(8, 4), tags="split_preview")
            self._canvas.create_text(
                ox + pw - 8, y - 8,
                text="разрез", anchor="se",
                fill=AMBER, font=("Segoe UI", 9, "bold"),
                tags="split_preview")
        else:
            span = max(0.001, dx1 - dx0)
            local = (fraction - dx0) / span
            if local < 0.0 or local > 1.0:
                return
            x = ox + local * pw
            self._canvas.create_line(
                x, oy, x, oy + ph,
                fill=AMBER, width=3, dash=(8, 4), tags="split_preview")
            self._canvas.create_text(
                x + 8, oy + 8,
                text="разрез", anchor="nw",
                fill=AMBER, font=("Segoe UI", 9, "bold"),
                tags="split_preview")

    def _split_current_page(self, orientation, fraction=0.5):
        if not self._ensure_document_ready("разделения страницы"):
            return False
        try:
            import fitz
        except ImportError:
            messagebox.showerror("Ошибка", "Установите pymupdf:\n\npip install pymupdf")
            return False

        before = self._snapshot_document_state()
        idx = self._current_page
        old_doc = self._doc

        try:
            page = old_doc.load_page(idx)
            rect = page.rect
            if rect.width < 2 or rect.height < 2:
                messagebox.showwarning("Маленькая страница", "Эту страницу нельзя разделить.")
                return False

            try:
                fraction = float(fraction)
            except (TypeError, ValueError):
                fraction = 0.5
            if fraction < 0.03 or fraction > 0.97:
                messagebox.showwarning(
                    "Линия слишком близко к краю",
                    "Выберите место разреза дальше от края страницы.")
                return False

            if orientation == "horizontal":
                mid = rect.y0 + rect.height * fraction
                clips = [
                    fitz.Rect(rect.x0, rect.y0, rect.x1, mid),
                    fitz.Rect(rect.x0, mid, rect.x1, rect.y1),
                ]
            else:
                mid = rect.x0 + rect.width * fraction
                clips = [
                    fitz.Rect(rect.x0, rect.y0, mid, rect.y1),
                    fitz.Rect(mid, rect.y0, rect.x1, rect.y1),
                ]

            new_doc = fitz.open()
            if idx > 0:
                new_doc.insert_pdf(old_doc, from_page=0, to_page=idx - 1)
            for clip in clips:
                new_page = new_doc.new_page(
                    width=max(1, clip.width),
                    height=max(1, clip.height))
                new_page.show_pdf_page(new_page.rect, old_doc, idx, clip=clip)
            if idx + 1 < len(old_doc):
                new_doc.insert_pdf(old_doc, from_page=idx + 1, to_page=len(old_doc) - 1)

            self._doc = new_doc
            try:
                old_doc.close()
            except Exception:
                pass
            self._shift_page_state_for_replace(idx, 2)
            self._after_document_structure_change(idx)
            after = self._snapshot_document_state()
            self._push_action({
                "type": "document_state",
                "before": before,
                "after": after,
            })
            self._sb_status_var.set("Страница разделена на две части")
            self._canvas.delete("split_preview")
            return True
        except Exception as e:
            self._doc = old_doc
            self._restore_document_state(before)
            messagebox.showerror("Не удалось разделить страницу", str(e))
            return False

    def _delete_current_page(self):
        if not self._ensure_document_ready("удаления страницы"):
            return
        if self._page_count <= 1:
            messagebox.showwarning(
                "Нельзя удалить",
                "В документе должна остаться хотя бы одна страница.")
            return
        if not messagebox.askyesno(
            "Удалить страницу",
            f"Удалить страницу {self._current_page + 1} из документа?"):
            return

        before = self._snapshot_document_state()
        idx = self._current_page
        try:
            self._doc.delete_page(idx)
            self._shift_page_state_for_delete(idx)
            new_current = min(idx, len(self._doc) - 1)
            self._after_document_structure_change(new_current)
            after = self._snapshot_document_state()
            self._push_action({
                "type": "document_state",
                "before": before,
                "after": after,
            })
            self._sb_status_var.set(f"Удалена страница {idx + 1}")
        except Exception as e:
            self._restore_document_state(before)
            messagebox.showerror("Не удалось удалить страницу", str(e))

    def _download_current_page(self):
        if not self._ensure_document_ready("скачивания страницы"):
            return

        idx = self._current_page
        source = Path(self._input_path).stem if self._input_path else "CrystalPDF"
        out_path = self._downloads_dir() / f"{source}_page_{idx + 1:03d}_CrystalPDF.pdf"
        out_path = self._unique_output_path(out_path)

        try:
            image = self._render_session_page_image(idx, 300)
            if image.mode != "RGB":
                image = image.convert("RGB")
            saved_path = _save_pdf_images([image], out_path, 300, False)
            self._sb_status_var.set(f"Текущая страница сохранена: {saved_path.name}")
            messagebox.showinfo(
                "Готово",
                f"Текущая страница сохранена:\n\n{saved_path}")
        except Exception as e:
            messagebox.showerror("Не удалось сохранить страницу", str(e))

    def _render_session_page_image(self, idx, render_dpi=300):
        import fitz
        import numpy as np
        from PIL import Image

        render_zoom = render_dpi / 72.0
        rot = self._rotations.get(idx, 0)
        page = self._doc.load_page(idx)
        mat = fitz.Matrix(render_zoom, render_zoom).prerotate(rot)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, 3)
        pil_img = Image.fromarray(img)
        original_rgb = pil_img.copy()

        brightness, contrast = self._page_adjustment_values(idx)
        if not self._skip_pages.get(idx, False) and not self._is_page_outside_clean_limit(idx):
            pil_img = _adjust_pil_image(pil_img, brightness, contrast)

        _apply_eraser(pil_img, self._eraser_masks.get(idx, []))
        _restore_protected_region(pil_img, original_rgb, self._protected_boxes.get(idx))
        return _apply_crop(pil_img, self._crop_boxes.get(idx))

    def _render_current_page_for_download(self, idx):
        import fitz
        import cv2
        import numpy as np
        from PIL import Image, ImageEnhance

        render_dpi = 300
        render_zoom = render_dpi / 72.0
        brightness, contrast = self._page_adjustment_values(idx)
        is_color = bool(self._color_pages.get(idx, False))
        preserve_color = bool(self.keep_color_var.get() and is_color)
        skip_page = bool(self._skip_pages.get(idx, False))
        edge_cleanup = _edge_cleanup_allowed(
            idx,
            self._page_count,
            bool(self.edge_clean_var.get()) and not skip_page,
            self._color_pages,
        )
        clean_settings = CleanSettings(
            mode="manual",
            dpi=render_dpi,
            denoise=int(self.denoise_var.get()),
            dot_area=int(self.dot_var.get()),
            clean_edges=edge_cleanup,
            edge_margin=int(self.margin_var.get()),
            edge_threshold=int(self.thresh_var.get()),
            deskew=bool(self.deskew_var.get()),
            max_angle=float(self.angle_var.get()),
            brightness=brightness,
            contrast=contrast,
        )

        rot = self._rotations.get(idx, 0)
        page = self._doc.load_page(idx)
        mat = fitz.Matrix(render_zoom, render_zoom).prerotate(rot)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, 3)
        original_rgb = Image.fromarray(img)
        protected_box = self._protected_boxes.get(idx)

        if skip_page:
            pil_img = original_rgb.copy()
        elif preserve_color:
            from PIL import ImageFilter
            pil_img = original_rgb.copy()
            pil_img = _adjust_pil_image(
                pil_img,
                brightness,
                contrast)
            pil_img = pil_img.filter(ImageFilter.MedianFilter(3))
            pil_img = ImageEnhance.Contrast(pil_img).enhance(1.2)
            if edge_cleanup:
                pil_img = _apply_edge_cleanup_pil(
                    pil_img,
                    self.margin_var.get(),
                    self.thresh_var.get())
        else:
            gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
            binary = clean_page_image(gray, clean_settings)
            pil_img = Image.fromarray(binary).convert("L")

        _apply_eraser(pil_img, self._eraser_masks.get(idx, []))
        source = original_rgb if pil_img.mode == "RGB" else original_rgb.convert("L")
        _restore_protected_region(pil_img, source, protected_box)
        return _apply_crop(pil_img, self._crop_boxes.get(idx))


    # ── ЗАГРУЗКА PDF ──────────────────────────────────────────────────────────
    def _load_pdf(self, path):
        if self._processing:
            messagebox.showwarning(
                "Подождите",
                "Сначала дождитесь окончания обработки PDF.")
            return
        if self._importing:
            return

        self._import_generation += 1
        generation = self._import_generation
        self._set_importing_ui(True, path)
        threading.Thread(
            target=self._run_import,
            args=(path, generation),
            daemon=True,
        ).start()

    def _load_image_folder(self, folder, save_path=None):
        if self._processing:
            messagebox.showwarning(
                "Подождите",
                "Сначала дождитесь окончания обработки PDF.")
            return
        if self._importing:
            return

        self._import_generation += 1
        generation = self._import_generation
        self._set_importing_ui(True, folder)
        threading.Thread(
            target=self._run_image_folder_import,
            args=(folder, generation, save_path),
            daemon=True,
        ).start()

    def _set_importing_ui(self, importing, path=None):
        self._importing = bool(importing)
        if importing:
            self._cancel_thumb_render_job()
            self._cancel_thumb_build_job()
            self._cancel_page_render_jobs()
            if self._color_detect_job is not None:
                try:
                    self.after_cancel(self._color_detect_job)
                except Exception:
                    pass
                self._color_detect_job = None
            self._color_detect_generation += 1
            self._color_detecting = False
            self._color_detect_scanned = 0
            self._color_detect_total = 0
            self._prog_var.set(0)
            self._prog_pct_var.set("0%")
            self._sb_status_var.set("Импортирование: открытие PDF")
            if hasattr(self, "_badge_label"):
                self._badge_label.config(text="импортирование", fg=BLUE)
            if hasattr(self, "_st_main"):
                self._st_main.config(text="импортирование", fg=BLUE)
            if hasattr(self, "_st_right"):
                self._st_right.config(text="импортирование PDF")
            if hasattr(self, "_import_btn"):
                self._import_btn.config(state="disabled", fg=TXT2, bg=BG2)
            if hasattr(self, "_import_folder_btn"):
                self._import_folder_btn.config(state="disabled", fg=TXT2, bg=BG2)
            if hasattr(self, "_export_btn"):
                self._export_btn.config(state="disabled", fg=TXT2, bg=BG2)
            if hasattr(self, "_run_btn"):
                self._run_btn.config(state="disabled", fg=TXT2, bg=BG3)
            if hasattr(self, "_cancel_btn"):
                self._cancel_btn.config(state="disabled", fg=TXT2, bg=BG2)
            if path and hasattr(self, "_file_chip"):
                self._file_chip.config(text=f"импортирование: {os.path.basename(path)}", fg=BLUE)
            self.config(cursor="watch")
            if hasattr(self, "_canvas"):
                self._canvas.config(cursor="watch")
                self._draw_import_placeholder()
        else:
            self.config(cursor="")
            if hasattr(self, "_import_btn"):
                self._import_btn.config(state="normal", fg=BLUE, bg=BLUE_BG,
                                        highlightbackground=BLUE_BDR)
            if hasattr(self, "_import_folder_btn"):
                self._import_folder_btn.config(state="normal", fg=CYAN, bg=CYAN_BG,
                                               highlightbackground=CYAN_BDR)
            if not self._processing:
                self._set_processing_buttons(False)
            if hasattr(self, "_canvas"):
                self._set_tool(self._tool)

    def _draw_import_placeholder(self):
        if not hasattr(self, "_canvas"):
            return
        self._canvas.delete("all")
        w = self._canvas.winfo_width() or 600
        h = self._canvas.winfo_height() or 400
        self._canvas.create_text(
            w // 2,
            h // 2,
            text="Импортирование PDF...",
            fill=BLUE,
            font=("Segoe UI", 12, "bold"),
        )

    def _run_import(self, path, generation):
        doc = None
        try:
            import fitz
            self._queue.put(("import_progress", generation, 5, "Импортирование: открытие PDF"))
            doc = fitz.open(path)
            page_count = len(doc)
            if page_count <= 0:
                raise ValueError("В PDF нет страниц")
            self._queue.put(("import_progress", generation, 20, f"Импортирование: найдено страниц {page_count}"))
            self._queue.put((
                "import_opened",
                generation,
                path,
                doc,
                page_count,
                {
                    "display_path": path,
                    "render_source_path": path,
                    "temporary_pdf": None,
                },
            ))
        except ImportError:
            self._queue.put(("import_error", generation, "Установите pymupdf:\n\npip install pymupdf"))
        except Exception as e:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass
            self._queue.put(("import_error", generation, str(e)))

    def _run_image_folder_import(self, folder, generation, save_path=None):
        temp_path = None
        doc = None
        final_path = None
        cancelled = False
        try:
            import fitz
            import io
            from PIL import Image, ImageFile, ImageOps
            ImageFile.LOAD_TRUNCATED_IMAGES = True

            image_paths = _list_image_files(folder)
            if not image_paths:
                raise ValueError("В папке нет поддерживаемых изображений")

            fd, temp_path = tempfile.mkstemp(prefix="crystalpdf_scan_", suffix=".pdf")
            os.close(fd)
            target = fitz.open()
            try:
                total = len(image_paths)
                imported_count = 0
                skipped_images = []
                for index, image_path in enumerate(image_paths, start=1):
                    if generation != self._import_generation:
                        cancelled = True
                        break

                    try:
                        with Image.open(image_path) as source:
                            image = ImageOps.exif_transpose(source)
                            if image.mode not in ("RGB", "L"):
                                image = image.convert("RGB")
                            if image.mode == "L":
                                image = image.convert("RGB")
                            image.load()

                            dpi = image.info.get("dpi") or (300, 300)
                            try:
                                xdpi = float(dpi[0])
                                ydpi = float(dpi[1])
                            except Exception:
                                xdpi = ydpi = 300.0
                            xdpi = max(72.0, min(600.0, xdpi or 300.0))
                            ydpi = max(72.0, min(600.0, ydpi or 300.0))

                            page_w = max(1.0, image.width / xdpi * 72.0)
                            page_h = max(1.0, image.height / ydpi * 72.0)
                            buffer = io.BytesIO()
                            image.save(buffer, format="JPEG", quality=95, optimize=True)
                    except Exception as image_error:
                        skipped_images.append(f"{Path(image_path).name}: {image_error}")
                        continue

                    page = target.new_page(width=page_w, height=page_h)
                    page.insert_image(page.rect, stream=buffer.getvalue())
                    imported_count += 1

                    if index == 1 or index % 10 == 0 or index == total:
                        pct = 5.0 + (index / max(1, total)) * 90.0
                        self._queue.put((
                            "import_progress",
                            generation,
                            pct,
                            f"Сборка PDF из сканов: {index}/{total}",
                        ))

                if imported_count <= 0:
                    details = "\n".join(skipped_images[:8])
                    raise ValueError(
                        "Не удалось прочитать изображения в папке."
                        + (f"\n\n{details}" if details else "")
                    )

                if not cancelled:
                    target.save(temp_path, garbage=4, deflate=True, clean=True)
            finally:
                target.close()

            if cancelled:
                if temp_path:
                    try:
                        Path(temp_path).unlink(missing_ok=True)
                    except Exception:
                        pass
                return

            render_source_path = temp_path
            temporary_pdf = temp_path
            if save_path:
                final_path = Path(save_path)
                final_path.parent.mkdir(parents=True, exist_ok=True)
                Path(temp_path).replace(final_path)
                temp_path = None
                render_source_path = str(final_path)
                temporary_pdf = None

            doc = fitz.open(render_source_path)
            self._queue.put((
                "import_opened",
                generation,
                folder,
                doc,
                len(doc),
                {
                    "display_path": folder,
                    "render_source_path": render_source_path,
                    "temporary_pdf": temporary_pdf,
                    "generated_pdf": str(final_path) if final_path else None,
                    "skipped_images": skipped_images,
                },
            ))
        except Exception as e:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass
            if temp_path:
                try:
                    Path(temp_path).unlink(missing_ok=True)
                except Exception:
                    pass
            self._queue.put(("import_error", generation, str(e)))

    def _apply_imported_doc(self, generation, path, doc, page_count, meta=None):
        meta = meta or {}
        if generation != self._import_generation or not self._importing:
            try:
                doc.close()
            except Exception:
                pass
            temp_pdf = meta.get("temporary_pdf")
            if temp_pdf:
                try:
                    Path(temp_pdf).unlink(missing_ok=True)
                except Exception:
                    pass
            return

        old_doc = self._doc
        old_temp_pdf = self._temporary_import_pdf
        temp_pdf = meta.get("temporary_pdf")
        self._cancel_thumb_render_job()
        self._cancel_thumb_build_job()
        self._cancel_page_render_jobs()
        self._color_detect_generation += 1
        self._doc = doc
        self._render_source_path_usable = True
        if old_doc is not None:
            try:
                old_doc.close()
            except Exception:
                pass
        if old_temp_pdf and old_temp_pdf != temp_pdf:
            try:
                Path(old_temp_pdf).unlink(missing_ok=True)
            except Exception:
                pass
        self._temporary_import_pdf = temp_pdf
        self._input_path = meta.get("display_path") or path
        self._render_source_path = meta.get("render_source_path") or path
        self._page_count = int(page_count)
        self._current_page = 0
        self._pan_x = 0
        self._pan_y = 0
        self._display_box = (0.0, 0.0, 1.0, 1.0)
        self._rotations.clear()
        self._eraser_masks.clear()
        self._crop_boxes.clear()
        self._protected_boxes.clear()
        self._skip_pages.clear()
        self._page_adjustments.clear()
        self._adjustment_controls_page = None
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._page_status = {i: "waiting" for i in range(self._page_count)}
        self._color_pages = {}
        self._edit_count = 0
        if hasattr(self, "clean_limit_var") and not self.clean_limit_var.get():
            self.clean_count_var.set(max(1, self._page_count))
        self._sync_clean_count_controls()

        fname = os.path.basename(str(self._input_path))
        self._title_label.config(text=f"✦  {APP_TITLE}  ·  {fname}")
        self._file_chip.config(text=fname, fg=TXT1)

        self._build_thumb_widgets_async(
            generation,
            done_callback=lambda: self._finish_import_success(generation, path, meta),
        )

    def _finish_import_success(self, generation, path, meta=None):
        if generation != self._import_generation:
            return
        meta = meta or {}
        self._set_importing_ui(False)
        self._go_page(0)
        self._prog_var.set(100)
        self._prog_pct_var.set("100%")
        generated_pdf = meta.get("generated_pdf")
        skipped_images = list(meta.get("skipped_images") or [])
        if generated_pdf:
            self._sb_status_var.set(
                f"PDF создан: {os.path.basename(generated_pdf)} · {self._page_count} стр."
                + (f" · пропущено {len(skipped_images)}" if skipped_images else ""))
        else:
            self._sb_status_var.set(
                f"Импортировано: {os.path.basename(path)} · {self._page_count} стр."
                + (f" · пропущено {len(skipped_images)}" if skipped_images else ""))
        self._update_status_bar()
        if skipped_images:
            preview = "\n".join(skipped_images[:8])
            if len(skipped_images) > 8:
                preview += f"\n...и ещё {len(skipped_images) - 8}"
            messagebox.showwarning(
                "Часть сканов пропущена",
                "PDF собран, но некоторые файлы не удалось прочитать:\n\n" + preview)
        self._schedule_color_detection(self._render_source_path or path, delay=900)

    def _finish_import_error(self, generation, error_text):
        if generation != self._import_generation:
            return
        self._set_importing_ui(False)
        self._prog_var.set(0)
        self._prog_pct_var.set("0%")
        self._sb_status_var.set("Ошибка импортирования")
        if hasattr(self, "_file_chip"):
            if self._input_path:
                self._file_chip.config(text=os.path.basename(self._input_path), fg=TXT1)
            else:
                self._file_chip.config(text="нет файла", fg=TXT3)
        self._update_status_bar()
        if self._doc is None and self._project_scan_watch_job is None:
            self._project_scan_watch_job = self.after(3000, self._watch_project_scan_folder)
        messagebox.showerror("Ошибка открытия", error_text)

    def _schedule_color_detection(self, source_path=None, delay=500, pdf_bytes=None):
        if self._color_detect_job is not None:
            try:
                self.after_cancel(self._color_detect_job)
            except Exception:
                pass
            self._color_detect_job = None

        self._color_detect_generation += 1
        generation = self._color_detect_generation
        self._color_detecting = True
        self._color_detect_scanned = 0
        self._color_detect_total = max(0, int(self._page_count or 0))
        self._update_status_bar()
        if self._page_count > AUTO_COLOR_DETECT_PAGE_LIMIT:
            self._color_detecting = False
            self._color_detect_scanned = 0
            if hasattr(self, "_color_info_lbl"):
                self._color_info_lbl.config(
                    text="Цвет будет определяться постранично при обработке")
            self._update_status_bar()
            return

        def start_detection():
            self._color_detect_job = None
            threading.Thread(
                target=self._detect_color_pages,
                args=(source_path, generation, pdf_bytes),
                daemon=True,
            ).start()

        self._color_detect_job = self.after(max(0, int(delay)), start_detection)

    def _detect_color_pages(self, source_path=None, generation=None, pdf_bytes=None):
        """Фоново определяет, какие страницы содержат цвет."""
        try:
            import fitz
            import numpy as np

            if generation != self._color_detect_generation:
                return

            if pdf_bytes is not None:
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            elif source_path:
                doc = fitz.open(source_path)
            else:
                return

            detected = {}
            try:
                total = len(doc)
                color_zoom = 0.16 if total > 300 else 0.22
                for i in range(total):
                    if generation != self._color_detect_generation:
                        return
                    page = doc.load_page(i)
                    pix = page.get_pixmap(matrix=fitz.Matrix(color_zoom, color_zoom), colorspace=fitz.csRGB)
                    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
                    if pix.n >= 3:
                        r = arr[:, :, 0].astype(int)
                        g = arr[:, :, 1].astype(int)
                        b = arr[:, :, 2].astype(int)
                        chroma = (np.abs(r - g) + np.abs(g - b) + np.abs(r - b)) // 3
                        detected[i] = bool(np.mean(chroma > 15) > 0.005)
                    else:
                        detected[i] = False
                    if (i + 1) % 25 == 0:
                        self._queue.put(("color_detect_partial", generation, dict(detected)))
                        self._queue.put(("color_detect_progress", generation, i + 1, total))
            finally:
                doc.close()

            self._queue.put(("color_detect_done", generation, detected))
        except Exception:
            self._queue.put(("color_detect_done", generation, {}))

    # ── ВИДЖЕТЫ МИНИАТЮР ──────────────────────────────────────────────────────
    def _bind_thumb_click(self, widget, idx):
        widget.bind("<Button-1>", lambda _e, page_idx=idx: self._go_page(page_idx))
        try:
            for child in widget.winfo_children():
                self._bind_thumb_click(child, idx)
        except Exception:
            pass

    def _create_thumb_widget(self, i):
        f = tk.Frame(self._thumb_inner, bg=BG1, cursor="hand2",
                     highlightthickness=2,
                     highlightbackground=BDR if i != 0 else BLUE)
        f.pack(side="left", padx=(4 if i == 0 else 2, 2), pady=4)

        thumb_bg = tk.Frame(f, bg=WHITE_PAGE,
                            width=self._ui.px(44), height=self._ui.px(58))
        thumb_bg.pack(padx=2, pady=(2, 0))
        thumb_bg.pack_propagate(False)
        for h in [8, 5, 5, 5, 5, 5, 5]:
            tk.Frame(thumb_bg, bg="#dddddd", height=self._ui.px(h)
                     ).pack(fill="x", padx=self._ui.px(5), pady=1)

        dot = tk.Frame(f, bg=TXT3,
                       width=self._ui.px(8), height=self._ui.px(8))
        dot.place(in_=thumb_bg, relx=1.0, rely=0, anchor="ne",
                  x=-2, y=2)

        num = tk.Label(f, text=str(i + 1),
                       font=("Courier New", 8), fg=TXT3, bg=BG1)
        num.pack()

        rot_lbl = tk.Label(f, text="",
                           font=("Courier New", 7, "bold"),
                           fg=AMBER, bg=BG1)
        rot_lbl.pack()

        self._thumb_frames.append((f, dot, rot_lbl))
        self._thumb_containers[i] = thumb_bg
        self._bind_thumb_click(f, i)
        self._update_thumb_status(i)

    def _build_thumb_widgets(self):
        if self._page_count > LARGE_DOCUMENT_PAGE_LIMIT:
            self._build_thumb_widgets_async(self._import_generation)
            return
        self._cancel_thumb_render_job()
        self._cancel_thumb_build_job()
        for w in self._thumb_inner.winfo_children():
            w.destroy()
        self._thumb_frames = []
        self._thumb_containers = {}
        self._thumb_rendered = set()
        self._thumb_render_queue = []

        for i in range(self._page_count):
            self._create_thumb_widget(i)

        for idx in range(min(self._page_count, 18)):
            self._queue_thumb_render(idx)
        self._queue_thumb_render(self._current_page, priority=True)
        self._schedule_visible_thumb_render(delay=120)

        self._thumb_canvas.after(50, lambda: self._thumb_canvas.xview_moveto(0))

    def _build_thumb_widgets_async(self, generation, done_callback=None):
        self._cancel_thumb_render_job()
        self._cancel_thumb_build_job()
        for w in self._thumb_inner.winfo_children():
            w.destroy()
        self._thumb_frames = []
        self._thumb_containers = {}
        self._thumb_rendered = set()
        self._thumb_render_queue = []
        self._thumb_build_generation = generation
        self._thumb_build_next = 0
        self._thumb_build_done_callback = done_callback
        self._thumb_build_job = self.after(1, self._build_thumb_widgets_batch)

    def _build_thumb_widgets_batch(self):
        self._thumb_build_job = None
        generation = self._thumb_build_generation
        if generation != self._import_generation or self._doc is None:
            return

        total = max(0, int(self._page_count or 0))
        start = int(self._thumb_build_next)
        if total <= 0:
            callback = self._thumb_build_done_callback
            self._thumb_build_done_callback = None
            if callback:
                callback()
            return

        batch_size = 80 if total > 1000 else 60 if total > 300 else 45
        end = min(total, start + batch_size)
        for i in range(start, end):
            self._create_thumb_widget(i)
        self._thumb_build_next = end

        pct = 20.0 + (end / max(1, total)) * 70.0
        self._prog_var.set(pct)
        self._prog_pct_var.set(f"{int(round(pct))}%")
        self._sb_status_var.set(f"Импортирование: страницы {end}/{total}")

        if end < total:
            delay = 16 if total > 1000 else 8 if total > 300 else 1
            self._thumb_build_job = self.after(delay, self._build_thumb_widgets_batch)
            return

        callback = self._thumb_build_done_callback
        self._thumb_build_done_callback = None
        if callback:
            callback()
        initial_count = 4 if self._page_count > LARGE_DOCUMENT_PAGE_LIMIT else 18
        for idx in range(min(self._page_count, initial_count)):
            self._queue_thumb_render(idx)
        self._queue_thumb_render(self._current_page, priority=True)
        self._schedule_visible_thumb_render(
            delay=700 if self._page_count > LARGE_DOCUMENT_PAGE_LIMIT else 120
        )
        self._thumb_canvas.after(50, lambda: self._thumb_canvas.xview_moveto(0))

    def _cancel_thumb_render_job(self):
        if self._thumb_render_job is not None:
            try:
                self.after_cancel(self._thumb_render_job)
            except Exception:
                pass
            self._thumb_render_job = None
        if self._thumb_visible_job is not None:
            try:
                self.after_cancel(self._thumb_visible_job)
            except Exception:
                pass
            self._thumb_visible_job = None

    def _cancel_thumb_build_job(self):
        if self._thumb_build_job is not None:
            try:
                self.after_cancel(self._thumb_build_job)
            except Exception:
                pass
            self._thumb_build_job = None
        self._thumb_build_done_callback = None
        self._thumb_build_next = 0

    def _cancel_page_render_jobs(self):
        self._page_render_generation += 1
        if self._page_render_after_job is not None:
            try:
                self.after_cancel(self._page_render_after_job)
            except Exception:
                pass
            self._page_render_after_job = None
        self._page_render_pending = None

    def _thumb_xview(self, *args):
        if not hasattr(self, "_thumb_canvas"):
            return
        self._thumb_canvas.xview(*args)
        self._schedule_visible_thumb_render(delay=30)

    def _schedule_visible_thumb_render(self, delay=80):
        if self._importing:
            return
        if self._doc is None or not getattr(self, "_thumb_frames", None):
            return
        if self._thumb_visible_job is not None:
            return
        self._thumb_visible_job = self.after(
            max(0, int(delay)),
            self._queue_visible_thumb_render,
        )

    def _queue_visible_thumb_render(self):
        self._thumb_visible_job = None
        if self._importing:
            return
        if self._doc is None or not getattr(self, "_thumb_frames", None):
            return

        near = 1 if self._page_count > LARGE_DOCUMENT_PAGE_LIMIT else 3
        for idx in range(max(0, self._current_page - near), min(self._page_count, self._current_page + near + 1)):
            self._queue_thumb_render(idx, priority=True)

        try:
            bbox = self._thumb_canvas.bbox("all")
            if not bbox:
                return
            total_width = max(1, bbox[2] - bbox[0])
            left_frac, right_frac = self._thumb_canvas.xview()
            left = left_frac * total_width
            right = right_frac * total_width
            preload = max(self._ui.px(260), self._thumb_canvas.winfo_width() // 2)
            left -= preload
            right += preload

            queued_visible = 0
            visible_limit = 4 if self._page_count > LARGE_DOCUMENT_PAGE_LIMIT else None
            for idx, (frame, _dot, _rot_lbl) in enumerate(self._thumb_frames):
                x0 = frame.winfo_x()
                x1 = x0 + max(1, frame.winfo_width())
                if x1 >= left and x0 <= right:
                    self._queue_thumb_render(idx)
                    queued_visible += 1
                    if visible_limit is not None and queued_visible >= visible_limit:
                        break
        except Exception:
            pass

    def _queue_thumb_render(self, idx, priority=False):
        if self._importing:
            return
        if idx < 0 or idx >= self._page_count:
            return
        if idx in self._thumb_rendered:
            return
        if idx in self._thumb_render_queue:
            if priority:
                self._thumb_render_queue.remove(idx)
                self._thumb_render_queue.insert(0, idx)
            return
        if priority:
            self._thumb_render_queue.insert(0, idx)
        else:
            self._thumb_render_queue.append(idx)
        self._schedule_thumb_render()

    def _schedule_thumb_render(self):
        if self._thumb_render_job is None and self._thumb_render_queue:
            delay = 160 if self._page_count > LARGE_DOCUMENT_PAGE_LIMIT else 20
            self._thumb_render_job = self.after(delay, self._render_next_thumb_batch)

    def _render_next_thumb_batch(self):
        self._thumb_render_job = None
        if self._importing:
            return
        rendered = 0
        batch_limit = 1 if self._page_count > LARGE_DOCUMENT_PAGE_LIMIT else 2
        while self._thumb_render_queue and rendered < batch_limit:
            idx = self._thumb_render_queue.pop(0)
            container = self._thumb_containers.get(idx)
            if container is None or idx in self._thumb_rendered:
                continue
            self._render_thumb(idx, container)
            self._thumb_rendered.add(idx)
            rendered += 1
        if self._thumb_render_queue:
            self._schedule_thumb_render()

    def _render_thumb(self, idx, container):
        """Рендерит реальную миниатюру страницы в контейнер."""
        if self._importing:
            return
        try:
            import fitz
            from PIL import Image, ImageTk

            page = self._doc.load_page(idx)
            rot  = self._rotations.get(idx, 0)
            # MIPMAP: подбираем масштаб рендера под физический размер вывода
            # (44×58 точек × масштаб × dpr)
            target_w = self._ui.px(44)
            target_h = self._ui.px(58)
            # PDF-страница в пунктах (1pt = 1/72 дюйма); рендерим так, чтобы
            # её пиксельная карта была чуть больше целевой — для LANCZOS-сжатия
            page_w = page.rect.width or 595
            base = max(target_w / page_w, target_h / (page.rect.height or 842))
            zoom = max(0.05, base * self._ui.render_dpr * 1.4)
            mat  = fitz.Matrix(zoom, zoom).prerotate(rot)
            pix  = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
            img  = Image.frombytes("RGB", (pix.w, pix.h), pix.samples)

            # Подгоняем под целевой размер с качественным фильтром
            img.thumbnail((target_w, target_h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)

            # Чистим и ставим новую картинку
            for w in container.winfo_children():
                w.destroy()
            lbl = tk.Label(container, image=photo, bg=WHITE_PAGE)
            lbl.image = photo
            lbl.pack()
            self._bind_thumb_click(container, idx)
        except Exception:
            pass

    def _update_thumb_status(self, idx):
        if idx >= len(self._thumb_frames):
            return
        frame, dot, rot_lbl = self._thumb_frames[idx]
        status = self._page_status.get(idx, "waiting")
        manual_skip = self._skip_pages.get(idx, False)
        limit_skip = self._is_page_outside_clean_limit(idx)
        if manual_skip or limit_skip:
            status = "skipped"
        dot.config(bg=STATUS_COLORS.get(status, TXT3))

        rot = self._rotations.get(idx, 0)
        marks = []
        if rot:
            marks.append(f"{rot}°")
        if manual_skip:
            marks.append("НЕ ЧИСТИТЬ")
        elif limit_skip:
            marks.append("ВНЕ ЛИМИТА")
        protected_count = _protected_box_count(self._protected_boxes.get(idx))
        if protected_count:
            marks.append(f"ЗАЩ {protected_count}")
        rot_lbl.config(text=" ".join(marks))
        if manual_skip or limit_skip:
            rot_lbl.config(fg=AMBER, bg=AMBER_BG, padx=3)
        elif protected_count:
            rot_lbl.config(fg=CYAN, bg=BG1, padx=0)
        else:
            rot_lbl.config(fg=AMBER, bg=BG1, padx=0)

        # Подсветка текущей
        is_cur = (idx == self._current_page)
        frame.config(highlightbackground=BLUE if is_cur else BDR)

    # ── НАВИГАЦИЯ ПО СТРАНИЦАМ ────────────────────────────────────────────────
    def _go_page(self, idx):
        if self._importing:
            return
        if self._doc is None:
            return
        idx = max(0, min(self._page_count - 1, idx))
        old = self._current_page
        if idx != old:
            self._store_current_page_adjustment(old)
        self._current_page = idx
        self._pan_x = 0
        self._pan_y = 0
        self._pan_anchor = None
        self._sync_adjustment_controls(idx)
        self._update_page_flags_ui()
        self._queue_thumb_render(idx, priority=True)

        # Обновить подсветку старой миниатюры
        self._update_thumb_status(old)
        self._update_thumb_status(idx)

        # Скроллим к текущей миниатюре
        try:
            if idx < len(self._thumb_frames):
                f = self._thumb_frames[idx][0]
                x1 = f.winfo_x()
                total = self._thumb_inner.winfo_width()
                if total > 0:
                    frac = x1 / total
                    self._thumb_canvas.xview_moveto(max(0, frac - 0.1))
                    self._schedule_visible_thumb_render(delay=30)
        except Exception:
            pass

        self._render_page()
        self._update_nav_dots()
        self._nav_pages_lbl.config(text=f"Страница {idx + 1} / {self._page_count}")

    def _update_nav_dots(self):
        for w in self._nav_dots_frame.winfo_children():
            w.destroy()
        n = self._page_count
        cur = self._current_page
        start = max(0, cur - 3)
        end   = min(n, start + 7)
        for i in range(start, end):
            is_cur = (i == cur)
            w_px = 14 if is_cur else 5
            bg = BLUE if is_cur else TXT3
            tk.Frame(self._nav_dots_frame,
                     bg=bg, width=w_px, height=5
                     ).pack(side="left", padx=1)

    # ── ОТРИСОВКА СТРАНИЦЫ ────────────────────────────────────────────────────
    def _render_page(self):
        if self._can_render_page_async():
            self._request_page_render()
            return
        self._render_page_sync()

    def _can_render_page_async(self):
        return (
            self._doc is not None
            and not self._importing
            and not self._processing
            and self._page_count >= ASYNC_PAGE_RENDER_PAGE_LIMIT
            and bool(self._render_source_path)
            and bool(self._render_source_path_usable)
        )

    def _render_dpr_for_page(self):
        dpr = float(getattr(self._ui, "render_dpr", 1.0))
        if self._page_count > LARGE_DOCUMENT_PAGE_LIMIT:
            return max(1.0, min(dpr, 1.2))
        return max(1.0, min(dpr, 1.6))

    def _make_page_render_request(self):
        idx = max(0, min(self._page_count - 1, self._current_page))
        self._page_render_generation += 1
        brightness, contrast = self._page_adjustment_values(idx)
        return {
            "generation": self._page_render_generation,
            "path": self._render_source_path,
            "idx": idx,
            "page_count": self._page_count,
            "cw": max(self._canvas.winfo_width(), 100),
            "ch": max(self._canvas.winfo_height(), 100),
            "zoom": float(self._zoom),
            "dpr": self._render_dpr_for_page(),
            "rotation": int(self._rotations.get(idx, 0)),
            "masks": list(self._eraser_masks.get(idx, [])),
            "crop_box": self._crop_boxes.get(idx),
            "protected_boxes": _sanitize_norm_boxes(self._protected_boxes.get(idx)),
            "brightness": brightness,
            "contrast": contrast,
            "skip_adjust": bool(self._skip_pages.get(idx, False) or self._is_page_outside_clean_limit(idx)),
        }

    def _request_page_render(self):
        request = self._make_page_render_request()
        self._page_render_pending = request
        self._draw_page_render_placeholder(request["idx"])
        if self._page_render_after_job is not None:
            try:
                self.after_cancel(self._page_render_after_job)
            except Exception:
                pass
            self._page_render_after_job = None
        if not self._page_render_active:
            self._page_render_after_job = self.after(45, self._start_pending_page_render)

    def _start_pending_page_render(self):
        self._page_render_after_job = None
        if self._page_render_active or not self._page_render_pending:
            return
        request = self._page_render_pending
        self._page_render_pending = None
        self._page_render_active = True
        threading.Thread(
            target=self._render_page_worker,
            args=(request,),
            daemon=True,
        ).start()

    def _finish_page_render_worker(self):
        self._page_render_active = False
        if self._page_render_pending and self._page_render_after_job is None:
            self._page_render_after_job = self.after(1, self._start_pending_page_render)

    def _draw_page_render_placeholder(self, idx):
        self._canvas.delete("all")
        self._page_render_w = 0
        self._page_render_h = 0
        self._page_full_render_w = 0
        self._page_full_render_h = 0
        self._display_box = (0.0, 0.0, 1.0, 1.0)
        w = self._canvas.winfo_width() or 600
        h = self._canvas.winfo_height() or 400
        self._canvas.create_text(
            w // 2,
            h // 2,
            text=f"Р—Р°РіСЂСѓР·РєР° СЃС‚СЂР°РЅРёС†С‹ {idx + 1}...",
            fill=TXT2,
            font=("Segoe UI", 12, "bold"),
        )

    def _render_page_worker(self, request):
        try:
            import fitz
            from PIL import Image

            doc = fitz.open(request["path"])
            try:
                page = doc.load_page(request["idx"])
                page_w, page_h = page.rect.width, page.rect.height
                rot = int(request["rotation"])
                if rot in (90, 270):
                    page_w, page_h = page_h, page_w
                pad = 24
                fit_scale = min((request["cw"] - pad * 2) / page_w, (request["ch"] - pad * 2) / page_h)
                fit_scale = max(0.05, fit_scale)
                scale = fit_scale * float(request["zoom"])
                dpr = float(request["dpr"])
                mat = fitz.Matrix(scale * dpr, scale * dpr).prerotate(rot)
                pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
                full_img = Image.frombytes("RGB", (pix.w, pix.h), pix.samples)
            finally:
                doc.close()

            if dpr > 1.01:
                target_w = max(1, int(round(pix.w / dpr)))
                target_h = max(1, int(round(pix.h / dpr)))
                full_img = full_img.resize((target_w, target_h), Image.Resampling.LANCZOS)

            protected_source = full_img.copy()
            if not request["skip_adjust"]:
                full_img = _adjust_pil_image(
                    full_img,
                    request["brightness"],
                    request["contrast"],
                )

            if request["masks"]:
                _apply_eraser(full_img, request["masks"])
            _restore_protected_region(full_img, protected_source, request["protected_boxes"])

            raw_crop = request["crop_box"]
            crop = _sanitize_norm_box(raw_crop) or (0.0, 0.0, 1.0, 1.0)
            has_crop = raw_crop is not None and crop != (0.0, 0.0, 1.0, 1.0)
            if has_crop:
                x0, y0, x1, y1 = crop
                left = min(max(0, int(round(x0 * full_img.width))), max(0, full_img.width - 1))
                top = min(max(0, int(round(y0 * full_img.height))), max(0, full_img.height - 1))
                right = min(full_img.width, max(left + 1, int(round(x1 * full_img.width))))
                bottom = min(full_img.height, max(top + 1, int(round(y1 * full_img.height))))
                img = full_img.crop((left, top, right, bottom))
            else:
                img = full_img

            self._queue.put((
                "page_render_done",
                request["generation"],
                request["idx"],
                {
                    "image": img,
                    "page_w": page_w,
                    "page_h": page_h,
                    "scale": scale,
                    "display_box": crop,
                    "has_crop": has_crop,
                    "full_w": full_img.width,
                    "full_h": full_img.height,
                    "rotation": rot,
                },
            ))
        except Exception as e:
            self._queue.put(("page_render_error", request["generation"], request["idx"], str(e)))

    def _apply_page_render_result(self, idx, result):
        from PIL import ImageTk

        if idx != self._current_page:
            return
        img = result["image"]
        self._page_view_w_pt = result["page_w"]
        self._page_view_h_pt = result["page_h"]
        self._display_box = result["display_box"]
        self._page_full_render_w = result["full_w"]
        self._page_full_render_h = result["full_h"]
        self._page_render_w = img.width
        self._page_render_h = img.height
        self._page_render_ox, self._page_render_oy = self._page_origin(
            max(self._canvas.winfo_width(), 100),
            max(self._canvas.winfo_height(), 100),
            img.width,
            img.height,
            24,
        )
        self._page_render_scale = result["scale"]

        photo = ImageTk.PhotoImage(img)
        self._page_photo = photo
        self._canvas.delete("all")
        ox, oy = self._page_render_ox, self._page_render_oy
        pw, ph = img.width, img.height
        self._canvas.create_rectangle(
            ox + 6, oy + 6, ox + pw + 6, oy + ph + 6,
            fill="#000000", outline="", tags=("page_layer",))
        self._canvas.create_image(ox, oy, anchor="nw", image=photo,
                                  tags=("page_layer",))

        if result["rotation"]:
            self._canvas.create_text(
                ox + pw - 6, oy + 6,
                text=f"{result['rotation']}В°", anchor="ne",
                fill=BLUE, font=("Courier New", 9, "bold"),
                tags=("page_layer",))

        if self._color_pages.get(idx, False):
            self._canvas.create_rectangle(
                ox, oy + ph - 6, ox + pw, oy + ph,
                fill=AMBER, outline="", tags=("page_layer",))

        self._draw_edge_zone_overlay(result["page_w"], result["page_h"])
        self._draw_protected_box_overlay()
        self._draw_skip_page_overlay()

        if result["has_crop"]:
            self._canvas.create_rectangle(
                ox, oy, ox + pw, oy + ph,
                outline=GREEN, width=2, tags=("page_layer",))

    def _render_page_sync(self):
        if self._importing:
            self._draw_import_placeholder()
            return
        if self._doc is None:
            self._canvas.delete("all")
            w = self._canvas.winfo_width() or 600
            h = self._canvas.winfo_height() or 400
            self._canvas.create_text(
                w // 2, h // 2,
                text="Откройте PDF через «Импорт PDF»",
                fill=TXT3, font=("Segoe UI", 12))
            return

        try:
            import fitz
            from PIL import Image, ImageTk

            cw = max(self._canvas.winfo_width(), 100)
            ch = max(self._canvas.winfo_height(), 100)

            idx = self._current_page
            rot = self._rotations.get(idx, 0)
            page = self._doc.load_page(idx)
            page_w, page_h = page.rect.width, page.rect.height
            if rot in (90, 270):
                page_w, page_h = page_h, page_w
            self._page_view_w_pt = page_w
            self._page_view_h_pt = page_h

            pad = 24
            fit_scale = min((cw - pad * 2) / page_w, (ch - pad * 2) / page_h)
            fit_scale = max(0.05, fit_scale)
            scale = fit_scale * self._zoom

            # ── MIPMAP: рендерим в физических пикселях для чёткости на HiDPI/4K ──
            # На обычном экране dpr=1.0 → как было.
            # На Retina/4K dpr=2.0: пиксельная карта в 2 раза детальнее,
            # затем PIL уменьшает её до размера показа с LANCZOS-фильтром.
            dpr = self._ui.render_dpr
            mat = fitz.Matrix(scale * dpr, scale * dpr).prerotate(rot)
            pix  = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
            full_img = Image.frombytes("RGB", (pix.w, pix.h), pix.samples)
            # Уменьшаем обратно до размера показа: так картинка чёткая и без мыла
            if dpr > 1.01:
                target_w = max(1, int(round(pix.w / dpr)))
                target_h = max(1, int(round(pix.h / dpr)))
                full_img = full_img.resize((target_w, target_h), Image.LANCZOS)

            protected_source = full_img.copy()
            brightness, contrast = self._page_adjustment_values(idx)
            if not self._skip_pages.get(idx, False) and not self._is_page_outside_clean_limit(idx):
                full_img = _adjust_pil_image(full_img, brightness, contrast)
            self._page_full_render_w = full_img.width
            self._page_full_render_h = full_img.height

            # Применяем маску ластика
            masks = self._eraser_masks.get(idx, [])
            if masks:
                _apply_eraser(full_img, masks)
            _restore_protected_region(full_img, protected_source, self._protected_boxes.get(idx))

            raw_crop = self._crop_boxes.get(idx)
            crop = self._sanitize_box(raw_crop)
            has_crop = raw_crop is not None and crop != (0.0, 0.0, 1.0, 1.0)
            self._display_box = crop
            if has_crop:
                x0, y0, x1, y1 = crop
                left = int(round(x0 * full_img.width))
                top = int(round(y0 * full_img.height))
                right = int(round(x1 * full_img.width))
                bottom = int(round(y1 * full_img.height))
                left = min(max(0, left), max(0, full_img.width - 1))
                top = min(max(0, top), max(0, full_img.height - 1))
                right = min(full_img.width, max(left + 1, right))
                bottom = min(full_img.height, max(top + 1, bottom))
                img = full_img.crop((left, top, right, bottom))
            else:
                img = full_img

            # Запоминаем геометрию для инструментов
            self._page_render_w = img.width
            self._page_render_h = img.height
            self._page_render_ox, self._page_render_oy = self._page_origin(
                cw, ch, img.width, img.height, pad)
            self._page_render_scale = scale

            photo = ImageTk.PhotoImage(img)
            self._page_photo = photo  # держим ссылку

            self._canvas.delete("all")
            # Тень
            ox, oy = self._page_render_ox, self._page_render_oy
            pw, ph = img.width, img.height
            self._canvas.create_rectangle(
                ox + 6, oy + 6, ox + pw + 6, oy + ph + 6,
                fill="#000000", outline="", tags=("page_layer",))
            self._canvas.create_image(ox, oy, anchor="nw", image=photo,
                                      tags=("page_layer",))

            # Поворот-бейдж
            if rot:
                self._canvas.create_text(
                    ox + pw - 6, oy + 6,
                    text=f"{rot}°", anchor="ne",
                    fill=BLUE, font=("Courier New", 9, "bold"),
                    tags=("page_layer",))

            # Цветная страница — полоска внизу
            if self._color_pages.get(idx, False):
                self._canvas.create_rectangle(
                    ox, oy + ph - 6, ox + pw, oy + ph,
                    fill=AMBER, outline="", tags=("page_layer",))

            self._draw_edge_zone_overlay(page_w, page_h)
            self._draw_protected_box_overlay()
            self._draw_skip_page_overlay()

            if has_crop:
                self._canvas.create_rectangle(
                    ox, oy, ox + pw, oy + ph,
                    outline=GREEN, width=2, tags=("page_layer",))

        except ImportError:
            self._canvas.delete("all")
            self._canvas.create_text(
                300, 200, text="Нужен pymupdf + Pillow\npip install pymupdf pillow",
                fill=RED, font=("Segoe UI", 10))
        except Exception as e:
            self._canvas.delete("all")
            self._canvas.create_text(
                24, 24,
                text=f"Не удалось показать страницу\n{e}",
                anchor="nw", fill=RED, font=("Segoe UI", 10))

    def _sanitize_box(self, box):
        if not box:
            return (0.0, 0.0, 1.0, 1.0)
        try:
            x0, y0, x1, y1 = [float(v) for v in box]
        except (TypeError, ValueError):
            return (0.0, 0.0, 1.0, 1.0)
        x0, x1 = sorted((max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))))
        y0, y1 = sorted((max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))))
        if (x1 - x0) < 0.001 or (y1 - y0) < 0.001:
            return (0.0, 0.0, 1.0, 1.0)
        return (x0, y0, x1, y1)

    def _page_origin(self, cw, ch, img_w, img_h, pad=24):
        base_x = (cw - img_w) // 2
        base_y = (ch - img_h) // 2
        ox = base_x + self._pan_x
        oy = base_y + self._pan_y

        min_x = min(pad, cw - pad - img_w)
        max_x = max(pad, cw - pad - img_w)
        min_y = min(pad, ch - pad - img_h)
        max_y = max(pad, ch - pad - img_h)

        ox = min(max(ox, min_x), max_x)
        oy = min(max(oy, min_y), max_y)
        self._pan_x = ox - base_x
        self._pan_y = oy - base_y

        return ox, oy

    def _move_page_layer_to_current_pan(self):
        cw = max(self._canvas.winfo_width(), 100)
        ch = max(self._canvas.winfo_height(), 100)
        pw = self._page_render_w
        ph = self._page_render_h
        if pw <= 0 or ph <= 0:
            return
        old_ox, old_oy = self._page_render_ox, self._page_render_oy
        new_ox, new_oy = self._page_origin(cw, ch, pw, ph, 24)
        dx = new_ox - old_ox
        dy = new_oy - old_oy
        if abs(dx) < 0.001 and abs(dy) < 0.001:
            return
        self._page_render_ox = new_ox
        self._page_render_oy = new_oy
        self._canvas.move("page_layer", dx, dy)

    def _canvas_to_norm(self, cx, cy, clamp=False):
        ox, oy = self._page_render_ox, self._page_render_oy
        pw, ph = self._page_render_w, self._page_render_h
        if pw <= 0 or ph <= 0:
            return None
        lx = (cx - ox) / pw
        ly = (cy - oy) / ph
        if not clamp and (lx < 0.0 or ly < 0.0 or lx > 1.0 or ly > 1.0):
            return None
        lx = min(1.0, max(0.0, lx))
        ly = min(1.0, max(0.0, ly))
        dx0, dy0, dx1, dy1 = self._sanitize_box(getattr(self, "_display_box", None))
        xf = dx0 + lx * (dx1 - dx0)
        yf = dy0 + ly * (dy1 - dy0)
        return xf, yf

    def _norm_box_to_canvas(self, box):
        ox, oy = self._page_render_ox, self._page_render_oy
        pw, ph = self._page_render_w, self._page_render_h
        x0, y0, x1, y1 = box
        dx0, dy0, dx1, dy1 = self._sanitize_box(getattr(self, "_display_box", None))
        dw = max(0.001, dx1 - dx0)
        dh = max(0.001, dy1 - dy0)
        x0 = (x0 - dx0) / dw
        x1 = (x1 - dx0) / dw
        y0 = (y0 - dy0) / dh
        y1 = (y1 - dy0) / dh
        return (
            ox + x0 * pw,
            oy + y0 * ph,
            ox + x1 * pw,
            oy + y1 * ph,
        )

    def _norm_box_intersection_to_canvas(self, box):
        clean = _sanitize_norm_box(box)
        if not clean:
            return None
        x0, y0, x1, y1 = clean
        dx0, dy0, dx1, dy1 = self._sanitize_box(getattr(self, "_display_box", None))
        x0 = max(x0, dx0)
        y0 = max(y0, dy0)
        x1 = min(x1, dx1)
        y1 = min(y1, dy1)
        if x1 <= x0 or y1 <= y0:
            return None
        return self._norm_box_to_canvas((x0, y0, x1, y1))

    def _draw_protected_box_overlay(self):
        boxes = _sanitize_norm_boxes(self._protected_boxes.get(self._current_page))
        if not boxes:
            return
        for number, box in enumerate(boxes, start=1):
            coords = self._norm_box_intersection_to_canvas(box)
            if not coords:
                continue
            cx0, cy0, cx1, cy1 = coords
            self._canvas.create_rectangle(
                cx0, cy0, cx1, cy1,
                fill=CYAN, stipple="gray25",
                outline=CYAN, width=2, dash=(6, 3),
                tags=("page_layer", "protect_overlay"))
            label = "Защита" if len(boxes) == 1 else f"Защита {number}"
            self._canvas.create_text(
                cx0 + 8, cy0 + 8,
                text=label, anchor="nw",
                fill=CYAN, font=("Segoe UI", 9, "bold"),
                tags=("page_layer", "protect_overlay"))

    def _draw_skip_page_overlay(self):
        manual_skip = self._skip_pages.get(self._current_page, False)
        limit_skip = self._is_page_outside_clean_limit(self._current_page)
        if not manual_skip and not limit_skip:
            return
        ox, oy = self._page_render_ox, self._page_render_oy
        pw = self._page_render_w
        if pw <= 0:
            return
        h = self._ui.px(24)
        text = (
            "Страница помечена: не чистить"
            if manual_skip
            else "Вне выбранного количества очистки"
        )
        self._canvas.create_rectangle(
            ox, oy, ox + pw, oy + h,
            fill=AMBER_BG, outline=AMBER, width=1,
            tags=("page_layer", "skip_page_overlay"))
        self._canvas.create_text(
            ox + 8, oy + h // 2,
            text=text,
            anchor="w", fill=AMBER,
            font=("Segoe UI", 9, "bold"),
            tags=("page_layer", "skip_page_overlay"))

    def _redraw_edge_zone_overlay(self):
        self._canvas.delete("edge_zone")
        page_w = getattr(self, "_page_view_w_pt", 0)
        page_h = getattr(self, "_page_view_h_pt", 0)
        if page_w and page_h:
            self._draw_edge_zone_overlay(page_w, page_h)

    def _draw_edge_zone_overlay(self, page_w_pt, page_h_pt):
        if not getattr(self, "_show_edge_zone", False):
            return
        if self._skip_pages.get(self._current_page, False):
            return
        if self._is_page_outside_clean_limit(self._current_page):
            return
        if not hasattr(self, "edge_clean_var") or not self.edge_clean_var.get():
            return
        if not _edge_cleanup_allowed(
            self._current_page,
            self._page_count,
            bool(self.edge_clean_var.get()),
            self._color_pages,
        ):
            return
        if not hasattr(self, "margin_var"):
            return

        try:
            margin_px = max(0, int(self.margin_var.get()))
        except tk.TclError:
            return
        if margin_px <= 0:
            return

        render_zoom = 300 / 72.0
        mx = min(0.5, margin_px / max(1.0, float(page_w_pt) * render_zoom))
        my = min(0.5, margin_px / max(1.0, float(page_h_pt) * render_zoom))
        dx0, dy0, dx1, dy1 = self._sanitize_box(getattr(self, "_display_box", None))
        dw = max(0.001, dx1 - dx0)
        dh = max(0.001, dy1 - dy0)
        ox, oy = self._page_render_ox, self._page_render_oy
        pw, ph = self._page_render_w, self._page_render_h

        def draw_rect(x0, y0, x1, y1):
            ix0 = max(dx0, x0)
            iy0 = max(dy0, y0)
            ix1 = min(dx1, x1)
            iy1 = min(dy1, y1)
            if ix1 <= ix0 or iy1 <= iy0:
                return
            cx0 = ox + ((ix0 - dx0) / dw) * pw
            cy0 = oy + ((iy0 - dy0) / dh) * ph
            cx1 = ox + ((ix1 - dx0) / dw) * pw
            cy1 = oy + ((iy1 - dy0) / dh) * ph
            self._canvas.create_rectangle(
                cx0, cy0, cx1, cy1,
                fill=AMBER, stipple="gray25",
                outline=AMBER, width=1, tags=("page_layer", "edge_zone"))

        draw_rect(0.0, 0.0, 1.0, my)
        draw_rect(0.0, 1.0 - my, 1.0, 1.0)
        draw_rect(0.0, 0.0, mx, 1.0)
        draw_rect(1.0 - mx, 0.0, 1.0, 1.0)

    # ── УПРАВЛЕНИЕ ИНСТРУМЕНТАМИ ──────────────────────────────────────────────
    def _set_tool(self, tool):
        if self._importing:
            return
        self._tool = tool
        if not str(tool).startswith("split_"):
            self._canvas.delete("split_preview")
        buttons = {
            "view": self._btn_view,
            "pan": self._btn_pan,
            "eraser": self._btn_eraser,
            "crop": self._btn_crop,
            "protect": self._btn_protect,
        }
        for name, btn in buttons.items():
            selected = name == tool
            btn.config(
                fg=BLUE if selected else TXT1,
                bg=BLUE_BG if selected else BG1,
                highlightbackground=BLUE_BDR if selected else BDR)
        cursors = {
            "view": "arrow",
            "pan": "fleur",
            "eraser": "crosshair",
            "crop": "crosshair",
            "protect": "crosshair",
            "split_vertical": "crosshair",
            "split_horizontal": "crosshair",
        }
        self._canvas.config(cursor=cursors.get(tool, "arrow"))

    # ── СОБЫТИЯ ХОЛСТА ────────────────────────────────────────────────────────
    def _canvas_motion(self, event):
        if self._importing:
            return
        if self._doc is None:
            return
        self._canvas.delete("eraser_cursor")
        if self._tool in ("split_vertical", "split_horizontal"):
            point = self._canvas_to_norm(event.x, event.y)
            if point is None:
                self._canvas.delete("split_preview")
                return
            orientation = "horizontal" if self._tool == "split_horizontal" else "vertical"
            fraction = point[1] if orientation == "horizontal" else point[0]
            self._draw_split_line(orientation, fraction)
            return
        if self._tool == "eraser":
            if self._canvas_to_norm(event.x, event.y) is None:
                return
            r = self._eraser_sz_var.get()
            x, y = event.x, event.y
            self._canvas.create_oval(
                x - r, y - r, x + r, y + r,
                outline=BLUE, width=2, tags="eraser_cursor")

    def _canvas_leave(self, _):
        self._canvas.delete("eraser_cursor")
        self._canvas.delete("split_preview")
        self._canvas.delete("protect_preview")

    def _canvas_click(self, event):
        if self._importing:
            return "break"
        if self._tool == "eraser":
            if self._canvas_to_norm(event.x, event.y) is None:
                self._stroke_before = None
                self._canvas.delete("eraser_cursor")
                return
            idx = self._current_page
            self._stroke_before = list(self._eraser_masks.get(idx, []))
            self._do_erase(event.x, event.y, render=False)
        elif self._tool == "pan":
            self._pan_anchor = (event.x, event.y, self._pan_x, self._pan_y)
        elif self._tool in ("split_vertical", "split_horizontal"):
            point = self._canvas_to_norm(event.x, event.y)
            if point is None:
                return
            orientation = "horizontal" if self._tool == "split_horizontal" else "vertical"
            fraction = point[1] if orientation == "horizontal" else point[0]
            if self._split_current_page(orientation, fraction):
                self._set_tool("pan")
        elif self._tool == "crop":
            point = self._canvas_to_norm(event.x, event.y)
            if point:
                self._crop_start = point
                self._canvas.delete("crop_preview")
        elif self._tool == "protect":
            point = self._canvas_to_norm(event.x, event.y)
            if point:
                self._protect_start = point
                self._canvas.delete("protect_preview")

    def _canvas_drag(self, event):
        if self._importing:
            return "break"
        if self._tool == "eraser":
            if self._canvas_to_norm(event.x, event.y) is None:
                self._canvas.delete("eraser_cursor")
                return
            self._do_erase(event.x, event.y, render=False)
            self._canvas_motion(event)
        elif self._tool in ("split_vertical", "split_horizontal"):
            self._canvas_motion(event)
        elif self._tool == "pan" and self._pan_anchor:
            sx, sy, px, py = self._pan_anchor
            self._pan_x = px + (event.x - sx)
            self._pan_y = py + (event.y - sy)
            self._move_page_layer_to_current_pan()
        elif self._tool == "crop" and self._crop_start:
            point = self._canvas_to_norm(event.x, event.y, clamp=True)
            if point:
                x0, y0 = self._crop_start
                x1, y1 = point
                box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
                self._canvas.delete("crop_preview")
                cx0, cy0, cx1, cy1 = self._norm_box_to_canvas(box)
                self._canvas.create_rectangle(
                    cx0, cy0, cx1, cy1,
                    outline=GREEN, width=2, dash=(6, 3), tags="crop_preview")
        elif self._tool == "protect" and self._protect_start:
            point = self._canvas_to_norm(event.x, event.y, clamp=True)
            if point:
                x0, y0 = self._protect_start
                x1, y1 = point
                box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
                self._canvas.delete("protect_preview")
                cx0, cy0, cx1, cy1 = self._norm_box_to_canvas(box)
                self._canvas.create_rectangle(
                    cx0, cy0, cx1, cy1,
                    outline=CYAN, width=2, dash=(6, 3), tags="protect_preview")

    def _canvas_release(self, event):
        if self._importing:
            return "break"
        if self._tool == "eraser" and self._stroke_before is not None:
            idx = self._current_page
            after = list(self._eraser_masks.get(idx, []))
            if after != self._stroke_before:
                self._push_action({"type": "eraser", "page": idx, "before": self._stroke_before, "after": after})
            self._stroke_before = None
            self._render_page()
        elif self._tool == "pan":
            self._pan_anchor = None
        elif self._tool == "crop" and self._crop_start:
            point = self._canvas_to_norm(event.x, event.y, clamp=True)
            idx = self._current_page
            if point:
                x0, y0 = self._crop_start
                x1, y1 = point
                box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
                if (box[2] - box[0]) > 0.03 and (box[3] - box[1]) > 0.03:
                    before = self._crop_boxes.get(idx)
                    self._crop_boxes[idx] = box
                    self._push_action({"type": "crop", "page": idx, "before": before, "after": box})
            self._crop_start = None
            self._canvas.delete("crop_preview")
            self._render_page()
        elif self._tool == "protect" and self._protect_start:
            point = self._canvas_to_norm(event.x, event.y, clamp=True)
            idx = self._current_page
            if point:
                x0, y0 = self._protect_start
                x1, y1 = point
                box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
                if (box[2] - box[0]) > 0.03 and (box[3] - box[1]) > 0.03:
                    before = _sanitize_norm_boxes(self._protected_boxes.get(idx))
                    if len(before) >= MAX_PROTECTED_BOXES_PER_PAGE:
                        self._sb_status_var.set(
                            f"На странице уже {MAX_PROTECTED_BOXES_PER_PAGE} защищённых областей")
                    else:
                        after = before + [box]
                        self._protected_boxes[idx] = after
                        self._push_action({"type": "protect", "page": idx, "before": before, "after": after})
                    self._update_thumb_status(idx)
                    self._update_page_flags_ui()
            self._protect_start = None
            self._canvas.delete("protect_preview")
            self._render_page()

    def _do_erase(self, cx, cy, render=True):
        if self._doc is None:
            return
        point = self._canvas_to_norm(cx, cy)
        if point is None:
            return
        xf, yf = point
        sz = self._eraser_sz_var.get()
        for protected in _sanitize_norm_boxes(self._protected_boxes.get(self._current_page)):
            if protected[0] <= xf <= protected[2] and protected[1] <= yf <= protected[3]:
                return
        full_w = max(1, getattr(self, "_page_full_render_w", self._page_render_w))
        full_h = max(1, getattr(self, "_page_full_render_h", self._page_render_h))
        rf = sz / min(full_w, full_h)

        idx = self._current_page
        if idx not in self._eraser_masks:
            self._eraser_masks[idx] = []
        if self._eraser_masks[idx]:
            lx, ly, lr = self._eraser_masks[idx][-1]
            if abs(lx - xf) * full_w < max(2, sz * 0.35) and abs(ly - yf) * full_h < max(2, sz * 0.35):
                return
        self._eraser_masks[idx].append((xf, yf, rf))

        self._draw_live_eraser(cx, cy, sz)
        self._recount_edits()
        self._update_status_bar()
        if render:
            self._render_page()

    def _draw_live_eraser(self, cx, cy, r):
        ox, oy = self._page_render_ox, self._page_render_oy
        pw, ph = self._page_render_w, self._page_render_h
        if cx - r < ox or cy - r < oy or cx + r > ox + pw or cy + r > oy + ph:
            return
        self._canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            fill=WHITE_PAGE, outline="", tags="live_edit")
        self._redraw_edge_zone_overlay()
        self._draw_protected_box_overlay()

    def _toggle_skip_page(self):
        if self._importing or self._doc is None:
            return
        idx = self._current_page
        before = bool(self._skip_pages.get(idx, False))
        after = not before
        if after:
            self._skip_pages[idx] = True
        else:
            self._skip_pages.pop(idx, None)
        self._push_action({"type": "skip_page", "page": idx, "before": before, "after": after})
        self._update_thumb_status(idx)
        self._update_page_flags_ui()
        self._render_page()
        self._update_status_bar()

    def _clear_protected_area(self):
        if self._importing or self._doc is None:
            return
        idx = self._current_page
        before = _sanitize_norm_boxes(self._protected_boxes.get(idx))
        if not before:
            return
        self._protected_boxes.pop(idx, None)
        self._push_action({"type": "protect", "page": idx, "before": before, "after": None})
        self._update_thumb_status(idx)
        self._update_page_flags_ui()
        self._render_page()
        self._update_status_bar()

    def _update_page_flags_ui(self):
        idx = getattr(self, "_current_page", 0)
        if hasattr(self, "_skip_page_btn"):
            if self._skip_pages.get(idx, False):
                self._skip_page_btn.config(
                    text="✓  Стр. не чистить",
                    fg=AMBER, bg=AMBER_BG,
                    highlightbackground=AMBER_BDR)
            else:
                self._skip_page_btn.config(
                    text="☐  Не чистить стр.",
                    fg=TXT1, bg=BG2,
                    highlightbackground=BDR)
        if hasattr(self, "_clear_protect_btn"):
            protected_count = _protected_box_count(self._protected_boxes.get(idx))
            has_box = protected_count > 0
            self._clear_protect_btn.config(
                text=f"Снять защиту ({protected_count})" if has_box else "Снять защиту области",
                state="normal" if has_box else "disabled",
                fg=CYAN if has_box else TXT2,
                bg=CYAN_BG if has_box else BG2,
                highlightbackground=CYAN_BDR if has_box else BDR)

    def _clean_page_limit(self):
        if not hasattr(self, "clean_limit_var") or not self.clean_limit_var.get():
            return None
        if self._page_count <= 0:
            return None
        try:
            count = int(self.clean_count_var.get())
        except (tk.TclError, ValueError):
            count = self._page_count
        return max(1, min(self._page_count, count))

    def _is_page_outside_clean_limit(self, idx):
        limit = self._clean_page_limit()
        return limit is not None and idx >= limit

    def _sync_clean_count_controls(self):
        if not hasattr(self, "clean_count_var"):
            return
        if getattr(self, "_clean_limit_syncing", False):
            return
        self._clean_limit_syncing = True
        try:
            page_count = max(1, int(self._page_count or 0))
            try:
                current = int(self.clean_count_var.get())
            except (tk.TclError, ValueError):
                current = page_count

            target = page_count if not self.clean_limit_var.get() else current
            target = max(1, min(page_count, target))
            if current != target:
                self.clean_count_var.set(target)

            enabled = self.clean_limit_var.get() and self._page_count > 0
            if hasattr(self, "_clean_count_spin"):
                self._clean_count_spin.config(
                    to=page_count,
                    state="normal" if enabled else "disabled",
                    disabledbackground=BG0,
                    disabledforeground=TXT2)

            if hasattr(self, "_clean_limit_hint_lbl"):
                if self._page_count <= 0:
                    text = "Откройте PDF, чтобы выбрать количество страниц"
                elif enabled:
                    skipped = max(0, self._page_count - target)
                    text = f"Будут очищены первые {target} стр.; остальные без очистки: {skipped}"
                else:
                    text = "Очищаются все страницы"
                self._clean_limit_hint_lbl.config(text=text)
        finally:
            self._clean_limit_syncing = False

    def _on_clean_limit_change(self, *_):
        if self._importing:
            return
        if getattr(self, "_clean_limit_syncing", False):
            return
        self._sync_clean_count_controls()
        if self._doc is not None:
            for idx in range(self._page_count):
                if (
                    self._page_status.get(idx) == "skipped"
                    and not self._skip_pages.get(idx, False)
                    and not self._is_page_outside_clean_limit(idx)
                ):
                    self._page_status[idx] = "waiting"
                self._update_thumb_status(idx)
            self._render_page()
        self._update_status_bar()

    def _clear_page(self):
        if self._importing or self._doc is None:
            return
        idx = self._current_page
        before_masks = list(self._eraser_masks.get(idx, []))
        before_crop = self._crop_boxes.get(idx)
        before_rot = self._rotations.get(idx, 0)
        before_adjust = dict(self._page_adjustments.get(idx, {}))
        before_skip = bool(self._skip_pages.get(idx, False))
        before_protect = _sanitize_norm_boxes(self._protected_boxes.get(idx)) or None
        if idx in self._eraser_masks:
            self._eraser_masks[idx] = []
        if idx in self._crop_boxes:
            del self._crop_boxes[idx]
        self._protected_boxes.pop(idx, None)
        self._skip_pages.pop(idx, None)
        if idx in self._rotations:
            del self._rotations[idx]
        self._page_adjustments.pop(idx, None)
        self._sync_adjustment_controls(idx)
        after = {"masks": [], "crop": None, "rot": 0, "adjust": {}, "skip": False, "protect": None}
        before = {
            "masks": before_masks,
            "crop": before_crop,
            "rot": before_rot,
            "adjust": before_adjust,
            "skip": before_skip,
            "protect": before_protect,
        }
        if before != after:
            self._push_action({"type": "page_state", "page": idx, "before": before, "after": after})
        self._update_thumb_status(idx)
        self._update_page_flags_ui()
        self._recount_edits()
        self._render_page()
        self._update_status_bar()

    # ── ПОВОРОТ ───────────────────────────────────────────────────────────────
    def _rotate_page(self, deg):
        if self._importing or self._doc is None:
            return
        idx = self._current_page
        cur = self._rotations.get(idx, 0)
        new_rot = (cur + deg) % 360
        self._rotations[idx] = new_rot
        self._push_action({"type": "rotate", "page": idx, "before": cur, "after": new_rot})
        self._update_thumb_status(idx)
        self._thumb_rendered.discard(idx)
        self._queue_thumb_render(idx, priority=True)

        # Перерендеривать миниатюру
        if idx < len(self._thumb_frames):
            container = self._thumb_frames[idx][0].winfo_children()
            if container:
                inner_frames = [w for w in container[0].winfo_children()
                                if isinstance(w, (tk.Frame, tk.Label))]
                # Пересоздаём миниатюру
                pass
        self._render_page()
        self._recount_edits()
        self._update_status_bar()

    def _deskew_current_page(self):
        if not self._ensure_document_ready("выравнивания страницы"):
            return
        idx = self._current_page
        try:
            image = self._render_session_page_image(idx, 300)
            fixed, angle = _deskew_pil_image(image, float(self.angle_var.get()))
        except Exception as e:
            messagebox.showerror("Не удалось выровнять страницу", str(e))
            return

        if abs(angle) < 0.25:
            self._sb_status_var.set("Перекос не найден или слишком мал")
            return

        if self._replace_current_page_with_image(fixed, 300):
            self._sb_status_var.set(f"Страница {idx + 1} выровнена на {angle:.2f}°")

    # ── МАСШТАБ ───────────────────────────────────────────────────────────────
    def _set_zoom(self, value):
        if self._importing:
            return
        raw = min(5.0, max(0.15, float(value)))
        steps = getattr(self, "_zoom_steps", [raw])
        self._zoom = min(steps, key=lambda step: abs(step - raw))
        self._zoom_lbl.config(text=f"{round(self._zoom * 100)}%")
        self._render_page()

    def _zoom_step_index(self):
        return min(
            range(len(self._zoom_steps)),
            key=lambda idx: abs(self._zoom_steps[idx] - self._zoom),
        )

    def _zoom_in(self):
        index = self._zoom_step_index()
        if self._zoom >= self._zoom_steps[index] and index < len(self._zoom_steps) - 1:
            index += 1
        self._set_zoom(self._zoom_steps[index])

    def _zoom_out(self):
        index = self._zoom_step_index()
        if self._zoom <= self._zoom_steps[index] and index > 0:
            index -= 1
        self._set_zoom(self._zoom_steps[index])

    def _canvas_wheel_zoom(self, event):
        if self._importing:
            return "break"
        if self._doc is None:
            return "break"
        if self._tool != "pan":
            return None
        if hasattr(event, "num") and event.num in (4, 5):
            direction = 1 if event.num == 4 else -1
        else:
            direction = 1 if event.delta > 0 else -1
        if direction > 0:
            self._zoom_in()
        else:
            self._zoom_out()
        return "break"

    def _push_action(self, action):
        self._undo_stack.append(action)
        self._redo_stack.clear()
        self._recount_edits()
        self._update_history_buttons()

    def _undo(self):
        if self._importing:
            return
        if not self._undo_stack:
            return
        action = self._undo_stack.pop()
        self._apply_history_action(action, undo=True)
        self._redo_stack.append(action)
        self._recount_edits()
        self._update_status_bar()
        self._update_history_buttons()

    def _redo(self):
        if self._importing:
            return
        if not self._redo_stack:
            return
        action = self._redo_stack.pop()
        self._apply_history_action(action, undo=False)
        self._undo_stack.append(action)
        self._recount_edits()
        self._update_status_bar()
        self._update_history_buttons()

    def _apply_history_action(self, action, undo):
        kind = action["type"]
        value = action["before"] if undo else action["after"]

        if kind == "document_state":
            self._restore_document_state(value)
            return

        page = action["page"]

        if kind == "eraser":
            self._eraser_masks[page] = list(value)
        elif kind == "rotate":
            if value:
                self._rotations[page] = value
            else:
                self._rotations.pop(page, None)
            self._update_thumb_status(page)
        elif kind == "crop":
            if value:
                self._crop_boxes[page] = value
            else:
                self._crop_boxes.pop(page, None)
        elif kind == "protect":
            boxes = _sanitize_norm_boxes(value)
            if boxes:
                self._protected_boxes[page] = boxes
            else:
                self._protected_boxes.pop(page, None)
            self._update_thumb_status(page)
            if page == self._current_page:
                self._update_page_flags_ui()
        elif kind == "skip_page":
            if value:
                self._skip_pages[page] = True
            else:
                self._skip_pages.pop(page, None)
            self._update_thumb_status(page)
            if page == self._current_page:
                self._update_page_flags_ui()
        elif kind == "page_state":
            self._eraser_masks[page] = list(value["masks"])
            if value["crop"]:
                self._crop_boxes[page] = value["crop"]
            else:
                self._crop_boxes.pop(page, None)
            protected_boxes = _sanitize_norm_boxes(value.get("protect"))
            if protected_boxes:
                self._protected_boxes[page] = protected_boxes
            else:
                self._protected_boxes.pop(page, None)
            if value.get("skip"):
                self._skip_pages[page] = True
            else:
                self._skip_pages.pop(page, None)
            if value["rot"]:
                self._rotations[page] = value["rot"]
            else:
                self._rotations.pop(page, None)
            if "adjust" in value:
                if value["adjust"]:
                    self._page_adjustments[page] = dict(value["adjust"])
                else:
                    self._page_adjustments.pop(page, None)
                if page == self._current_page:
                    self._sync_adjustment_controls(page)
                self._update_thumb_status(page)
            if page == self._current_page:
                self._update_page_flags_ui()

        self._recount_edits()
        self._render_page()
        self._update_status_bar()

    def _recount_edits(self):
        masks = sum(len(v) for v in self._eraser_masks.values())
        crops = len(self._crop_boxes)
        protected = _protected_box_total(self._protected_boxes)
        skipped = len(self._skip_pages)
        rotations = sum(1 for v in self._rotations.values() if v)
        adjustments = len(self._page_adjustments)
        structure_edits = sum(
            1 for action in self._undo_stack
            if action.get("type") == "document_state")
        self._edit_count = masks + crops + protected + skipped + rotations + adjustments + structure_edits

    def _update_history_buttons(self):
        if hasattr(self, "_btn_undo"):
            self._btn_undo.config(state="normal" if self._undo_stack else "disabled")
            self._btn_redo.config(state="normal" if self._redo_stack else "disabled")

    def _page_adjustment_values(self, idx):
        return _adjustment_values_from_map(self._page_adjustments, idx)

    def _store_current_page_adjustment(self, idx=None):
        if self._doc is None or not hasattr(self, "brightness_var"):
            return
        if idx is None:
            idx = getattr(self, "_adjustment_controls_page", None)
            if idx is None:
                idx = self._current_page
        idx = int(idx)
        if idx < 0 or idx >= self._page_count:
            return
        brightness = int(self.brightness_var.get())
        contrast = int(self.contrast_var.get())
        if brightness == DEFAULT_BRIGHTNESS and contrast == DEFAULT_CONTRAST:
            self._page_adjustments.pop(idx, None)
        else:
            self._page_adjustments[idx] = {
                "brightness": brightness,
                "contrast": contrast,
            }

    def _sync_adjustment_controls(self, idx):
        if not hasattr(self, "brightness_var") or not hasattr(self, "contrast_var"):
            return
        idx = int(idx)
        brightness, contrast = self._page_adjustment_values(idx)
        self._loading_page_adjustments = True
        try:
            if int(self.brightness_var.get()) != brightness:
                self.brightness_var.set(brightness)
            if int(self.contrast_var.get()) != contrast:
                self.contrast_var.set(contrast)
        finally:
            self._loading_page_adjustments = False
        self._adjustment_controls_page = idx

    def _reset_brightness(self):
        self.brightness_var.set(DEFAULT_BRIGHTNESS)
        self._update_status_bar()

    def _reset_contrast(self):
        self.contrast_var.set(DEFAULT_CONTRAST)
        self._update_status_bar()

    def _reset_edge_margin(self):
        self.margin_var.set(DEFAULT_EDGE_MARGIN)
        self._update_status_bar()

    def _toggle_edge_zone(self):
        self._show_edge_zone = not self._show_edge_zone
        self._update_edge_zone_button()
        self._schedule_preview_render()

    def _update_edge_zone_button(self):
        if not hasattr(self, "_edge_zone_btn"):
            return
        if self._show_edge_zone:
            self._edge_zone_btn.config(text="Скрыть зону", fg=AMBER, bg=AMBER_BG)
        else:
            self._edge_zone_btn.config(text="Показать зону", fg=TXT1, bg=BG2)

    def _schedule_preview_render(self):
        if self._doc is None:
            return
        if self._preview_adjust_job is not None:
            try:
                self.after_cancel(self._preview_adjust_job)
            except tk.TclError:
                pass
        self._preview_adjust_job = self.after(70, self._render_page)

    def _on_preview_adjust_change(self, *_):
        if getattr(self, "_loading_page_adjustments", False):
            return
        idx = getattr(self, "_adjustment_controls_page", None)
        if idx is None:
            idx = self._current_page
        self._store_current_page_adjustment(idx)
        self._recount_edits()
        self._update_status_bar()
        if idx == self._current_page:
            self._schedule_preview_render()

    def _on_edge_zone_change(self, *_):
        self._update_edge_zone_button()
        self._update_status_bar()
        self._schedule_preview_render()

    # ── СМЕНА РЕЖИМА ──────────────────────────────────────────────────────────
    def _on_mode_change(self, _=None):
        mode = self._mode_var.get()
        self._mode_hint_lbl.config(text=self._mode_hints.get(mode, ""))
        preset = self._mode_presets.get(mode)
        if preset:
            self.dot_var.set(preset["dot"])
            self.denoise_var.set(preset["h"])
            self.thresh_var.set(preset["thresh"])
            self.angle_var.set(preset["angle"])
        self._update_status_bar()

    def _on_color_toggle(self):
        self._update_status_bar()

    # ── СТРОКА СОСТОЯНИЯ ──────────────────────────────────────────────────────
    def _update_status_bar(self):
        if self._importing:
            self._st_main.config(text="импортирование", fg=BLUE)
            self._badge_label.config(text="импортирование", fg=BLUE)
            return
        if self._processing:
            self._st_main.config(text="⚙  Обработка страниц…", fg=BLUE)
            self._badge_label.config(text="◉ Обработка", fg=BLUE)
        else:
            self._st_main.config(text="● Готов к работе", fg=GREEN)
            self._badge_label.config(text="● Готов к работе", fg=GREEN)

        if self._edit_count > 0:
            self._st_edits.config(text=f"✏ {self._edit_count} правок", fg=AMBER)
        else:
            self._st_edits.config(text="")

        color_count = sum(1 for v in self._color_pages.values() if v)
        if color_count > 0 and self.keep_color_var.get():
            self._st_color.config(text=f"🎨 {color_count} цветных стр.")
            self._color_info_lbl.config(
                text=f"{color_count} цветных стр. будут сохранены без конвертации")
        elif self._color_detecting:
            scanned = max(0, int(self._color_detect_scanned))
            total = max(0, int(self._color_detect_total))
            self._st_color.config(text=f"цвет {scanned}/{total}" if total else "цвет")
            self._color_info_lbl.config(text="Идёт фоновое определение цветных страниц")
        elif self._page_count > AUTO_COLOR_DETECT_PAGE_LIMIT:
            self._st_color.config(text="")
            self._color_info_lbl.config(
                text="Цвет будет определяться постранично при обработке")
        else:
            self._st_color.config(text="")
            self._color_info_lbl.config(text="")

        clean_limit = self._clean_page_limit()
        limit_skipped = 0
        if clean_limit is not None:
            limit_skipped = sum(
                1
                for idx in range(clean_limit, self._page_count)
                if not self._skip_pages.get(idx, False)
            )
        skipped_total = len(self._skip_pages) + limit_skipped
        if hasattr(self, "_st_skip"):
            if skipped_total > 0:
                self._st_skip.config(
                    text=f"{skipped_total} не чистить",
                    bg=AMBER_BG, padx=6)
            else:
                self._st_skip.config(text="", bg="#0a0c12", padx=0)

        if self._page_count > 0:
            mode_short = self._mode_var.get().split()[-1] if self._mode_var else "—"
            flags = []
            if self._skip_pages:
                flags.append(f"{len(self._skip_pages)} не чистить")
            if clean_limit is not None:
                flags.append(f"очистка {clean_limit}/{self._page_count}")
            protected_count = _protected_box_total(self._protected_boxes)
            if protected_count:
                flags.append(f"{protected_count} защ. обл.")
            suffix = ("  ·  " + "  ·  ".join(flags)) if flags else ""
            self._st_right.config(
                text=f"{self._page_count} стр.  ·  300 dpi  ·  {mode_short}{suffix}")
            self._nav_pages_lbl.config(
                text=f"Страница {self._current_page + 1} / {self._page_count}")

    # ── ЗАПУСК ОБРАБОТКИ ──────────────────────────────────────────────────────
    def _set_processing_buttons(self, processing):
        if not hasattr(self, "_run_btn"):
            return
        if processing:
            self._run_btn.config(
                text="⏳  Обработка…",
                state="disabled", bg=BG3, fg=TXT2)
            if hasattr(self, "_import_btn"):
                self._import_btn.config(state="disabled", fg=TXT2, bg=BG2)
            if hasattr(self, "_import_folder_btn"):
                self._import_folder_btn.config(state="disabled", fg=TXT2, bg=BG2)
            if hasattr(self, "_cancel_btn"):
                self._cancel_btn.config(
                    state="normal", fg=RED, bg=RED_BG,
                    highlightbackground=RED_BDR)
            if hasattr(self, "_export_btn"):
                self._export_btn.config(state="disabled", fg=TXT2, bg=BG2)
        else:
            self._run_btn.config(
                text="▶  Запустить очистку",
                state="normal", bg=BLUE, fg="white",
                highlightbackground=BLUE_BDR)
            if hasattr(self, "_cancel_btn"):
                self._cancel_btn.config(
                    state="disabled", fg=TXT2, bg=BG2,
                    highlightbackground=BDR)
            if hasattr(self, "_export_btn"):
                self._export_btn.config(state="normal", fg=GREEN, bg=GREEN_BG)
            if hasattr(self, "_import_btn") and not self._importing:
                self._import_btn.config(state="normal", fg=BLUE, bg=BLUE_BG,
                                        highlightbackground=BLUE_BDR)
            if hasattr(self, "_import_folder_btn") and not self._importing:
                self._import_folder_btn.config(state="normal", fg=CYAN, bg=CYAN_BG,
                                               highlightbackground=CYAN_BDR)

    def _cancel_processing(self):
        if not self._processing:
            return
        self._cancel_requested.set()
        if hasattr(self, "_cancel_btn"):
            self._cancel_btn.config(state="disabled", fg=TXT2, bg=BG2)
        self._sb_status_var.set("Отмена обработки…")

    def _start_processing(self):
        if self._processing:
            return
        if self._doc is None:
            messagebox.showwarning("Ошибка", "Сначала откройте PDF через «Импорт PDF».")
            return

        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

        self._page_status = {i: "waiting" for i in range(self._page_count)}
        for idx in range(self._page_count):
            self._update_thumb_status(idx)

        self._processing = True
        self._cancel_requested = threading.Event()
        self._set_processing_buttons(True)
        self._prog_var.set(0)
        self._prog_pct_var.set("0%")
        self._sb_status_var.set("Запуск обработки…")
        self._sync_clean_count_controls()
        self._update_status_bar()
        self._store_current_page_adjustment()
        self._processing_before_state = self._snapshot_document_state()
        if not self._processing_before_state:
            self._processing = False
            self._set_processing_buttons(False)
            messagebox.showerror("Ошибка", "Не удалось подготовить PDF к обработке.")
            return

        params = {
            "pdf_bytes":    self._processing_before_state["pdf"],
            "dot_limit":    self.dot_var.get(),
            "h_val":        self.denoise_var.get(),
            "edge_clean":   self.edge_clean_var.get(),
            "edge_margin":  self.margin_var.get(),
            "edge_thresh":  self.thresh_var.get(),
            "deskew":       self.deskew_var.get(),
            "max_angle":    self.angle_var.get(),
            "skip_first":   self.skip_first_var.get(),
            "skip_last":    self.skip_last_var.get(),
            "keep_color":   self.keep_color_var.get(),
            "rotations":    dict(self._rotations),
            "eraser_masks": {k: list(v) for k, v in self._eraser_masks.items()},
            "crop_boxes":   dict(self._crop_boxes),
            "protected_boxes": {
                k: _sanitize_norm_boxes(v)
                for k, v in self._protected_boxes.items()
                if _protected_box_count(v)
            },
            "skip_pages":   dict(self._skip_pages),
            "color_pages":  dict(self._color_pages),
            "clean_page_limit": self._clean_page_limit(),
            "cancel_event": self._cancel_requested,
            "page_adjustments": {
                k: dict(v) for k, v in self._page_adjustments.items()
            },
        }
        threading.Thread(target=self._run_processing, args=(params,),
                         daemon=True).start()

    # ── ПОТОК ОБРАБОТКИ ───────────────────────────────────────────────────────
    def _run_processing(self, p):
        doc = None
        close_doc = False
        try:
            import fitz
            import cv2
            import numpy as np
            from PIL import Image, ImageEnhance

            if p.get("pdf_bytes") is not None:
                doc = fitz.open(stream=p["pdf_bytes"], filetype="pdf")
                close_doc = True
            else:
                doc = p["doc"]
            total = len(doc)
            clean_page_limit = p.get("clean_page_limit")
            cancel_event = p.get("cancel_event")
            out_images = []
            page_statuses = {}
            render_dpi = 300
            render_zoom = render_dpi / 72.0
            clean_settings = CleanSettings(
                mode="manual",
                dpi=render_dpi,
                denoise=int(p["h_val"]),
                dot_area=int(p["dot_limit"]),
                clean_edges=False,
                edge_margin=int(p["edge_margin"]),
                edge_threshold=int(p["edge_thresh"]),
                deskew=bool(p["deskew"]),
                max_angle=float(p["max_angle"]),
                brightness=DEFAULT_BRIGHTNESS,
                contrast=DEFAULT_CONTRAST,
            )

            if clean_page_limit is None:
                self._queue.put(("status", f"Открыт: {total} стр.", TXT1))
            else:
                self._queue.put(("status", f"Открыт: {total} стр.; очистка первых {clean_page_limit}", TXT1))

            for page_num in range(total):
                if cancel_event is not None and cancel_event.is_set():
                    self._queue.put(("cancelled",))
                    return
                self._queue.put(("page_status", page_num, "working"))
                is_first = (page_num == 0)
                is_last  = (page_num == total - 1)
                manual_skip = bool(p["skip_pages"].get(page_num, False))
                limit_skip = clean_page_limit is not None and page_num >= clean_page_limit
                skip     = (manual_skip or
                            limit_skip or
                            (is_first and p["skip_first"]) or
                            (is_last  and p["skip_last"]))
                rot = p["rotations"].get(page_num, 0)
                brightness, contrast = _adjustment_values_from_map(
                    p["page_adjustments"],
                    page_num,
                )

                # Рендерим страницу в 300 dpi для точной очистки и сохранения размера.
                page = doc.load_page(page_num)
                mat  = fitz.Matrix(render_zoom, render_zoom).prerotate(rot)
                pix  = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
                img  = np.frombuffer(pix.samples, dtype=np.uint8
                                     ).reshape(pix.h, pix.w, 3)
                original_rgb = Image.fromarray(img)
                protected_box = p["protected_boxes"].get(page_num)
                if page_num in p["color_pages"]:
                    is_detected_color = bool(p["color_pages"].get(page_num, False))
                elif p["keep_color"]:
                    is_detected_color = _rgb_array_has_color(img)
                    p["color_pages"][page_num] = is_detected_color
                else:
                    is_detected_color = False
                is_color = bool(p["keep_color"] and is_detected_color)
                edge_cleanup = _edge_cleanup_allowed(
                    page_num,
                    total,
                    bool(p["edge_clean"]),
                    p["color_pages"],
                )
                page_clean_settings = replace(
                    clean_settings,
                    clean_edges=edge_cleanup,
                    brightness=brightness,
                    contrast=contrast,
                )

                pct = (page_num + 1) / total * 100
                lbl = f"Стр. {page_num + 1}/{total}"

                if skip:
                    pil_img = original_rgb.copy()
                    if not is_color and not manual_skip and not limit_skip:
                        pil_img = pil_img.convert("L")
                    _apply_eraser(pil_img, p["eraser_masks"].get(page_num, []))
                    source = original_rgb if pil_img.mode == "RGB" else original_rgb.convert("L")
                    _restore_protected_region(pil_img, source, protected_box)
                    pil_img = _apply_crop(pil_img, p["crop_boxes"].get(page_num))
                    out_images.append(pil_img)
                    page_statuses[page_num] = "skipped" if (manual_skip or limit_skip) else "done"
                    self._queue.put(("page_status", page_num, page_statuses[page_num]))
                    if manual_skip:
                        suffix = " — не чистить"
                    elif limit_skip:
                        suffix = " — вне лимита очистки"
                    else:
                        suffix = " — пропущена"
                    self._queue.put(("progress", pct, lbl + suffix))
                    continue

                # ── Цветная страница — мягкая обработка ──────────────────────
                if is_color:
                    from PIL import ImageFilter
                    pil_img = original_rgb.copy()
                    pil_img = _adjust_pil_image(pil_img, brightness, contrast)
                    pil_img = pil_img.filter(ImageFilter.MedianFilter(3))
                    pil_img = ImageEnhance.Contrast(pil_img).enhance(1.2)
                    if edge_cleanup:
                        pil_img = _apply_edge_cleanup_pil(
                            pil_img,
                            p["edge_margin"],
                            p["edge_thresh"])
                    _apply_eraser(pil_img, p["eraser_masks"].get(page_num, []))
                    _restore_protected_region(pil_img, original_rgb, protected_box)
                    pil_img = _apply_crop(pil_img, p["crop_boxes"].get(page_num))
                    out_images.append(pil_img)
                    page_statuses[page_num] = "done"
                    self._queue.put(("page_status", page_num, "done"))
                    self._queue.put(("progress", pct, lbl + " — цвет сохранён"))
                    continue

                # ── Полная ч/б обработка ──────────────────────────────────────
                gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
                binary = clean_page_image(
                    gray,
                    page_clean_settings,
                )

                pil_img = Image.fromarray(binary).convert("L")
                _apply_eraser(pil_img, p["eraser_masks"].get(page_num, []))
                _restore_protected_region(pil_img, original_rgb.convert("L"), protected_box)
                pil_img = _apply_crop(pil_img, p["crop_boxes"].get(page_num))
                out_images.append(pil_img)

                page_statuses[page_num] = "done"
                self._queue.put(("page_status", page_num, "done"))
                self._queue.put(("progress", pct, lbl))

            # Сохранение
            if cancel_event is not None and cancel_event.is_set():
                self._queue.put(("cancelled",))
                return
            self._queue.put(("status", "Применение к текущей сессии…", TXT1))
            if out_images:
                # Конвертируем в RGB чтобы смешанные цветные/ч-б сохранились
                save_images = []
                for im in out_images:
                    if im.mode != "RGB":
                        im = im.convert("RGB")
                    save_images.append(im)
                pdf_bytes = _pdf_images_to_bytes(save_images, render_dpi)
            else:
                pdf_bytes = b""

            self._queue.put(("session_done", pdf_bytes, page_statuses))

        except ImportError as e:
            self._queue.put(("error",
                f"Не установлена зависимость: {e}\n\n"
                "Запустите:\n  pip install pymupdf opencv-python pillow numpy"))
        except Exception:
            import traceback
            self._queue.put(("error", traceback.format_exc()))
        finally:
            if close_doc and doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

    # ── ОПРОС ОЧЕРЕДИ ─────────────────────────────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]

                if kind == "import_progress":
                    _, generation, pct, text = msg
                    if generation == self._import_generation and self._importing:
                        self._prog_var.set(pct)
                        self._prog_pct_var.set(f"{int(round(pct))}%")
                        self._sb_status_var.set(text)
                        self._badge_label.config(text="импортирование", fg=BLUE)
                        self._st_main.config(text="импортирование", fg=BLUE)

                elif kind == "import_opened":
                    _, generation, path, doc, page_count, *rest = msg
                    meta = rest[0] if rest else None
                    self._apply_imported_doc(generation, path, doc, page_count, meta)

                elif kind == "import_error":
                    _, generation, error_text = msg
                    self._finish_import_error(generation, error_text)

                elif kind == "page_render_done":
                    _, generation, idx, result = msg
                    self._finish_page_render_worker()
                    if (
                        generation == self._page_render_generation
                        and idx == self._current_page
                        and self._can_render_page_async()
                    ):
                        self._apply_page_render_result(idx, result)

                elif kind == "page_render_error":
                    _, generation, idx, error_text = msg
                    self._finish_page_render_worker()
                    if generation == self._page_render_generation and idx == self._current_page:
                        self._canvas.delete("all")
                        self._canvas.create_text(
                            24, 24,
                            text=f"РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРєР°Р·Р°С‚СЊ СЃС‚СЂР°РЅРёС†Сѓ\n{error_text}",
                            anchor="nw", fill=RED, font=("Segoe UI", 10))

                elif kind == "progress":
                    _, pct, text = msg
                    self._prog_var.set(pct)
                    pct_text = f"{int(round(pct))}%"
                    self._prog_pct_var.set(pct_text)
                    self._sb_status_var.set(f"{text} · {pct_text}")

                elif kind == "page_status":
                    _, idx, status = msg
                    self._page_status[idx] = status
                    self._update_thumb_status(idx)

                elif kind == "status":
                    _, text, color = msg
                    self._sb_status_var.set(text)

                elif kind in ("color_detect_partial", "color_detect_done"):
                    _, generation, color_pages = msg
                    if generation == self._color_detect_generation:
                        if kind == "color_detect_done":
                            self._color_detecting = False
                            self._color_detect_scanned = self._color_detect_total
                        changed_current = False
                        for idx, value in color_pages.items():
                            idx = int(idx)
                            if 0 <= idx < self._page_count:
                                prev = self._color_pages.get(idx)
                                self._color_pages[idx] = bool(value)
                                changed_current = changed_current or (
                                    idx == self._current_page and prev != bool(value)
                                )
                        if kind == "color_detect_done" or changed_current:
                            self._render_page()
                        self._update_status_bar()

                elif kind == "color_detect_progress":
                    _, generation, scanned, total = msg
                    if generation == self._color_detect_generation:
                        self._color_detect_scanned = int(scanned)
                        self._color_detect_total = int(total)
                        self._update_status_bar()

                elif kind == "session_done":
                    _, pdf_bytes, page_statuses = msg
                    self._prog_var.set(100)
                    self._prog_pct_var.set("100%")
                    before_state = self._processing_before_state
                    self._processing_before_state = None
                    ok = bool(pdf_bytes) and self._apply_processed_session(
                        pdf_bytes,
                        page_statuses,
                        before_state,
                    )
                    self._processing = False
                    self._set_processing_buttons(False)
                    if ok:
                        self._sb_status_var.set("Очистка применена к текущей сессии")
                        self._badge_label.config(text="✓ Готово", fg=GREEN)
                        self._st_main.config(text="✓ Очистка применена", fg=GREEN)
                        messagebox.showinfo(
                            "Готово!",
                            "Очистка применена к текущей сессии.\n\nДля сохранения нажмите «Экспорт PDF».")

                elif kind == "done":
                    _, out_path = msg
                    self._prog_var.set(100)
                    self._prog_pct_var.set("100%")
                    self._sb_status_var.set(f"✅  Готово → {os.path.basename(out_path)}")
                    self._processing = False
                    self._set_processing_buttons(False)
                    self._badge_label.config(text="✓ Готово", fg=GREEN)
                    self._st_main.config(text="✓ Очистка завершена", fg=GREEN)
                    messagebox.showinfo("Готово!",
                        f"PDF успешно очищен!\n\n{out_path}")

                elif kind == "cancelled":
                    self._processing = False
                    self._processing_before_state = None
                    self._set_processing_buttons(False)
                    for idx, status in list(self._page_status.items()):
                        if status == "working":
                            self._page_status[idx] = "waiting"
                            self._update_thumb_status(idx)
                    self._sb_status_var.set("Обработка отменена")
                    self._badge_label.config(text="Отменено", fg=AMBER)
                    self._st_main.config(text="Отменено", fg=AMBER)

                elif kind == "error":
                    _, err = msg
                    self._processing = False
                    self._processing_before_state = None
                    self._set_processing_buttons(False)
                    self._sb_status_var.set("❌  Ошибка обработки")
                    self._badge_label.config(text="✗ Ошибка", fg=RED)
                    messagebox.showerror("Ошибка обработки", err)

        except queue.Empty:
            pass
        self.after(80, self._poll_queue)


# ──────────────────────────────────────────────────────────────────────────────
#  Алгоритмы обработки
# ──────────────────────────────────────────────────────────────────────────────

def _sanitize_norm_box(box):
    if not box:
        return None
    try:
        x0, y0, x1, y1 = [float(v) for v in box]
    except (TypeError, ValueError):
        return None
    x0, x1 = sorted((max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))))
    y0, y1 = sorted((max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))))
    if (x1 - x0) < 0.001 or (y1 - y0) < 0.001:
        return None
    return (x0, y0, x1, y1)


def _looks_like_single_norm_box(value):
    if isinstance(value, (str, bytes, dict)):
        return False
    try:
        if len(value) != 4:
            return False
    except TypeError:
        return False
    return not any(isinstance(item, (list, tuple, dict)) for item in value)


def _sanitize_norm_boxes(boxes, limit=MAX_PROTECTED_BOXES_PER_PAGE):
    if not boxes:
        return []
    raw_boxes = [boxes] if _looks_like_single_norm_box(boxes) else boxes
    cleaned = []
    for raw_box in raw_boxes:
        box = _sanitize_norm_box(raw_box)
        if not box:
            continue
        cleaned.append(box)
        if len(cleaned) >= limit:
            break
    return cleaned


def _protected_box_count(boxes):
    return len(_sanitize_norm_boxes(boxes))


def _protected_box_total(mapping):
    return sum(_protected_box_count(value) for value in mapping.values())


def _restore_protected_region(pil_img, original_img, protected_box):
    boxes = _sanitize_norm_boxes(protected_box)
    if not boxes:
        return pil_img

    w, h = pil_img.size
    if w <= 0 or h <= 0:
        return pil_img

    source = original_img
    if source.size != pil_img.size:
        source = source.resize(pil_img.size)
    if source.mode != pil_img.mode:
        source = source.convert(pil_img.mode)

    for box in boxes:
        x0, y0, x1, y1 = box
        left = max(0, min(w - 1, int(x0 * w)))
        top = max(0, min(h - 1, int(y0 * h)))
        right = max(left + 1, min(w, int(x1 * w)))
        bottom = max(top + 1, min(h, int(y1 * h)))
        pil_img.paste(source.crop((left, top, right, bottom)), (left, top))
    return pil_img


def _apply_eraser(pil_img, masks):
    """Рисует белые круги на PIL Image по списку масок [(xf, yf, rf)]."""
    if not masks:
        return
    from PIL import ImageDraw
    draw = ImageDraw.Draw(pil_img)
    w, h = pil_img.size
    for (xf, yf, rf) in masks:
        cx = int(xf * w)
        cy = int(yf * h)
        r  = int(rf * min(w, h))
        fill = (255, 255, 255, 255) if pil_img.mode == "RGBA" else ((255, 255, 255) if pil_img.mode == "RGB" else 255)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)


def _apply_edge_cleanup_pil(pil_img, margin, dark_threshold):
    import numpy as np
    from PIL import Image

    try:
        margin = int(margin)
        dark_threshold = int(dark_threshold)
    except (TypeError, ValueError):
        return pil_img
    if margin <= 0:
        return pil_img

    gray = np.array(pil_img.convert("L"))
    dark_limit = max(90, min(240, dark_threshold))
    remove = edge_artifact_mask(gray < dark_limit, margin, dark_threshold)
    if not np.any(remove):
        return pil_img

    if pil_img.mode == "L":
        arr = np.array(pil_img)
        arr[remove] = 255
        return Image.fromarray(arr, "L")

    rgba = pil_img.convert("RGBA")
    arr = np.array(rgba)
    arr[remove, 0:3] = 255
    return Image.fromarray(arr, "RGBA").convert(pil_img.mode if pil_img.mode in ("RGB", "RGBA") else "RGB")


def _apply_crop(pil_img, crop_box):
    if not crop_box:
        return pil_img
    w, h = pil_img.size
    x0, y0, x1, y1 = crop_box
    left = max(0, min(w - 1, int(x0 * w)))
    top = max(0, min(h - 1, int(y0 * h)))
    right = max(left + 1, min(w, int(x1 * w)))
    bottom = max(top + 1, min(h, int(y1 * h)))
    return pil_img.crop((left, top, right, bottom))


def _adjust_pil_image(pil_img, brightness, contrast):
    from PIL import ImageEnhance
    if int(brightness) != 0:
        factor = max(0.1, 1.0 + int(brightness) / 100.0)
        pil_img = ImageEnhance.Brightness(pil_img).enhance(factor)
    if int(contrast) != 100:
        pil_img = ImageEnhance.Contrast(pil_img).enhance(max(0.1, int(contrast) / 100.0))
    return pil_img


def _adjustment_values_from_map(adjustments, idx):
    adjustment = adjustments.get(idx) or {}
    return (
        int(adjustment.get("brightness", DEFAULT_BRIGHTNESS)),
        int(adjustment.get("contrast", DEFAULT_CONTRAST)),
    )


def _compression_options_from_labels(enabled, level_label, scope_label):
    level_key = "medium"
    for key, item in COMPRESSION_LEVELS.items():
        if item["label"] == level_label:
            level_key = key
            break

    scope_key = "all"
    for key, label in COMPRESSION_SCOPES.items():
        if label == scope_label:
            scope_key = key
            break

    level = COMPRESSION_LEVELS[level_key]
    return {
        "enabled": bool(enabled),
        "level": level_key,
        "scope": scope_key,
        "dpi": int(level["dpi"]),
        "quality": int(level["quality"]),
    }


def _compression_applies(compression, is_color=False, is_processed=False):
    if not compression or not compression.get("enabled"):
        return False
    scope = compression.get("scope", "all")
    if scope == "color":
        return bool(is_color)
    if scope == "processed":
        return bool(is_processed)
    return True


def _prepare_pdf_image(image, dpi, compression=None, compress_page=False):
    from PIL import Image

    page_dpi = max(1.0, float(dpi))
    quality = 95
    img = image.convert("RGB")

    if compress_page and compression and compression.get("enabled"):
        target_dpi = max(72.0, min(page_dpi, float(compression.get("dpi", page_dpi))))
        if target_dpi < page_dpi - 0.5:
            scale = target_dpi / page_dpi
            new_size = (
                max(1, int(round(img.width * scale))),
                max(1, int(round(img.height * scale))),
            )
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            page_dpi = target_dpi
        quality = max(35, min(95, int(compression.get("quality", quality))))

    page_w = max(1.0, img.width / page_dpi * 72.0)
    page_h = max(1.0, img.height / page_dpi * 72.0)
    return img, page_w, page_h, quality


def _image_to_pdf_stream(image, quality, compressed=False):
    import io

    buffer = io.BytesIO()
    kwargs = {"format": "JPEG", "quality": int(quality)}
    if compressed:
        kwargs["optimize"] = True
        kwargs["progressive"] = True
    image.save(buffer, **kwargs)
    return buffer.getvalue()


def _write_pdf_images(images, output_path, dpi, compression=None, page_compression_flags=None):
    import fitz

    if not images:
        raise ValueError("No pages to save")

    target = fitz.open()
    try:
        for idx, image in enumerate(images):
            compress_page = bool(
                page_compression_flags[idx]
                if page_compression_flags is not None and idx < len(page_compression_flags)
                else False
            )
            prepared, page_w, page_h, quality = _prepare_pdf_image(
                image,
                dpi,
                compression,
                compress_page,
            )
            stream = _image_to_pdf_stream(prepared, quality, compressed=compress_page)
            page = target.new_page(width=page_w, height=page_h)
            page.insert_image(page.rect, stream=stream)

        target.save(str(output_path), garbage=4, deflate=True, clean=True)
    finally:
        target.close()


def _save_pdf_images(
    images,
    output_path,
    dpi,
    split_pages=False,
    compression=None,
    page_compression_flags=None,
):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if split_pages:
        folder = output_path.with_suffix("")
        folder = folder.with_name(folder.name + "_pages")
        folder.mkdir(parents=True, exist_ok=True)
        for index, image in enumerate(images, start=1):
            page_path = folder / f"{output_path.stem}_{index:03d}.pdf"
            temp_path = page_path.with_name(f"{page_path.stem}.tmp-{uuid4().hex}.pdf")
            try:
                flag = bool(
                    page_compression_flags[index - 1]
                    if page_compression_flags is not None and index - 1 < len(page_compression_flags)
                    else False
                )
                _write_pdf_images(
                    [image],
                    temp_path,
                    dpi,
                    compression,
                    [flag],
                )
                temp_path.replace(page_path)
            finally:
                if temp_path.exists():
                    temp_path.unlink()
        return folder

    temp_output = output_path.with_name(f"{output_path.stem}.tmp-{uuid4().hex}.pdf")
    try:
        _write_pdf_images(
            images,
            temp_output,
            dpi,
            compression,
            page_compression_flags,
        )
        temp_output.replace(output_path)
    finally:
        if temp_output.exists():
            temp_output.unlink()
    return output_path


def _pdf_images_to_bytes(images, dpi):
    import io

    if not images:
        return b""

    save_images = []
    for image in images:
        if image.mode != "RGB":
            image = image.convert("RGB")
        save_images.append(image)

    buffer = io.BytesIO()
    save_images[0].save(
        buffer,
        format="PDF",
        save_all=True,
        append_images=save_images[1:],
        resolution=float(dpi),
        quality=95)
    return buffer.getvalue()


def _rgb_array_has_color(arr, max_pixels=250000):
    import numpy as np

    arr = np.asarray(arr)
    if arr.size == 0:
        return False
    if arr.ndim < 3 or arr.shape[2] < 3:
        return False
    pixels = int(arr.shape[0]) * int(arr.shape[1])
    if pixels > max_pixels:
        step = max(1, int(np.ceil(np.sqrt(pixels / float(max_pixels)))))
        arr = arr[::step, ::step, :]
    arr = arr[:, :, :3].astype(np.int16, copy=False)
    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]
    chroma = (np.abs(r - g) + np.abs(g - b) + np.abs(r - b)) // 3
    return bool(np.mean(chroma > 15) > 0.005)


def _pil_image_has_color(image):
    import numpy as np

    return _rgb_array_has_color(np.asarray(image.convert("RGB")))


def _deskew_pil_image(pil_img, max_angle_deg):
    import cv2
    import numpy as np
    from PIL import Image

    source = pil_img.convert("RGB")
    gray = np.asarray(source.convert("L"))
    h, w = gray.shape[:2]
    if h <= 0 or w <= 0:
        return source, 0.0

    angle = estimate_deskew_angle(gray, max_angle_deg, min_abs_angle=0.65)
    if abs(angle) < 0.001:
        return source, 0.0

    arr = np.asarray(source)
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(
        arr,
        matrix,
        (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    return Image.fromarray(rotated, "RGB"), float(angle)


def _deskew(gray, max_angle_deg):
    """
    Определяет угол наклона через преобразование Хафа и поворачивает изображение.
    Возвращает выровненный grayscale.
    """
    import cv2

    angle = estimate_deskew_angle(gray, max_angle_deg)
    if abs(angle) < 0.001:
        return gray

    h, w = gray.shape
    M = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
    return cv2.warpAffine(gray, M, (w, h),
                          flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def _clean_edges(binary, margin, dark_thresh):
    """
    Убирает тёмные горизонтальные и вертикальные полосы у краёв страницы.
    """
    import numpy as np

    h, w = binary.shape
    result = binary.copy()
    margin = min(margin, h // 6, w // 6)

    for row in range(margin):
        if np.mean(binary[row, :]) < (255 - dark_thresh):
            result[row, :] = 255
    for row in range(h - margin, h):
        if np.mean(binary[row, :]) < (255 - dark_thresh):
            result[row, :] = 255
    for col in range(margin):
        if np.mean(binary[:, col]) < (255 - dark_thresh):
            result[:, col] = 255
    for col in range(w - margin, w):
        if np.mean(binary[:, col]) < (255 - dark_thresh):
            result[:, col] = 255

    return result


def _natural_sort_key(value):
    text = str(value).casefold()
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def _is_excluded_scan_path(path, root):
    try:
        relative_parts = Path(path).relative_to(root).parts
    except Exception:
        relative_parts = Path(path).parts
    return any(part in PROJECT_SCAN_EXCLUDE_DIRS or part.startswith(".") for part in relative_parts[:-1])


def _list_image_files(folder, recursive=True):
    root = Path(folder)
    if not root.exists() or not root.is_dir():
        return []

    try:
        iterator = root.rglob("*") if recursive else root.iterdir()
        images = [
            path
            for path in iterator
            if path.is_file()
            and path.suffix.lower() in IMAGE_EXTENSIONS
            and not _is_excluded_scan_path(path, root)
        ]
    except Exception:
        return []

    return sorted(images, key=lambda path: _natural_sort_key(path.relative_to(root)))


# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = PdfCleanerApp()
    app.mainloop()
