@echo off
setlocal
title Claude Agent - phone server
cd /d "%~dp0"

REM UTF-8 console so the scan-to-connect QR code renders as solid blocks.
chcp 65001 >nul

REM ============================================================
REM  One-click launcher for the Claude Agent phone UI.
REM  Double-click, then open one of the printed URLs on your
REM  phone and enter the access token when asked (once).
REM  Keep this window open while you use it.
REM ============================================================

REM --- Sanity check: is the Claude CLI available? -------------
where claude >nul 2>nul
if errorlevel 1 (
  echo [!] The 'claude' CLI was not found on PATH.
  echo     Install Claude Code and sign in, then try again.
  echo.
  pause
  exit /b 1
)

REM --- Access token: generate once, reuse every launch -------
set "TOKENFILE=%~dp0.agent_token"
if not exist "%TOKENFILE%" (
  for /f "usebackq delims=" %%t in (`powershell -NoProfile -Command "[Guid]::NewGuid().ToString('N')"`) do > "%TOKENFILE%" echo %%t
)
set "AGENT_API_KEY="
set /p AGENT_API_KEY=<"%TOKENFILE%"

REM --- This computer's Wi-Fi (DHCP) IPv4 ----------------------
set "WIFIIP="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$i=Get-NetIPAddress -AddressFamily IPv4 | Where-Object PrefixOrigin -eq 'Dhcp' | Select-Object -First 1 -ExpandProperty IPAddress; if($i){$i}"`) do set "WIFIIP=%%i"
if not defined WIFIIP set "WIFIIP=127.0.0.1"

REM --- Tailscale IPv4 (for use away from home) ----------------
set "TSIP="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$i=Get-NetIPAddress -AddressFamily IPv4 | Where-Object InterfaceAlias -like '*Tailscale*' | Select-Object -First 1 -ExpandProperty IPAddress; if($i){$i}"`) do set "TSIP=%%i"

REM --- Scan-to-connect: token baked into the URL so no typing ------
REM Prefer Tailscale (works at home AND away); fall back to Wi-Fi.
set "PRIMARYIP=%WIFIIP%"
if defined TSIP set "PRIMARYIP=%TSIP%"
set "MAGICURL=http://%PRIMARYIP%:8007/?token=%AGENT_API_KEY%"

echo.
echo ============================================================
echo   Claude Agent is starting...
echo.
echo   SCAN THIS WITH YOUR PHONE CAMERA TO CONNECT:
echo.
python "%~dp0claude-agent\qr.py" "%MAGICURL%"
echo.
echo   ...or tap one of these links on your phone (auto sign-in):
echo.
if defined TSIP (
  echo   Anywhere ^(Tailscale^):  http://%TSIP%:8007/?token=%AGENT_API_KEY%
)
echo   On the same Wi-Fi:     http://%WIFIIP%:8007/?token=%AGENT_API_KEY%
if not defined TSIP (
  echo   Anywhere ^(Tailscale^):  not detected - start Tailscale to use away from home
)
echo.
echo   (If asked to sign in manually, the access token is:)
echo        %AGENT_API_KEY%
echo.
echo   Keep this window open. Press Ctrl+C to stop the server.
echo ============================================================
echo.

python -m uvicorn server:app --host 0.0.0.0 --port 8007 --app-dir claude-agent

echo.
echo Server stopped. Press any key to close this window.
pause >nul
