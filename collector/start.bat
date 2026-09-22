@echo off
rem ============================================================
rem  !!! 编码警告: 本文件必须保持 GBK(ANSI) 编码 + CRLF 行尾 !!!
rem  勿用记事本 / VSCode 另存（默认存成 UTF-8 会让 cmd 乱码并
rem  静默失败）；乱码后请从 GitLab 重新下载原文件。
rem ============================================================
rem ============================================================
rem  DAQ 数据采集系统 —— Windows 一键部署脚本
rem
rem  用法:
rem    start.bat               部署(按需) + 启动主程序
rem    start.bat reinstall     删除 venv 强制重装（出问题首选）
rem    start.bat extras        追加安装 mediapipe（裸手 3D 关键点）
rem    start.bat extras-torch  追加安装 CPU 版 torch
rem    start.bat help          打开 使用说明.md
rem
rem  本脚本自带 venv，可在已激活 conda / 其它 venv 的窗口里直接运行（互不影响）
rem  依赖安装顺序: 离线 wheels\ 包 → 阿里云镜像 → 清华镜像 → 官方源
rem  错误码 A-G 对应 使用说明.md「常见异常与解决方案」章节
rem
rem  默认已包含: D435/D405 深度相机（pyrealsense2）与手套实时骨架解算
rem  （scipy/pydantic/polars + 随包工具包，见 [4/7]）
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

set "MODE=run"
set "FORCE=0"
set "MAIN_ARGS="
if /i "%~1"=="reinstall"    goto :sub_reinstall
if /i "%~1"=="extras"       goto :sub_extras
if /i "%~1"=="extras-torch" goto :sub_extras_torch
if /i "%~1"=="help"         goto :show_help
if /i "%~1"=="guide"        goto :show_help
set "MAIN_ARGS=%*"
goto :banner

:sub_reinstall
set "FORCE=1"
goto :banner
:sub_extras
set "MODE=extras"
goto :banner
:sub_extras_torch
set "MODE=extras-torch"
goto :banner

:banner
echo.
echo  ============================================================
echo     DAQ 数据采集系统 —— 一键部署
echo  ============================================================
echo.

rem ── 错误 G: 解压层次自检 ──
if not exist "main.py"          goto :errG
if not exist "requirements.txt" goto :errG

rem ────────────────────────────────────────────────────────────
rem  [1/7] 定位 Python 3.10（SDK 加密链的 ABI 要求，不是「及以上」）
rem  为什么不接受 3.12: tools\glove_sdk\algorithm\ 是 PyArmor 按 3.10 ABI 加密的
rem  （引用了 3.11 起移除的 _PyFloat_Pack8），别的版本下 import sdk.api 直接
rem  失败 —— 那时整条手套链路都不可用，不是「少个骨架」那么轻。
rem  所以候选只认 3.10: py -3 / python 留在后面，是给「恰好就是 3.10」的机器兜底。
rem ────────────────────────────────────────────────────────────
set "PY="
set "VPY=venv\Scripts\python.exe"
if not exist "%VPY%" goto :find_py
set "PY=%VPY%"
goto :have_python

:find_py
echo  [1/7] 检查 Python 环境 ...
call :try_py py -3.10
if defined PY goto :have_python
call :try_py py -3
if defined PY goto :have_python
call :try_py python
if defined PY goto :have_python

rem ── 未检测到 → 自动下载并静默安装 Python 3.10 ──
rem  3.10.11 是 3.10 系列最后一个带 Windows 安装包的版本（3.10.12 起只有源码包）。
rem  三个镜像的路径格式与官方一致，实测都是 29037240 字节（== python.org 官方站）。
echo  [1/7] 未检测到 Python，自动下载安装 Python 3.10.11（约 28MB）...
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
echo  [1/7] 使用 wheels\ 目录内的 Python 安装包 ...
copy /y "wheels\python-%PY_VER%-amd64.exe" "%PY_EXE%" >nul
:py_run_installer
echo  [1/7] 静默安装 Python 中（请勿关闭窗口，约 1 分钟）...
"%PY_EXE%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_test=0 Include_launcher=1
del "%PY_EXE%" >nul 2>&1
call :try_py py -3.10
if defined PY goto :have_python
goto :errA

