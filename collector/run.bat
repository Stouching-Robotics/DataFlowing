@echo off
cd /d "%~dp0"
rem 日常启动（已部署过才用它）: 直接跑项目自带 venv，跳过 start.bat 的全部检查。
rem 但「还没部署」这一种情况必须拦住 —— 否则 cmd 只会甩一句
rem 「系统找不到指定的路径。」，用户完全不知道下一步该做什么。
rem 注意: 本文件必须保持 GBK + CRLF（改错编码 cmd 会乱码并静默失败）。
if not exist "venv\Scripts\python.exe" goto :no_venv
set QT_QPA_PLATFORM_PLUGIN_PATH=%~dp0venv\Lib\site-packages\PyQt5\Qt5\plugins\platforms
venv\Scripts\python.exe main.py
pause
exit /b 0

:no_venv
echo.
echo  [错误] 还没有部署: 找不到 venv\Scripts\python.exe
echo.
echo  请先双击 start.bat 一键部署（自动建 venv + 装依赖，约 3-10 分钟）。
echo  不要手工建 venv —— 那样会绕过离线 wheels/ 与依赖签名，装完仍可能缺包。
echo  部署过一次之后，日常双击本脚本启动最快。
echo.
pause
exit /b 1
