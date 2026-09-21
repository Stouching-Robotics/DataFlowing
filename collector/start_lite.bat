@echo off
rem ============================================================
rem  !!! 编码警告: 本文件必须保持 GBK(ANSI) 编码 + CRLF 行尾 !!!
rem  用记事本 / VSCode 另存（默认存 UTF-8 会乱码导致 cmd 解析
rem  静默失败），如损坏请从 GitLab 重新下载原文件。
rem ============================================================
rem ============================================================
rem  DAQ 极简采集版 —— Windows 一键部署脚本
rem
rem  用法:
rem    start_lite.bat            部署(按需) + 启动极简采集
rem    start_lite.bat reinstall  删除 venv_lite 强制重装（出问题首选）
rem    start_lite.bat help       打开 使用说明_lite.md
rem
rem  只安装 requirements-lite.txt 白名单依赖（独立 venv_lite/，
rem  与主程序 venv/ 互不影响）；wheels/ 与 data/ 两版本共用。
rem  本脚本自带 venv，可在已激活 conda / 其它 venv 的窗口里直接运行（互不影响）
rem  依赖安装顺序: wheels\ 离线包 → 阿里云镜像 → 清华镜像 → 官方源
rem  错误码 A-G 对应 使用说明_lite.md（异常处理章节）
rem
rem  【夹爪为什么这里不查】Windows 包**有意不带** UMI/Fays 夹爪的原生资源
rem  （core/gripper/native，约 460MB）与触觉 SDK（core/gripper/sightac_sdk，
rem  里面是 pyarmor 运行时的 .so 与 libSonixCamera.so）—— 这些全是 ELF，
rem  在 Windows 上物理跑不了。夹爪的 Python 代码两个包都带（无平台绑定），
rem  缺载荷时 core/gripper/paths.py 的 gripper_resources_available() 返回假
rem  → 设备列表里的「UMI 夹爪」组框还在，但里面是空的（点「开启」提示「列表中没有设备」），属**预期降级**、不是故障。
rem  Linux 包带载荷，那边 start_lite.sh 有 [错误 B] 逐项校验；两边刻意
rem  不对称，别在这里补一个「资源缺失」的检查（在这份包里它永远不通过）。
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ── 环境隔离: 调用任何 Python 之前，先清掉会「串味」的外部变量 ──
rem 本脚本一律用项目自带 venv，但用户可能是在 conda / 另一个 venv 里双击的，
rem 或自己设过 PYTHONHOME。这些变量会穿透进我们的 venv，把解释器指到别处
rem （症状: 依赖明明装了却 import 失败 / DLL load failed / pip 装到了别的环境）。
rem 直接清空并提示，不让用户去猜；只提示，不打断。
if defined VIRTUAL_ENV   echo  [提示] 检测到已激活的虚拟环境 "%VIRTUAL_ENV%"，本脚本不使用它（仍用项目自带 venv）
if defined CONDA_PREFIX  echo  [提示] 检测到已激活的 conda 环境 "%CONDA_PREFIX%"，本脚本不使用它（仍用项目自带 venv）
if defined PYTHONHOME    echo  [提示] 已忽略外部变量 PYTHONHOME="%PYTHONHOME%"
if defined PYTHONPATH    echo  [提示] 已忽略外部变量 PYTHONPATH="%PYTHONPATH%"
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONSTARTUP="
rem 屏蔽用户级 site-packages（%APPDATA%\Python 下的包），让 venv 完全自给自足
set "PYTHONNOUSERSITE=1"


set "FORCE=0"
if /i "%~1"=="reinstall" set "FORCE=1"
if /i "%~1"=="help"      goto :show_help

echo.
echo  ============================================================
echo     DAQ 极简采集版 —— 一键部署
echo  ============================================================
echo.

rem ── [G] 解压层次自检 ──
if not exist "main_lite.py"          goto :errG
if not exist "requirements-lite.txt" goto :errG

