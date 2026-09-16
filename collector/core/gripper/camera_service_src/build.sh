#!/usr/bin/env bash
# 构建 libuvc 相机服务（ksq-camera-service）与 UVC 扫描程序（discover-uvc-config），
# 产物安装到 core/gripper/native/camera_service/build/。
#
# 为什么有这份脚本：这两个程序的源码原先只在 online/（.gitignore 里），core/ 下
# 只有编译好的二进制，重建配方只活在当时那次会话里 —— 一旦二进制丢了或要改一行
# 就得从头摸。现在源码随包入库（core/gripper/camera_service_src/），配方固化于此。
#
# 依赖：gcc、cmake(>=3.16)、make、libusb-1.0 头文件；libjpeg 头文件可选（缺了
#       libuvc 会退化：不支持 MJPEG 解码，实测产物 NEEDED 里就没有 libjpeg.so.8）。
#
# 用法：
#   ./build.sh                 构建 + 安装（旧的同名二进制先备份为 .pre_rebuild_<时间戳>）
#   ./build.sh --no-install    只构建，不动 core/gripper/native/
#   ./build.sh --test          构建后跑 ctest（全部无硬件回归测试）
#   ./build.sh --jobs 4        限制并行度
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
OUT="$REPO/core/gripper/native/camera_service/build"
BUILD="$HERE/build"
SHIM="$BUILD/shim"

DO_INSTALL=1
DO_TEST=0
JOBS="$(nproc 2>/dev/null || echo 4)"

while [ $# -gt 0 ]; do
    case "$1" in
        --no-install) DO_INSTALL=0 ;;
        --test)       DO_TEST=1 ;;
        --jobs)       JOBS="${2:?--jobs 需要一个数字}"; shift ;;
        -h|--help)    sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $1（-h 看用法）" >&2; exit 2 ;;
    esac
    shift
done

die() { echo "错误: $*" >&2; exit 1; }

# ---------------------------------------------------------------- 依赖定位

# libusb 头文件：系统装了 libusb-1.0-0-dev 最省事；否则退到 conda 的 include。
# 注意头文件父目录要进 -I，因为源码里既有 <libusb-1.0/libusb.h>（sonix_flash_probe）
# 也有 <libusb.h>（libuvc 自己），两种写法都得能解析。
find_libusb_include() {
    local candidate
    for candidate in \
        "${LIBUSB_INCLUDE_PARENT:-}" \
        /usr/include \
        /usr/local/include \
        "${CONDA_PREFIX:-}/include" \
        "$HOME/miniconda3/include" \
        "$HOME/anaconda3/include"
    do
        [ -n "$candidate" ] && [ -f "$candidate/libusb-1.0/libusb.h" ] && {
            echo "$candidate"; return 0; }
    done
    return 1
}

# libusb 运行库：优先系统 multiarch 的 .so.0（只链 soname，产物不带任何本机
# RUNPATH 依赖），再退到 conda。
find_libusb_library() {
    local candidate
    for candidate in \
        "${LIBUSB_LIBRARY:-}" \
        /usr/lib/x86_64-linux-gnu/libusb-1.0.so.0 \
        /usr/lib/libusb-1.0.so.0 \
        /usr/local/lib/libusb-1.0.so.0 \
        "${CONDA_PREFIX:-}/lib/libusb-1.0.so" \
        "$HOME/miniconda3/lib/libusb-1.0.so" \
        "$HOME/anaconda3/lib/libusb-1.0.so"
    do
        [ -n "$candidate" ] && [ -e "$candidate" ] && { echo "$candidate"; return 0; }
    done
    return 1
}

LIBUSB_INC_PARENT="$(find_libusb_include)" \
    || die "找不到 libusb-1.0 头文件。装 libusb-1.0-0-dev，或用 LIBUSB_INCLUDE_PARENT=<含 libusb-1.0/ 的目录> 指定。"
LIBUSB_LIB="$(find_libusb_library)" \
    || die "找不到 libusb-1.0 运行库。装 libusb-1.0-0，或用 LIBUSB_LIBRARY=<路径> 指定。"

# libjpeg：FindJpegPkg 会先走 CMake 自带的 FindJPEG，这里预置缓存值把它钉死在
# 系统头/库上，免得 cmake 是 conda 装的时候误抓 conda 的 libjpeg。
JPEG_ARGS=()
if [ -f /usr/include/jpeglib.h ]; then
    JPEG_ARGS+=("-DJPEG_INCLUDE_DIR=/usr/include")
    for lib in /usr/lib/x86_64-linux-gnu/libjpeg.so /usr/lib/libjpeg.so; do
        [ -e "$lib" ] && { JPEG_ARGS+=("-DJPEG_LIBRARY=$lib"); break; }
    done
