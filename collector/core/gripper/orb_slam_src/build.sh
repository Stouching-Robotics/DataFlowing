#!/usr/bin/env bash
# 构建 ORB-SLAM3 核心库（libORB_SLAM3.so）与 Fays 桥接二进制
# （fayssense_orb_slam_sn219_opencv48_mark_only），产物安装到
#   core/gripper/native/dist/orb_mark_only/lib/
#   core/gripper/native/dist/fays_opencv48/bin/
#
# 为什么有这份脚本：这两个程序的构建源原先只在 online/（在 .gitignore 里、不上传），
# core/ 下只有编译好的二进制，重建配方只活在会话里。四个崩溃家族（mpcpi 空指针 /
# FullInertialBA 顶点缺失 / KeyFrameCulling 空预积分 / Frame::mpLastKeyFrame 未初始化）
# 的修复**全都只写在那些源码里** —— online 一丢就只剩二进制，一行都改不动。
# 现在源码随包入库（core/gripper/orb_slam_src/），配方固化于此。
#
# 依赖：gcc/g++、cmake(>=3.16)、make、OpenCV 4.8.x（conda 包里那份开发文件）、
#       Pangolin（带 PangolinConfig.cmake 的 build 目录）、Eigen3、Boost 1.74+
#       serialization、OpenSSL libcrypto。
#       另外必须有 core/gripper/native/ 下的厂商 SDK 载荷（785MB，不入库）：本脚本
#       幂等建立 FaysSense_VI_Kit_Release/{lib,thirdparty,config} → ../native/ 的软链。
#
# 用法：
#   ./build.sh                       构建 + 安装（旧产物先备份为 .pre_rebuild_<时间戳>）
#   ./build.sh --no-install          只构建，不动 core/gripper/native/
#   ./build.sh --test                构建后跑 ctest（7 个无硬件原生回归）
#   ./build.sh --all-targets         把 dist 里其它诊断 target 也编了（默认只编生产桥接）
#   ./build.sh --target T --suffix S 换一个桥接 target / 产物后缀
#   ./build.sh --clean               先清掉 build/ 与树内生成物再编
#   ./build.sh --jobs 8              并行度（默认 4，见下）
#   KSQ_NATIVE_ROOT=<dir> ./build.sh 把安装目标换个地方（演练安装/回退分支用，
#                                    该目录下仍需有 FaysSense_VI_Kit_Release/ 与
#                                    ORB-SLAM/Thirdparty/{DBoW2,g2o}/lib/）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
# 安装目标默认是 native/。换成别的目录只为**演练**：装到 scratch 上跑一遍
# 备份→安装→回退打印，生产一个字节都不动（RPATH 断言里的 $ORB_LIB_DEST 跟着
# 这个变量走，所以换了地方照样自洽，只是产出的 RPATH 与生产不同）。
#
# ★ 那个目录必须长得像部署树（有 FaysSense_VI_Kit_Release/ 与 ORB-SLAM/Thirdparty/
#   {DBoW2,g2o}/lib/）：链接期 ld 顺着**目标 .so 自己**的 RPATH
#   $ORIGIN/../../../ORB-SLAM/Thirdparty/DBoW2/lib 去找 DBoW2/g2o，$ORIGIN 就是
#   目标目录 —— 这是第 3/3 步（见下「KSQ_ORB_LIBRARY_PATH 必须给到文件」）的
#   副作用，演练时缺了它就会得到一大片 `undefined reference to DBoW2::…`。
NATIVE="${KSQ_NATIVE_ROOT:-$REPO/core/gripper/native}"
SDK="$HERE/FaysSense_VI_Kit_Release"
ORB_SRC="$HERE/ORB-SLAM"
BRIDGE_SRC="$HERE/dist/fays_opencv48"
BUILD="$HERE/build"
ORB_OUT="$BUILD/orb-out"
DBOW2_BUILD="$BUILD/dbow2"
ORB_BUILD="$BUILD/orb"
BRIDGE_BUILD="$BUILD/bridge"
ARCH="$(uname -m)"

