@echo off
setlocal
title Claude Agent - phone server
cd /d "%~dp0"

REM ============================================================
REM  One-click launcher for the Claude Agent phone UI.
REM  Double-click this file, then open the printed URL on your
REM  phone (same Wi-Fi). Keep this window open while you use it.
REM ============================================================

REM --- Optional: require a token from the phone. Uncomment and
REM     set a long secret; the phone asks for it once.
REM set "AGENT_API_KEY=change-me-to-a-long-secret"

REM --- Sanity check: is the Claude CLI available? --------------
where claude >nul 2>nul
if errorlevel 1 (
  echo [!] The 'claude' CLI was not found on PATH.
  echo     Install Claude Code and sign in, then try again.
  echo.
  pause
  exit /b 1
)

REM --- Find this computer's Wi-Fi (DHCP) IPv4 for the phone URL -
set "LANIP="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$i=Get-NetIPAddress -AddressFamily IPv4 | Where-Object PrefixOrigin -eq 'Dhcp' | Select-Object -First 1 -ExpandProperty IPAddress; if(-not $i){$i='127.0.0.1'}; $i"`) do set "LANIP=%%i"
if not defined LANIP set "LANIP=127.0.0.1"

echo.
echo ============================================================
echo   Claude Agent is starting...
echo.
echo   On your phone (same Wi-Fi), open:
echo.
echo        http://%LANIP%:8007
echo.
echo   (If that IP doesn't work, run 'ipconfig' and use your
echo    Wi-Fi adapter's IPv4 Address instead.)
echo.
echo   Keep this window open. Press Ctrl+C to stop the server.
echo ============================================================
echo.

python -m uvicorn server:app --host 0.0.0.0 --port 8007 --app-dir claude-agent

echo.
echo Server stopped. Press any key to close this window.
pause >nul