else
    echo "警告: 没有 /usr/include/jpeglib.h，libuvc 将编译成不支持 MJPEG 解码" >&2
fi

# ---------------------------------------------------------------- 头文件 shim
#
# 顶层的 CMakeLists 用 pkg_check_modules(LIBUSB REQUIRED ... libusb-1.0)，而系统
# 上根本没有 libusb-1.0.pc（那个文件由 -dev 包提供）。这里现造一个，指向真实头
# 文件和运行库的 soname（-l:libusb-1.0.so.0 不带 -L，所以不会给产物塞 RUNPATH）。
#
# 只有**一个** -I，而且是 shim 目录而不是真实头目录：vendored 的
# FindLibUSB.cmake 把 ${LibUSB_INCLUDE_DIRS} 不 quote 地交给
# set_target_properties，一旦 pkg-config 给出两个 -I，它就会变成三个实参报
# 「set_target_properties called with incorrect number of arguments」。
# 而两种 include 写法都得满足 —— libuvc 内部是 <libusb.h>，两个工具是
# <libusb-1.0/libusb.h> —— 所以 shim 目录里同时放 libusb-1.0/ 和 libusb.h 两个软链。

mkdir -p "$SHIM/include" "$SHIM/pkgconfig"
ln -sfn "$LIBUSB_INC_PARENT/libusb-1.0" "$SHIM/include/libusb-1.0"
ln -sfn "$LIBUSB_INC_PARENT/libusb-1.0/libusb.h" "$SHIM/include/libusb.h"

cat > "$SHIM/pkgconfig/libusb-1.0.pc" <<EOF
prefix=$SHIM
exec_prefix=\${prefix}
libdir=$(dirname "$LIBUSB_LIB")
includedir=$SHIM/include

Name: libusb-1.0
Description: C API for USB device access from Linux, Mac OS X, Windows, OpenBSD/NetBSD and Solaris userspace
Version: 1.0.27
Libs: -l:$(basename "$LIBUSB_LIB")
Cflags: -I\${includedir}
EOF

# ---------------------------------------------------------------- 配置 + 构建
#
# -DCMAKE_POLICY_VERSION_MINIMUM=3.5：libuvc 的 cmake_minimum_required 是 3.1，
#   CMake 4.x 直接拒绝，给个下限放行。
# -DLibUSB_LIBRARY=...：vendored 的 FindLibUSB.cmake 走 find_library(NAMES
#   ${LibUSB_LIBRARIES})，而系统的 libusb 只有 .so.0、没有给链接器用的 .so 软链，
#   find_library 会失败并把 "LibUSB::LibUSB-NOTFOUND" 写进依赖表 —— GNU make 会
#   把那条依赖里的第二个冒号当成静态模式规则分隔符，报出与病因毫不相干的
#   「*** 目标模式不含有"%"」。预置这个缓存变量让 find_library 整个跳过。

echo "libusb 头文件 : $LIBUSB_INC_PARENT"
echo "libusb 运行库 : $LIBUSB_LIB"
echo "构建目录      : $BUILD"

PKG_CONFIG_PATH="$SHIM/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}" \
cmake -S "$HERE" -B "$BUILD" \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DBUILD_EXAMPLE=OFF \
    -DBUILD_TEST=OFF \
    -DLibUSB_LIBRARY="$LIBUSB_LIB" \
    "${JPEG_ARGS[@]}"

cmake --build "$BUILD" -j"$JOBS"

# ksq-camera-service 的 RUNPATH 末尾会多出一个空条目（"$ORIGIN/third_party/libuvc:"）——
# CMake 把它链的 Threads::Threads 在本机算成了空目录，空条目进了 rpath 列表。RUNPATH
# 里的空条目等于让加载器去**当前工作目录**找 libuvc.so.0，线上那份是手工 gcc 链的、
# 没有这个口子。这里把那个冒号就地改成字符串终止符：长度不变、ELF 结构不动，等价于
# `chrpath -d` 的收缩改写（chrpath/patchelf 本机都没有）。只在真的出现时才改写。