rem ── [1/6] 定位 Python（版本 >= 3.10，推荐 3.12）──
set "PY="
set "VPY=venv_lite\Scripts\python.exe"
if exist "%VPY%" (set "PY=%VPY%" & goto :have_python)
echo  [1/6] 查找 Python 环境 ...
call :try_py py -3.12
if defined PY goto :have_python
call :try_py py -3
if defined PY goto :have_python
call :try_py python
if defined PY goto :have_python
echo  [1/6] 未检测到 Python，自动下载安装 Python 3.12（约 25MB）...
set "PY_VER=3.12.10"
set "PY_EXE=%TEMP%\python-%PY_VER%-amd64.exe"
set "PY_URL=https://mirrors.aliyun.com/python-release/windows/python-%PY_VER%-amd64.exe"
set "PY_URL2=https://registry.npmmirror.com/-/binary/python/%PY_VER%/python-%PY_VER%-amd64.exe"
set "PY_URL3=https://mirrors.huaweicloud.com/python/%PY_VER%/python-%PY_VER%-amd64.exe"
if exist "wheels\python-%PY_VER%-amd64.exe" goto :py_copy_local
curl -L -o "%PY_EXE%" "%PY_URL%" --connect-timeout 20 --retry 2 --silent --show-error
if exist "%PY_EXE%" goto :py_run_installer
curl -L -o "%PY_EXE%" "%PY_URL2%" --connect-timeout 20 --retry 2 --silent --show-error
if exist "%PY_EXE%" goto :py_run_installer
curl -L -o "%PY_EXE%" "%PY_URL3%" --connect-timeout 20 --retry 2 --silent --show-error
if exist "%PY_EXE%" goto :py_run_installer
goto :errA
:py_copy_local
echo  [1/6] 使用 wheels\ 目录内的 Python 安装包 ...
copy /y "wheels\python-%PY_VER%-amd64.exe" "%PY_EXE%" >nul
:py_run_installer
echo  [1/6] 静默安装 Python 中（请不要关闭窗口，约 1 分钟）...
"%PY_EXE%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_test=0 Include_launcher=1
del "%PY_EXE%" >nul 2>&1
call :try_py py -3.12
if defined PY goto :have_python
goto :errA

:have_python
echo  [1/6] 使用 Python: %PY%

rem ── [2/6] 虚拟环境 venv_lite ──
rem 为什么体检: venv 目录在 ≠ venv 可用。
rem   ① 从别的机器拷来的 venv: python.exe 在，但里面硬编码的是那台机器的
rem      Python 路径 → 一跑就「No Python at ...」；
rem   ② pip 升级 / 杀软 / 断电打断: pip 被删到一半（目录还在、模块没了）→
rem      所有 pip 命令都报 ModuleNotFoundError: pip._internal.cli。
rem 这两类都不该让用户去猜: 一律先离线修（ensurepip 用 Python 自带组件），
rem 修不动就整目录重建（同样不联网）。
if not exist "%VPY%" goto :make_venv
if "%FORCE%"=="1" goto :reinstall_venv
rem 体检 ①: 解释器本身能不能跑、版本够不够（拷来的 venv 在这里现形）
"%VPY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :venv_broken
rem 体检 ②: pip 是否完整（半装 pip 在这里现形）
"%VPY%" -m pip --version >nul 2>&1
if not errorlevel 1 goto :deps_check
echo  [2/6] 检测到 venv_lite 的 pip 不完整，正在离线修复 ...
"%VPY%" -m ensurepip --upgrade >nul 2>&1
"%VPY%" -m pip --version >nul 2>&1
if not errorlevel 1 goto :deps_check
:venv_broken
echo  [2/6] venv_lite 不可用（pip 缺失或解释器异常），自动重建（不需要联网，约 1 分钟）...
goto :reinstall_venv_do
:reinstall_venv
echo  [2/6] reinstall: 删除旧 venv_lite ...
:reinstall_venv_do
rmdir /s /q "venv_lite" 2>nul
if exist "%VPY%" goto :errC2
:make_venv
echo  [2/6] 创建虚拟环境 venv_lite（首次约 1 分钟）...
rem 注意: PY 可能是带空格的命令（py -3.12 / python），不能加引号
%PY% -m venv "venv_lite"
if not exist "%VPY%" goto :errC

rem ── [3/6] 安装依赖（requirements-lite.txt 有变化或首次时）──
:deps_check
rem 用 requirements-lite.txt 的 修改时间|大小 做签名（内容变了才重装）
for %%I in (requirements-lite.txt) do set "SIG=%%~tI;%%~zI"
if "%FORCE%"=="1" goto :install_deps
if not exist "venv_lite\.deps-lite-ok" goto :install_deps
set /p STAMP=<"venv_lite\.deps-lite-ok"
if "%STAMP%"=="%SIG%" goto :after_deps

