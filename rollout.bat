@echo off
rem TriLola Rollout - Doppelklick startet die Oberflaeche im Browser.
setlocal
chcp 65001 >nul
cd /d "%~dp0deploy"
set "VENV=%CD%\.venv"
if exist "%VENV%\Scripts\python.exe" goto venvok
echo Richte die Rollout-Umgebung ein (einmalig, ca. 1 Minute) ...
where py >nul 2>&1
if errorlevel 1 goto usepython
py -3 -m venv "%VENV%"
goto venvcheck
:usepython
python -m venv "%VENV%"
:venvcheck
if exist "%VENV%\Scripts\python.exe" goto venvok
echo.
echo Python 3 wurde nicht gefunden.
echo Bitte von https://www.python.org installieren (Haken bei "Add python.exe to PATH").
pause
exit /b 1
:venvok
fc /b requirements-gui.txt "%VENV%\requirements.stamp" >nul 2>&1
if not errorlevel 1 goto start
echo Installiere benoetigte Pakete ...
"%VENV%\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements-gui.txt
if errorlevel 1 goto pipfail
copy /y requirements-gui.txt "%VENV%\requirements.stamp" >nul
goto start
:pipfail
echo Warnung: Pakete konnten nicht installiert werden (Internet?). Versuche trotzdem zu starten.
:start
"%VENV%\Scripts\python.exe" bluecat_gui.py %*
if errorlevel 1 pause
