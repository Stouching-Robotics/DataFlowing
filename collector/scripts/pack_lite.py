"""极简采集版发布包收集脚本 —— 把 lite 运行时闭包收集到一个目录。

只收集 start_lite.bat 真正需要的最小集合（约 1.6 MB 代码）与夹爪原生
载荷（Linux 包，约 440 MB），不拷贝 venv/venv_lite/data/wheels
（--with-wheels 除外）、tools 下的 demo/其它测试、
stouch_glove_toolkit-*/ 等与 lite 无关的内容。

代码文件以仓库根为源；start_lite.bat/.sh 与 使用说明_lite.md 是分发壳。
2026-09-16 起三个分发壳全部住在**仓库根**（与 main_lite.py、
requirements-lite.txt 同级 —— 这几个齐了根目录本身就是可直接运行的 lite
部署目录，根上已有 venv_lite/），由 _dist_shell_source 优先从那里取，
复制进包目录。

--force 只重建代码部分（main_lite/requirements-lite/config/core/ui/tools），
venv_lite/、wheels/、data/、分发壳与 core/gripper/native（470MB 载荷）
原样保留 —— lite_package 可同时当"发布模板"和"本机测试部署目录"用
（在包内跑过 start_lite.sh 装的环境不会被冲掉）。

收集完成后自动做导入自检：在目标目录内 `import main_lite`，缺任何被闭包
引用的模块都会当场报错（与 start_lite.bat 的错误 E 同口径）；Linux 包再
断言夹爪闭包（`core.gripper.bridge`）与 `gripper_resources_available()`。

两个目标平台收的**夹爪 Python 代码是同一份**（core/gripper/**、core/
gripper_codec.py、core/s80m_manager.py），差别只在平台绑定件：Windows 包
不收 core/gripper/native/（ELF 原生栈）与 core/gripper/sightac_sdk/（pyarmor
.so 与 libSonixCamera.so 也是 ELF），并硬断言包内零 ELF。缺这些时
gripper_resources_available() 为假 → 扫描不到夹爪（设备组框还在、里面是
空的，属预期降级）。于是
「Windows 包」≡「Linux 包去掉载荷」，后者在 Linux 开发机上可直接造出来验证。

闭包清单与 tools/tests/lite_import_guard_test.py 的 WHITELIST 一致，
外加 egodata_writer/d435_camera 实际 import 的 core.calibration。

用法:
    python scripts/pack_lite.py                收集到 lite_package/（本机平台）
    python scripts/pack_lite.py --target windows   收集 Windows 包（代码同源，不含平台绑定件）
    python scripts/pack_lite.py --out D:/lite  指定目录
    python scripts/pack_lite.py --with-wheels  连同 wheels/ 离线依赖包一起
    python scripts/pack_lite.py --force        重建代码部分（保留环境与载荷）
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 分发壳的兜底目录（三个壳 2026-09-16 都搬到仓库根了，这里只作回落）
DIST_SOURCE = os.path.join(ROOT, "lite_package")

# 代码文件（仓库根为源，收集时拷入包目录）
CODE_FILES = [
    "main_lite.py",
    "requirements-lite.txt",
]

# 分发壳文件（仓库根优先，找不到再回落 lite_package/；均原样复制）
DIST_FILES = [
    "start_lite.bat",
    "start_lite.sh",
    "使用说明_lite.md",
]


def _dist_shell_source(rel: str):
    """分发壳的源路径：仓库根优先，其次 lite_package/；都没有返回 None。

    三个壳 2026-09-16 都搬到了仓库根（与 main_lite.py 同级，使根目录直接
    可运行）；lite_package/ 只作回落，服务老布局的目录。
    """
    for base in (ROOT, DIST_SOURCE):
        path = os.path.join(base, rel)
        if os.path.isfile(path):
            return path
    return None


# 夹爪载荷：Linux 包整目录收集，Windows 包只收代码（见 _PLATFORM_BOUND）。
GRIPPER_REL = "core/gripper"
GRIPPER_PAYLOAD_REL = "core/gripper/native"      # --force 时原样保留

# 与平台绑定的原生件：Windows 包一律不收。
#   native/       —— 整套 ELF x86-64 原生栈（460MB）
#   sightac_sdk/  —— pyarmor 运行时 .so 与 libSonixCamera.so 同样是 ELF
# 缺它们时 paths.gripper_resources_available() 为假 → 扫描不到夹爪（设备组
# 框照建、里面空着），
# 且 core/gripper/*.py 在 Windows 上因顶层 `import fcntl` 本就导入不了
# （双重保险）。**Python 代码两个包都收**：一份源码两个包，差异只在载荷，
# 于是「Windows 包」= 「Linux 包去掉载荷」，这种形态在 Linux 开发机上可以
# 直接造出来验证（mv 走 native/ 即可），不必真去 Windows 上撞。
_PLATFORM_BOUND_NAMES = ("native", "sightac_sdk")

# 载荷里可以剪掉的（≈33MB）：备份二进制、静态库、缓存。
# ★ 绝不能粗剪：native/ORB-SLAM 必须留着 —— 桥接二进制的 RUNPATH 里有
#   $ORIGIN/../../../ORB-SLAM/Thirdparty/{DBoW2,g2o}/lib（实测 readelf），
#   剪掉会让 libDBoW2/libg2o 在客户机上找不到；ORBvoc.txt（139MB）与
#   FaysSense_VI_Kit_Release/ 同理全是运行时资产。
_PAYLOAD_SKIP_SUFFIX = (".pyc", ".a")
_PAYLOAD_SKIP_INFIX = (".pre_", ".bak_")     # 现场备份：xxx.pre_rebuild_2026...
# 构建源，不是运行时资产：两个包都不收（和 camera_service_src 同理）。
# orb_slam_src/ 是 2026-09-17 从 online/ 搬进 core/ 的 ORB-SLAM 真源，只在开发机
# 上 build.sh 用；运行时一切路径都由 core/gripper/paths.py 指到 native/。
# ★ 必须排除而不是「反正没 ELF」：它下面有 build/、dist/fays_opencv48/bin/、
#   ORB-SLAM/{lib,Thirdparty/*/lib}/ 这些树内构建产物（全是 ELF），收进 Windows
#   包会直接撞上下面的「零 ELF」硬断言 sys.exit(1)。
_PAYLOAD_SKIP_NAMES = ("__pycache__", "camera_service_src", "orb_slam_src")


def _gripper_ignore_factory(src_root: str, windows: bool,
                            keep_existing_native: bool):
    """core/gripper 整目录收集的排除表（见上方注释）。

    windows: 连平台绑定件一起排（Windows 包）
    keep_existing_native: 目标包已有 native/（--force 重打包）时整棵跳过 ——
        copytree 遇到已存在的同名软链会直接抛 File exists（symlinks=True 下
        的 os.symlink 不覆盖），实测第二次打包必炸。
    """
    def _ignore(dir_path: str, names: list):
        ignored = set()
        for name in names:
            if name in _PAYLOAD_SKIP_NAMES:
                ignored.add(name)
            elif name.endswith(_PAYLOAD_SKIP_SUFFIX) or any(
                    s in name for s in _PAYLOAD_SKIP_INFIX):
                ignored.add(name)
        if os.path.abspath(dir_path) == os.path.abspath(src_root):
            if windows:
                ignored.update(_PLATFORM_BOUND_NAMES)
            if keep_existing_native and not windows:
                ignored.add("native")
        return ignored
    return _ignore


# 整目录拷贝（core.glove_usb 是包，内部有 __init__.py 与互引模块）。
# core.gripper 的排除表按目标平台现算（见 _gripper_ignore_factory），
# 且必须 symlinks=True —— 载荷里有 76 个相对软链，展开会让体积 468MB→800MB。
WHOLE_DIRS = ["core/glove_usb", GRIPPER_REL]

# 单文件拷贝：目录 → 文件列表（只收 lite 闭包，主程序 UI/重依赖模块不带）
MODULE_FILES = {
    "ui": ["__init__.py", "lite_window.py"],
    "config": ["__init__.py", "settings.py", "i18n.py"],
    "core": [
        "__init__.py",
        "pipeline.py", "egodata_writer.py", "encoder_probe.py",
        "depth_codec.py", "d435_manager.py", "stereo_depth.py",
        "d435_camera.py", "camera.py", "device_detector.py",
        "ble_engine.py", "usb_glove_engine.py", "uploader.py",
        "api_client.py", "database.py", "helpers.py",
        # egodata_writer.py:41 / d435_camera.py:447 实际 import
        "calibration.py",
        # 夹爪闭包（2026-09-16 并入 lite）：力矩阵编码的家（主程序
        # ui.main_window 只是 re-export —— 契约不能存两份）
        "gripper_codec.py",
        # core/gripper/bridge.py:39 顶层 import 它的双目丢帧窗口常量
        "s80m_manager.py",
        # core/pipeline.py:19 顶层 import 的帧空洞看门狗（录制侧与
        # tools/audit_frame_gaps.py 审计脚本共用同一份口径）
        "frame_gap.py",
        # core/pipeline.py / core/egodata_writer.py 顶层 import 的收尾
        # 分段计时（v1.3.11 L0-1；漏了它 lite 一 import 就 ModuleNotFoundError）
        "timing.py",
        # main_lite.py 顶层 import 的启动环境守卫（用错解释器时给出启动
        # 指引再退出；零第三方依赖，必须在包里）
        "startup_guard.py",
    ],
}

# 自检测试（客户机可选跑：无硬件验证导入隔离与采集链）
SELFTEST_FILES = [
    "tools/tests/lite_import_guard_test.py",
    "tools/tests/lite_smoke_test.py",
    "tools/tests/lite_gripper_smoke_test.py",
]

# 随包工具：只有 Linux 包带 —— 它要驱动 native/ 里的厂商二进制，
# 在 Windows 上无处落脚。夹爪的「重新读取出厂标定」在 lite 里**不做 UI**，
# 命令行就是唯一入口，所以这个工具必须在包里，否则那条路在纯 lite 机器上
# 走不通（它顶层只 import stdlib + core.gripper.*，venv_lite 的依赖足够）。
TOOL_FILES_LINUX = [
    "tools/import_gripper_calibration.py",
]


def _force_remove(path: str, keep: str) -> None:
    """删除 path；但 keep 那棵树（连同其本身）一个字节都不动。"""
    path, keep = os.path.abspath(path), os.path.abspath(keep)
    if path == keep:
        return                       # 保留整棵树（夹爪载荷）
    if keep.startswith(path + os.sep):
        if os.path.isdir(path):      # path 是 keep 的祖先：只删其它子项
            for name in os.listdir(path):
                _force_remove(os.path.join(path, name), keep)
        return
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.isfile(path):
        os.remove(path)


def _tree_size(path: str) -> int:
    """目录的字节数（不跟随软链 —— 与分发后的实际落盘一致）。"""
    total = 0
    for dir_path, _, files in os.walk(path):
        for name in files:
            full = os.path.join(dir_path, name)
            if not os.path.islink(full):
                try:
                    total += os.path.getsize(full)
                except OSError:
                    pass
    return total


def _count_symlinks(path: str) -> int:
    count = 0
    for dir_path, _, files in os.walk(path):
        for name in files:
            if os.path.islink(os.path.join(dir_path, name)):
                count += 1
    return count


def _count_py(path: str) -> int:
    """**.py 个数（不跟随软链、不计 __pycache__）—— Windows 包「同一份源码」
    的量化证据：与 Linux 包只差 sightac_sdk/ 里那 7 个包壳文件。"""
    count = 0
    for dir_path, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        count += sum(1 for n in files
                     if n.endswith(".py")
                     and not os.path.islink(os.path.join(dir_path, n)))
    return count


def _find_elf(root: str) -> list:
    """root 下所有 ELF 文件（前 4 字节 \\x7fELF）的相对路径。

    Windows 包的硬不变量就是「一个 ELF 都没有」—— native/ 与 sightac_sdk/
    里的 .so 都是 Linux 二进制，带上去了在客户机上只会以各种奇怪方式失败。
    """
    found = []
    for dir_path, _, files in os.walk(root):
        for name in files:
            full = os.path.join(dir_path, name)
            if os.path.islink(full):
                continue
            try:
                with open(full, "rb") as fh:
                    if fh.read(4) == b"\x7fELF":
                        found.append(os.path.relpath(full, root))
            except OSError:
                continue
    return sorted(found)


def _load_paths_module():
    """独立加载 core/gripper/paths.py —— 故意**不经** core.gripper/__init__。

    两个原因：① Windows 上 `import core.gripper` 会因顶层 `import fcntl`
       直接失败，而打 Windows 包时本脚本仍要用它核对资源清单；
    ② __init__ 有导入期猴补丁，不必让打包进程沾上。
    paths.py 顶层只 import os，独立加载是安全的。
    """
    import importlib.util
    path = os.path.join(ROOT, "core", "gripper", "paths.py")
    spec = importlib.util.spec_from_file_location("_pack_lite_paths", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_shell_matches_paths() -> None:
    """start_lite.sh 的 [错误 B] 资源清单必须与 paths.required_resources() 一致。

    部署脚本那时还没装 venv、不能 import，只能把 7 条路径抄成 shell 字面量；
    这个断言把「改了 paths.py 却忘了改脚本」（→ 客户机上缺资源却照常启动、
    夹爪静默不可见）变成**打包当场失败**。
    """
    shell_path = _dist_shell_source("start_lite.sh")
    if shell_path is None:
        print("[警告] 找不到 start_lite.sh，跳过 [错误 B] 清单核对")
        return
    text = open(shell_path, encoding="utf-8").read()
    # 脚本里路径写的是变量（$GRIPPER_NATIVE 等，为的是认 KSQ_* 环境变量）；
    # 未设环境变量时它们就取默认值，先把默认值代回去再比字面量
    normalized = (text
                  .replace("$GRIPPER_NATIVE", "core/gripper/native")
                  .replace("$GRIPPER_SIGHTAC", "core/gripper/sightac_sdk"))
    required = _load_paths_module().required_resources()
    missing = [os.path.relpath(p, ROOT).replace(os.sep, "/")
               for p, _ in required.values()
               if os.path.relpath(p, ROOT).replace(os.sep, "/") not in normalized]
    n_checks = text.count("    check_res ")
    if missing or n_checks != len(required):
        print(f"[失败] {os.path.basename(shell_path)} 的 [错误 B] 清单与 "
              f"core/gripper/paths.py:required_resources() 不一致：")
        for rel in missing:
            print(f"  脚本里没有: {rel}")
        print(f"  check_res 条数 {n_checks} ≠ 资源项数 {len(required)}")
        sys.exit(1)
    print(f"[核对] start_lite.sh 的 [错误 B] 清单与 paths.py 一致（{n_checks} 项）")


def _verify_payload(out_dir: str, target: str) -> None:
    """载荷完整性硬断言。

    Linux 包：软链没被展开（R3）+ 7 项必需资源逐项就位。
    Windows 包：代码照收、平台绑定件一个不留（含「零 ELF」这条硬不变量）——
        于是「Windows 包」在语义上就是「Linux 包去掉载荷」，而这种形态在
        Linux 开发机上可以直接造出来验证（mv 走 native/ 即可），不必真去
        Windows 上撞。
    """
    _assert_shell_matches_paths()
    gr = os.path.join(out_dir, GRIPPER_REL)
    if target != "linux":
        for name in _PLATFORM_BOUND_NAMES:
            if os.path.exists(os.path.join(gr, name)):
                print(f"[失败] Windows 包不该含 {GRIPPER_REL}/{name}/")
                sys.exit(1)
        elf = _find_elf(gr)
        if elf:
            print(f"[失败] Windows 包含 ELF 文件 {len(elf)} 个：{elf[:5]}")
            sys.exit(1)
        absent = [f"core/gripper/{f}"
                  for f in ("__init__.py", "bridge.py", "paths.py",
                            "fays_runtime.py", "fays_single.py")
                  if not os.path.isfile(os.path.join(gr, f))]
        if not os.path.isfile(os.path.join(out_dir, "core",
                                           "gripper_codec.py")):
            absent.append("core/gripper_codec.py")
        for name in ("s80m_manager.py", "frame_gap.py"):
            if not os.path.isfile(os.path.join(out_dir, "core", name)):
                absent.append(f"core/{name}")
        if absent:
            print(f"[失败] Windows 包缺夹爪代码（两个包都收 .py）: {absent}")
            sys.exit(1)
        print(f"[载荷] Windows 包：夹爪代码 {_count_py(gr)} 个 .py、零 ELF、"
              f"无 {' / '.join(_PLATFORM_BOUND_NAMES)} —— "
              f"那边的夹爪组框在、里面空着，属预期降级")
        return
    src = os.path.join(ROOT, GRIPPER_PAYLOAD_REL)
    dst = os.path.join(out_dir, GRIPPER_PAYLOAD_REL)
    if not os.path.isdir(dst):
        print(f"[失败] Linux 包缺夹爪载荷 {GRIPPER_PAYLOAD_REL}/")
        sys.exit(1)
    n_src, n_dst = _count_symlinks(src), _count_symlinks(dst)
    if n_src != n_dst:
        print(f"[失败] 载荷软链被展开：源 {n_src} 个 → 包内 {n_dst} 个"
              f"（copytree 必须传 symlinks=True）")
        sys.exit(1)
    size_mb = _tree_size(dst) / 1e6
    size_src_mb = _tree_size(src) / 1e6
    if size_mb > size_src_mb + 20:
        print(f"[失败] 载荷体积异常：源 {size_src_mb:.0f}MB → 包内 {size_mb:.0f}MB")
        sys.exit(1)
    env = dict(os.environ, PYTHONPATH=out_dir, QT_QPA_PLATFORM="offscreen")
    probe = ("import core.gripper.bridge, core.s80m_manager\n"
             "from core.gripper import paths\n"
             "missing = [k for k, (p, x) in paths.required_resources().items()\n"
             "           if not os.path.exists(p)\n"
             "           or (x and not os.access(p, os.X_OK))]\n"
             "assert not missing, missing\n"
             "assert paths.gripper_resources_available()\n"
             "print('OK')")
    proc = subprocess.run(
        [sys.executable, "-c", "import os\n" + probe], cwd=out_dir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        print("[失败] 包内夹爪资源自检未通过：")
        print(proc.stderr.decode("utf-8", "replace"))
        sys.exit(1)
    print(f"[载荷] {GRIPPER_PAYLOAD_REL}/ {size_mb:.0f}MB、"
          f"软链 {n_dst} 个原样保留、7 项必需资源全部可执行")


def collect(out_dir: str, with_wheels: bool, force: bool,
            target: str) -> None:
    out_dir = os.path.abspath(out_dir)
    if force:
        # 只重建代码路径；venv_lite/wheels/data 与分发壳原样保留；
        # core/gripper/native（470MB）也不动 —— 它不由本脚本生成，
        # 白删白拷一轮纯浪费（Windows 包目录里本来就没有它）
        keep = os.path.join(out_dir, GRIPPER_PAYLOAD_REL)
        for rel in (CODE_FILES + ["config", "core", "ui", "tools"]):
            _force_remove(os.path.join(out_dir, rel), keep)
    elif os.path.isfile(os.path.join(out_dir, "main_lite.py")):
        print(f"[跳过] 目标目录已存在: {out_dir}")
        print("  --force 只重建代码部分，venv_lite/wheels/data 与载荷会保留")
        sys.exit(0)

    copied = []
    for rel in CODE_FILES + SELFTEST_FILES + (
            TOOL_FILES_LINUX if target == "linux" else []):
        src = os.path.join(ROOT, rel)
        if not os.path.isfile(src):
            print(f"[警告] 缺文件: {rel}")
            continue
        dst = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)

    for rel in DIST_FILES:
        src = _dist_shell_source(rel)
        if src is None:
            print(f"[警告] 缺分发文件: {rel}（应在仓库根或 lite_package/ 下）")
            continue
        dst = os.path.join(out_dir, rel)
        if os.path.abspath(src) == os.path.abspath(dst):
            copied.append(rel)   # 源即目标（在 lite_package/ 内收集）
            continue
        shutil.copy2(src, dst)
        copied.append(rel)

    for rel in WHOLE_DIRS:
        src = os.path.join(ROOT, rel)
        dst = os.path.join(out_dir, rel)
        if rel == GRIPPER_REL:
            ignore = _gripper_ignore_factory(
                src, windows=(target != "linux"),
                # 目标包已有载荷（--force 重打包）：native/ 整棵跳过，
                # 只刷新 Python 代码 —— 也避开已存在软链的 File exists
                keep_existing_native=os.path.isdir(
                    os.path.join(dst, "native")))
        else:
            ignore = shutil.ignore_patterns("__pycache__")
        # symlinks=True 是硬要求（载荷里 76 个相对软链，展开 +330MB）
        shutil.copytree(src, dst, symlinks=True, ignore=ignore,
                        dirs_exist_ok=True)
        copied.append(rel + "/")

    for sub, files in MODULE_FILES.items():
        for name in files:
            rel = os.path.join(sub, name)
            src = os.path.join(ROOT, rel)
            if not os.path.isfile(src):
                print(f"[警告] 缺文件: {rel}")
                continue
            dst = os.path.join(out_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)

    if with_wheels:
        src = os.path.join(ROOT, "wheels")
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(out_dir, "wheels"))
            copied.append("wheels/")
        else:
            print("[提示] --with-wheels 但仓库没有 wheels/ 目录，跳过")

    # 自检：在目标目录内 import main_lite（与 start_lite.bat 错误 E 同口径）
    env = dict(os.environ, PYTHONPATH=out_dir,
               QT_QPA_PLATFORM=os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    proc = subprocess.run(
        [sys.executable, "-c", "import main_lite"],
        cwd=out_dir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        print("[失败] 目标目录内 import main_lite 自检未通过：")
        # 入口守卫（core/startup_guard.py）是往 **stdout** 打印指引再退出的，
        # 只看 stderr 会把「缺哪个依赖」整个吞掉，只留一句无信息的失败。
        # 自检必须用装了依赖的解释器跑（venv/bin/python），否则必然失败。
        print(proc.stderr.decode("utf-8", "replace")
              or proc.stdout.decode("utf-8", "replace")
              or f"（无输出，退出码 {proc.returncode}）")
        sys.exit(1)
    _verify_payload(out_dir, target)

    total = sum(os.path.getsize(os.path.join(out_dir, r))
                for r in copied if os.path.isfile(os.path.join(out_dir, r)))
    if target == "linux":
        payload = _tree_size(os.path.join(out_dir, GRIPPER_REL)) / 1e6
        hint = (f"  客户机落盘 = 本目录 + 夹爪载荷 {payload:.0f}MB"
                f" + venv_lite(自动安装, ~750MB) ≈ {payload / 1000 + 0.75:.2f}GB"
                + (" + wheels/" if with_wheels else ""))
    else:
        hint = ("  客户机落盘 = 本目录 + venv_lite(自动安装, ~750MB) ≈ 0.75GB"
                "（Windows 包不含夹爪载荷 native/ 与 sightac_sdk/，"
                "那边的夹爪组框在、里面空着）"
                + (" + wheels/" if with_wheels else ""))
    print(f"[完成] 收集 {len(copied)} 个文件到 {out_dir}/，"
          f"代码共 {total / 1024:.0f} KB，import main_lite 自检通过（{target}）")
    print(hint)


def main() -> None:
    parser = argparse.ArgumentParser(description="收集极简采集版发布包")
    parser.add_argument("--out", default=os.path.join(ROOT, "lite_package"),
                        help="输出目录（默认仓库根 lite_package/）")
    parser.add_argument("--target", default=None,
                        choices=("linux", "windows"),
                        help="目标平台（默认本机）。windows 包不含平台绑定件"
                             "（native/sightac_sdk），夹爪代码两个包同源")
    parser.add_argument("--with-wheels", action="store_true",
                        help="连同 wheels/ 离线依赖包一起收集")
    parser.add_argument("--force", action="store_true",
                        help="重建代码部分（保留 venv_lite/wheels/data/载荷）")
    args = parser.parse_args()
    target = args.target or ("windows" if os.name == "nt" else "linux")
    collect(os.path.abspath(args.out), args.with_wheels, args.force, target)


if __name__ == "__main__":
    main()
