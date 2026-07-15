@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "__APPDIR__="
for %%I in ("%__APPDIR__%..") do set "SystemRoot=%%~fI"
set "WINDIR=%SystemRoot%"
set "ComSpec=%__APPDIR__%cmd.exe"
"%__APPDIR__%WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0codex-tier-launcher.ps1" %*
exit /b %ERRORLEVEL%
