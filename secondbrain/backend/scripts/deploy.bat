@echo off
REM ============================================================
REM  SecondBrain 部署脚本（换新版本用）
REM  用法：deploy.bat "<新构建目录>" "<安装目录>"
REM        两个路径都由参数传入，脚本内不含任何机器专用路径
REM
REM  关键点：
REM   - 先停进程 → 备份 → 清空目标 → 再拷入
REM   - 绝不 Copy-Item -Recurse 到已存在目录（会套出 data\data\data…）
REM   - 拷贝后校验关键文件（db / config / logs）
REM ============================================================
setlocal enabledelayedexpansion

REM ---- 参数 ----
set "DEPLOY_SRC=%~1"
set "TARGET=%~2"
if "%DEPLOY_SRC%"=="" (
  echo 用法: %~nx0 "<新构建目录>" "<安装目录>"
  echo 例:   %~nx0 "D:\_dist\SecondBrain" "D:\Apps\SecondBrain"
  exit /b 1
)
if "%TARGET%"=="" (
  echo [错误] 缺少安装目录参数
  exit /b 1
)

if not exist "%DEPLOY_SRC%\SecondBrain.exe" (
  echo [错误] 源目录没有 SecondBrain.exe: %DEPLOY_SRC%
  exit /b 1
)

REM ---- 0. 检查是否在运行（避免打断用户！）----
tasklist /FI "IMAGENAME eq SecondBrain.exe" 2>nul | find /I "SecondBrain.exe" >nul
if not errorlevel 1 (
  echo.
  echo [警告] SecondBrain 正在运行！
  echo        换版本需要关掉它。请先关闭它的窗口，然后按任意键继续。
  echo        （直接杀进程会打断你正在做的事）
  echo.
  pause >nul
)

REM ---- 1. 停进程 ----
echo [1/5] 停止运行中的 SecondBrain...
taskkill /IM SecondBrain.exe /F >nul 2>&1
timeout /t 2 /nobreak >nul

REM ---- 2. 备份现有数据与配置（只留这两样）----
set "STAMP=%DATE:~-4%%DATE:~4,2%%DATE:~7,2%-%TIME:~0,2%%TIME:~3,2%"
set "STAMP=%STAMP: =0%"
set "BAK=%TARGET%\..\_sb_backup_%STAMP%"
echo [2/5] 备份数据与配置到 %BAK%
if not exist "%BAK%" mkdir "%BAK%"
if exist "%TARGET%\data" robocopy "%TARGET%\data" "%BAK%\data" /E /NFL /NDL /NJH /NJS /NP >nul
if exist "%TARGET%\config" robocopy "%TARGET%\config" "%BAK%\config" /E /NFL /NDL /NJH /NJS /NP >nul
echo        （备份仅保留 data 与 config，程序文件会被整体替换）

REM ---- 3. 清空目标目录（只删程序文件，保留 data/config）----
echo [3/5] 替换程序文件...
if exist "%TARGET%\SecondBrain.exe" del /F /Q "%TARGET%\SecondBrain.exe"
if exist "%TARGET%\_internal" rmdir /S /Q "%TARGET%\_internal"

REM ---- 4. 拷入新程序（robocopy /MIR 不会套娃）----
robocopy "%DEPLOY_SRC%" "%TARGET" /E /NFL /NDL /NJH /NJS /NP /XD data config >nul
if errorlevel 8 (
  echo [错误] robocopy 失败，错误码 %errorlevel%
  exit /b 1
)

REM ---- 5. 校验 ----
echo [4/5] 校验...
if not exist "%TARGET%\SecondBrain.exe" (
  echo [错误] SecondBrain.exe 未部署成功
  exit /b 1
)
if not exist "%TARGET%\_internal\frontend\dist\index.html" (
  echo [警告] 前端资源缺失: _internal\frontend\dist\index.html
)
if exist "%TARGET%\data\secondbrain.db" echo        数据库 OK
if exist "%TARGET%\config\config.toml" echo        配置 OK

REM ---- 6. 清理嵌套副本（历史遗留）----
if exist "%TARGET%\data\data" (
  echo [5/5] 清理历史遗留的 data\data 嵌套副本...
  rmdir /S /Q "%TARGET%\data\data"
)

echo.
echo 完成。直接双击 "%TARGET%\SecondBrain.exe" 即可。
echo （无小黑窗；日志在 %TARGET%\data\logs\app.log）
endlocal
