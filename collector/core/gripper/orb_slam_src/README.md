# ORB-SLAM3 核心库与 Fays 桥接（源码入库版）

主程序连夹爪时真正跑的那两个原生程序的**源码与构建配方**：

| 产物 | 源码入口 | 装到 |
|---|---|---|
| `libORB_SLAM3.so`（核心库） | `ORB-SLAM/{src,include,Thirdparty}` | `core/gripper/native/dist/orb_mark_only/lib/` |
| `fayssense_orb_slam_sn219_opencv48_mark_only`（桥接） | `ORB-SLAM/Examples/fays/fayssense_orb_slam.cc` | `core/gripper/native/dist/fays_opencv48/bin/` |

用 `./build.sh` 构建。这份源码树是**唯一真源**：改这里的 `.cc` 会进二进制，
改别处的同名副本不会。

## 为什么源码在这里

这两个程序的构建源原先**只存在于 `online/`**（旧上位机树，`.gitignore:63` 整个
目录不上传），`core/` 下只有编译好的二进制。后果已经发生过两次：

- `core/gripper/native/ORB-SLAM/Examples/fays/` 下有份同名副本，与真源分叉了
  几个月，还缺 09-11 那批崩溃修复 —— 排查 SLAM 崩溃时改在那里，白改
  （该副本已于 2026-09-17 删除，原地留 `SUPERSEDED.md`）；
- **四个崩溃家族的修复全都只写在 `online/` 的源码里**。`online/` 一丢，就只剩
  二进制，一行都改不动，连复现崩溃都做不到。

2026-09-17 把源码 1:1 搬进 core，配方固化成 `build.sh`。判据是**把 `online/`
删掉主程序照样能连夹爪** —— 主程序侧本来就不读 `online/`（`core/gripper/paths.py`
全部指向 `native/`），这次补上的是「源码这一侧」。

## 目录

```
build.sh                  构建脚本（唯一入口）
ORB-SLAM/                 ORB-SLAM3 源码（src/include/Thirdparty/Examples）
  CMakeLists.txt          ★ 唯一改过的一处：KSQ_GRIPPER_NATIVE_ROOT（见下）
  Examples/fays/          桥接源码 + 离线工具 + 原生回归测试源
dist/fays_opencv48/       桥接的构建配方（CMakeLists.txt ★ 零改动）+ 出厂标定 yaml
FaysSense_VI_Kit_Release/ 厂商 SDK：include/ orb_slam/ tools/ 入库；
                          lib/ thirdparty/ config/ 是 build.sh 建的软链 → ../native/
tests/native/             7 个无硬件 C++ 回归（由 ctest 驱动，构建的一部分）
tests/test_*.py           11 个只读 ORB/桥接源码的 Python 契约测试
```

**唯一那处 CMake 改动**：`ORB-SLAM/CMakeLists.txt` 里 ctest 用的
`KSQ_TEST_RUNTIME_RPATH` 原来按 `online/` 的深度写死了
`${PROJECT_SOURCE_DIR}/../../core/gripper/native/…`，在新树里 `../../` 落到
`core/gripper`，**静默失效**（测试照样跑，只是找不到库）。现在改成可覆盖变量
`KSQ_GRIPPER_NATIVE_ROOT`，缺省 `${PROJECT_SOURCE_DIR}/../../native`。

## 形状为什么必须是这样（改目录前先读这段）

`dist/fays_opencv48/CMakeLists.txt:299-323` 有一段「可移植 RPATH」循环：源码树
内的路径用 `file(RELATIVE_PATH KSQ_ROOT …)` 改写成 `$ORIGIN/…`，树外的保持绝对
路径。这个改写是**纯字典序**的，产出的字符串要到**部署树**里解析 —— 而部署树
`core/gripper/native/` 的形状恰好和这里一样（`FaysSense_VI_Kit_Release/`、
`ORB-SLAM/`、`dist/` 三个兄弟）。现役桥接的 RPATH 六条，逐字节等于这里
`KSQ_ROOT=core/gripper/orb_slam_src` 时算出来的：

```
/usr/local/lib
$ORIGIN/../../../FaysSense_VI_Kit_Release/lib/fays_atrak/x86_64/Release
$ORIGIN/../../../FaysSense_VI_Kit_Release/thirdparty/ft602-linux-x86_64
/home/stouch/collector/core/gripper/native/dist/orb_mark_only/lib
$ORIGIN/../../../ORB-SLAM/Thirdparty/DBoW2/lib
$ORIGIN/../../../ORB-SLAM/Thirdparty/g2o/lib
```

