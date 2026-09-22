#!/usr/bin/env bash
# ============================================================
#  DAQ 数据采集系统 —— Linux 一键部署脚本（与 Windows start.bat 行为一致）
#
#  用法:
#    ./start.sh               部署(按需) + 启动主程序
#    ./start.sh reinstall     删除 venv 强制重装（出问题首选）
#    ./start.sh extras        追加安装 mediapipe（裸手 3D 关键点）
#    ./start.sh extras-torch  追加安装 CPU 版 torch
#    ./start.sh help          打开 使用说明.md
#
#  依赖安装顺序: 离线 wheels/ 包 → 阿里云镜像 → 清华镜像 → 官方源
#
#  本脚本自带 venv，可在已激活 conda / 其它 venv 的终端里直接运行（互不影响）
#
#  默认已包含: D435/D405 深度相机（pyrealsense2）与手套实时骨架解算
#  （scipy/pydantic/loguru + 随包厂商 SDK，见 [4/7]）
#
#  ⚠️ 本脚本要求 Python **3.10**（不是"3.10 及以上"）: 手套 SDK 的解算核心
#     是按 3.10 ABI 加密的，别的版本下 import 即失败、整条手套链路不可用。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

# ── 环境隔离: 调用任何 Python 之前，先清掉会「串味」的外部变量 ──
# 本脚本一律用项目自带 venv，但用户可能是在 conda / 另一个 venv 里跑的，
# 或自己设过 PYTHONHOME。这些变量会穿透进我们的 venv，把解释器指到别处
# （症状: 依赖明明装了却 import 失败 / pip 装到了别的环境）。
# 直接清空并提示，不让用户去猜；只提示，不打断。
if [ -n "${VIRTUAL_ENV:-}" ]; then
    echo "[提示] 检测到已激活的虚拟环境 $VIRTUAL_ENV，本脚本不使用它（仍用项目自带 venv）"
fi
if [ -n "${CONDA_PREFIX:-}" ]; then
    echo "[提示] 检测到已激活的 conda 环境 $CONDA_PREFIX，本脚本不使用它（仍用项目自带 venv）"
fi
if [ -n "${PYTHONHOME:-}" ]; then
    echo "[提示] 已忽略外部变量 PYTHONHOME=$PYTHONHOME"
fi
if [ -n "${PYTHONPATH:-}" ]; then
    echo "[提示] 已忽略外部变量 PYTHONPATH=$PYTHONPATH"
fi
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP
# 屏蔽用户级 site-packages（~/.local/lib/pythonX.Y/...），让 venv 完全自给自足
export PYTHONNOUSERSITE=1

MODE=run
FORCE=0
case "${1:-}" in
    reinstall)    FORCE=1 ;;
    extras)       MODE=extras ;;
    extras-torch) MODE=extras-torch ;;
    help|guide)   MODE=help ;;
esac
# 非子命令参数原样透传给 main.py

if [ "$MODE" = help ]; then
    [ -f "使用说明.md" ] && xdg-open "使用说明.md" 2>/dev/null || true
    echo "常用命令: ./start.sh [reinstall|extras|extras-torch|help]"
    echo "English guide: 使用说明_EN.md"
    exit 0
fi

# ── [G] 解压层次自检 ──
if [ ! -f main.py ] || [ ! -f requirements.txt ]; then
    echo "[错误 G] 未找到 main.py —— 目录层次不对。start.sh 与 main.py 必须在同一目录。"
    exit 1
fi

# ── [1/7] 定位 Python 3.10（手套 SDK 加密链的 ABI 要求，**不是"及以上"**）──
# 为什么不接受 3.12: tools/glove_sdk/algorithm/ 是 PyArmor 按 3.10 ABI 加密的
# （pyarmor_runtime.so 引用了 3.11 起被移除的 _PyFloat_Pack8），别的版本下
# `import sdk.api` 直接 ImportError —— 届时**整条手套链路都不可用**，不是
# "少个骨架"那么轻。候选顺序因此必须 3.10 在前，且**只认 3.10**。
PY=""
PY_OK='import sys; sys.exit(0 if sys.version_info[:2] == (3, 10) else 1)'
find_python() {
    for c in python3.10 python3 python; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c "$PY_OK" 2>/dev/null; then
            PY="$(command -v "$c")"
            return 0
        fi
    done
    return 1
}