:have_python
echo  [1/7] 使用 Python: %PY%

rem ────────────────────────────────────────────────────────────
rem  [2/7] 虚拟环境 venv
rem ────────────────────────────────────────────────────────────
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
rem  SDK 在里面 import 不了。这类「起得来但功能缺一块」的失败最难排查，而且
rem  老客户机上那个 venv 正是 3.12 的，所以当成「venv 不可用」直接重建。
"%VPY%" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :venv_wrong_ver
rem 体检 ③: pip 是否完整（半装 pip 在这里现形）
"%VPY%" -m pip --version >nul 2>&1
if not errorlevel 1 goto :deps_check
echo  [2/7] 检测到 venv 的 pip 不完整，正在离线修复 ...
call :purge_pip
"%VPY%" -m ensurepip --upgrade >nul 2>&1
"%VPY%" -m pip --version >nul 2>&1
if not errorlevel 1 goto :deps_check
:venv_wrong_ver
echo  [2/7] 已有 venv 不是 Python 3.10，自动重建 —— 手套 SDK 要求 3.10。
goto :reinstall_venv_do
:venv_broken
echo  [2/7] venv 不可用（pip 缺失或解释器异常），自动重建（不需要联网，约 1 分钟）...
goto :reinstall_venv_do
:reinstall_venv
echo  [2/7] reinstall: 删除旧 venv ...
:reinstall_venv_do
rmdir /s /q "venv" 2>nul
if exist "%VPY%" goto :errC2
:make_venv
rem PY 可能正指向刚被删掉的那只 venv python —— reinstall 与「体检不过自动重建」
rem 走的都是这条路: 原脚本在这里拿已删除的解释器去建 venv，于是静默失败
rem （start.bat reinstall 一直是坏的）。清空 PY，重新走一遍解释器查找。
if /i "%PY%"=="%VPY%" set "PY="
if not defined PY goto :find_py
echo  [2/7] 创建虚拟环境 venv（首次约 1 分钟）...
rem 注意: PY 是命令（py -3.12 / python），不能加引号
%PY% -m venv "venv"
if not exist "%VPY%" goto :errC

rem ────────────────────────────────────────────────────────────
rem  [3/7] 安装依赖（requirements.txt 有变化或首次运行时）
rem ────────────────────────────────────────────────────────────
:deps_check
rem 用 requirements.txt 的 修改时间|大小 做签名（内容变了才重装）
for %%I in (requirements.txt) do set "SIG=%%~tI;%%~zI"
if "%FORCE%"=="1" goto :install_deps
if not exist "venv\.deps-ok" goto :install_deps
set /p STAMP=<"venv\.deps-ok"
if "%STAMP%"=="%SIG%" goto :toolkit_check

:install_deps
echo  [3/7] 安装依赖（首次约 3-10 分钟，之后启动秒开）...
rem ────────────────────────────────────────────────────────────
rem  这里以前有一句静默的 pip install --upgrade pip，已移除 —— 它是「半装 pip」
rem  的唯一来源: pip 升级是「先删旧、再解新」，中途被打断（关窗口 / 断网 /
rem  杀软 / 断电）就只剩一个空壳，之后每次启动都报
rem  ModuleNotFoundError: pip._internal.cli，而用户看到的是「依赖下载失败」
rem  （错误 D）—— 方向完全跑偏，而且重试多少次都一样。
rem  Python 3.10+ 自带的 pip 足够装本项目的全部依赖，故不再自动升级；
rem  确有需要请在 cmd 里手动执行（坏了的 pip 下次启动会被 [2/7] 体检修好）:
rem      venv\Scripts\python.exe -m pip install --upgrade pip
rem ────────────────────────────────────────────────────────────
call :pip_install_req
if not errorlevel 1 goto :deps_write_ok
rem 安装失败: 先确认 pip 本身还在不在（被半装 / 被杀软删是常见现场）
"%VPY%" -m pip --version >nul 2>&1
if not errorlevel 1 goto :errD
call :repair_pip
if errorlevel 1 goto :errD
echo  [3/7] pip 已修复，重试安装 ...
call :pip_install_req
if errorlevel 1 goto :errD
:deps_write_ok
> "venv\.deps-ok" echo %SIG%

