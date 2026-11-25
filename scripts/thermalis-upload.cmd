@echo off
rem THERMALIS paket yukleyici (ikinci bilgisayar). Tam uygulama gerekmez; yalniz "upload" pixi ortami.
rem   thermalis-upload configure --endpoint https://<HESAP>.r2.cloudflarestorage.com --bucket thermalis-data-dev
rem   thermalis-upload credentials
rem   thermalis-upload upload "D:\Paketler\XYZ_ADMIN_v1.0.0" [--authorization yetki.json] [--dry-run]
rem Gizli degerleri komut satirina yazmayin: "credentials" yanki olmadan sorar.
setlocal
set "THERMALIS_CALLER_CWD=%CD%"
pixi run --manifest-path "%~dp0..\pixi.toml" --environment upload upload-package %*
exit /b %ERRORLEVEL%