所以 `dist/fays_opencv48/CMakeLists.txt` **一个字都不用改**（连
`KSQ_FAYS_BINARY_SUFFIX`、`KSQ_ORB_LIBRARY_PATH` 都照旧从 `-D` 传）。把目录深度
或兄弟目录名改了，桥接会在运行时找不到 Pangolin/OpenCV/DBoW2，**且不报错**，
只是起不来。`build.sh` 每次构建后都会把 RPATH 与现役那串硬编码比对，不一致直接
报错停机。

## 构建

```sh
core/gripper/orb_slam_src/build.sh                 # 构建 + 安装（旧产物先备份）
core/gripper/orb_slam_src/build.sh --no-install    # 只编，不动 native/
core/gripper/orb_slam_src/build.sh --test          # 另跑 ctest（7 个原生回归）
core/gripper/orb_slam_src/build.sh --clean         # 清 build/ 与树内生成物
core/gripper/orb_slam_src/build.sh --jobs 8        # 并行度（默认 4，见下）
```

依赖：`g++`、`cmake >= 3.16`、`make`、OpenCV 4.8.x 开发文件（conda 包里那份）、
Pangolin（带 `PangolinConfig.cmake` 的 build 目录）、Eigen3、Boost serialization、
OpenSSL libcrypto，以及 **`core/gripper/native/` 的厂商载荷**（785MB，不入库；
`build.sh` 幂等建 `FaysSense_VI_Kit_Release/{lib,thirdparty,config}` 三个软链）。
`PANGOLIN_DIR` / `KSQ_OPENCV48_DIR` 可用环境变量覆盖。

`--jobs` 默认 **4** 而不是 `nproc`(24)：`-O3 -march=native` 的大 TU 单个编译峰值
能到 GB 级（`Optimizer.cc` 最狠），24 路并行在这台机上会打爆内存。

### 配方是冻结的（三段，顺序不能换）

1. **DBoW2**（out-of-source）：上游 `ORB-SLAM/CMakeLists.txt` 只
   `add_subdirectory(Thirdparty/g2o)`，DBoW2 是**裸文件路径链接**
   `${ROOT}/Thirdparty/DBoW2/lib/libDBoW2.so`，树内没有生产者 —— 不先编它，
   核心库链接直接失败。它自己的 `cmake_minimum_required(2.8)` 在 CMake 4.x 下
   需要 `-DCMAKE_POLICY_VERSION_MINIMUM=3.5`（CMake 4.1.3 已移除对 <3.5 的兼容）。
2. **核心库**：`CMAKE_BUILD_WITH_INSTALL_RPATH=ON` +
   `CMAKE_INSTALL_RPATH='/usr/local/lib:$ORIGIN/../../../ORB-SLAM/Thirdparty/DBoW2/lib:$ORIGIN/../../../ORB-SLAM/Thirdparty/g2o/lib:$ORIGIN'`
   + `-DKSQ_ORB_LIBRARY_OUTPUT_DIRECTORY=<build>/orb-out`
   + `-DKSQ_ORB_THIRDPARTY_LIBRARY_ROOT=<本树>/ORB-SLAM` + `-DKSQ_BUILD_RUNTIME_TESTS=ON`。
   **装核心库必须排在编桥接之前** —— 桥接链的就是这颗新的。核心库拿到的是
   **DT_RUNPATH**（CMake 默认 new-dtags）：它只解析自己的直接依赖（DBoW2/g2o），
   这正是它需要的。
3. **桥接**：`-DKSQ_ORB_LIBRARY_PATH=<native>/dist/orb_mark_only/lib/libORB_SLAM3.so`
   ——**必须给到文件**，给目录会被 `get_filename_component(… DIRECTORY)` 吃掉
   末尾的 `/lib`；`-DKSQ_FAYS_BINARY_SUFFIX=_mark_only` 决定产物名。桥接拿到的是
   **DT_RPATH**（`dist/fays_opencv48/CMakeLists.txt:92-98` 给每个 target 挂了
   `LINK_FLAGS "-Wl,--disable-new-dtags"`，注释就在上面一行），因为
   DT_RPATH 对**传递**依赖也生效，而桥接要找的 Pangolin/OpenCV 是
   `libORB_SLAM3.so` 的依赖。用 `readelf -d` 一验便知：桥接那份显示 `(RPATH)`，
   核心库显示 `(RUNPATH)`，两者都与现役相同。