# 产物去向：与 core/gripper/paths.py 的 ORB_LIBRARY / FAYS_MARK_ONLY_BINARY 一致
ORB_LIB_DEST="$NATIVE/dist/orb_mark_only/lib"
BRIDGE_DEST="$NATIVE/dist/fays_opencv48/bin"

DO_INSTALL=1
DO_TEST=0
DO_CLEAN=0
ALL_TARGETS=0
# 默认 4 而不是 nproc(24)：-O3 -march=native 的大 TU 单个编译峰值能到 GB 级
# （Optimizer.cc 最狠，CMakeLists 里专门有个 KSQ_LOW_MEMORY_OPTIMIZER_BUILD 开关），
# 24 路并行在这台机上会打爆内存。要快就 --jobs 自己加。
JOBS=4
BRIDGE_TARGET="fayssense_orb_slam_sn219_opencv48"
BRIDGE_SUFFIX="_mark_only"

while [ $# -gt 0 ]; do
    case "$1" in
        --no-install)   DO_INSTALL=0 ;;
        --test)         DO_TEST=1 ;;
        --clean)        DO_CLEAN=1 ;;
        --all-targets)  ALL_TARGETS=1 ;;
        --jobs)         JOBS="${2:?--jobs 需要一个数字}"; shift ;;
        --target)       BRIDGE_TARGET="${2:?--target 需要一个 CMake target 名}"; shift ;;
        --suffix)       BRIDGE_SUFFIX="${2:?--suffix 需要一个后缀}"; shift ;;
        -h|--help)      sed -n '2,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $1（-h 看用法）" >&2; exit 2 ;;
    esac
    shift
done

die() { echo "错误: $*" >&2; exit 1; }

# dist/fays_opencv48/CMakeLists.txt:16 是 "$ENV{KSQ_ORB_ROOT}"，环境变量会绕过它自己
# 的默认值；FAYS_SDK_ROOT 同理属于「宁可清干净」的一类。这两个一旦从外部串进来，
# 编出来的就是另一棵树上的库，而且 RPATH 看着还挺正常。
unset KSQ_ORB_ROOT FAYS_SDK_ROOT

# ---------------------------------------------------------------- SDK 软链
#
# native/ 里那份 SDK 只有 config/ lib/ thirdparty/（镜像时刻意裁掉了 include/），
# 而每个 target 都要 -I${FAYS_SDK_ROOT}/include、桥接源要 fays_vikit.h —— 所以
# include/ orb_slam/ tools/ 是真目录（随包入库，1.3MB 厂商文件），三个大家伙走软链。
# 软链路径必须让 SDK 目录仍叫 <KSQ_ROOT>/FaysSense_VI_Kit_Release，否则
# dist 那份 CMakeLists 产出的 $ORIGIN/../../../FaysSense_VI_Kit_Release/... 在
# 部署树里解析不到（而且不报错，只是启动失败）。

[ -d "$NATIVE/FaysSense_VI_Kit_Release/thirdparty" ] \
    || die "找不到 $NATIVE/FaysSense_VI_Kit_Release/thirdparty。夹爪原生载荷（785MB，不入库）必须在位，见 core/gripper/native/README。"

for name in lib thirdparty config; do
    link="$SDK/$name"
    target="../../native/FaysSense_VI_Kit_Release/$name"
    if [ -L "$link" ]; then
        [ "$(readlink "$link")" = "$target" ] || die "$link 是个指向别处的软链：$(readlink "$link")"
    elif [ -e "$link" ]; then
        die "$link 是真实目录而不是软链。厂商大件不入库，请先把它挪走（或改成本脚本建的链）。"
    else
        ln -s "$target" "$link"
        echo "建软链 $name → $target"
    fi
done

# ---------------------------------------------------------------- 依赖定位

