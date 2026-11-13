@echo off
REM Builds dist\LetItDownload.exe   (pip install pyinstaller pywebview pillow numpy)
set R=%~dp0
cd /d "%R%"
python tools\make_icon.py
python -m PyInstaller --noconfirm --onefile --windowed --name LetItDownload --icon "%R%assets\icon.ico" --version-file "%R%version.txt" ^
  --add-data "%R%index.html;." --add-data "%R%assets;assets" --distpath "%R%dist" --workpath "%R%build" --specpath "%R%build" server.py
echo.
echo Done: dist\LetItDownload.exe