### 三个已经踩过的坑

1. **`-Wl,-rpath-link` 两个都不能少**（否则链接期报一堆 `jas_*` undefined）：
   conda 的 `libopencv_imgcodecs.so.4.8.1` 带未定义的 jasper 符号，而它的第三方
   依赖只记在 NEEDED 里、不在 OpenCVConfig 的 IMPORTED target 里。要给
   `<native>/…/orb48_env/lib` 与 conda opencv 包的 `lib/`。`-rpath-link` 只给
   ld 找传递依赖用，**不进 RUNPATH、也不进 NEEDED**（线上产物零 jasper 条目）。
2. **脏 `CMakeCache.txt` 会静默产出 RPATH 不同的二进制**：`FAYS_SDK_ROOT` /
   `ORB_ROOT` / `KSQ_ORB_LIBRARY_PATH` / `FAYS_BRIDGE_SOURCE` / `OpenCV_DIR` /
   `Pangolin_DIR` 全是 `CACHE` 变量，从 `online/` 拷过来的 build 目录会把旧绝对
   路径写进 RPATH。脚本的 `assert_cache_is_local` 发现构建目录不是本树建的
   （`.ksq-source` 标记对不上）或缓存里有 `online/` 路径就报错退出。
3. **`$ENV{KSQ_ORB_ROOT}` 会绕过缺省值**（`dist/CMakeLists.txt:16`），
   `build.sh` 构建前显式 `unset KSQ_ORB_ROOT FAYS_SDK_ROOT`。
4. **反向验证时要 `touch` 改过的源文件**：`cp -a` 恢复会连 mtime 一起还原，
   恢复出来的文件比 `.o` 还旧，make 认为「不用重编」，于是**上一次那个坏产物
   被原样留下**（实测踩过：ctest 照旧红、`.so` 的 md5 是坏那版的）。
   改完源码用 `touch` 顶一下 mtime，或 `build.sh --clean`。

## 部署与回退

`build.sh`（不加 `--no-install`）把两个产物装到 native/ 的现役路径，旧件先备份成
`.pre_rebuild_<时间戳>`，结束时打印两行回退命令（原样执行即可退回）。装完记得：

- `lite_package/` 里的 native 载荷要**手工同步**（`scripts/pack_lite.py --force`），
  否则极简版的包还是旧二进制；
- 重新录一段做真机验收 —— 构建等价不等于行为等价（见下）。

**安装/回退分支演练过**（2026-09-17，装到 scratch 而不是生产）：
`KSQ_NATIVE_ROOT=/tmp/scratch_native ./build.sh` 走通了「备份旧件 → 装核心库 →
重链桥接 → RPATH 断言 → 装桥接 → 打印回退」，旧件备份是生产原件（`c21808e2` /
`a76a602c`）、装进去的是本次产物；把打印出来的两行 `cp -a` 原样执行，scratch 回到
`c21808e2` / `a76a602c` **逐字节**。生产目录全程零改动（md5 前后一致、无备份件残留）。

那个覆盖目录必须**长得像部署树**（有 `FaysSense_VI_Kit_Release/` 与
`ORB-SLAM/Thirdparty/{DBoW2,g2o}/lib/`）：链接期 ld 是顺着**目标 .so 自己**的
RPATH `$ORIGIN/../../../ORB-SLAM/Thirdparty/DBoW2/lib` 去找 DBoW2/g2o 的，
而 `$ORIGIN` 就是安装目标目录（这是「`KSQ_ORB_LIBRARY_PATH` 给到文件」的副作用）。
缺了它，第 3/3 步会以一片 `undefined reference to DBoW2::…` 收场 —— 演练时实测踩到。

## 复现性：重编出来的和现役差在哪（2026-09-17 实测）

| | 大小 | md5 | BuildID |
|---|---|---|---|
| 核心库 现役 | 5432608 | `c21808e2…` | `efd49c48…` |
| 核心库 新编 | 5432608 | `63b60b63…` | `2424aa1f…` |
| 桥接 现役 | 234840 | `a76a602c…` | `6fa2ac54…` |
| 桥接 新编 | 234840 | `7c012c57…` | `39bf71d3…` |