# OpenCV：构建机上没有 /usr/local/lib/cmake/opencv4（那个是 CMakeLists 里的默认值，
# 纯粹是为了让别的机器能覆盖），开发文件只有 conda 包目录里那份 4.8.1。系统
# /usr/lib/x86_64-linux-gnu/cmake/opencv4 是 4.5，必须排掉 —— dist 的 CMakeLists
# 会以 "requires OpenCV 4.8.x" 直接 FATAL_ERROR。
opencv_version() {
    local dir="$1" raw
    for f in "$dir/OpenCVConfig-version.cmake" "$dir/OpenCVConfig.cmake"; do
        [ -f "$f" ] || continue
        raw="$(sed -n 's/^set(OpenCV_VERSION "\([0-9.]*\)")$/\1/p;s/^set(OpenCV_VERSION \([0-9.]*\))$/\1/p' "$f" | head -1)"
        [ -n "$raw" ] && { echo "$raw"; return 0; }
    done
    return 1
}

find_opencv48() {
    local dir
    for dir in "${OPENCV48_DIR:-}" \
        "$HOME"/miniconda3/pkgs/libopencv-4.8*/lib/cmake/opencv4 \
        "$HOME"/anaconda3/pkgs/libopencv-4.8*/lib/cmake/opencv4 \
        "$HOME"/miniconda3/lib/cmake/opencv4 \
        /usr/local/lib/cmake/opencv4
    do
        [ -n "$dir" ] && [ -f "$dir/OpenCVConfig.cmake" ] || continue
        case "$(opencv_version "$dir" || echo '?')" in
            4.8*) echo "$dir"; return 0 ;;
        esac
    done
    return 1
}

# Pangolin：现役机器上是源码 build 出来的 build_orb 目录（只有它带 PangolinConfig.cmake）。
# 用户注册表 ~/.cmake/packages/Pangolin 一般也指得到，但显式给 -D 更稳。
find_pangolin() {
    local dir
    for dir in "${PANGOLIN_DIR:-}" \
        "$HOME/桌面/workspace/vio/third_party/Pangolin/build_orb" \
        /usr/local/lib/cmake/Pangolin \
        /usr/lib/x86_64-linux-gnu/cmake/Pangolin
    do
        [ -n "$dir" ] && [ -f "$dir/PangolinConfig.cmake" ] && { echo "$dir"; return 0; }
    done
    return 1
}

# libcrypto：find_library(NAMES crypto libcrypto.so.3) 只在装了 libssl-dev 时才有
# libcrypto.so 链接名。用现役那份配方的取值（dev 软链优先，退回 .so.3）。
find_crypto() {
    local path
    for path in "${KSQ_CRYPTO_LIBRARY:-}" \
        /usr/lib/x86_64-linux-gnu/libcrypto.so \
        /usr/lib/x86_64-linux-gnu/libcrypto.so.3 \
        /usr/lib/libcrypto.so
    do
        [ -n "$path" ] && [ -e "$path" ] && { echo "$path"; return 0; }
    done
    return 1
}

OPENCV_DIR_48="$(find_opencv48)" || die "找不到 OpenCV 4.8.x 的 CMake 包目录。用 OPENCV48_DIR=<含 OpenCVConfig.cmake 的目录> 指定（conda 的 pkgs/libopencv-4.8*/lib/cmake/opencv4）。"
PANGOLIN="$(find_pangolin)" || die "找不到 PangolinConfig.cmake。用 PANGOLIN_DIR=<Pangolin 的 build 目录> 指定。"
CRYPTO="$(find_crypto)" || die "找不到 libcrypto。装 libssl-dev，或用 KSQ_CRYPTO_LIBRARY=<路径> 指定。"