rem ────────────────────────────────────────────────────────────
rem  [4/7] 手套厂商 SDK（传输 + 触觉降噪 + 骨架解算）
rem  手套链路（串口采集、设备枚举、骨架解算）整体走 tools\glove_sdk\
rem  （core\glove_sdk_boot.py 的 find_sdk_dir() 找它）。裁剪版随 wheels\ 分发，
rem  首次部署在这里展开。**缺失只警告、不拦截** —— 相机录制等都不受影响。
rem ────────────────────────────────────────────────────────────
:toolkit_check
set "TOOLKIT="
if exist "tools\glove_sdk\sdk\__init__.py" set "TOOLKIT=%CD%\tools\glove_sdk"

rem SDK 版本跟随 wheels\toolkit\glove_sdk.zip。**不能只看目录在不在**:
rem 老机器上目录早就有了而 zip 换了版本 —— 只看目录名会永远不解压，症状是
rem 「手套连上了但没反应/没有骨架」。所以按 zip 的大小+时间判断要不要覆盖解压
rem （extractall 覆盖同名文件，天然即升级）。解压与探针各自独立记账，
rem 探针因缺依赖失败时不至于每次启动都重解压 18MB。
set "TK_ZIP_SIG=none"
if exist "wheels\toolkit\glove_sdk.zip" for %%Z in ("wheels\toolkit\glove_sdk.zip") do set "TK_ZIP_SIG=%%~zZ-%%~tZ"
if not exist "wheels\toolkit\glove_sdk.zip" goto :toolkit_verify
set "TK_UNPACK="
if exist "venv\.toolkit-unpacked" set /p TK_UNPACK=<"venv\.toolkit-unpacked"
rem 要展开的两种情况: (1) zip 换了（戳不匹配）(2) 戳说"已展开"但目录不在 ——
rem 陈旧戳（误删 / 上次解压中断 / 杀软隔离）。少了 (2) 就会「zip 就在旁边，
rem 却永远不解压，还提示去开发机重打包」。
set "TK_NEED="
if not "%TK_UNPACK%"=="%TK_ZIP_SIG%" set "TK_NEED=1"
if not defined TOOLKIT set "TK_NEED=1"
if not defined TK_NEED goto :toolkit_verify

echo  [4/7] 展开随包的手套 SDK ...
"%VPY%" -c "import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall('tools')" "wheels\toolkit\glove_sdk.zip"
if exist "tools\glove_sdk\sdk\__init__.py" set "TOOLKIT=%CD%\tools\glove_sdk"
rem 只在真解出目录时才落戳，否则下次启动会自动重试（而不是永远跳过）
if not defined TOOLKIT goto :toolkit_verify
> "venv\.toolkit-unpacked" echo %TK_ZIP_SIG%

:toolkit_verify
if not defined TOOLKIT goto :toolkit_missing
rem SDK 在 != 能用: 导入期还要 scipy/pydantic/loguru，且解释器必须是 3.10
rem （加密链的 ABI）。冒烟自检(import main)查不到这些 —— 都是惰性导入，缺了
rem 要到连手套时才报错。这里按「SDK 目录 + 依赖签名 + zip 签名」缓存结论。
set "TK_SIG=%TOOLKIT%;%SIG%;%TK_ZIP_SIG%"
if not exist "venv\.toolkit-ok" goto :toolkit_probe
set /p TK_STAMP=<"venv\.toolkit-ok"
if "%TK_STAMP%"=="%TK_SIG%" goto :toolkit_ok

