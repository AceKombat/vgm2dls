@echo off
setlocal EnableExtensions

rem vgm2dls helper: install vgmstream into tools\vgmstream
set "ROOT=%~dp0"
set "TOOLS_DIR=%ROOT%tools"
set "DEST_DIR=%TOOLS_DIR%\vgmstream"
set "BAK_DIR=%TOOLS_DIR%\vgmstreamBAK"

echo.
echo [vgm2dls] Preparing vgmstream...

if not exist "%TOOLS_DIR%" (
  mkdir "%TOOLS_DIR%" >nul 2>&1
)

if exist "%DEST_DIR%\vgmstream-cli.exe" (
  if exist "%BAK_DIR%" rmdir /S /Q "%BAK_DIR%" >nul 2>&1
  echo [info] Backing up current tools\vgmstream to tools\vgmstreamBAK...
  robocopy "%DEST_DIR%" "%BAK_DIR%" /E >nul
)

if exist "%DEST_DIR%" rmdir /S /Q "%DEST_DIR%" >nul 2>&1
mkdir "%DEST_DIR%" >nul 2>&1

echo [info] Downloading latest vgmstream release (this overwrites tools\vgmstream)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$ProgressPreference='SilentlyContinue';" ^
  "$tmp=Join-Path $env:TEMP ('vgm2dls_vgmstream_' + [guid]::NewGuid().ToString('N'));" ^
  "$zip=Join-Path $tmp 'vgmstream.zip';" ^
  "$ext=Join-Path $tmp 'extract';" ^
  "New-Item -ItemType Directory -Force -Path $ext | Out-Null;" ^
  "$rel=Invoke-RestMethod -Uri 'https://api.github.com/repos/vgmstream/vgmstream/releases/latest';" ^
  "$asset=$rel.assets | Where-Object { $_.name -match 'win' -and $_.name -match '\.zip$' } | Select-Object -First 1;" ^
  "if(-not $asset){ $asset=$rel.assets | Where-Object { $_.name -match '\.zip$' } | Select-Object -First 1 };" ^
  "if(-not $asset){ throw 'No zip asset found in latest release.' };" ^
  "Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip;" ^
  "Expand-Archive -Path $zip -DestinationPath $ext -Force;" ^
  "$exe=Get-ChildItem -Path $ext -Recurse -File -Filter 'vgmstream-cli.exe' | Select-Object -First 1;" ^
  "if(-not $exe){ throw 'vgmstream-cli.exe not found in package.' };" ^
  "$src=$exe.Directory.FullName;" ^
  "$keep=@('vgmstream-cli.exe','avcodec-vgmstream-*.dll','avformat-vgmstream-*.dll','avutil-vgmstream-*.dll','libatrac9*.dll','libcelt*.dll','libg719_decode*.dll','libmpg123*.dll','libspeex*.dll','libvorbis*.dll');" ^
  "foreach($p in $keep){ Copy-Item -Path (Join-Path $src $p) -Destination '%DEST_DIR%' -Force -ErrorAction SilentlyContinue };" ^
  "Remove-Item -Path $tmp -Recurse -Force;"
if errorlevel 1 goto :fail

if exist "%DEST_DIR%\vgmstream-cli.exe" (
  echo [ok] Ready: "%DEST_DIR%\vgmstream-cli.exe"
  exit /B 0
)

:fail
echo [fail] Could not set up vgmstream.
if exist "%DEST_DIR%" rmdir /S /Q "%DEST_DIR%" >nul 2>&1
if exist "%BAK_DIR%\vgmstream-cli.exe" (
  echo [info] Restoring from tools\vgmstreamBAK...
  robocopy "%BAK_DIR%" "%DEST_DIR%" /E >nul
  if exist "%DEST_DIR%\vgmstream-cli.exe" (
    echo [ok] Restored previous vgmstream from backup.
    exit /B 0
  )
)
exit /B 1
