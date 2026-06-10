# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

import PySide6
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


pyside6_dir = Path(PySide6.__file__).resolve().parent
all_pyside6_binaries = collect_dynamic_libs('PySide6')
pyside6_binaries = [
    item
    for item in all_pyside6_binaries
    if Path(item[0]).parent == pyside6_dir
]
shiboken6_binaries = collect_dynamic_libs('shiboken6')
pyside6_datas = collect_data_files(
    'PySide6',
    includes=[
        'resources/*',
        'translations/*',
        'Qt/resources/*',
        'Qt/translations/*',
    ],
)
qtwebengine_process = pyside6_dir / 'QtWebEngineProcess.exe'
qt_runtime_binaries = []
if qtwebengine_process.exists():
    qt_runtime_binaries.append((str(qtwebengine_process), 'PySide6'))


a = Analysis(
    ['main_qt.py'],
    pathex=[],
    binaries=pyside6_binaries + shiboken6_binaries + qt_runtime_binaries,
    datas=pyside6_datas + [('ui\\CrystalPDF_UI_v2.0.0.html', 'ui'), ('icon.ico', '.')],
    hiddenimports=[
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtNetwork',
        'PySide6.QtWidgets',
        'PySide6.QtWebChannel',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CrystalPDF-v2.0.0',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=['Qt*.dll', 'PySide6*.dll', 'vcruntime*.dll', 'msvcp*.dll'],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
