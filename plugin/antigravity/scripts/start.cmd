@echo off
rem Antigravity Marketplace launcher: runs the NexusRelay engine installed on this PC as an stdio relay to its local
rem daemon (NexusRelay.exe --bridge <url>). A Marketplace plugin can only name its own folder, so every install records
rem its engine in %USERPROFILE%\.nexus\nexus-relay\engine.txt (NexusRelay.exe --engine-pointer, run by install.ps1):
rem one line "<version>|<path>", the path relative to the user profile or absolute. It names the immutable versioned
rem copy (%LOCALAPPDATA%\NexusRelay\versions\<fingerprint>), never the stable bin folder, which an install renames: a
rem running bridge locks nothing an update needs. An engine older than MINIMUM (no --bridge) is refused with how to
rem update it, instead of starting a server that does not know this plugin.
setlocal
set "MINIMUM=1.13.8"
set "POINTER=%USERPROFILE%\.nexus\nexus-relay\engine.txt"
set "VERSION="
set "ENGINE="
if exist "%POINTER%" for /f "usebackq tokens=1,* delims=|" %%A in ("%POINTER%") do if not defined ENGINE (
    set "VERSION=%%A"
    set "ENGINE=%%B"
)
if not defined ENGINE goto missing
if not "%ENGINE:~1,1%"==":" if not "%ENGINE:~0,2%"=="\\" set "ENGINE=%USERPROFILE%\%ENGINE%"
if not exist "%ENGINE%" goto missing
call :number "%VERSION%" HAVE
call :number "%MINIMUM%" NEED
if %HAVE% LSS %NEED% goto old
"%ENGINE%" --bridge %*
exit /b %ERRORLEVEL%

:missing
>&2 echo NexusRelay is not installed on this PC. Ask Antigravity to install it ^(see the nexus-relay skill^), or follow https://github.com/pironjulien/NexusRelay#readme, then open a new conversation.
exit /b 1

:old
>&2 echo NexusRelay %VERSION% is installed on this PC, but this plugin needs %MINIMUM% or later. Ask Antigravity to update it ^(see the nexus-relay skill^), or follow https://github.com/pironjulien/NexusRelay#readme, then open a new conversation.
exit /b 1

rem :number "<x.y.z>" <variable>: the version as one comparable number (0 when unreadable, so refused as too old).
:number
set "%~2=0"
for /f "tokens=1-3 delims=.-+" %%a in ("%~1") do set /a "%~2=%%a*1000000+%%b*1000+%%c" >nul 2>&1
exit /b 0
