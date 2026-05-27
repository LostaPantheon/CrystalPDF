"""
CrystalPDF v7.0 — настольное приложение
============================================
Полный рефакторинг под новый интерфейс:
  • Двухпанельная компоновка (сайдбар + предпросмотр)
  • Просмотр страниц с масштабированием
  • Инструмент «Ластик» с рисованием прямо на странице
  • Поворот страниц (±90°) с сохранением в PDF
  • Полоса миниатюр с цветными индикаторами статуса
  • Сохранение цветных страниц без бинаризации
  • Статусы: серый=ожидание, синий=обработка, зелёный=готово, красный=ошибка
  • Все алгоритмы v6.0: выравнивание наклона, NL-Means, очистка краёв, удаление точек
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import os
import queue
import math
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
import base64
import json
import subprocess
import sys

from pdf_cleaner import CleanSettings, clean_page_image, edge_artifact_mask


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
MAX_PROTECTED_BOXES_PER_PAGE = 15
DESKTOP_SHORTCUT_NAME = "CrystalPDF"
LEGACY_DESKTOP_SHORTCUT_NAMES = ("Mini_Icon_CrystalPDF",)
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
        self.title("CrystalPDF")

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
        self._cancel_requested = threading.Event()
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
        self._sidebar_width = self._ui.px(292)
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
        self.after(600, self._maybe_prompt_desktop_shortcut)

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
        dialog.title("Ярлык CrystalPDF")
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
            text=f"Будет создан ярлык «{DESKTOP_SHORTCUT_NAME}» для быстрого запуска CrystalPDF.",
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
                "$Shortcut.Description = 'CrystalPDF'",
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
            tb, text="✦  CrystalPDF  ·  нет файла",
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
        min_w = self._ui.px(230)
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
            for widget in (self._mode_hint_lbl, self._color_info_lbl):
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
        tk.Label(logo, text="✦  CrystalPDF",
                 font=("Segoe UI", 13, "bold"), fg=TXT0, bg=BG1,
                 anchor="w").pack(fill="x", padx=14, pady=(14, 2))
        tk.Label(logo, text="v7.0 — очистка сканов",
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
        self._section(inner, "Файл")
        file_sec = tk.Frame(inner, bg=BG1)
        file_sec.pack(fill="x", padx=12, pady=(6, 12))

        btn_imp = styled_btn(file_sec, "⬆  Импорт PDF",
                             self._browse_input, fg=BLUE, bg=BLUE_BG,
                             padx=12, pady=8, font_size=10)
        btn_imp.pack(fill="x", pady=(0, 5))
        btn_imp.config(highlightbackground=BLUE_BDR)

        # Чип выбранного файла
        self._file_chip = tk.Label(
            file_sec, text="нет файла",
            font=("Courier New", 8), fg=TXT3, bg=BG0,
            anchor="w", padx=8, pady=5,
            relief="flat",
            highlightthickness=1, highlightbackground=BDR)
        self._file_chip.pack(fill="x")
        sep(inner)

        # ── Выходной файл ──────────────────────────────────────────────────────
        self._section(inner, "Выходной файл")
        out_sec = tk.Frame(inner, bg=BG1)
        out_sec.pack(fill="x", padx=12, pady=(6, 12))

        self.output_var = tk.StringVar()
        out_row = tk.Frame(out_sec, bg=BG1)
        out_row.pack(fill="x")
        out_entry = tk.Entry(out_row, textvariable=self.output_var,
                             font=("Segoe UI", 9), fg=TXT0, bg=BG0,
                             insertbackground=TXT0, relief="flat", bd=0,
                             highlightthickness=1, highlightbackground=BDR)
        out_entry.pack(side="left", fill="x", expand=True, ipady=5, ipadx=6)
        styled_btn(out_row, "…", self._browse_output_dlg,
                   fg=TXT1, bg=BG3, pady=5, padx=8, width=3
                   ).pack(side="left", padx=(4, 0))
        sep(inner)

        # ── Режим очистки ─────────────────────────────────────────────────────
        self._section(inner, "Режим очистки")
        mode_sec = tk.Frame(inner, bg=BG1)
        mode_sec.pack(fill="x", padx=12, pady=(6, 12))

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
            wraplength=196, justify="left")
        self._mode_hint_lbl.pack(fill="x", pady=(4, 0))
        sep(inner)

        # ── Параметры ─────────────────────────────────────────────────────────
        self._section(inner, "Параметры")
        par_sec = tk.Frame(inner, bg=BG1)
        par_sec.pack(fill="x", padx=12, pady=(6, 12))

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
        sep(inner)

        # ── Страницы ──────────────────────────────────────────────────────────
        self._section(inner, "Страницы")
        pg_sec = tk.Frame(inner, bg=BG1)
        pg_sec.pack(fill="x", padx=12, pady=(6, 12))

        self.edge_clean_var  = tk.BooleanVar(value=True)
        self.deskew_var      = tk.BooleanVar(value=True)
        self.skip_first_var  = tk.BooleanVar(value=True)
        self.skip_last_var   = tk.BooleanVar(value=True)
        self.keep_color_var  = tk.BooleanVar(value=True)
        self.split_pages_var = tk.BooleanVar(value=False)
        self.clean_limit_var = tk.BooleanVar(value=False)
        self.clean_count_var = tk.IntVar(value=1)
        self.edge_clean_var.trace_add("write", self._on_edge_zone_change)
        self.clean_limit_var.trace_add("write", self._on_clean_limit_change)
        self.clean_count_var.trace_add("write", self._on_clean_limit_change)

        self._chk(pg_sec, "Очистка краёв (линии сканера)", self.edge_clean_var)
        self._chk(pg_sec, "Выравнивание наклона (deskew)",  self.deskew_var)
        self._chk(pg_sec, "Пропустить первую стр.",         self.skip_first_var)
        self._chk(pg_sec, "Пропустить последнюю стр.",      self.skip_last_var)
        self._chk(pg_sec, "Разбить результат на страницы",  self.split_pages_var)

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
            anchor="w", wraplength=190, justify="left")
        self._clean_limit_hint_lbl.pack(fill="x", pady=(3, 0))
        self._sync_clean_count_controls()

        # «Сохранить цветные» с золотым бейджем
        color_row = tk.Frame(pg_sec, bg=BG1)
        color_row.pack(fill="x", pady=(4, 0))
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
            pg_sec, text="",
            font=("Segoe UI", 8), fg=AMBER, bg=AMBER_BG,
            anchor="w", padx=6, pady=4, wraplength=190,
            justify="left",
            relief="flat",
            highlightthickness=1, highlightbackground=AMBER_BDR)
        self._color_info_lbl.pack(fill="x", pady=(5, 0))

        sep(inner)
        self._section(inner, "Текущая страница")
        page_ops = tk.Frame(inner, bg=BG1)
        page_ops.pack(fill="x", padx=12, pady=(6, 12))

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
        hsb = tk.Scrollbar(frame, orient="horizontal", command=sc.xview)
        sc.configure(xscrollcommand=hsb.set)
        # Полоса прокрутки появляется только при необходимости
        self._thumb_canvas = sc
        self._thumb_inner  = tk.Frame(sc, bg=BG1)
        sc_win = sc.create_window((0, 0), window=self._thumb_inner, anchor="nw")

        def _cfg(e): sc.configure(scrollregion=sc.bbox("all"))
        self._thumb_inner.bind("<Configure>", _cfg)
        hsb.pack(side="bottom", fill="x")
        sc.pack(fill="both", expand=True)

        self._thumb_frames = []   # список (frame, dot_label, rot_label, num_label)

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
        path = filedialog.askopenfilename(
            title="Выберите входной PDF",
            filetypes=[("PDF", "*.pdf"), ("Все файлы", "*.*")])
        if not path:
            return
        self.output_var.set(str(self._default_output_path(path)))
        self._load_pdf(path)

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

    def _ensure_document_ready(self, action_text="операции"):
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
        self._doc = new_doc
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
        threading.Thread(target=self._detect_color_pages, daemon=True).start()

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
            image = self._render_current_page_for_download(idx)
            if image.mode != "RGB":
                image = image.convert("RGB")
            saved_path = _save_pdf_images([image], out_path, 300, False)
            self._sb_status_var.set(f"Текущая страница сохранена: {saved_path.name}")
            messagebox.showinfo(
                "Готово",
                f"Текущая страница сохранена:\n\n{saved_path}")
        except Exception as e:
            messagebox.showerror("Не удалось сохранить страницу", str(e))

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
        try:
            import fitz
        except ImportError:
            messagebox.showerror("Ошибка", "Установите pymupdf:\n\npip install pymupdf")
            return

        try:
            doc = fitz.open(path)
        except Exception as e:
            messagebox.showerror("Ошибка открытия", str(e))
            return

        self._doc = doc
        self._input_path = path
        self._page_count = len(doc)
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

        fname = os.path.basename(path)
        self._title_label.config(text=f"✦  CrystalPDF  ·  {fname}")
        self._file_chip.config(text=fname, fg=TXT1)

        # Определяем цветные страницы в фоне
        threading.Thread(target=self._detect_color_pages, daemon=True).start()

        self._build_thumb_widgets()
        self._go_page(0)
        self._update_status_bar()

    def _detect_color_pages(self):
        """Фоново определяет, какие страницы содержат цвет."""
        try:
            import fitz
            import numpy as np

            for i in range(self._page_count):
                if self._doc is None:
                    return
                page = self._doc.load_page(i)
                pix  = page.get_pixmap(matrix=fitz.Matrix(0.3, 0.3))
                import numpy as np
                arr  = np.frombuffer(pix.samples, dtype=np.uint8
                                     ).reshape(pix.h, pix.w, pix.n)
                if pix.n >= 3:
                    r, g, b = arr[:,:,0].astype(int), arr[:,:,1].astype(int), arr[:,:,2].astype(int)
                    chroma = (np.abs(r - g) + np.abs(g - b) + np.abs(r - b)) // 3
                    self._color_pages[i] = bool(np.mean(chroma > 15) > 0.005)
                else:
                    self._color_pages[i] = False

            self._queue.put(("color_detect_done",))
        except Exception:
            pass

    # ── ВИДЖЕТЫ МИНИАТЮР ──────────────────────────────────────────────────────
    def _build_thumb_widgets(self):
        # Чистим старые
        for w in self._thumb_inner.winfo_children():
            w.destroy()
        self._thumb_frames = []

        for i in range(self._page_count):
            f = tk.Frame(self._thumb_inner, bg=BG1, cursor="hand2",
                         highlightthickness=2,
                         highlightbackground=BDR if i != 0 else BLUE)
            f.pack(side="left", padx=(4 if i == 0 else 2, 2), pady=4)
            f.bind("<Button-1>", lambda e, idx=i: self._go_page(idx))

            # Миниатюра-заглушка
            thumb_bg = tk.Frame(f, bg=WHITE_PAGE,
                                width=self._ui.px(44), height=self._ui.px(58))
            thumb_bg.pack(padx=2, pady=(2, 0))
            thumb_bg.pack_propagate(False)
            for h in [8, 5, 5, 5, 5, 5, 5]:
                tk.Frame(thumb_bg, bg="#dddddd", height=self._ui.px(h)
                         ).pack(fill="x", padx=self._ui.px(5), pady=1)

            # Статус-точка
            dot = tk.Frame(f, bg=TXT3,
                           width=self._ui.px(8), height=self._ui.px(8))
            dot.place(in_=thumb_bg, relx=1.0, rely=0, anchor="ne",
                      x=-2, y=2)

            # Номер
            num = tk.Label(f, text=str(i + 1),
                           font=("Courier New", 8), fg=TXT3, bg=BG1)
            num.pack()

            # Метка поворота
            rot_lbl = tk.Label(f, text="",
                               font=("Courier New", 7, "bold"),
                               fg=AMBER, bg=BG1)
            rot_lbl.pack()

            # Рендерим миниатюру реального содержимого
            self._thumb_frames.append((f, dot, rot_lbl))
            self._render_thumb(i, thumb_bg)
            self._update_thumb_status(i)

        # Прокрутка к первой
        self._thumb_canvas.after(50, lambda: self._thumb_canvas.xview_moveto(0))

    def _render_thumb(self, idx, container):
        """Рендерит реальную миниатюру страницы в контейнер."""
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
            lbl.bind("<Button-1>",
                     lambda e, i=idx: self._go_page(i))
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
                fill="#000000", outline="")
            self._canvas.create_image(ox, oy, anchor="nw", image=photo)

            # Поворот-бейдж
            if rot:
                self._canvas.create_text(
                    ox + pw - 6, oy + 6,
                    text=f"{rot}°", anchor="ne",
                    fill=BLUE, font=("Courier New", 9, "bold"))

            # Цветная страница — полоска внизу
            if self._color_pages.get(idx, False):
                self._canvas.create_rectangle(
                    ox, oy + ph - 6, ox + pw, oy + ph,
                    fill=AMBER, outline="")

            self._draw_edge_zone_overlay(page_w, page_h)
            self._draw_protected_box_overlay()
            self._draw_skip_page_overlay()

            if has_crop:
                self._canvas.create_rectangle(
                    ox, oy, ox + pw, oy + ph,
                    outline=GREEN, width=2)

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
                tags="protect_overlay")
            label = "Защита" if len(boxes) == 1 else f"Защита {number}"
            self._canvas.create_text(
                cx0 + 8, cy0 + 8,
                text=label, anchor="nw",
                fill=CYAN, font=("Segoe UI", 9, "bold"),
                tags="protect_overlay")

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
            tags="skip_page_overlay")
        self._canvas.create_text(
            ox + 8, oy + h // 2,
            text=text,
            anchor="w", fill=AMBER,
            font=("Segoe UI", 9, "bold"),
            tags="skip_page_overlay")

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
                outline=AMBER, width=1, tags="edge_zone")

        draw_rect(0.0, 0.0, 1.0, my)
        draw_rect(0.0, 1.0 - my, 1.0, 1.0)
        draw_rect(0.0, 0.0, mx, 1.0)
        draw_rect(1.0 - mx, 0.0, 1.0, 1.0)

    # ── УПРАВЛЕНИЕ ИНСТРУМЕНТАМИ ──────────────────────────────────────────────
    def _set_tool(self, tool):
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
            self._render_page()
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
        if self._doc is None:
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
        if self._doc is None:
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
        if self._doc is None:
            return
        idx = self._current_page
        cur = self._rotations.get(idx, 0)
        new_rot = (cur + deg) % 360
        self._rotations[idx] = new_rot
        self._push_action({"type": "rotate", "page": idx, "before": cur, "after": new_rot})
        self._update_thumb_status(idx)

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

    # ── МАСШТАБ ───────────────────────────────────────────────────────────────
    def _set_zoom(self, value):
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
        if not self._undo_stack:
            return
        action = self._undo_stack.pop()
        self._apply_history_action(action, undo=True)
        self._redo_stack.append(action)
        self._recount_edits()
        self._update_status_bar()
        self._update_history_buttons()

    def _redo(self):
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
            if hasattr(self, "_cancel_btn"):
                self._cancel_btn.config(
                    state="normal", fg=RED, bg=RED_BG,
                    highlightbackground=RED_BDR)
        else:
            self._run_btn.config(
                text="▶  Запустить очистку",
                state="normal", bg=BLUE, fg="white",
                highlightbackground=BLUE_BDR)
            if hasattr(self, "_cancel_btn"):
                self._cancel_btn.config(
                    state="disabled", fg=TXT2, bg=BG2,
                    highlightbackground=BDR)

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
        out = self.output_var.get().strip()
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

        out_path = self._unique_output_path(out_path, self.split_pages_var.get())
        self.output_var.set(str(out_path))
        out = str(out_path)

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

        params = {
            "doc":          self._doc,
            "output_path":  out,
            "dot_limit":    self.dot_var.get(),
            "h_val":        self.denoise_var.get(),
            "edge_clean":   self.edge_clean_var.get(),
            "edge_margin":  self.margin_var.get(),
            "edge_thresh":  self.thresh_var.get(),
            "deskew":       self.deskew_var.get(),
            "max_angle":    self.angle_var.get(),
            "skip_first":   self.skip_first_var.get(),
            "skip_last":    self.skip_last_var.get(),
            "split_pages":  self.split_pages_var.get(),
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
        try:
            import fitz
            import cv2
            import numpy as np
            from PIL import Image, ImageEnhance

            doc   = p["doc"]
            total = len(doc)
            clean_page_limit = p.get("clean_page_limit")
            cancel_event = p.get("cancel_event")
            out_images = []
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
                is_detected_color = bool(p["color_pages"].get(page_num, False))
                is_color = bool(p["keep_color"] and is_detected_color)
                edge_cleanup = _edge_cleanup_allowed(
                    page_num,
                    total,
                    bool(p["edge_clean"]),
                    p["color_pages"],
                )

                rot = p["rotations"].get(page_num, 0)
                brightness, contrast = _adjustment_values_from_map(
                    p["page_adjustments"],
                    page_num,
                )
                page_clean_settings = replace(
                    clean_settings,
                    clean_edges=edge_cleanup,
                    brightness=brightness,
                    contrast=contrast,
                )

                # Рендерим страницу в 300 dpi для точной очистки и сохранения размера.
                page = doc.load_page(page_num)
                mat  = fitz.Matrix(render_zoom, render_zoom).prerotate(rot)
                pix  = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
                img  = np.frombuffer(pix.samples, dtype=np.uint8
                                     ).reshape(pix.h, pix.w, 3)
                original_rgb = Image.fromarray(img)
                protected_box = p["protected_boxes"].get(page_num)

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
                    self._queue.put(("page_status", page_num, "skipped" if (manual_skip or limit_skip) else "done"))
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

                self._queue.put(("page_status", page_num, "done"))
                self._queue.put(("progress", pct, lbl))

            # Сохранение
            if cancel_event is not None and cancel_event.is_set():
                self._queue.put(("cancelled",))
                return
            self._queue.put(("status", "Сохранение PDF…", TXT1))
            saved_path = p["output_path"]
            if out_images:
                # Конвертируем в RGB чтобы смешанные цветные/ч-б сохранились
                save_images = []
                for im in out_images:
                    if im.mode != "RGB":
                        im = im.convert("RGB")
                    save_images.append(im)
                saved_path = _save_pdf_images(save_images, p["output_path"], render_dpi, p["split_pages"])

            self._queue.put(("done", str(saved_path)))

        except ImportError as e:
            self._queue.put(("error",
                f"Не установлена зависимость: {e}\n\n"
                "Запустите:\n  pip install pymupdf opencv-python pillow numpy"))
        except Exception:
            import traceback
            self._queue.put(("error", traceback.format_exc()))

    # ── ОПРОС ОЧЕРЕДИ ─────────────────────────────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]

                if kind == "progress":
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

                elif kind == "color_detect_done":
                    # Перерисовываем миниатюры с отметками цвета
                    self._build_thumb_widgets()
                    self._render_page()
                    self._update_status_bar()

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


def _save_pdf_images(images, output_path, dpi, split_pages=False):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if split_pages:
        folder = output_path.with_suffix("")
        folder = folder.with_name(folder.name + "_pages")
        folder.mkdir(parents=True, exist_ok=True)
        for index, image in enumerate(images, start=1):
            page_path = folder / f"{output_path.stem}_{index:03d}.pdf"
            temp_path = page_path.with_name(f"{page_path.stem}.tmp-{uuid4().hex}.pdf")
            image.save(temp_path, resolution=float(dpi), quality=95)
            temp_path.replace(page_path)
        return folder

    temp_output = output_path.with_name(f"{output_path.stem}.tmp-{uuid4().hex}.pdf")
    images[0].save(
        temp_output,
        save_all=True,
        append_images=images[1:],
        resolution=float(dpi),
        quality=95)
    temp_output.replace(output_path)
    return output_path


def _deskew(gray, max_angle_deg):
    """
    Определяет угол наклона через преобразование Хафа и поворачивает изображение.
    Возвращает выровненный grayscale.
    """
    import cv2
    import numpy as np

    _, thresh = cv2.threshold(gray, 0, 255,
                              cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    dilated = cv2.dilate(thresh, kernel, iterations=1)

    coords = np.column_stack(np.where(dilated > 0))
    if len(coords) < 50:
        return gray

    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = 90 + angle

    if abs(angle) > max_angle_deg or abs(angle) < 0.3:
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


# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = PdfCleanerApp()
    app.mainloop()