# ---------------------------------------------------------------- 脏缓存闸门
#
# FAYS_SDK_ROOT / ORB_ROOT / KSQ_ORB_LIBRARY_PATH / FAYS_BRIDGE_SOURCE / OpenCV_DIR /
# Pangolin_DIR 全是 CACHE 变量。一个从 online/ 拷过来的 CMakeCache.txt 会把旧绝对
# 路径原样带进新二进制 —— 编得过、跑得起来、RPATH 却不是部署树的那条。本脚本把
# 上面每个路径都用 -D 显式钉死（这是主要防线），下面这层是第二道网：认标记 + 搜
# 缓存里残留的 online/ 路径。

assert_cache_is_local() {
    # 三行分开写：local 的参数是**先整体展开、再逐个赋值**的，
    # `local dir="$1" cache="$dir/..."` 在 set -u 下会报 dir 未绑定。
    local dir="$1"
    local label="$2"
    local cache="$dir/CMakeCache.txt"
    local away
    [ -f "$cache" ] || return 0
    if [ ! -f "$dir/.ksq-source" ] || [ "$(cat "$dir/.ksq-source")" != "$HERE" ]; then
        die "$label 的构建目录里有个来路不明的 $cache。
     缓存变量会把旧绝对路径静默带进产物。先跑 ./build.sh --clean。"
    fi
    away="$(command grep -oE '^[A-Za-z_]+:(PATH|FILEPATH|STRING)=[^;]*/(online|online\.off)/[^;]*' "$cache" | head -3 || true)"
    if [ -n "$away" ]; then
        die "$label 的缓存里还有指向 online/ 的路径：
$away
     先跑 ./build.sh --clean。"
    fi
}

mark_cache_local() { printf '%s' "$HERE" > "$1/.ksq-source"; }

# ---------------------------------------------------------------- 清理

if [ "$DO_CLEAN" = 1 ]; then
    rm -rf "$BUILD"
    # 树内生成物：DBoW2/g2o 的 .so 与 g2o 的 configure_file 产物。它们由本脚本重建，
    # 留着会让「清干净了没」这件事说不清。
    rm -rf "$ORB_SRC/Thirdparty/DBoW2/lib" "$ORB_SRC/Thirdparty/g2o/lib" \
           "$ORB_SRC/Thirdparty/g2o/config.h"
    rm -rf "$BRIDGE_SRC/bin"
    echo "已清理 build/ 与树内生成物"
fi

mkdir -p "$BUILD" "$ORB_OUT" "$DBOW2_BUILD" "$ORB_BUILD" "$BRIDGE_BUILD"

echo "OpenCV 4.8   : $OPENCV_DIR_48 ($(opencv_version "$OPENCV_DIR_48"))"
echo "Pangolin     : $PANGOLIN"
echo "libcrypto    : $CRYPTO"
echo "构建目录     : $BUILD (--jobs $JOBS)"

# ---------------------------------------------------------------- 1/3 DBoW2
#
# 上游 ORB-SLAM/CMakeLists.txt 只 add_subdirectory(Thirdparty/g2o)，DBoW2 是**裸文件
# 路径**链接（${KSQ_ORB_THIRDPARTY_LIBRARY_ROOT}/Thirdparty/DBoW2/lib/libDBoW2.so），
# 树内没有生产者 —— 必须单独编一次。out-of-source 编，但它的 CMakeLists:40 用
# LIBRARY_OUTPUT_PATH ${PROJECT_SOURCE_DIR}/lib 把产物写回源码树，所以结果正好落在
# 上面那条链接路径上，也正是 CMakeLists 给回归测试准备的 KSQ_TEST_RUNTIME_RPATH。
# cmake_minimum_required(2.8) 在 CMake 4.x 下要放行（独立配置，得自己带一份）。

assert_cache_is_local "$DBOW2_BUILD" "DBoW2"
echo "--- 1/3 构建 DBoW2 ---"
cmake -S "$ORB_SRC/Thirdparty/DBoW2" -B "$DBOW2_BUILD" \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DCMAKE_BUILD_TYPE=Release \
    -DOpenCV_DIR="$OPENCV_DIR_48"