**构建本身是可复现的**：同一份源码、同一台机、同一个 build 目录，隔几次构建编
出来的两个产物**逐字节相同**（核心库 `63b60b63` / `2424aa1f`，桥接 `7c012c57` /
`39bf71d3`，已复现两次 —— 第二次还是在中间夹了一次**链接参数不同**的构建
（scratch 安装演练，桥接链的是 `/tmp/...` 那颗）之后复现的，说明输出只由命令行
决定、不受上一次链接残留影响）。所以「与现役不同」**唯一**的原因是源码树路径变了：
`__FILE__` 进 `.rodata`，外加 BuildID 是内容的哈希。换句话说，若把本树放在
`online/` 那个路径上重编，产物就是逐字节相同的。

**不是逐字节相同**（构建时间/路径会进 `.comment`、BuildID 与 `.rodata`），但四层
比对都指向「同一份代码」：

- `readelf -d` 的 **RPATH/NEEDED/FLAGS 与现役逐字符相同**（本方案的成败判据）；
- **归一化反汇编**（所有数值操作数替换成 `H`、去注释）：核心库 571107 条指令、
  桥接 27368 条，**两边条数相同且逐行零差异** —— 指令流完全一致，差的只是地址
  操作数；
- 核心库 `.text` 里 4200 个不同字节（0.159%）**全部**落在 4 字节对齐的字段位移
  上（0 个无法归因）；`.dynstr` 与 `.gnu.version_r` 完全相同；
- 唯一不同的字符串是三个 Sophus 头的 `__FILE__` 路径
  （`…/orb_slam_src/ORB-SLAM/Thirdparty/Sophus/…` 比 `online/…` 长 20 字符，
  三条正好 +60 B + 对齐 = 实测 `.rodata` +64 B）。
- 在**同形状的部署树**里 `ldd -r`：新编与现役都是 `not found=0 / 未解析重定位=0`。
  （直接在 `build/orb-out/` 里 ldd 会报 DBoW2/g2o not found —— RPATH 是相对的，
  那个位置本来就没有 `ORB-SLAM/` 兄弟目录，不是缺陷。）

**一处已知的输入差异**：`libDBoW2.so` 重编后与厂商那份不同（75088 B / `6e823023…`
对 75704 B / `c5dc7095…`），因为厂商是用 GCC 11.4 编的、本机是 GCC 13.3，符号表
差异全是 libstdc++/glibc 版本产物（`ios_base_library_initv@GLIBCXX_3.4.32`、
`__isoc23_strtol@GLIBC_2.38` 进，`__cxa_atexit`、`strtol@GLIBC_2.2.5` 出），
NEEDED 一致、DBoW2 自身 API 符号一个没变。**运行时用的是 native 里厂商那份**
（核心库的 RUNPATH 指向 `$ORIGIN/../../../ORB-SLAM/Thirdparty/DBoW2/lib`，解析到
部署树），所以这处差异不进线上行为。`libg2o.so` 则是**逐字节相同**（`e27f42ff…`）。

`ctest` 7 个原生回归（无硬件）：triangulation-observation、time-regression、
frame-imu、prior-recovery、full-inertial-ba-guard、keyframe-payload-cleanup、
frame-lastkf-init —— 全绿。

## 四个崩溃家族的补丁位置（改崩溃先看这里）

| 崩溃 | 症状 | 补丁 |
|---|---|---|
| `mpcpi` 空指针 | `PoseInertialOptimizationLastFrame` 用缺失的先验建惯性图 | `src/Optimizer.cc:4922` 守卫 + `:4928` 的 `[IMU_RECOVERY]` 日志，退回纯视觉 `PoseOptimization` |
| IMU 预积分缺失 | `PoseInertialOptimizationLastKeyFrame` / 跟踪链 | `src/Optimizer.cc:4521`(守卫)/`:4530`(日志)；`Tracking::PreintegrateIMU()` 的**每条早退路径**都补写链头 `mCurrentFrame.mpLastKeyFrame`（`src/Tracking.cc:1659/1689/1754/1765`，其中 `:1759` 那条带 `[IMU_RECOVERY]` 日志） |
| `KeyFrameCulling` 空预积分 | 惯性分支对 `NULL` 调 `MergePrevious` → SIGSEGV | `src/LocalMapping.cc:1413-1415` 两侧都判空（全文件唯一漏判处） |
| `Frame::mpLastKeyFrame` 未初始化 | 拷贝赋值临时对象后链头带垃圾值 | `src/Frame.cc` 5 处构造初始化（231/297/426/515/1261） |