:install_deps
echo  [3/6] 安装依赖（首次约 3-8 分钟，之后启动秒开）...
rem ────────────────────────────────────────────────────────────
rem  这里以前有一句静默的 pip install --upgrade pip，已移除 —— 它是「半装 pip」
rem  的唯一来源: pip 升级是「先删旧、再解新」，中途被打断（关窗口 / 断网 /
rem  杀软 / 断电）就只剩一个空壳，之后每次启动都报
rem  ModuleNotFoundError: pip._internal.cli，而用户看到的是「依赖下载失败」
rem  （错误 D）—— 方向完全跑偏，而且重试多少次都一样。
rem  Python 3.10+ 自带的 pip 足够装本项目的全部依赖，故不再自动升级；
rem  确有需要请在 cmd 里手动执行（坏了的 pip 下次启动会被 [2/6] 体检修好）:
rem      venv_lite\Scripts\python.exe -m pip install --upgrade pip
rem ────────────────────────────────────────────────────────────
call :pip_install_req
if not errorlevel 1 goto :deps_write_ok
rem 安装失败: 先确认 pip 本身还在不在（被半装 / 被杀软删是常见现场）
"%VPY%" -m pip --version >nul 2>&1
if not errorlevel 1 goto :errD
call :repair_pip
if errorlevel 1 goto :errD
echo  [3/6] pip 已修复，重试安装 ...
call :pip_install_req
if errorlevel 1 goto :errD
:deps_write_ok
> "venv_lite\.deps-lite-ok" echo %SIG%

rem ── [4/6] 依赖冒烟自检（能 import 即通过）──
:after_deps
echo  [4/6] 依赖自检 ...
"%VPY%" -c "import main_lite" >nul 2>&1
if errorlevel 1 goto :errE
echo  [4/6] 依赖自检通过

rem ── [5/6] 启动 ──
echo  [5/6] 启动极简采集 ...
echo.
echo  【操作指引】
echo    · 设备: 插入后约 2 秒自动出现在列表，选中后点 开启
echo    · 录制: 先开 D435 / UVC 摄像头 / 夹爪（至少一个视频源），再点 开始/停止（正常停止=保存，X 丢弃=作废）
echo    · 上传: 录制完成后自动上传，或在下方列表手动上传
echo    · 说明: 双击 start_lite.bat help 打开 使用说明_lite.md
echo.
set "QT_QPA_PLATFORM_PLUGIN_PATH=%~dp0venv_lite\Lib\site-packages\PyQt5\Qt5\plugins\platforms"
"%VPY%" main_lite.py
set "EXITCODE=%errorlevel%"
if "%EXITCODE%"=="0" exit /b 0
goto :errF

rem ════════════════════════════════════════════════════════
rem  子程序: 校验并记录可用 Python 解释器
rem ════════════════════════════════════════════════════════
:try_py
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set "PY=%*"
exit /b 0

rem ────────────────────────────────────────────────────────────
rem  子程序: pip 半装时的离线自救
rem  ensurepip 用 Python 自带组件重装 pip，不联网；修不好返回非零，
rem  由调用方决定是重试安装还是直接报错（再不行就只能重建 venv）。
rem ────────────────────────────────────────────────────────────
:repair_pip
echo  [3/6] pip 异常，尝试离线修复 ...
"%VPY%" -m ensurepip --upgrade >nul 2>&1
"%VPY%" -m pip --version >nul 2>&1
exit /b %errorlevel%

rem ── 检查 wheels\ 目录下是否有 .whl 文件 ──
:wheels_exists
dir /b "wheels\*.whl" >nul 2>&1
exit /b %errorlevel%

rem ── 安装 requirements-lite.txt（离线包 → 阿里云 → 清华 → 官方源）──
:pip_install_req
call :wheels_exists
if errorlevel 1 goto :req_online
echo  [3/6] 检测到 wheels\ 离线包，优先离线安装 ...
"%VPY%" -m pip install --no-index --find-links "wheels" -r requirements-lite.txt
if not errorlevel 1 exit /b 0
echo  [3/6] 离线包安装失败，转在线安装 ...
:req_online
"%VPY%" -m pip install -r requirements-lite.txt -i https://mirrors.aliyun.com/pypi/simple/
if not errorlevel 1 exit /b 0
"%VPY%" -m pip install -r requirements-lite.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if not errorlevel 1 exit /b 0
"%VPY%" -m pip install -r requirements-lite.txt
exit /b %errorlevel%

rem ════════════════════════════════════════════════════════
rem  帮助 / 使用说明
rem ════════════════════════════════════════════════════════
:show_help
echo.
echo  【常用命令】
echo    start_lite.bat            部署并启动（默认）
echo    start_lite.bat reinstall  删除 venv_lite 强制重装
echo    start_lite.bat help       打开本文档
echo.
if exist "使用说明_lite.md" start "" "使用说明_lite.md"
if not exist "使用说明_lite.md" echo  [警告] 未找到 使用说明_lite.md，请从 GitLab 重新下载完整文件
pause
exit /b 0

