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

rem ── [1/6] 定位 Python 3.10（SDK 加密链的 ABI 要求，不是「及以上」）──
rem  极简版虽然不解算骨架，但手套的串口采集走 SDK 的传输层，而 SDK 根下的
rem  algorithm\（PyArmor 按 3.10 ABI 加密）就摆在那里 —— 版本不对时 import sdk
rem  是否失败取决于载荷是否含 algorithm，属于「看情况坏」，所以一律按 3.10 要求。
set "PY="
set "VPY=venv_lite\Scripts\python.exe"
if exist "%VPY%" (set "PY=%VPY%" & goto :have_python)
:find_py
echo  [1/6] 查找 Python 环境 ...
call :try_py py -3.10
if defined PY goto :have_python
call :try_py py -3
if defined PY goto :have_python
call :try_py python
if defined PY goto :have_python
echo  [1/6] 未检测到 Python，自动下载安装 Python 3.10.11（约 28MB）...
set "PY_VER=3.10.11"
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
call :try_py py -3.10
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
rem 体检 ①: 解释器本身能不能跑（拷来的 venv 在这里现形）
"%VPY%" -c "import sys" >nul 2>&1
if errorlevel 1 goto :venv_broken
rem 体检 ②: 版本必须**恰好** 3.10（不是 >=）—— 3.12 的 venv 起得来，但手套
rem  SDK 在里面 import 不了。老客户机上那个 venv 正是 3.12 的，所以当成
rem  「venv 不可用」直接重建。
"%VPY%" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :venv_wrong_ver
rem 体检 ③: pip 是否完整（半装 pip 在这里现形）
"%VPY%" -m pip --version >nul 2>&1
if not errorlevel 1 goto :deps_check
echo  [2/6] 检测到 venv_lite 的 pip 不完整，正在离线修复 ...
call :purge_pip
"%VPY%" -m ensurepip --upgrade >nul 2>&1
"%VPY%" -m pip --version >nul 2>&1
if not errorlevel 1 goto :deps_check
:venv_wrong_ver
echo  [2/6] 已有 venv_lite 不是 Python 3.10，自动重建 —— 手套 SDK 要求 3.10。
goto :reinstall_venv_do
:venv_broken
echo  [2/6] venv_lite 不可用（pip 缺失或解释器异常），自动重建（不需要联网，约 1 分钟）...
goto :reinstall_venv_do
:reinstall_venv
echo  [2/6] reinstall: 删除旧 venv_lite ...
:reinstall_venv_do
rmdir /s /q "venv_lite" 2>nul
if exist "%VPY%" goto :errC2
:make_venv
rem PY 可能正指向刚被删掉的那只 venv python —— reinstall 与「体检不过自动重建」
rem 走的都是这条路: 原脚本在这里拿已删除的解释器去建 venv，于是静默失败
rem （start.bat reinstall 一直是坏的）。清空 PY，重新走一遍解释器查找。
if /i "%PY%"=="%VPY%" set "PY="
if not defined PY goto :find_py
echo  [2/6] 创建虚拟环境 venv_lite（首次约 1 分钟）...
rem 注意: PY 可能是带空格的命令（py -3.10 / python），不能加引号
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

rem ── [4/6] 手套 SDK（串口采集 + 触觉降噪）──
rem  极简版**不解算骨架**（不装 sdk.solver / algorithm），但手套的串口采集走
rem  SDK 的传输层、触觉降噪也换成了它 —— 所以载荷与探针只覆盖这两段。
rem  **缺失只警告、不拦截**: 相机/夹爪录制都不受影响。
rem  SDK 版本跟随 wheels\toolkit\glove_sdk.zip。**不能只看目录在不在**:
rem  老机器上目录早就有了而 zip 换了版本 —— 只看目录名会永远不解压。
rem ────────────────────────────────────────────────────────────
:after_deps
set "TOOLKIT="
if exist "tools\glove_sdk\sdk\__init__.py" set "TOOLKIT=%CD%\tools\glove_sdk"
set "TK_ZIP_SIG=none"
if exist "wheels\toolkit\glove_sdk.zip" for %%Z in ("wheels\toolkit\glove_sdk.zip") do set "TK_ZIP_SIG=%%~zZ-%%~tZ"
if not exist "wheels\toolkit\glove_sdk.zip" goto :toolkit_verify
set "TK_UNPACK="
if exist "venv_lite\.toolkit-unpacked" set /p TK_UNPACK=<"venv_lite\.toolkit-unpacked"
rem 要展开的两种情况: (1) zip 换了（戳不匹配）(2) 戳说"已展开"但目录不在 ——
rem 陈旧戳（误删 / 上次解压中断 / 杀软隔离）。少了 (2) 就会「zip 就在旁边，
rem 却永远不解压，还提示去开发机重打包」。
set "TK_NEED="
if not "%TK_UNPACK%"=="%TK_ZIP_SIG%" set "TK_NEED=1"
if not defined TOOLKIT set "TK_NEED=1"
if not defined TK_NEED goto :toolkit_verify

echo  [4/6] 展开随包的手套 SDK ...
"%VPY%" -c "import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall('tools')" "wheels\toolkit\glove_sdk.zip"
if exist "tools\glove_sdk\sdk\__init__.py" set "TOOLKIT=%CD%\tools\glove_sdk"
rem 只在真解出目录时才落戳，否则下次启动会自动重试（而不是永远跳过）
if not defined TOOLKIT goto :toolkit_verify
> "venv_lite\.toolkit-unpacked" echo %TK_ZIP_SIG%