另有两处与崩溃同源的**内存回收**补丁：`src/Optimizer.cc:744` 的
`[FULL_BA_RECOVERY]`（顶点缺失的观测被跳过时只报一行汇总，文案**刻意不含**
"error"/"fail" 字样，否则会被 `core/gripper/slam/protocol.py` 的 `_ERROR_RE`
当错误弹到 UI）；`src/LocalMapping.cc:404` 的 `PeriodicTimeWindowCleanup()`。

**反向验证法**：摘掉守卫重编，对应的原生回归必须变红，再装回去 —— 只在正向通过
的测试不算数。2026-09-17 对第四类做过一轮：摘掉 `Frame.cc` 那 5 处
`mpLastKeyFrame(static_cast<KeyFrame*>(NULL))` → `frame-lastkf-init-safety`
**红**，报的正是 4 个构造函数留下 `0xabab…` 毒值（另 6 个测试照旧绿）；
装回 → **7/7 绿**。

### 一个**没修**的可疑点（2026-09-17 记，供后续排查）

`src/Optimizer.cc:490-492`（`FullInertialBA` 的 IMU 链接段）判的只有
`pKFi->bImu && pKFi->mPrevKF->bImu`，**没判 `mpImuPreintegrated` 是否为空**就直接
`pKFi->mpImuPreintegrated->SetNewBias(...)`；533/552/560 三处同样。而 `bImu` 在
IMU 初始化后是对所有关键帧置真的（`Tracking.cc:3431`、`LocalMapping.cc:1691/1700`），
空预积分又是设计内合法态（`src/LocalMapping.cc:1410` 的注释、`Tracking.cc:2721`
明写会置 `NULL`）。触发路径与第三类崩溃同源（掉线 → 重建地图 → 初始化期出现空
预积分的关键帧），所以看着像**第五个同族空指针**。本次没动它：它要改二进制，
而本轮的目标是「源码搬进 core 且重编产物与现役指令流一致」，动它会破坏这个可比
对性。要修的话照第三类那样两侧判空 + 反向验证。

## Python 契约测试（tests/*.py）

11 个只读 ORB/桥接源码的契约测试，从 `online/tests/` 复制过来（那边原样保留），
`ROOT` 改指本树。**跑法与 online 基线逐个比对过：7 绿 4 红，失败的用例名与计数
逐条一致**（两棵树各跑一遍、逐行 diff）：

```sh
venv/bin/python core/gripper/orb_slam_src/tests/run_contract_tests.py
```

★ **别用 `python tests/test_x.py` 直跑**：这 11 份里有 4 份
（`test_connect_debug`、`test_fays_sdk_shutdown`、`test_orb_stereo_baseline`、
`test_slam_offline_evaluation`）**没有 `unittest.main()` 入口**，直接执行只
import 一遍就退出 —— **rc=0、零输出，看着像全绿**（实测踩过）。两棵树都没有
`__init__.py`，`unittest discover` 也进不去，所以随树带了个按模块加载的
`tests/run_contract_tests.py`：没入口的照样跑，纯 clone 上缺上位机 runtime 的
如实报 SKIP 与原因。

| | 测试 |
|---|---|
| 绿 | orb_frame_lastkf_contract、orb_stereo_baseline、fays_calibration_container、fays_input_trace、fays_sdk_shutdown\*、connect_debug\*、fays_factory_calibration_contract\*† |
| 红（**本来就是红的**，陈旧契约，原样带过来不趁机修） | orb_mp_cleanup_contract(2)、fays_historical_orb_contract(3)、slam_offline_evaluation(6)、fays_factory_calibration_contract\*†(1+1) |

\* 标星的三个**还依赖上位机侧源码**（`online/gripper_version1/` 的 `runtime/`、
`scripts/`）：那部分**不在 core 里、也不在夹爪运行链路上**（主程序只从
`native/gripper_version1/` 读 `fays_config/` 与 `device_manifest.json` 两个数据文件，
不 import 它的 Python）。所以这三个模块在**纯 clone 上没有 online 时会整体
`SkipTest`**（带原因，不算失败），本机（有 online）则照常跑。
† `fays_factory_calibration_contract` 里那条断言部署库位置的判据改指
`NATIVE/dist/orb_mark_only/lib/libORB_SLAM3.so`（本树只放源码与配方）。