# 已有 venv 的解释器版本也要查：老客户机上那个 venv 是 3.12 的，直接拿来用会
# **静默**失去手套功能（升级后一切照旧，只有连手套时才现形）。所以这里当成
# "venv 不可用"处理，交给下面 [2/7] 用找到的 3.10 重建。
VENV_PY="venv/bin/python"
VENV_REBUILD=0
if [ -x "$VENV_PY" ]; then
    if "$VENV_PY" -c "$PY_OK" 2>/dev/null; then
        PY="$VENV_PY"
    else
        VENV_REBUILD=1
        VENV_VER="$("$VENV_PY" -V 2>&1 || echo '无法运行')"
        echo "[1/7] 已有 venv 不是 Python 3.10（$VENV_VER），自动重建 —— 手套 SDK 要求 3.10。"
    fi
fi
if [ -z "$PY" ] && ! find_python; then
    echo "[错误 A] 未找到 Python 3.10。手套 SDK 的解算核心按 3.10 ABI 加密，"
    echo "         3.11/3.12 下 import 会失败、手套功能整体不可用。请先安装:"
    echo "  Ubuntu 22.04:   sudo apt install python3.10 python3.10-venv"
    echo "  Ubuntu 24.04+:  sudo add-apt-repository ppa:deadsnakes/ppa"
    echo "                  sudo apt install python3.10 python3.10-venv"
    echo "  CentOS/RHEL:    sudo dnf install python3.10"
    echo "  conda:          conda create -n daq python=3.10"
    exit 1
fi
echo "[1/7] 使用 Python: $PY"

# ── [2/7] venv（完整性体检 + 自愈）──
# 为什么体检: venv 目录在 ≠ venv 可用。
#   ① 从别的机器拷来的 venv: python 在，但 pyvenv.cfg 指向那台机器的解释器
#      → 一跑就报找不到 Python；
#   ② pip 升级 / 断电 / 杀软打断: pip 被删到一半（目录还在、模块没了）→
#      所有 pip 命令都报 ModuleNotFoundError: pip._internal.cli。
# 这两类都不该让用户去猜: 先离线修（ensurepip 用 Python 自带组件），
# 修不动就整目录重建（同样不联网）。
purge_pip() {
    # ensurepip 的判据是 dist-info: 半装 pip 时包里的文件被删了、dist-info 还在，
    # 而它的版本又恰好等于 Python 自带的那只 wheel → ensurepip 判「已满足」，
    # 什么都不做（2026-09-21 在 Wine 真 cmd 里实测到）。必须先清干净再让它装回。
    rm -rf venv/lib/python*/site-packages/pip venv/lib/python*/site-packages/pip-[0-9]*.dist-info 2>/dev/null || true
}

venv_healthy() {
    # 版本必须**恰好** 3.10（不是 >=）: 3.12 的 venv 起得来，但手套 SDK
    # 在里面 import 不了 —— 这类"起得来但功能缺一块"的失败最难排查。
    "$VPY" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 10) else 1)' \
        >/dev/null 2>&1 || return 1
    "$VPY" -m pip --version >/dev/null 2>&1 && return 0
    echo "[2/7] 检测到 venv 的 pip 不完整，正在离线修复 ..."
    purge_pip
    "$VPY" -m ensurepip --upgrade >/dev/null 2>&1 || true
    "$VPY" -m pip --version >/dev/null 2>&1
}