mark_cache_local "$DBOW2_BUILD"
cmake --build "$DBOW2_BUILD" -j"$JOBS"

[ -f "$ORB_SRC/Thirdparty/DBoW2/lib/libDBoW2.so" ] \
    || die "DBoW2 没产出 $ORB_SRC/Thirdparty/DBoW2/lib/libDBoW2.so"

# ---------------------------------------------------------------- 2/3 核心库
#
# 配方取自现役那颗 libORB_SLAM3.so 的构建缓存（c21808e2 / BuildID efd49c48），逐项对齐：
#   -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON  产物一出来就带部署 RPATH（本机没有 patchelf/chrpath）
#   -DCMAKE_INSTALL_RPATH=...            $ORIGIN/../../../ORB-SLAM/... 是相对**部署**位置
#                                        native/dist/orb_mark_only/lib 算的，正好指向
#                                        native/ORB-SLAM/Thirdparty/{DBoW2,g2o}/lib
#   -DKSQ_ORB_THIRDPARTY_LIBRARY_ROOT    链接期去这里找 libDBoW2.so / libg2o.so（树内的）
# g2o 由 ORB-SLAM/CMakeLists.txt:135 的 add_subdirectory 带编，它自己的 CMakeLists:32
# 把 CMAKE_LIBRARY_OUTPUT_DIRECTORY 覆写成 ${g2o_SOURCE_DIR}/lib（源码树内），
# 不会被上面的 KSQ_ORB_LIBRARY_OUTPUT_DIRECTORY 改道。

assert_cache_is_local "$ORB_BUILD" "核心库"
echo "--- 2/3 构建 ORB-SLAM3 核心库 ---"
cmake -S "$ORB_SRC" -B "$ORB_BUILD" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DOpenCV_DIR="$OPENCV_DIR_48" \
    -DPangolin_DIR="$PANGOLIN" \
    -DKSQ_CRYPTO_LIBRARY="$CRYPTO" \
    -DKSQ_ORB_LIBRARY_OUTPUT_DIRECTORY="$ORB_OUT" \
    -DKSQ_ORB_THIRDPARTY_LIBRARY_ROOT="$ORB_SRC" \
    -DKSQ_BUILD_RUNTIME_TESTS=ON \
    -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON \
    -DCMAKE_INSTALL_RPATH='/usr/local/lib:$ORIGIN/../../../ORB-SLAM/Thirdparty/DBoW2/lib:$ORIGIN/../../../ORB-SLAM/Thirdparty/g2o/lib:$ORIGIN'
mark_cache_local "$ORB_BUILD"
cmake --build "$ORB_BUILD" -j"$JOBS"

[ -f "$ORB_OUT/libORB_SLAM3.so" ] || die "核心库没产出 $ORB_OUT/libORB_SLAM3.so"

# 部署 RPATH 的逐字符断言：这段一旦和现役不一样，桥接在真机上会找不到 Pangolin/OpenCV/
# DBoW2 —— 而且**不报错**，只是起不来。所以在这里就拦住。
EXPECTED_ORB_RPATH='/usr/local/lib:$ORIGIN/../../../ORB-SLAM/Thirdparty/DBoW2/lib:$ORIGIN/../../../ORB-SLAM/Thirdparty/g2o/lib:$ORIGIN'
if command -v readelf >/dev/null 2>&1; then
    got="$(readelf -d "$ORB_OUT/libORB_SLAM3.so" | sed -n 's/.*(R\(UN\)\?PATH)[^[]*\[\(.*\)\].*/\2/p' | head -1)"
    [ "$got" = "$EXPECTED_ORB_RPATH" ] \
        || die "核心库的 RPATH 不是部署值。
     期望: $EXPECTED_ORB_RPATH
     实得: $got"
fi