:toolkit_verify
if not defined TOOLKIT goto :toolkit_missing
set "TK_SIG=%TOOLKIT%;%SIG%;%TK_ZIP_SIG%"
if not exist "venv_lite\.toolkit-ok" goto :toolkit_probe
set /p TK_STAMP=<"venv_lite\.toolkit-ok"
if "%TK_STAMP%"=="%TK_SIG%" goto :toolkit_ok

:toolkit_probe
echo  [4/6] 校验手套 SDK ...
rem 探针走 core\glove_sdk_boot 的自检入口（真实装配 + 传输/触觉两段（不含解算链）），
rem 带 --no-solver —— 查 solver_parts() 会把「极简版没有解算链」误判成
rem 「SDK 不可用」。
rem 错误原文由 Python 写文件、下面用 type 原样打 —— cmd 的 for /f 读
rem 文件会按控制台代码页做一次转换、中文全变成 ?（见 glove_sdk_boot._main）。
set "TK_ERR=%TEMP%\daq_toolkit_err.txt"
del "%TK_ERR%" >nul 2>&1
"%VPY%" -m core.glove_sdk_boot "%TK_ERR%" --no-solver >nul 2>&1
if not errorlevel 1 goto :toolkit_pass
echo  [4/6] [警告] SDK 目录在，但导入失败（多半是依赖没装全，或解释器
echo         不是 3.10 —— 本 SDK 要求 3.10）。
echo         原因:
type "%TK_ERR%" 2>nul
del "%TK_ERR%" >nul 2>&1
echo         重装依赖: start_lite.bat reinstall
echo         主程序照常启动，只是手套采集不可用。
goto :smoke_test

:toolkit_pass
del "%TEMP%\daq_toolkit_err.txt" >nul 2>&1
> "venv_lite\.toolkit-ok" echo %TK_SIG%
:toolkit_ok
echo  [4/6] 手套 SDK 就绪：采集 + 触觉降噪已具备（骨架解算不在极简版内）
goto :smoke_test

:toolkit_missing
echo  [4/6] [警告] 未找到手套 SDK 目录（tools\glove_sdk\）—— 主程序照常启动，
echo         但手套采集不可用。补装: 把 wheels\toolkit\glove_sdk.zip 放到
echo         wheels\toolkit\ 下再重跑 start_lite.bat
goto :smoke_test

rem ── [5/6] 依赖冒烟自检（能 import 即通过）──
:smoke_test
echo  [5/6] 依赖自检 ...
"%VPY%" -c "import main_lite" >nul 2>&1
if errorlevel 1 goto :errE
echo  [5/6] 依赖自检通过

rem ── [6/6] 启动 ──
echo  [6/6] 启动极简采集 ...
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
%* -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 10) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set "PY=%*"
exit /b 0

rem ────────────────────────────────────────────────────────────
rem ────────────────────────────────────────────────────────────
rem  子程序: 清掉残缺的 pip（包目录 + dist-info）
rem  ensurepip 的判据是 dist-info: 半装 pip 时包里的文件被删了、dist-info 还在，
rem  而它的版本又恰好等于 Python 自带的那只 wheel → ensurepip 判「已满足」,
rem  什么都不做（2026-09-21 在 Wine 真 cmd 里实测到）。必须先清干净再让它装回。
rem  用解释器自己删而不用 .bat 通配符: for /d 的集合一加引号就不展开通配符,
rem  不加引号又会在含空格的路径上出事 —— 这里不值得赌。
rem ────────────────────────────────────────────────────────────
:purge_pip
"%VPY%" -c "import glob,os,shutil,sys;r=os.path.dirname(os.path.dirname(sys.executable));[shutil.rmtree(p,ignore_errors=True) for pat in (r+'/Lib/site-packages/pip',r+'/Lib/site-packages/pip-[0-9]*.dist-info',r+'/lib/python*/site-packages/pip',r+'/lib/python*/site-packages/pip-[0-9]*.dist-info') for p in glob.glob(pat)]" >nul 2>&1
exit /b 0

rem  子程序: pip 半装时的离线自救
rem  ensurepip 用 Python 自带组件重装 pip，不联网；修不好返回非零，
rem  由调用方决定是重试安装还是直接报错（再不行就只能重建 venv）。
rem ────────────────────────────────────────────────────────────
:repair_pip
echo  [3/6] pip 异常，尝试离线修复 ...
call :purge_pip
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
echo  [错误 A] 未找到且自动安装 Python 3.10 失败
echo  ------------------------------------------------------------
echo   注意: 必须是 3.10，**3.11/3.12 都不行** —— 手套的串口采集走厂商 SDK，
echo         而该 SDK 按 3.10 ABI 加密，别的版本下 import 会失败。
echo   0. 需 Python 3.10，推荐手动安装 3.10.11
echo   1. 离线环境: 把 python-3.10.11-amd64.exe 放入本目录 wheels\ 后重试
echo      （由管理员用 scripts\pack_wheels.py --lite 生成）
echo   2. 手动安装: 浏览器打开官网下载 Python 3.10.11 64 位
echo      （3.10 系列只有 3.10.11 及更早带安装包）
echo      安装时务必勾选 "Add python.exe to PATH"
echo   3. 已安装但没反应: 卸载 Microsoft Store 的 Python 替身后重装
echo.
start https://www.python.org/downloads/release/python-31011/
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
echo       重跑一次 start_lite.bat 就会自动离线修好（几秒，不用重装）；仍报同一句再双击 start_lite.bat reinstall 重建 venv（约 1 分钟，不需要联网）。
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
