# 相机服务与 UVC 扫描程序（源码入库版）

`ksq-camera-service`（libuvc 推流服务，走 IPC 给主程序供帧）与
`discover-uvc-config`（UVC 相机扫描：拓扑枚举 + Flash 身份读取 + 生成配置）的
**源码**。用 `./build.sh` 构建，产物安装到
`core/gripper/native/camera_service/build/`。

## 为什么源码在这里

这两个程序的源码原先**只存在于 `online/`**（`.gitignore:56` 整个目录不上传），
`core/gripper/native/` 下只有编译好的二进制，重建配方只活在当时那次排查的会话里
—— 二进制一旦丢失或要改一行，就得从头摸清 libusb 头文件、pkg-config、RUNPATH
这一整串。2026-09-15 把源码连同构建配方搬进 core，让采集程序自己的相机逻辑随包入库。

## 目录

```
CMakeLists.txt            与交付包 online/camera_service/CMakeLists.txt **逐字节一致**
src/camera_service.c      推流服务
tools/discover_uvc_config.c   UVC 扫描程序（三档扫描深度）
tools/sonix_flash_probe.c     Sonix Flash 读写工具（CMakeLists 里的目标之一）
tests/test_frame_integrity.c  无硬件回归测试（MJPEG 回调完整性）
third_party/libuvc/       vendored libuvc 0.0.7，含本地补丁 ksq_altsetting_quirks
                          （src/stream.c：DECXIN 3060→1280、Sightac 3072→800）
build.sh                  构建脚本（唯一入口）
```

## 构建

```sh
core/gripper/camera_service_src/build.sh            # 构建 + 安装（旧的先备份）
core/gripper/camera_service_src/build.sh --test     # 顺带跑 test-frame-integrity
core/gripper/camera_service_src/build.sh --no-install
```

依赖：`gcc`、`cmake >= 3.16`、`make`、libusb-1.0 头文件（`libusb-1.0-0-dev`，或退到
conda 的 `include/libusb-1.0`）。libjpeg 头文件可选，但**缺了 libuvc 会退化成不支持
MJPEG 解码**，构建脚本会告警 —— 线上产物是带 libjpeg 的，两者不一致。

安装会按 `.pre_rebuild_<时间戳>` 备份旧二进制，回退就是覆盖回去。

## 三个已踩过的坑（改脚本前先看）

1. **`*** 目标模式不含有"%"`**：真正的病因是 vendored `FindLibUSB.cmake` 走
   `find_library()` 找不到库（系统只有 `libusb-1.0.so.0`，没有给链接器用的
   `libusb-1.0.so`），把 `LibUSB::LibUSB-NOTFOUND` 写进了依赖表；GNU make 把那条
   依赖里的第二个冒号当成静态模式规则的分隔符，于是报出这个与病因毫不相干的错。
   解法是 `-DLibUSB_LIBRARY=...` 预置缓存，让 `find_library` 整个跳过。
2. **pkg-config 里只能有一个 `-I`**：`FindLibUSB.cmake` 把 `${LibUSB_INCLUDE_DIRS}`
   不 quote 地交给 `set_target_properties`，两个 `-I` 就变成三个实参报错。而
   libuvc 内部用 `<libusb.h>`、两个工具用 `<libusb-1.0/libusb.h>`，两种写法都要能
   解析 —— 所以 shim 目录里同时放 `libusb-1.0/` 与 `libusb.h` 两个软链，只给一个 `-I`。
3. **`ksq-camera-service` 的 RUNPATH 会多一个空条目**（`$ORIGIN/third_party/libuvc:`）：
   CMake 把它链的 `Threads::Threads` 在本机算成了空目录。RUNPATH 里的空条目等于让
   加载器去**当前工作目录**找 `libuvc.so.0`，线上那份手工链的没有这个口子，所以
   构建脚本会在链接后把这个冒号就地改成字符串终止符（等价 `chrpath -d` 的收缩改写；
   本机没有 chrpath/patchelf）。校验段会断言空条目已消失。

## 等时档位（altsetting）：ini 说了算，服务会自己降档

**`forced_altsetting` / `forced_payload` 是真正生效的**，不是校验用的装饰。服务
打开设备后立刻把这两个值交给 libuvc 的新 API `uvc_set_altsetting_override()`
（声明在 `include/libuvc/libuvc.h`，实现在 `src/device.c`，`uvc_device_handle_t`
是 `calloc` 出来的，新字段自动零初始化，没设过就退回编译期那张
`ksq_altsetting_quirks` 表）。**改档位只需要改 ini。**