# 核心库先落盘，桥接才能链到**这次编的**那一颗（链接期 ld 会去读它解析符号）。
if [ "$DO_INSTALL" = 1 ]; then
    STAMP="$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$ORB_LIB_DEST"
    if [ -f "$ORB_LIB_DEST/libORB_SLAM3.so" ]; then
        cp -p "$ORB_LIB_DEST/libORB_SLAM3.so" "$ORB_LIB_DEST/libORB_SLAM3.so.pre_rebuild_$STAMP"
    fi
    cp -p "$ORB_OUT/libORB_SLAM3.so" "$ORB_LIB_DEST/libORB_SLAM3.so"
    echo "安装 libORB_SLAM3.so → $ORB_LIB_DEST/"
fi

# ---------------------------------------------------------------- 3/3 桥接
#
# KSQ_ORB_LIBRARY_PATH 必须给到**文件**：CMakeLists 接过去后做
# get_filename_component(... DIRECTORY) 取它的目录进 RPATH，给目录的话末尾的 /lib
# 会被当成文件名吃掉。指向 native 的安装位置（而不是 build 树）是刻意的 —— 桥接的
# RPATH 里那一条就是这个绝对路径，现役二进制里也是它。--no-install 时不动它，
# 用的是现役那颗库，RPATH 照样和线上一致。
#
# KSQ_FAYS_BINARY_SUFFIX 决定产物名（OUTPUT_NAME 拼后缀），生产要 _mark_only。
#
# 两条 -Wl,-rpath-link 是**必需**的，不是保险：conda 的 libopencv_imgcodecs.so.4.8.1
# 自己带着一堆未定义的 `jas_*`（它的第三方依赖只以 NEEDED 形式记在 .so 里，
# OpenCVConfig.cmake 的 IMPORTED 目标并不列），链接期 ld 找不到 libjasper.so.7 就报
# 「undefined reference to `jas_image_create'」一大片。rpath-link 只是给 ld 一条
# 解析 DSO 传递依赖的搜索路径：**不进 RUNPATH、不进 NEEDED**（现役二进制的 NEEDED
# 里没有 jasper，实测），运行时照旧由 orb48_env/lib 那份 libjasper.so.7 兜。两处
# 取值照抄现役的构建缓存（/tmp/ksq-bridge-build/CMakeCache.txt）。

ORB48_ENV="$NATIVE/FaysSense_VI_Kit_Release/thirdparty/orb48_env"
OPENCV_PKG_LIB="$(cd "$OPENCV_DIR_48/../.." && pwd)"     # <pkg>/lib/cmake/opencv4 → <pkg>/lib
[ -e "$ORB48_ENV/lib/libjasper.so.7" ] || echo "警告: $ORB48_ENV/lib 下没有 libjasper.so.7，链接可能失败" >&2

assert_cache_is_local "$BRIDGE_BUILD" "桥接"
echo "--- 3/3 构建桥接 $BRIDGE_TARGET$BRIDGE_SUFFIX ---"
cmake -S "$BRIDGE_SRC" -B "$BRIDGE_BUILD" \
    -DCMAKE_BUILD_TYPE=Release \
    -DOpenCV_DIR="$OPENCV_DIR_48" \
    -DPangolin_DIR="$PANGOLIN" \
    -DORB_ROOT="$ORB_SRC" \
    -DFAYS_SDK_ROOT="$SDK" \
    -DFAYS_BRIDGE_SOURCE="$ORB_SRC/Examples/fays/fayssense_orb_slam.cc" \
    -DKSQ_ORB_LIBRARY_PATH="$ORB_LIB_DEST/libORB_SLAM3.so" \
    -DKSQ_FAYS_BINARY_SUFFIX="$BRIDGE_SUFFIX" \
    -DCMAKE_EXE_LINKER_FLAGS="-Wl,-rpath-link,$ORB48_ENV/lib -Wl,-rpath-link,$OPENCV_PKG_LIB"
mark_cache_local "$BRIDGE_BUILD"
if [ "$ALL_TARGETS" = 1 ]; then
    cmake --build "$BRIDGE_BUILD" -j"$JOBS"
