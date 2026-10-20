@echo off
rem Builds "SQL-Lens.exe" next to this file.
rem Needs Python with:  pip install pyinstaller pillow pywebview
rem The build files go to %TEMP%\sql-lens-build, so this folder stays clean.

setlocal
set "AQUI=%~dp0"
set "TRABAJO=%TEMP%\sql-lens-build"

python -m PyInstaller --noconfirm --clean --onefile --windowed --name "SQL-Lens" ^
  --icon "%AQUI%icon.ico" ^
  --add-data "%AQUI%app.html;." ^
  --add-data "%AQUI%visor.html;." ^
  --add-data "%AQUI%comparar.html;." ^
  --add-data "%AQUI%extraer.ps1;." ^
  --add-data "%AQUI%icon.ico;." ^
  --distpath "%AQUI%." --workpath "%TRABAJO%" --specpath "%TRABAJO%" ^
  "%AQUI%app.py"

if errorlevel 1 (
  echo.
  echo Build failed.
  exit /b 1
)
echo.
echo Done: "%AQUI%SQL-Lens.exe"