:toolkit_probe
echo  [4/7] 校验手套 SDK ...
rem 探针走 core\glove_sdk_boot 的自检入口（真实装配 + 传输/触觉/解算三段，
rem 不是脚本里手写 sys.path + import —— 后者能过而主程序仍会失败），
rem 错误原文由 Python 写文件、下面用 type 原样打 —— cmd 的 for /f 读
rem 文件会按控制台代码页做一次转换、中文全变成 ?（见 glove_sdk_boot._main）。
set "TK_ERR=%TEMP%\daq_toolkit_err.txt"
del "%TK_ERR%" >nul 2>&1
"%VPY%" -m core.glove_sdk_boot "%TK_ERR%" >nul 2>&1
if not errorlevel 1 goto :toolkit_pass
echo  [4/7] [警告] SDK 目录在，但导入失败（多半是依赖没装全，或解释器
echo         不是 3.10 —— 本 SDK 要求 3.10）。
echo         原因:
type "%TK_ERR%" 2>nul
del "%TK_ERR%" >nul 2>&1
echo         重装依赖: start.bat reinstall
echo         主程序照常启动，只是手套功能不可用。
goto :after_deps

:toolkit_pass
del "%TEMP%\daq_toolkit_err.txt" >nul 2>&1
> "venv\.toolkit-ok" echo %TK_SIG%
:toolkit_ok
echo  [4/7] 手套 SDK 就绪：采集 + 触觉降噪 + 骨架解算已具备
goto :after_deps

:toolkit_missing
echo  [4/7] [警告] 未找到手套 SDK 目录（tools\glove_sdk\）—— 主程序照常启动，
echo         但没有手套的采集与骨架解算。
echo         补装: 把 wheels\toolkit\glove_sdk.zip 放到 wheels\toolkit\ 下重跑，
echo         或在开发机执行 python scripts\pack_toolkit.py 生成它。
goto :after_deps

rem ────────────────────────────────────────────────────────────
:after_deps
echo  [5/7] 依赖自检 ...
"%VPY%" -c "import main" >nul 2>&1
if errorlevel 1 goto :errE
echo  [5/7] 依赖自检通过

rem ────────────────────────────────────────────────────────────
rem  [6/7] 可选功能（extras / extras-torch 子命令）
rem ────────────────────────────────────────────────────────────
if "%MODE%"=="extras"      goto :install_extras
if "%MODE%"=="extras-torch" goto :install_torch
goto :launch

:install_extras
if exist "venv\.extras-ok" goto :extras_done
rem pyrealsense2（D435/D405）自本次起随 requirements.txt 默认安装，不再属于可选包
echo  [6/7] 安装可选功能: mediapipe(裸手3D关键点) ...
call :pip_pkg "mediapipe"
if errorlevel 1 echo   [警告] mediapipe 安装失败（主程序不受影响，详见使用说明.md）
> "venv\.extras-ok" echo done
:extras_done
echo  [6/7] 可选功能安装完成。双击 start.bat 启动主程序。
pause
exit /b 0

:install_torch
if exist "venv\.torch-ok" goto :torch_done
echo  [6/7] 安装可选功能: torch CPU 版（手部关键点 RTMPose 用）...
call :pip_torch
if errorlevel 1 echo   [警告] torch 安装失败（主程序不受影响，GPU 版安装见使用说明.md）
> "venv\.torch-ok" echo done
:torch_done
echo  [6/7] 可选功能安装完成。双击 start.bat 启动主程序。
pause
exit /b 0

rem ────────────────────────────────────────────────────────────
rem  [7/7] 启动主程序
rem ────────────────────────────────────────────────────────────
:launch
echo  [7/7] 启动主程序 ...
echo.
echo  【操作指引】
echo    · 设备面板: 相机插入后约 2 秒自动出现，点击即可预览
echo    · 网格布局: 拖动分割条调整画面大小与位置
echo    · 录制: 每路相机独立的 开始/停止 按钮；正常停止=保存，异常停止=丢弃
echo    · 任务: 选择任务后开始录制；左侧可查看录制历史与回放
echo    · 上传: 录制完成后可上传服务器（配置见 data\server_config.example.json）
echo    · 语言: 设置页可切换中英文界面
echo    · 完整说明: 双击 start.bat help 或查看 使用说明.md
echo.
set "QT_QPA_PLATFORM_PLUGIN_PATH=%~dp0venv\Lib\site-packages\PyQt5\Qt5\plugins\platforms"
rem 标记本次为 start.bat 启动 → 主程序弹出使用步骤窗口（可勾选不再显示）
set "DAQ_SHOW_GUIDE=1"
"%VPY%" main.py %MAIN_ARGS%
set "EXITCODE=%errorlevel%"
if "%EXITCODE%"=="0" exit /b 0
goto :errF