strip_empty_rpath_entry() {
    local binary="$1"
    [ -f "$binary" ] || return 0
    command -v python3 >/dev/null 2>&1 || return 0
    python3 - "$binary" <<'PY'
import sys
path = sys.argv[1]
needle = b"$ORIGIN/third_party/libuvc:\x00"
fixed = b"$ORIGIN/third_party/libuvc\x00\x00"
with open(path, "rb") as handle:
    data = handle.read()
if needle not in data:
    sys.exit(0)
with open(path, "wb") as handle:
    handle.write(data.replace(needle, fixed))
print(f"  去掉 RUNPATH 空条目: {path}")
PY
}

for binary in ksq-camera-service discover-uvc-config sonix-flash-probe; do
    strip_empty_rpath_entry "$BUILD/$binary"
done

# ---------------------------------------------------------------- 校验
#
# 产物必须链上 libuvc.so.0 且 RUNPATH 为 $ORIGIN/third_party/libuvc —— 部署布局
# 靠的就是这个相对路径（ksq-camera-service 里 libusb_reset_device 是 dlsym 取的，
# 所以它自己 NEEDED 里没有 libusb，libuvc 带进来就够）。

for binary in ksq-camera-service discover-uvc-config; do
    [ -x "$BUILD/$binary" ] || die "没有产出 $BUILD/$binary"
    if command -v readelf >/dev/null 2>&1; then
        readelf -d "$BUILD/$binary" | grep -q 'libuvc.so.0' \
            || die "$binary 没有链接 libuvc.so.0"
        readelf -d "$BUILD/$binary" | grep -q '\$ORIGIN/third_party/libuvc' \
            || die "$binary 的 RUNPATH 不是 \$ORIGIN/third_party/libuvc"
        readelf -d "$BUILD/$binary" | grep -q 'libuvc:' \
            && die "$binary 的 RUNPATH 里还有空条目（strip_empty_rpath_entry 没生效）"
    fi
done
readelf -d "$BUILD/third_party/libuvc/libuvc.so.0.0.7" 2>/dev/null | grep -q libjpeg \
    && echo "libuvc: 带 libjpeg（支持 MJPEG 解码）" \
    || echo "警告: libuvc 没链上 libjpeg —— 不带 MJPEG 解码，与线上产物不一致" >&2

if [ "$DO_TEST" = 1 ]; then
    # 走 ctest 而不是点名跑某一个：以前这里写死跑 test-frame-integrity，
    # 结果 test-altsetting-ladder 虽然在 CMakeLists 里注册了 add_test，却
    # 从来没有被任何入口调起来 —— 降档梯子是真机上最难复现、最该靠测试
    # 兜住的一块，却一直没人跑。新增测试只要 add_test 就自动纳进来。
    echo "--- ctest ---"
    ( cd "$BUILD" && ctest --output-on-failure )
fi

if [ "$DO_INSTALL" = 0 ]; then
    echo "已完成构建（--no-install，未改动 core/gripper/native/）"
    exit 0
fi

# ---------------------------------------------------------------- 安装
#
# 旧的同名二进制按 .pre_rebuild_<时间戳> 备份，保留现场可比对（历史上这里存过
# pre_stall_watchdog / pre_usb_reset_escalation 两份，都是用来回退的）。

[ -d "$OUT" ] || die "安装目标不存在: $OUT"

STAMP="$(date +%Y%m%d_%H%M%S)"
for binary in ksq-camera-service discover-uvc-config sonix-flash-probe; do
    [ -f "$BUILD/$binary" ] || continue
    if [ -f "$OUT/$binary" ]; then
        cp -p "$OUT/$binary" "$OUT/$binary.pre_rebuild_$STAMP"
    fi
    cp -p "$BUILD/$binary" "$OUT/$binary"
    echo "安装 $binary → $OUT/$binary"
done

mkdir -p "$OUT/third_party/libuvc"
for name in libuvc.so libuvc.so.0 libuvc.so.0.0.7; do
    [ -e "$BUILD/third_party/libuvc/$name" ] || continue
    rm -f "$OUT/third_party/libuvc/$name"
    cp -a "$BUILD/third_party/libuvc/$name" "$OUT/third_party/libuvc/$name"
done
rm -rf "$OUT/third_party/libuvc/include"
cp -a "$BUILD/third_party/libuvc/include" "$OUT/third_party/libuvc/include"
echo "安装 libuvc → $OUT/third_party/libuvc/"

echo
echo "完成。回退：把 $OUT/<名字>.pre_rebuild_$STAMP 覆盖回去即可。"
