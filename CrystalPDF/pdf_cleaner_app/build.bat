@echo off
chcp 65001 >nul
title CrystalPDF v2.0.0 - сборка EXE

echo.
echo  ==========================================
echo       CrystalPDF v2.0.0 - установка и сборка
echo  ==========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo  [ОШИБКА] Python не найден. Установите Python 3.10 или новее.
    pause
    exit /b 1
)

echo  [1/3] Установка зависимостей...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo  [ОШИБКА] Не удалось установить зависимости.
    pause
    exit /b 1
)

echo.
echo  [2/3] Сборка приложения...
python -m PyInstaller CrystalPDF-v2.0.0.spec --clean --noconfirm
if errorlevel 1 (
    echo  [ОШИБКА] Сборка не удалась.
    pause
    exit /b 1
)

echo.
echo  [3/3] Готово.
echo  EXE: dist\CrystalPDF-v2.0.0.exe
echo.

if exist "dist\CrystalPDF-v2.0.0.exe" explorer dist

pause