rem ────────────────────────────────────────────────────────────
rem  子程序: 校验并记录可用 Python 解释器
rem  参数: 解释器命令（如 "py -3.10" / "python"）
rem ────────────────────────────────────────────────────────────
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
echo  [3/7] pip 异常，尝试离线修复 ...
call :purge_pip
"%VPY%" -m ensurepip --upgrade >nul 2>&1
"%VPY%" -m pip --version >nul 2>&1
exit /b %errorlevel%

rem ── wheels\ 目录内是否有 .whl 文件 ──
:wheels_exists
dir /b "wheels\*.whl" >nul 2>&1
exit /b %errorlevel%

rem ── 安装 requirements.txt（离线优先 → 阿里云 → 清华 → 官方）──
:pip_install_req
call :wheels_exists
if errorlevel 1 goto :req_online
echo  [3/7] 检测到 wheels\ 离线包，优先离线安装 ...
"%VPY%" -m pip install --no-index --find-links "wheels" -r requirements.txt
if not errorlevel 1 exit /b 0
echo  [3/7] 离线包安装失败，转在线安装 ...
:req_online
"%VPY%" -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
if not errorlevel 1 exit /b 0
"%VPY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if not errorlevel 1 exit /b 0
"%VPY%" -m pip install -r requirements.txt
exit /b %errorlevel%

rem ── 安装单个包（离线优先 → 阿里云 → 清华 → 官方）──
:pip_pkg
call :wheels_exists
if errorlevel 1 goto :pkg_online
"%VPY%" -m pip install --no-index --find-links "wheels" "%~1"
if not errorlevel 1 exit /b 0
echo  [6/7] 离线包缺失或失败，转在线安装 ...
:pkg_online
"%VPY%" -m pip install "%~1" -i https://mirrors.aliyun.com/pypi/simple/
if not errorlevel 1 exit /b 0
"%VPY%" -m pip install "%~1" -i https://pypi.tuna.tsinghua.edu.cn/simple
if not errorlevel 1 exit /b 0
"%VPY%" -m pip install "%~1"
exit /b %errorlevel%

rem ── 安装 CPU 版 torch（离线优先 → 阿里云 CPU 源 → 官方 CPU 源）──
:pip_torch
call :wheels_exists
if errorlevel 1 goto :torch_online
"%VPY%" -m pip install --no-index --find-links "wheels" torch
if not errorlevel 1 exit /b 0
echo  [6/7] 离线包缺失或失败，转在线安装 ...
:torch_online
"%VPY%" -m pip install torch --index-url https://mirrors.aliyun.com/pytorch-wheels/cpu/
if not errorlevel 1 exit /b 0
"%VPY%" -m pip install torch --index-url https://download.pytorch.org/whl/cpu
exit /b %errorlevel%

rem ────────────────────────────────────────────────────────────
rem  帮助 / 使用说明
rem ────────────────────────────────────────────────────────────
:show_help
echo.
echo  【常用命令】
echo    start.bat               部署并启动（默认）
echo    start.bat reinstall     删除 venv 重装（出问题首选）
echo    start.bat extras        追加安装 mediapipe / pyrealsense2
echo    start.bat extras-torch  追加安装 CPU 版 torch
echo    start.bat help          打开本文档
echo.
echo    English guide: 使用说明_EN.md
echo.
if exist "使用说明.md" start "" "使用说明.md"
if not exist "使用说明.md" echo  [警告] 未找到 使用说明.md，请从 GitLab 重新下载完整代码
pause
exit /b 0

