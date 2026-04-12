@echo off
title YT-FB Bot - Kurulum
cd /d "%~dp0"

echo.
echo  ================================================
echo    YouTube Shorts ^> Facebook Bot  -  Kurulum
echo  ================================================
echo.

:: Python yuklu mu kontrol et
python --version >nul 2>&1
if errorlevel 1 (
    echo  [HATA] Python bulunamadi!
    echo.
    echo  Lutfen once Python yukleyin:
    echo  https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo  Python bulundu:
python --version
echo.

:: pip'i guncelle
echo  [1/2] pip guncelleniyor...
python -m pip install --upgrade pip --quiet
echo  Tamam.
echo.

:: Paketleri yukle
echo  [2/2] Gerekli paketler yukleniyor...
echo.
pip install -r requirements.txt
echo.

if errorlevel 1 (
    echo  ================================================
    echo   [HATA] Bazi paketler yuklenemedi!
    echo   Yukaridaki hata mesajini kontrol edin.
    echo  ================================================
) else (
    echo  ================================================
    echo   Kurulum tamamlandi!
    echo   Botu baslatmak icin "Bot Baslat.bat" dosyasini
    echo   cift tiklayin.
    echo  ================================================
)

echo.
pause