rem ════════════════════════════════════════════════════════
rem  异常处理（错误码 A-G，详见 使用说明_lite.md）
rem ════════════════════════════════════════════════════════
:errA
echo.
echo  [错误 A] 未找到且自动安装 Python 3.12 失败
echo  ------------------------------------------------------------
echo   0. 需 Python 3.10 以上版本，推荐手动安装 3.12
echo   1. 离线环境: 把 python-3.12.10-amd64.exe 放入本目录 wheels\ 后重试
echo      （由管理员用 scripts\pack_wheels.py --lite 生成）
echo   2. 手动安装: 浏览器打开官网下载 Python 3.12.x 64 位
echo      安装时务必勾选 "Add python.exe to PATH"
echo   3. 已安装但没反应: 卸载 Microsoft Store 的 Python 替身后重装
echo.
start https://www.python.org/downloads/
pause
exit /b 1

:errC
echo.
echo  [错误 C] 虚拟环境创建失败
echo  ------------------------------------------------------------
echo   1. 磁盘空间不足: 清理磁盘后重试，需要约 2GB 空间
echo   2. 杀毒软件拦截: 把本目录加入白名单后 双击 start_lite.bat reinstall
echo   3. 路径问题: 把项目文件夹移到路径较短的位置（如 C:\DAQ_lite）
echo   4. 路径含特殊字符: 换一个纯英文/数字的目录再试
echo.
pause
exit /b 1

:errC2
echo.
echo  [错误 C2] 旧 venv_lite 删除失败（文件被占用）
echo  ------------------------------------------------------------
echo   先关闭所有打开的采集窗口，再双击 start_lite.bat reinstall
echo.
pause
exit /b 1

:errD
echo.
echo  [错误 D] 依赖下载/安装失败
echo  ------------------------------------------------------------
echo   先看上一屏的报错，再对症处理:
echo.
echo   · 报 ModuleNotFoundError: pip._internal.cli / No module named 'pip'
echo     → venv 里的 pip 坏了（升级被打断 / 杀软删了文件），不是网络问题。
echo       直接双击 start_lite.bat reinstall 重建 venv（约 1 分钟，不需要联网）。
echo   · 报 No Python at ... / 找不到 Python
echo     → venv 是从别的机器拷来的，双击 start_lite.bat reinstall 重建即可。
echo   · 报 Could not find a version / connection / timeout / 证书错误
echo     → 才是网络或权限问题:
echo       1. 检查网络: 稍后双击 start_lite.bat 重试（已下载部分会缓存）
echo       2. 公司内网/防火墙: 请联系管理员开通 pypi 镜像，或使用离线包
echo          管理员用 scripts\pack_wheels.py --lite 生成 wheels\ 目录
echo       3. 杀毒软件/防火墙拦截 pip: 加入白名单后重试
echo.
pause
exit /b 1

:errE
echo.
echo  [错误 E] 依赖自检失败（依赖已安装但程序无法导入）
echo  ------------------------------------------------------------
echo   1. 杀毒软件破坏了 venv_lite 文件: 加白名单后重装
echo   2. 依赖版本冲突: 双击 start_lite.bat reinstall 重装
echo   3. 查看具体原因: 在 cmd 里执行
echo        venv_lite\Scripts\python.exe -c "import main_lite"
echo.
pause
exit /b 1

:errF
echo.
echo  [错误 F] 程序启动后异常退出
echo  ------------------------------------------------------------
echo   1. 显卡驱动过旧: 更新显卡驱动后重试
echo   2. 远程桌面/虚拟机: 请在本机实体环境运行
echo   3. Qt 平台插件损坏: 双击 start_lite.bat reinstall 重装
echo   4. 摄像头隐私权限: Windows 设置 - 隐私和安全性 - 相机
echo      允许应用访问摄像头
echo   5. 查看错误信息: 在 cmd 里执行
echo        venv_lite\Scripts\python.exe main_lite.py
echo.
pause
exit /b 1

:errG
echo.
echo  [错误 G] 未找到 main_lite.py —— 解压层次不对
echo  ------------------------------------------------------------
echo   请保持文件夹结构完整: start_lite.bat 与 main_lite.py 必须在同一目录。
echo   不要只拷贝 start_lite.bat，需解压完整项目；压缩包若带外层文件夹，
echo   请进入内层目录再双击 start_lite.bat。
echo.
pause
exit /b 1