rem ────────────────────────────────────────────────────────────
rem  异常处理（错误码 A-G，与 使用说明.md 对应）
rem ────────────────────────────────────────────────────────────
:errA
echo.
echo  [错误 A] 未能找到或自动安装 Python 3.10
echo  ------------------------------------------------------------
echo   注意: 必须是 3.10，**3.11/3.12 都不行** —— 手套 SDK 的解算核心按 3.10
echo         ABI 加密，别的版本下 import 会失败、手套功能整体不可用。
echo   0. 已有 Python 但版本不对（3.11/3.12 都算不对）？按下面装 3.10.11
echo   1. 离线环境: 将 python-3.10.11-amd64.exe 放入本目录 wheels\ 后重试
echo      （由管理员用 scripts\pack_wheels.py 生成，见使用说明.md）
echo   2. 手动安装: 即将打开官网下载页，请下载 Python 3.10.11 64 位
echo      （3.10 系列只有 3.10.11 及更早带安装包；3.10.12 起只有源码包）
echo      安装时务必勾选 "Add python.exe to PATH"
echo   3. 已安装仍报错: 电脑可能装有 Microsoft Store 版 Python 干扰，
echo      请在 设置-应用 中卸载后安装官网版
echo.
start https://www.python.org/downloads/release/python-31011/
pause
exit /b 1

:errC
echo.
echo  [错误 C] 虚拟环境创建失败
echo  ------------------------------------------------------------
echo   1. 磁盘空间不足: 清理磁盘后重试（需要约 2GB 空闲）
echo   2. 杀毒软件拦截: 将本目录加入白名单后双击 start.bat reinstall
echo   3. 路径过长: 把整个项目文件夹移到短路径（如 C:\DAQ_sdk）后重试
echo   4. 路径含特殊字符: 换一个纯英文/数字的目录重试
echo.
pause
exit /b 1

:errC2
echo.
echo  [错误 C2] 旧 venv 删除失败（文件被占用）
echo  ------------------------------------------------------------
echo   请先关闭正在运行的主程序窗口，再双击 start.bat reinstall
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
echo       重跑一次 start.bat 就会自动离线修好（几秒，不用重装）；仍报同一句再双击 start.bat reinstall 重建 venv（约 1 分钟，不需要联网）。
echo   · 报 No Python at ... / 找不到 Python
echo     → venv 是从别的机器拷来的，双击 start.bat reinstall 重建即可。
echo   · 报 Could not find a version / connection / timeout / 证书错误
echo     → 才是网络或权限问题:
echo       1. 网络问题: 检查网络后重新双击 start.bat（已下载部分会缓存，不重复下载）
echo       2. 公司网络限制/代理: 请管理员放行 pypi 镜像，或改用离线包交付
echo          （管理员运行 scripts\pack_wheels.py 生成 wheels\，见使用说明.md）
echo       3. 杀毒软件/防火墙拦截 pip: 加入白名单后重试
echo.
pause
exit /b 1

:errE
echo.
echo  [错误 E] 依赖自检失败（依赖已安装但程序无法导入）
echo  ------------------------------------------------------------
echo   1. 杀毒软件隔离了 venv 文件: 从隔离区恢复并加入白名单
echo   2. 依赖版本冲突: 双击 start.bat reinstall 重装
echo   3. 查看具体原因: 在 cmd 中运行
echo        venv\Scripts\python.exe -c "import main"
echo.
pause
exit /b 1

:errF
echo.
echo  [错误 F] 主程序启动后异常退出
echo  ------------------------------------------------------------
echo   1. 显卡驱动过旧: 更新显卡驱动后重试
echo   2. 远程桌面/虚拟机环境: 请在本地实机运行
echo   3. Qt 平台插件错误: 双击 start.bat reinstall 重装
echo   4. 摄像头无画面: Windows 设置 - 隐私和安全性 - 相机，
echo      允许应用访问相机后重启主程序
echo   5. 查看具体错误: 在 cmd 中运行
echo        venv\Scripts\python.exe main.py
echo.
pause
exit /b 1

:errG
echo.
echo  [错误 G] 未找到 main.py —— 解压层次不对
echo  ------------------------------------------------------------
echo   请保持文件夹结构完整: start.bat 与 main.py 必须在同一目录。
echo   部分解压工具会多套一层文件夹，请进入内层目录再双击 start.bat。
echo.
pause
exit /b 1
