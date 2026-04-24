@echo off
setlocal enabledelayedexpansion
title CrowdWorks Bot — Build

echo ============================================================
echo  CrowdWorks Bot — PyInstaller build
echo ============================================================
echo.

:: ── 1. Regenerate icon ───────────────────────────────────────────────────────
echo [1/3] Generating icon.ico ...
python -c "
from PIL import Image, ImageDraw, ImageFont
SIZES = [16,24,32,48,64,128,256]
BG = '#0f172a'; ACCENT = '#3b82f6'
FONT = 'C:/Windows/Fonts/arialbd.ttf'
frames = []
for sz in SIZES:
    img  = Image.new('RGBA', (sz, sz), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    r    = max(2, sz // 6)
    draw.rounded_rectangle([0, 0, sz-1, sz-1], radius=r, fill=BG)
    fs   = max(6, int(sz * 0.55))
    font = ImageFont.truetype(FONT, fs)
    bb   = draw.textbbox((0,0), 'CW', font=font)
    tw, th = bb[2]-bb[0], bb[3]-bb[1]
    x = (sz-tw)//2 - bb[0]
    y = (sz-th)//2 - bb[1] - max(1, sz//20)
    draw.text((x,y), 'CW', fill=ACCENT, font=font)
    frames.append(img)
frames[0].save('icon.ico', format='ICO', sizes=[(s,s) for s in SIZES], append_images=frames[1:])
print('  icon.ico OK')
"
if errorlevel 1 (
    echo ERROR: Icon generation failed.
    pause
    exit /b 1
)

:: ── 2. Run PyInstaller ───────────────────────────────────────────────────────
echo.
echo [2/3] Running PyInstaller ...
pyinstaller CrowdWorksBot.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed. See output above.
    pause
    exit /b 1
)

:: ── 3. Post-build reminder ───────────────────────────────────────────────────
echo.
echo [3/3] Build complete!
echo.
echo   Executable : dist\CrowdWorksBot\CrowdWorksBot.exe
echo   Data files : stored next to the .exe (auto-created on first run)
echo.
echo   IMPORTANT — run this once after deploying to install Playwright browsers:
echo     playwright install chromium
echo.
echo ============================================================
pause
