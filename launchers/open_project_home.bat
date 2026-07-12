@echo off
setlocal
for %%I in ("%~dp0..") do set "PACKAGE_DIR=%%~fI"
start "" "%PACKAGE_DIR%"