`tests/native/` 那 7 个 C++ 回归是构建的一部分（`--test` 走 ctest），不需要设备。

## 已知缺口（诚实清单）

- **生产从未真装过**：`core/gripper/native/` 里那两颗至今是原件（核心库 `c21808e2`、
  桥接 `a76a602c`），本次两次构建都走 `--no-install`；安装分支只在 scratch 上演练
  （见「部署与回退」）。之所以不装也不缺东西：重编产物与现役**指令流逐条一致**
  （见上），装上去换来的只是 `__FILE__` 路径字符串不同。要装随时 `./build.sh`。
- **真机端到端未验证**：2026-09-17 构建/装机当天机器上没有相机，只验证到
  「资源校验 + 导入链 + 路径解析 + 指令流等价」（含把 `online/` 改名藏起来跑通
  全链路）。装到 native/ 后要重录一段做验收。
- 上一轮部署的核心库（`efd49c48`，第四类 `Frame::mpLastKeyFrame` 修复）的**真机
  验收仍欠着**，一并等录制会话。
- **没真发版**：打包演练过了（`scripts/pack_lite.py` 两个 target 都打到 `/tmp`，
  包里零 `orb_slam_src`、零构建源，Windows 包零 ELF 硬断言过，Linux 包 459MB
  载荷 / 76 个软链原样 / 7 项资源自检过），但 `lite_package/` 与线上发布都没动。
- `FaysSense_VI_Kit_Release/compat_libs/` **没有搬进来**（428KB，全是 prebuilt
  ELF）。它只被 SDK-4.2 诊断模式的目标引用（`Examples/fays/CMakeLists.txt` 的
  sdk42 分支、`FaysSense_VI_Kit_Release/orb_slam/CMakeLists.txt`、厂商
  `run_*.sh`），生产桥接不用；要用那条路再从 `online/` 拷。
- `ORB-SLAM/Examples/fays/offline/` 的 Python 工具（`codec_benchmark.py` 等）里
  写死了 `PROJECT = parents[4]` 再 import 上位机的 `runtime.cpu_policy`，纯 clone
  上跑不了（同上面 \* 的原因）。那是离线评测工具，不在任何运行链路上。
- `core/gripper/fays_runtime.py` 的 `ORB_WORK_DIR` / `ORB_TRAJECTORY` 是**死常量**
  （`paths.py:134` 还在给它们赋值）：真正的 cwd 与 traj 路径都是每租约的
  `tempfile.mkdtemp`，`native/ORB-SLAM/traj.txt` 从来不存在。本次不删，记一笔。

## 与 `online/` 的关系

按用户要求是**复制**不是移动，所以现在有两份：**真源是这里**，`online/ORB-SLAM/`
那份是历史副本（`online/` 整个目录不入库、随时可能被删）。两边一旦分叉，以这里
为准；改 `online/` 那份不会进任何二进制。同理，`core/gripper/native/ORB-SLAM/`
下只剩 `Examples/fays/s80m_stereo_inertial.yaml`（运行时要读，别删）与
`Thirdparty/{DBoW2,g2o}/lib/*.so`（现役核心库的运行时依赖，别删）。

## 入不入库

源码入库，产物不入库（`.gitignore` 里有对应规则，注意 `dist/` 那条全局规则会把
整棵 `dist` 排掉，所以本树有一条 `!core/gripper/orb_slam_src/dist/` 的负向规则
把它捞回来）。入库约 3.8MB / 338 个文件。不入库的：`build/`、
`dist/fays_opencv48/bin/`、`ORB-SLAM/{lib,Thirdparty/*/lib}/`、
`Thirdparty/g2o/config.h`（g2o 的 `configure_file` 写回源码树）、三个 SDK 软链。

`scripts/pack_lite.py` 的 `_PAYLOAD_SKIP_NAMES` 里加了 `orb_slam_src`：
`WHOLE_DIRS` 会整棵 copytree `core/gripper`，不排除的话本树（含编译出的 ELF）
会进 lite 包，Windows 包的零 ELF 自检会直接 `sys.exit(1)`。