在此之前这两个 ini 字段只用于校验和打印，真正决定档位的是编译进 libuvc 的表
——所以「改 ini」完全不改变行为，这一点极容易误判。**判断一条腿实际跑在哪个档
位，只认日志里的 `[LIBUVC-QUIRK] ... alt=N payload=M ... xfers=` 那一行**（带
`(altsetting override)` 后缀表示走的覆盖值）；服务自己那行
`vidpid=... alt=7 payload=1280` 只是 ini 的回声，在旧二进制上与真实档位无关。

档位表在 `src/camera_service.c`（`kDecxinModes` / `kSightacModes`），**必须按
从好到差降序**，降档梯子按表往下走一格、走到最后一项就不再降。两个门槛分开计：
连着停摆 `STALLS_BEFORE_ALTSETTING_DOWNGRADE`(2) 次降档；连着开不起来
`OPEN_FAILURES_BEFORE_ALTSETTING_DOWNGRADE`(3) 次也降档（覆盖「STREAMON 阶段
就排不下带宽、流根本起不来」那种，停摆那条路永远等不到）。结论按
「相机序列号 × 控制器 PCI 路径」落盘到 `state_dir`，下次开机直接读。

**降档必须把两个计数都清零**（2026-09-15 修）。门槛是拿 `==` 比的，计数停在
门槛上就再也不会命中——原来 `maybe_downgrade_altsetting` 只清了 `stalls_at_mode`，
于是「开不起来」那条路**降完一档就永久卡死**，表里剩下的档位形同不存在
（DECXIN 只有两档，正好被掩盖住；三档机型上就会露出来）。两条路的日志都在
调用**之前**抓计数，否则清零后只能打出「0 failed opens」，把最该看的那次降档
说成没有依据。

**这套东西能测，而且以前从来没被测过**（2026-09-15 修）。`tests/test_altsetting_ladder.c`
一直在 `CMakeLists.txt` 里注册着 `add_test`，但 `build.sh --test` 是点名跑
`test-frame-integrity` 的，梯子那个**一次也没执行过**——上面那个「降一档就卡死」
的 bug 正好落在它本该覆盖的范围内。现在 `--test` 走 `ctest`，新增测试只要
`add_test` 就自动纳进来。反向验证过：把清零那行注释掉，`altsetting-ladder`
立刻断言中止，不是个空跑的测试。

**起始档位由 app 决定，不在这张表里**：`uvc_camera_service.py` 的
`_starting_mode` 取**表里最保守的一档**（＝最后一项）。实测 DECXIN 的 alt7
（10.24 MB/s）在整机三路一起出流时每 45~130 秒必停摆一次，alt6（7.552 MB/s）
同样三路下长跑不停，而**两档出帧率一模一样（都 30.00fps）**——alt7 多出来的每帧
余量（327680 对 241664 B）换不到任何看得见的好处，所以默认不用它。想要那点余量
就把 ini 改回 alt7，服务停两次会自己退回 alt6 并把结论落盘。

`tools/discover_uvc_config.c` 报的是**相机描述符里的首选档**（alt7），是如实汇报
硬件，不是起始档；app 拿它跟服务端档位表对一遍，对不上就报「扫描程序与服务端
档位表已经脱节」——那说明两个程序分开编了，必须一起重编。

## SIGTERM 收尾：join 必须有界（2026-09-15 修）

**症状**：服务收到 SIGTERM 后退不出来。实测线上二进制卡了 3 分钟以上，只攥着
DECXIN 的 `/dev/bus/usb/007/032` 一个设备 fd（left/right 都已干净释放），日志
停在断流那一刻——连 `[SUPERVISION] parent exited` 都没打出来。

**病根**：相机线程陷在 `uvc_open` 的同步 libusb 调用里（卡死的设备上它不返回），
永远走不到循环头那个 `g_stop` 判断；而 `main` 堵在 `pthread_join` 上等它。
`g_stop` 置了也没用——没人去看。

**后果**：客户端只等 2 秒（`uvc_camera_service.py` 的 `terminate()` → 等 2 秒 →
`kill()`），超了就是 SIGKILL，进程死在 libusb 调用中间、设备停在激活的 alt
setting 上，下一轮照样起不来——正是 2026-09-15 09:24 那次事故的形状。
`teardown_failed_open` 里「复位抢在 `uvc_stop_streaming` 之前」只覆盖**失败路径**，
覆盖不到「线程已经卡在 `uvc_open` 里」这一种。

**改法**：`pthread_join` → `pthread_timedjoin_np`。整体预算 1.5 秒、单路 0.8 秒，
超时就 `pthread_detach` 掉继续走，`main` 一返回进程即结束（内核替我们释放句柄），
并打一行 `收尾超时：线程未在预算内退出（多半卡在设备调用里），不再等它`。
跳过超时那一路的统计与 `pthread_mutex_destroy`——线程可能还在用，销毁是 UAF。

