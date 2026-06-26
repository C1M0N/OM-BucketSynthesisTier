@echo off
chcp 65001 >nul
setlocal
title osu4k - 4K classifier

rem Pause at the end only when double-clicked (not when called from a terminal).
set "PAUSE_AT_END="
echo "%cmdcmdline%" | find /i "%~0" >nul && set "PAUSE_AT_END=1"

set "SCRIPT=%~dp0engine\run_workflow.ps1"
if not exist "%SCRIPT%" (
  echo [ERROR] run_workflow.ps1 not found: %SCRIPT%
  set "RC=1"
  goto :end
)

echo ============================================================
echo   osu!mania 4K composite-difficulty classifier
echo   Running incremental pass...  (make sure osu!lazer is CLOSED)
echo ============================================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -ResetBaseline %*
set "RC=%ERRORLEVEL%"

echo.
echo ============================================================
if "%RC%"=="0" echo   [DONE] updated 4k_collections.db ^& 4k_report.pdf in this folder
if "%RC%"=="7" echo   [SKIP] osu!lazer is running or realm not ready - nothing changed
if not "%RC%"=="0" if not "%RC%"=="7" echo   [FAIL] exit %RC%  -  log: %LOCALAPPDATA%\osu4k\logs
echo ============================================================

:end
if not defined RC set "RC=1"
if defined PAUSE_AT_END (
  echo.
  echo Press any key to close...
  pause >nul
)
endlocal & exit /b %RC%