make_venv() {
    # PY 可能正指向刚被删掉的那只 venv python —— reinstall 与「体检不过自动重建」
    # 走的都是这条路，原先在这里拿已删除的解释器去建 venv，于是静默失败
    # （`./start.sh reinstall` 一直是坏的，rc=127）。这时重新找一个系统 Python。
    case "$PY" in
        venv/*)
            PY=""
            find_python || {
                echo "[错误 A] 未找到可用于重建 venv 的 Python 3.10"
                exit 1
            } ;;
    esac
    echo "[2/7] 创建虚拟环境 venv（首次约 1 分钟）..."
    "$PY" -m venv venv
}

VPY="venv/bin/python"
if [ ! -x "$VPY" ]; then
    make_venv
elif [ "$FORCE" = 1 ] || [ "$VENV_REBUILD" = 1 ]; then
    if [ "$FORCE" = 1 ]; then
        echo "[2/7] reinstall: 删除旧 venv ..."
    else
        echo "[2/7] 旧 venv 解释器版本不符，重建为 Python 3.10 ..."
    fi
    rm -rf venv
    make_venv
elif ! venv_healthy; then
    echo "[2/7] venv 不可用（pip 缺失或解释器异常），自动重建（不需要联网，约 1 分钟）..."
    rm -rf venv
    make_venv
fi
if [ ! -x "$VPY" ]; then
    echo "[错误 C] 虚拟环境创建失败:"
    echo "  ① Ubuntu/Debian 先装: sudo apt install python3.10-venv"
    echo "  ② 磁盘空间不足（需约 2GB）；③ 目录写权限；④ 路径含特殊字符"
    exit 1
fi

# ── [3/7] 依赖 ──
HASH="$(md5sum requirements.txt | cut -d' ' -f1)"
NEED_INSTALL=0
if [ "$FORCE" = 1 ]; then
    NEED_INSTALL=1
elif [ ! -f venv/.deps-ok ] || [ "$(cat venv/.deps-ok)" != "$HASH" ]; then
    NEED_INSTALL=1
fi

install_req() {
    local req="$1"
    if [ -d wheels ] && ls wheels/*.whl >/dev/null 2>&1; then
        echo "[3/7] 检测到 wheels/ 离线包，优先离线安装 ..."
        "$VPY" -m pip install --no-index --find-links wheels -r "$req" && return 0
        echo "[3/7] 离线包安装失败，转在线安装 ..."
    fi
    "$VPY" -m pip install -r "$req" -i https://mirrors.aliyun.com/pypi/simple/ && return 0
    "$VPY" -m pip install -r "$req" -i https://pypi.tuna.tsinghua.edu.cn/simple && return 0
    "$VPY" -m pip install -r "$req"
}

err_d() {
    echo "[错误 D] 依赖下载/安装失败"
    echo "  先看上一屏的报错再对症处理:"
    echo "  · 报 ModuleNotFoundError: pip._internal.cli / No module named 'pip'"
    echo "    → venv 里的 pip 坏了（升级被打断 / 被杀软删了文件），不是网络问题。"
    echo "      重跑一次 ./start.sh 就会自动离线修好（几秒，不用重装）；仍报同一句再执行 ./start.sh reinstall 重建（约 1 分钟，不需要联网）。"
    echo "  · 报 Could not find a version / connection / timeout / 证书错误"
    echo "    → 才是网络问题: 检查网络；内网环境请用 scripts/pack_wheels.py 生成 wheels/ 离线包。"
    exit 1
}

if [ "$NEED_INSTALL" = 1 ]; then
    echo "[3/7] 安装依赖（首次约 3-10 分钟，之后启动秒开）..."
    # 这里以前有一句静默的 `pip install --upgrade pip`，已移除 —— 它是「半装
    # pip」的唯一来源: pip 升级是「先删旧、再解新」，中途被打断（关窗口 /
    # 断网 / 被杀软删）就只剩一个空壳，之后每次启动都报
    # ModuleNotFoundError: pip._internal.cli，而用户看到的是「依赖安装失败」
    # （错误 D）—— 方向完全跑偏，而且重试多少次都一样。
    # Python 3.10+ 自带的 pip 足够安装本项目的全部依赖，故不再自动升级；
    # 确有需要时手动执行（坏了的 pip 下次启动会被 [2/7] 体检修好）:
    #     venv/bin/python -m pip install --upgrade pip
    if ! install_req requirements.txt; then
        # 安装失败: 先确认 pip 本身还在不在（被半装 / 被杀软删是常见现场）
        if "$VPY" -m pip --version >/dev/null 2>&1; then
            err_d
        fi
        echo "[3/7] pip 异常，尝试离线修复 ..."
        purge_pip
        "$VPY" -m ensurepip --upgrade >/dev/null 2>&1 || true
        "$VPY" -m pip --version >/dev/null 2>&1 || err_d
        echo "[3/7] pip 已修复，重试安装 ..."
        install_req requirements.txt || err_d
    fi
    echo "$HASH" > venv/.deps-ok
fi

# ── [4/7] 手套厂商 SDK（传输 + 触觉降噪 + 骨架解算）──
# 手套链路（串口采集、设备枚举、骨架解算）现在整体走厂商 SDK，位于
# tools/glove_sdk/（core/glove_sdk_boot.py 的 find_sdk_dir() 找它）。裁剪版随
# wheels/ 分发，首次部署在这里展开进 tools/。**缺失只警告、不拦截** —— 相机
# 录制、夹爪等都不受影响，只少了手套相关的功能。
TOOLKIT=""
find_toolkit() {
    TOOLKIT=""
    if [ -d "tools/glove_sdk" ]; then TOOLKIT="tools/glove_sdk"; return 0; fi
    return 1
}
find_toolkit || true

# SDK 版本跟随 wheels/toolkit/glove_sdk.zip。**不能只看目录在不在**：
# 老机器上目录早就有了，而 zip 换了版本 —— 只看目录名会永远不解压，
# 症状是"手套连上了但没反应/没有骨架"。所以按 zip 的哈希判断要不要覆盖解压
# （extractall 覆盖同名文件，天然即升级）。解压与探针各自独立记账，
# 探针因缺依赖失败时不至于每次启动都重解压 18MB。
TK_UNPACK_MARK="venv/.toolkit-unpacked"
TK_ZIP_SIG="none"
if [ -f wheels/toolkit/glove_sdk.zip ]; then
    TK_ZIP_SIG="$(md5sum wheels/toolkit/glove_sdk.zip | cut -d' ' -f1)"
fi
# 要展开的两种情况：①zip 换了（戳不匹配）②戳说"已展开"但目录不在 ——
# 陈旧戳（误删 / 上次解压中断 / 杀软隔离）。少了 ② 就会「zip 就在旁边，
# 却永远不解压，还提示去开发机重打包」。
TK_NEED=""
if [ -f wheels/toolkit/glove_sdk.zip ]; then
    [ "$(cat "$TK_UNPACK_MARK" 2>/dev/null || echo '')" != "$TK_ZIP_SIG" ] \
        && TK_NEED=1
    [ -z "$TOOLKIT" ] && TK_NEED=1
fi
if [ -n "$TK_NEED" ]; then
    echo "[4/7] 展开随包的手套 SDK ..."
    # zip 里是裸的 glove_sdk/（不含 tools/ 前缀），所以解压目标是 tools/
    # —— 位置只写在这一处，zip 本身与位置无关（再挪地方不用重打包）。
    # extractall 会自己建 tools/，不必事先存在。
    "$VPY" -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall("tools")' \
        wheels/toolkit/glove_sdk.zip
    find_toolkit || true
    # 只在真解出目录时才落戳，否则下次启动会自动重试（而不是永远跳过）
    [ -n "$TOOLKIT" ] && echo "$TK_ZIP_SIG" > "$TK_UNPACK_MARK"
fi

if [ -z "$TOOLKIT" ]; then
    echo "[4/7] [警告] 未找到手套 SDK 目录（tools/glove_sdk/）—— 主程序照常启动，"
    echo "        但没有手套的采集/骨架解算。"
    echo "        补装: 把 wheels/toolkit/glove_sdk.zip 放到 wheels/toolkit/ 下重跑，"
    echo "        或在开发机执行 python scripts/pack_toolkit.py 生成它。"
else
    # SDK 在 ≠ 能用：导入期还要 scipy/pydantic/loguru，且**解释器必须是 3.10**
    # （加密链的 ABI）。冒烟自检 (import main) 查不到这些 —— 都是惰性导入，
    # 缺了要到连手套时才报错。这里按「SDK 内容 + requirements 签名」缓存结论，
    # 命中缓存就不重复跑（省 ~1s）。
    TK_MARK="venv/.toolkit-ok"
    TK_SIG="$TOOLKIT:$HASH:$TK_ZIP_SIG"
    if [ -f "$TK_MARK" ] && [ "$(cat "$TK_MARK" 2>/dev/null)" = "$TK_SIG" ]; then
        echo "[4/7] 手套 SDK 就绪：$TOOLKIT"
    else
        echo "[4/7] 校验手套 SDK（$TOOLKIT）..."
        # 探针必须走**真实**导入链（core/glove_sdk_boot 的装配逻辑），
        # 而不是手写 sys.path + import —— 后者能过而主程序仍失败。
        # 探针走 core/glove_sdk_boot 的自检入口（真实装配 + 传输/触觉/解算
        # 三段全覆盖）。**错误原文由 Python 写文件、这里原样读** —— 让 cmd
        # 去读这个文件会把中文变成 ?（Windows 侧同款代码见 .bat，原因见
        # glove_sdk_boot._main 的注释）。
        TK_ERR_FILE="venv/.toolkit-err.txt"
        rm -f "$TK_ERR_FILE"
        if "$VPY" -m core.glove_sdk_boot "$TK_ERR_FILE" >/dev/null 2>&1; then
            echo "$TK_SIG" > "$TK_MARK"
            echo "[4/7] 手套 SDK 就绪：采集 + 触觉降噪 + 骨架解算已具备"
        else
            echo "[4/7] [警告] SDK 目录在，但导入失败（多半是依赖没装全或"
            echo "        解释器版本不对 —— 本 SDK 要求 Python 3.10）。"
            echo "        原因: $(head -n 1 "$TK_ERR_FILE" 2>/dev/null)"
            echo "        重装依赖: ./start.sh reinstall"
            echo "        主程序照常启动，只是手套功能不可用。"
        fi
    fi
fi

# ── [5/7] 冒烟自检 ──
echo "[5/7] 依赖自检 ..."
if ! "$VPY" -c "import main" >/dev/null 2>&1; then
    echo "[错误 E] 依赖自检失败。查看具体原因:"
    echo "  venv/bin/python -c \"import main\""
    echo "重装: ./start.sh reinstall"
    exit 1
fi
echo "[5/7] 依赖自检通过"

# ── [6/7] 可选功能 ──
install_pkg() {
    if [ -d wheels ] && ls wheels/*.whl >/dev/null 2>&1; then
        "$VPY" -m pip install --no-index --find-links wheels "$1" && return 0
    fi
    "$VPY" -m pip install "$1" -i https://mirrors.aliyun.com/pypi/simple/ && return 0
    "$VPY" -m pip install "$1" -i https://pypi.tuna.tsinghua.edu.cn/simple && return 0
    "$VPY" -m pip install "$1"
}

case "$MODE" in
    extras)
        [ -f venv/.extras-ok ] || {
            # pyrealsense2（D435/D405）自本次起随 requirements.txt 默认安装，不再属于可选包
            echo "[6/7] 安装可选功能: mediapipe ..."
            install_pkg mediapipe || echo "  [警告] mediapipe 安装失败（主程序不受影响）"
            echo done > venv/.extras-ok
        }
        echo "[6/7] 可选功能安装完成。运行 ./start.sh 启动主程序。"
        exit 0 ;;
    extras-torch)
        [ -f venv/.torch-ok ] || {
            echo "[6/7] 安装可选功能: torch CPU 版 ..."
            install_torch() {
                if [ -d wheels ] && ls wheels/*.whl >/dev/null 2>&1; then
                    "$VPY" -m pip install --no-index --find-links wheels torch && return 0
                fi
                "$VPY" -m pip install torch --index-url https://mirrors.aliyun.com/pytorch-wheels/cpu/ && return 0
                "$VPY" -m pip install torch --index-url https://download.pytorch.org/whl/cpu
            }
            install_torch || echo "  [警告] torch 安装失败（主程序不受影响）"
            echo done > venv/.torch-ok
        }
        echo "[6/7] 可选功能安装完成。运行 ./start.sh 启动主程序。"
        exit 0 ;;
esac

# ── [7/7] 启动 ──
echo "[7/7] 启动主程序 ..."
echo
echo "【操作指引】"
echo "  · 设备面板: 相机插入后约 2 秒自动出现，点击即可预览"
echo "  · 录制: 每路相机独立的 开始/停止；正常停止=保存，异常停止=丢弃"
echo "  · 任务/回放/上传: 见主界面与 使用说明.md"
echo
# 标记本次为 start.sh 启动 → 主程序弹出使用步骤窗口（可勾选不再显示）
DAQ_SHOW_GUIDE=1 exec "$VPY" main.py "$@"