**一个坑（第一版就踩了）**：这个 join 循环**同时就是服务的主运行循环**——它在
**启动时**进入、一直跑到 `g_stop`，不是「收尾时才走的一段」。所以截止时间不能在
进循环前算好：第一版把 `shutdown_deadline` 放在 `for` 之前，结果服务启动 1.5 秒后
三路一起「收尾超时」、自己退出了（现象：三路 `总帧=0`、日志里三行收尾超时、
`SIGTERM → pid` 那行根本没机会打）。现在改成**等 `g_stop` 真置位时才开始计时**，
没置位时只用 200ms 短超时轮询（成本可忽略）。

**验证**：正常路径真机 45 秒三路 —— left/right 各 1351 帧满帧、decxin 1111 帧，
SIGTERM 退出 **0.71 秒**（改前 0.56 秒，仍在 2 秒宽限内），服务全程不自杀。
**超时那条分支本身没能在真机上触发**（没法按需把设备弄卡），只做了代码审查。

## 与交付包的关系

`CMakeLists.txt` 与四个 C 源文件都从交付包 `online/camera_service/` 原样搬来
（逐字节一致），本仓库只新增 `build.sh` 与这份 README。**改这里的 C 源码时要同步
回交付包**，否则两边会分叉。

构建产物与线上二进制的实测关系（2026-09-15）：

| 产物 | 与线上二进制 |
|---|---|
| `discover-uvc-config` | **逐字节一致**（sha256 `b9d8bba870f60c11…`）——配方忠实 |
| `ksq-camera-service` | **源码已领先线上**（多了下面的 SIGTERM 收尾修复），且编译选项不同：CMake Release 用 `-O3 -DNDEBUG`，线上那份是手工 `-O2`（源码无 `assert()`，`NDEBUG` 无语义影响） |
| `sonix-flash-probe` | 同上，代码同源、编译选项不同 |
| `libuvc.so.0.0.7` | 代码同源、编译选项不同；`ksq_altsetting_quirks` 补丁与 `[LIBUVC-QUIRK]` 日志**都在**，NEEDED 三者一致（libusb-1.0.so.0 / libjpeg.so.8 / libc.so.6） |

也就是说 `build.sh` 现在是**单一机制**（一套 CMake 配置编全部目标），代价是
`ksq-camera-service` 与当年手工链的那个二进制不是同一个字节。重新构建后应重新做
一次三路相机回归，而不是假定等价。

**2026-09-15 19:29 已按此装机**（`STAMP=20260915_192940`）：`ksq-camera-service`
现在带自适应档位 + 两个计数都清零的修复。装机前三路回归已做（app 生成的 ini、
alt6 默认，**600 秒三路零停摆零重建，decxin/left 30.00fps、right 29.80fps**，
17513/17496/17487 帧），装机后又做了一次 300 秒回归。

**2026-09-15 11:34 曾按此装机**（`STAMP=20260915_113444`）：
`core/gripper/native/camera_service/build/` 里的 `ksq-camera-service` 当时是
`07f1f5a34b569b7b`（带 SIGTERM 收尾修复），旧件 `b64b74d810abe482` 备份在同目录
`.pre_rebuild_20260915_113444`；`discover-uvc-config` 装前装后同为
`b9d8bba870f60c11`，装不装等价。装机前三路回归已做（90 秒 ×2 腿 + 45 秒修复腿）。
**另注意 `build.sh` 的安装段不备份 `third_party/libuvc/`**（`rm -f` 直接覆盖、
`include/` 还是 `rm -rf`），所以这次装机前手工备份了库文件为
`libuvc.so.0.0.7.pre_rebuild_20260915_113330` + `include.pre_rebuild_20260915_113330/`。
改脚本时补上这段备份更稳妥。

### glibc 下限：`ksq-camera-service` 2.34 → 2.38（不是新约束）

新编的 `ksq-camera-service` 引用 `__isoc23_strtol@GLIBC_2.38`（GCC 15 默认 C23，
`strtol` 解析成 C23 语义的符号），最低 glibc 从线上的 2.34 抬到 2.38。看着像门槛
变高，其实**没有新增约束**：那个**逐字节一致**的 `discover-uvc-config` 本来就要求
GLIBC_2.38，而它是扫描时每次都要在采集机上跑的 —— 能跑扫描程序就一定能跑这个
服务。其余产物（`sonix-flash-probe`、libuvc）两边都是 2.34。

不要去给 CMake 钉 `-DCMAKE_C_STANDARD=17` 来消掉它：那会连带把
`discover-uvc-config` 的 `strtol` 换回旧符号，而它正是目前唯一能逐字节复现线上
二进制的产物，是「配方忠实」的活证据。
