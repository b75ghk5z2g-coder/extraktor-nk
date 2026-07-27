@echo off
REM Interny pomocny skript - nespustat priamo.
REM Vola ho spustit-appku.bat, aby sa predislo problemom s vnorenymi
REM uvodzovkami, ked cesta k priecinku obsahuje medzery
REM (napr. C:\Users\Jana Novakova\...).
cd /d "%~dp0"
%~1 -m uvicorn main:app --reload --host 0.0.0.0 --port 8000

echo.
echo Server sa zastavil (alebo nastala chyba vyssie).
echo Toto okno mozete zatvorit.
pause