else
    cmake --build "$BRIDGE_BUILD" -j"$JOBS" --target "$BRIDGE_TARGET"
fi

BRIDGE_BIN="$BRIDGE_SRC/bin/$BRIDGE_TARGET$BRIDGE_SUFFIX"
[ -x "$BRIDGE_BIN" ] || die "桥接没产出 $BRIDGE_BIN"

# 成败判据：桥接的 RPATH 必须与现役逐字符相同（六条，顺序也要对）。
EXPECTED_BRIDGE_RPATH="/usr/local/lib:\$ORIGIN/../../../FaysSense_VI_Kit_Release/lib/fays_atrak/$ARCH/Release:\$ORIGIN/../../../FaysSense_VI_Kit_Release/thirdparty/ft602-linux-$ARCH:$ORB_LIB_DEST:\$ORIGIN/../../../ORB-SLAM/Thirdparty/DBoW2/lib:\$ORIGIN/../../../ORB-SLAM/Thirdparty/g2o/lib"
if command -v readelf >/dev/null 2>&1; then
    got="$(readelf -d "$BRIDGE_BIN" | sed -n 's/.*(R\(UN\)\?PATH)[^[]*\[\(.*\)\].*/\2/p' | head -1)"
    [ "$got" = "$EXPECTED_BRIDGE_RPATH" ] \
        || die "$BRIDGE_TARGET$BRIDGE_SUFFIX 的 RPATH 与现役不同。
     期望: $EXPECTED_BRIDGE_RPATH
     实得: $got"
    echo "RPATH 与现役一致 ✓"
fi

# ---------------------------------------------------------------- ctest

if [ "$DO_TEST" = 1 ]; then
    # 走 ctest 而不是点名跑某一个：7 个原生回归里新增的只要 add_test 就自动纳进来
    # （camera_service 那边就吃过「注册了却从没被跑过」的亏）。
    echo "--- ctest ---"
    ( cd "$ORB_BUILD" && ctest --output-on-failure )
fi

if [ "$DO_INSTALL" = 0 ]; then
    echo
    echo "已完成构建（--no-install，未改动 core/gripper/native/）"
    echo "产物: $BRIDGE_BIN"
    exit 0
fi

# ---------------------------------------------------------------- 安装（桥接）

mkdir -p "$BRIDGE_DEST"
if [ -f "$BRIDGE_DEST/$BRIDGE_TARGET$BRIDGE_SUFFIX" ]; then
    cp -p "$BRIDGE_DEST/$BRIDGE_TARGET$BRIDGE_SUFFIX" \
          "$BRIDGE_DEST/$BRIDGE_TARGET$BRIDGE_SUFFIX.pre_rebuild_$STAMP"
fi
cp -p "$BRIDGE_BIN" "$BRIDGE_DEST/$BRIDGE_TARGET$BRIDGE_SUFFIX"
echo "安装 $BRIDGE_TARGET$BRIDGE_SUFFIX → $BRIDGE_DEST/"

echo
echo "完成。回退："
echo "  cp -a $ORB_LIB_DEST/libORB_SLAM3.so.pre_rebuild_$STAMP $ORB_LIB_DEST/libORB_SLAM3.so"
echo "  cp -a $BRIDGE_DEST/$BRIDGE_TARGET$BRIDGE_SUFFIX.pre_rebuild_$STAMP $BRIDGE_DEST/$BRIDGE_TARGET$BRIDGE_SUFFIX"
echo
echo "提醒：lite_package/ 里的原生载荷不由任何脚本生成（pack_lite.py 明确不动"
echo "core/gripper/native/），换库后要手工同步："
echo "  lite_package/**/dist/orb_mark_only/lib/libORB_SLAM3.so"
echo "  lite_package/**/dist/fays_opencv48/bin/$BRIDGE_TARGET$BRIDGE_SUFFIX"
