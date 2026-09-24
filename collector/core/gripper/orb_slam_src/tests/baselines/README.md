# 换 SLAM 核心前的基线（2026-09-23 冻结）

用途：把主程序 SLAM 核心替换成 `SLAM_SDK_upload`（v0.2.0）时，证明
「改动只增不减、没悄悄丢掉符号、没改行为」。本目录是 P0 阶段冻结的
**四项基线**，换核心之后的每一项改动都要能对着它对账。

| 项 | 资产 | 状态 |
|---|---|---|
| P0.1 产物指纹 | `*.symbols.txt.gz`、`*.readelf-d.txt`、下表 | 已冻结 |
| P0.2 行分类 | `pre_sdk_swap_lines.json` | 已冻结（20 份日志 / 227646 行） |
| P0.3 轨迹 | — | **未冻结**，见下节「P0.3 为什么没做」 |
| P0.4 契约测试 | 本节记录 | 已跑（见下节） |

产物指纹部分方法沿用主 README 的「复现性：重编出来的和现役差在哪」小节。

## 冻结时的现役产物

| | 大小 | md5 | BuildID | 导出符号数 |
|---|---|---|---|---|
| 核心库 `libORB_SLAM3.so` | 5432608 | `c21808e2…` | `efd49c48…` | 6936 行 / **5847 去重** |
| 桥接 `fayssense_orb_slam_sn219_opencv48_mark_only` | 234840 | `a76a602c…` | `6fa2ac54…` | 70 |

★ **两个数别混**：`nm -DC --defined-only | wc -l` 是 **6936 行**（同名符号在多个
TU 里各出现一次），`… | sed 's/^[0-9a-f]* //' | sort -u` 才是 **5847** 个唯一符号。
`libORB_SLAM3.symbols.txt.gz` 存的是**去重后的 5847**，`comm` 对账必须两边同法。

路径（`core/gripper/native/` 下，该目录不入库）：

- `dist/orb_mark_only/lib/libORB_SLAM3.so`
- `dist/fays_opencv48/bin/fayssense_orb_slam_sn219_opencv48_mark_only`

md5 / BuildID 与主 README 第 158/160 行记录逐位吻合 ⇒ 冻结的就是 README 描述的那两件。

## 文件

| 文件 | 内容 |
|---|---|
| `libORB_SLAM3.symbols.txt.gz` | 核心库全部 6936 个导出符号（`nm -DC --defined-only` 去地址后排序去重） |
| `bridge.symbols.txt.gz` | 桥接 70 个导出符号，同法 |
| `libORB_SLAM3.readelf-d.txt` / `bridge.readelf-d.txt` | `readelf -d` 全量，用于核对 RPATH/NEEDED/FLAGS **逐字符**相同 |

符号表用 gzip 存（908 KB → 45 KB）；`readelf -d` 体积小，直接存原文。

## 怎么用（改动后的门）

```bash
# 1) 符号集「只增不减」
#    重新构建后，对新产物跑同样的 nm，与基线做差：
nm -DC --defined-only <新产物> | sed 's/^[0-9a-f]* //' | sort -u > /tmp/new.symbols.txt
zcat tests/baselines/libORB_SLAM3.symbols.txt.gz > /tmp/base.symbols.txt
comm -23 /tmp/base.symbols.txt /tmp/new.symbols.txt   # 期望：空（没有任何符号消失）

# 2) readelf -d 逐字符
diff <(readelf -d <新产物>) tests/baselines/libORB_SLAM3.readelf-d.txt
#    注意：BuildID 不会出现在 -d 输出里；期望只有 NEEDED 增删是有意的改动

# 3) 同形状部署树里 ldd -r
ldd -r <新产物> | { command grep 'not found' || echo 'not found=0'; }
```

`comm` 那一步是本基线的主要价值：核心库有 6936 个符号，肉眼看不出少没少。

## P0.2 行分类基线（`pre_sdk_swap_lines.json`）

用 `tools/audit_slam_line_classes.py --capture` 生成；换核心后用 `--compare` 对账。
20 份 `logs/slam_native/*_slam_stdout.log`、227646 行：

| kind | 条数 | | kind | 条数 |
|---|---:|---|---|---:|
| pose | 154828 | | mp_cleanup | 5255 |
| log | 44161 | | fays_rates | 5326 |
| orb_stage | 5401 | | error | **1110** |
| orb_diagnostic | 5361 | | status | 716 |
| affinity | 5379 | | time_drop | 75 |
| origin | 17 | | ready | 17 |

**★ 关键发现：现役生产本来就有 1110 条 error 分类行**，其中
`ERROR building inertial edge` ×1106 独占（另 4 条是 2026-09-20 某次的
`StereoOnlineCapture(): get img failed` 等一次性文案）。这条来自上游 ORB-SLAM3
的 `Optimizer.cc`（本树 `:2692/:4277`，SDK 树 `:2663/:4248` **两棵树都有**），
**不是换核心引入的**。换核心后的对账要拿它当既有基线，不能当成回归。

`_ERROR_RE` 裸命中 6 种 > error 分类 5 种，多出来的是
`[FAYS-CALIB] WARN SetStereoFPS(50) failed` ×19 —— 它命中正则但被
`protocol.py` 的 `_IGNORED_NON_FATAL_RE` 白名单提前挡掉，**证明白名单确实在生效**，
也说明「只看裸命中会高估风险」。

**⚠️ 纠正一条我方案里写错的契约**：`_ERROR_RE` 命中**不再锁死就绪门**。
`process_controller.py:332-345 wait_sdk_ready()` 的判据已经改成
`_sdk_ready_event.is_set() and snapshot.running`，注释里显式写明「不能再挂
`not snapshot.error`」（2026-09-10 18:32 假超时事故的修复）。`state.error` 今天
唯一的消费者是 `:993-999`，只做「记一行 `[SLAM] ⚠` + 把 `status` 写成
`Error: …`」——**纯显示字段，没有任何控制流读它**（`status.startswith("error")`
只有 `core/usb_glove_engine.py:237` 一处，那是手套引擎）。所以新增 error 文案的
后果是「UI 状态栏可能闪一下」，不是「就绪门超时」。

## P0.4 契约测试

```sh
venv/bin/python core/gripper/orb_slam_src/tests/run_contract_tests.py   # rc=0
QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_slam_ready_gate.py  # ALL PASS
```

本机实测 **6 OK / 2 RED / 4 SKIP**（合计 12；`slam_engine_switch_contract`
是第二步 2.1 时加的，见下文那节）：

- OK：`fays_calibration_container(2/2)`、`fays_input_trace(4/4)`、
  `orb_frame_lastkf_contract(4/4)`、`orb_stereo_baseline(3/3)`、`slam_offline_evaluation(6/6)`、
  `slam_engine_switch_contract(11/11)`
- RED：`orb_mp_cleanup_contract(2F)`、`fays_historical_orb_contract(3F)`
- SKIP：`codec_benchmark`（缺 `yaml`）、`connect_debug` / `fays_sdk_shutdown` /
  `fays_factory_calibration_contract`（都要 `online/` 上位机源码，本机没有）

★ **必须用 `venv/bin/python`**，用 PATH 上的 conda（3.13 + 新 scipy）跑会多出 6 个
假 RED：`slam_offline_evaluation` 的 `trajectory()` 用
`Rotation.from_euler('z', <1 维数组>)` 造合成轨迹，新 scipy 拒绝 1 维角度数组
（要求 `(N,1)`），6 个用例全 ERROR。`venv/bin/python` 是 3.10.20 + scipy 1.15.3，
实测 OK 6/6 —— **是解释器差异，不是回归**。

**与主 README 那节的两处出入**（主 README 记的是「7 绿 4 红」）：

1. 主 README 记 7 绿含 `connect_debug`、`fays_sdk_shutdown`、
   `fays_factory_calibration_contract` —— 它们标了 `*`，脚注写明「纯 clone 上没有
   online 时整体 SkipTest（带原因，不算失败）」。本机 `online/` 不存在，所以是
   SKIP 而非绿，**符合脚注，不算出入**。
2. **真出入：`slam_offline_evaluation`**。主 README 红榜写 `slam_offline_evaluation(6)`，
   本机实测 **OK 6/6**。该文件 6 个用例全部加载本树的
   `ORB-SLAM/Examples/fays/offline/evaluate.py` 喂**合成**轨迹，只依赖 numpy/scipy，
   没有任何 skip 逻辑；而这份 `evaluate.py` 与本树逐字节等同、SDK 树根本没有这份
   测试。⇒ 主 README 那条红是从 `online/tests/` 抄来的陈旧判决（online 那份
   `evaluate.py` 是旧版本），**本树实测绿才是对的**。主 README 的表格把
   `fays_factory_calibration_contract` 同时列进绿榜和红榜、又漏掉 `codec_benchmark`，
   一并记在此处以免下次再对不上。

## P0.3 不能照原方案做（数据已备，跑法待定）

原方案写的是「用 `run_suite.py` **跑一段录制**出 TUM+ATE/RPE」。查实后这句不成立：

1. **`replay.cc` 吃的是 mav0 数据集，不是我们的 episode。** 生产桥接
   `fayssense_orb_slam.cc:2259` 只收 `<vocab> <orb.yaml> <cam.yaml> [traj.txt]`，
   是实时采集二进制，**没有离线回放入口**；离线只有 `replay.cc` 这一条路，
   它要 `cam{0,1}/data.csv` + `imu0/data.csv`。
2. **我们的录制里没有可转 mav0 的数据。** `data/recordings/<proj>/data/chunk-000/episode-*.parquet`
   的 23 列里只有 `observation.slam_trajectory{,_ns}`（SLAM 的**输出**）；
   `meta/info.json` 声明的 28 个特征里 `observation.imu`、`observation.slam_pose`
   **没有落盘**。`videos/chunk-000/gripper_rgb/` 是 DECXIN **单目 RGB**
   （一条一段），不是 SLAM 的双目对。⇒ SLAM 的双目+IMU 是实时消费、**从不持久化**，
   录完就没了。
3. 所以离线 A/B 的数据只能来自公网：`fetch_dataset.py` 下 TUM-VI room1。
   **2026-09-23 11:15 已下并解包完成**（当时根分区 98%，余 24 G）：

   | | 路径 | 实测量 |
   |---|---|---|
   | 解包后 | `/home/stouch/tumvi/dataset-room1_512_16/mav0/` | 3.2 G |
   | 压缩包 | `/home/stouch/tumvi/dataset-room1_512_16.tar` | 1.6 G（**解包后可删，删了省 1.6 G**） |
   | 校验记录 | `/home/stouch/tumvi/dataset_integrity.json` | `sha256=20354392…398f`，另记了 2 条跳过的 dso 软链 |

   内容：`cam0`/`cam1` 各 **2821** 帧、`imu0` **28123** 行、`mocap0` **16542** 行
   （mocap 是真值，出 ATE 用得上；`dso/` 那部分没解，SLAM 不需要）。
4. 另有两处待处理：`replay.cc` 需要构建（`offline/CMakeLists.txt` 在，未见产物）；
   `replay.cc:37,48` 对时间戳**要求严格单调**（非单调直接 `throw`），而本树基线里
   就有 75 次 `[TIME_DROP]` —— 用真机数据时得先决定怎么处理。

已顺手修掉 `run_suite.py` 的**死路径**（原 `parents[4]/'gripper_version1'` 是
`online/` 时代写的，搬树后 sys.path 指向不存在的目录、整套 ImportError）：
改成向上探测 `core/gripper/fays_runtime.py` + `KSQ_COLLECTOR_ROOT` 覆盖口，
**不再硬编码深度**。`--help` 实测可跑。

**对 L3 判据的影响**：`s80m_stereo_inertial.yaml` 是**夹爪 rig 专用标定**，拿 TUM-VI
喂它，绝对 ATE 数字没有意义；两引擎同数据集同配置跑的**相对**差异才有效
（rig 不匹配的劣势两边一样，会抵消）。L3 的判据应当据此表述。

## SLAM SDK 接入已整体回退（2026-09-23）

厂商 SLAM SDK v0.2.0（`tools/SLAM_SDK_upload`）的接入**全部撤销**：三轮真机会话
（13:52 / 14:10 / 14:21，都跑在 `engine=sdk`）反复出现 IMU 饥饿
（`Insufficient IMU measurements samples=1 coverage_s≈-0.020`）、惯性边丢失
（`ERROR building inertial edge` 1021 / 2373 / 6733）、跟踪器整段丢失、轨迹游走
——结论是这条路不可用。回退到 SDK 接入之前的状态；**本地就有目标，不需要上 GitLab 取旧程序**。

| | 值 | 出处 |
|---|---|---|
| 桥接（**生产位，已换回**） | 234840 B `a76a602c…` | `….pre_rebuild_20260923_113609`（09-16 15:19）= SDK 前最后一版 |
| 桥接源码 | blob `3524fa97` | `d3f7306b`（= `30018f84` 的父提交）那一份 |
| 核心库 | 5442072 B `f3446320…` | **未动**（见下一节；本次回退不需要它） |

回退三步与判据：

```sh
D=core/gripper/native
# ① 先确认旧桥接不依赖 SDK：NEEDED 无 libslam_sdk.so.0、slam_sdk:: 未定义符号 0 条
readelf -d $D/dist/fays_opencv48/bin/fayssense_orb_slam_sn219_opencv48_mark_only | grep -c libslam_sdk   # → 0
nm -DC --undefined-only $D/dist/fays_opencv48/bin/fayssense_orb_slam_sn219_opencv48_mark_only \
  | grep -c 'slam_sdk::'                                                                                # → 0
# ② 换生产位二进制（**顺序关键**：换完再删库；反了会让在役的 SDK 桥接装载失败）
cp -a $D/dist/fays_opencv48/bin/fayssense_orb_slam_sn219_opencv48_mark_only.pre_rebuild_20260923_113609 \
      $D/dist/fays_opencv48/bin/fayssense_orb_slam_sn219_opencv48_mark_only
# ③ 删 SDK 运行库
rm -f $D/dist/orb_mark_only/lib/libslam_sdk.so{,.0,.0.2.0}
```

**这次只换桥接、不动核心库**（与 2.1 那节「两个文件同进同退」相反）：回退后的桥接调的是
`System::MapChanged()`，现役核心库同时导出它和 `GetRuntimeMapChangeIndex()` ⇒ 该桥接的全部
未定义符号都有定义。**回退源码也不丢任何 legacy 功能**：`Host:(<ns>)`、`[TIME_DROP]`、
`[TIME_REBASE]`、`[POSE_REBASE]`、`[POSE_HOLD]`、`TimeRegressionGuard`、CPU 亲和与
`[FAYS-AFFINITY]` 在父提交里**全都已存在**（逐项与 `a76a602c` 对过），唯一被撤掉的是只在
SDK 路径生效的 `g_sdk_imu_carry`。

**已删除**（本次）：`orb_slam_src/slam_sdk/`（厂商源码树 67 文件）、`build.sh` 的 SDK 构建段
（段序回到 `1/3 DBoW2 → 2/3 核心库 → 3/3 桥接`）、`dist/fays_opencv48/CMakeLists.txt` 的
`KSQ_SLAM_SDK_*` 挂载点、契约测试 `test_slam_engine_switch_contract.py`（及
`run_contract_tests.py` 里的条目）、`process_controller.py` 的 `DEFAULT_SLAM_ENGINE` 注入点、
`tools/tests/test_slam_ready_gate.py` 的 [9] 节、部署位 `libslam_sdk.so{,.0,.0.2.0}`、
带 SDK 路径的构建缓存（`build/slam_sdk*` 与 `build/bridge`）。
厂商交付目录 `tools/SLAM_SDK_upload/`（159MB）一并删除，留档
`~/SLAM_SDK_upload_20260923.tgz`（5.7MB；**已排除**那份与 `native/ORBvoc.txt`
逐字节相同（`5420bad0…`）的 139MB 词典，需要时从 `native/` 取）。

**保留**：`System.h` / `System.cc` 的只读适配接口与 `GetRuntimeMapChangeIndex()`（它们在
现役核心库 `f3446320` 里，删掉要重编核心库，而回退只需换桥接）；`pre_sdk_swap_lines.json`、
`bridge.readelf-d.txt`、`bridge.symbols.txt.gz`、`libORB_SLAM3.*` 与
`tools/audit_slam_line_classes.py` 属 P0 记录与通用工具，不是桥接程序。

**L1 实测（回退后）**：契约测试 **12 模块 / 6 OK / 4 SKIP / 2 RED**（RED 的两个是本就陈旧的
契约，与 SDK 无关），`test_orb_preintegration_guard_contract` 6/6 OK；
`tools/tests/test_slam_ready_gate.py` **ALL PASS**。
**L2 实测（回退后）**：真实 `LD_LIBRARY_PATH`（`build_fays_runtime_env()`）下桥接 `ldd`
not found = 0、`ldd -r` undefined symbol = 0、`libORB_SLAM3.so` 只解析到一处、
`libslam_sdk` 一行都没有。
**未做**：L3 真机复验——起主程序录一段，看日志里没有 `[SLAM_ENGINE]`/`[SDK_IMU]`、
没有 `Insufficient IMU measurements samples=1`、位姿列非零、`[FPS_DATA] image≈50 imu≈1000`。

### 空预积分守卫修复（2026-09-23 13:43:51 装入，**桥接未动**）

换 SDK 引擎后「主程序一解算就崩」的根因：`Optimizer::InertialOptimization`
（11 参数重载）的加边循环里，判空是**只打印不跳过**的形态 ——

```cpp
if(!pKFi->mpImuPreintegrated)
    std::cout << "Not preintegrated measurement" << std::endl;
pKFi->mpImuPreintegrated->SetNewBias(...);   // ← 下一行照样解引用
```

原生日志在 `[FATAL_SIGNAL]` 前一行正是那句打印。空预积分是**设计内的合法态**
（`Tracking.cc` 里 IMU 空档期建的关键帧就写 `NULL`），所以这不是竞态，是
「没处理的分支」。同族共 5 处，形态各不相同（只打印不跳过 / 连打印都没有 /
只看 `bImu` / 赋值派生 / 清理路径），全部补上判空 + 跳过。

| | 大小 | md5 | 说明 |
|---|---|---|---|
| `libORB_SLAM3.so`（**新**） | 5442072 | `f3446320…` | 5 处判空 |
| 同上（回退点） | 5442072 | `583a3533…` | 存为 `….pre_rebuild_20260923_134351` |
| 桥接 | 240688 | `92855fd2…` | **未重编**（这次只动核心库） |

**回退只需换一个文件**（与 2.1 那节不同，那次桥接也换了）：

```sh
cp -a core/gripper/native/dist/orb_mark_only/lib/libORB_SLAM3.so.pre_rebuild_20260923_134351 \
      core/gripper/native/dist/orb_mark_only/lib/libORB_SLAM3.so
```

L1 实测：导出符号集 6946 → 6946 **零增零减**；`readelf -d` 的
NEEDED/RPATH/RUNPATH/FLAGS 与回退点**逐字符相同**；同一环境下新旧库
`ldd -r` 输出**逐字节相同**（含同样的 10 项 not-found —— 那是我少给
`LD_LIBRARY_PATH` 造成的，不是回归）；原生 ctest **7/7**；
`test_orb_preintegration_guard_contract` **6/6 OK**（全套 12 模块，
RED 的仍是那 2 个陈旧契约）。

`Not preintegrated measurement` **不命中** `_ERROR_RE`（`protocol.py:96`），
不会把 UI 锁进 Error。

**未做**：L4 真机验收。当初让触发变密的上游条件（SDK 引擎路径按 `imu_ts <= ts` 过滤 IMU
⇒ IMU 饥饿更频繁 ⇒ NULL 预积分的关键帧更多）已随 SDK 接入整体回退而消失，但这 5 处判空
**是在役修复，不要回退**——空预积分本身是设计内的合法态，legacy 路径同样会产生（只是撞得少）。
守卫让那些帧**被静默跳过**（少一条惯性边）而不是崩，所以「不崩了」不等于「解算变好了」，
量级看那句 `Not preintegrated measurement` 的行数。

## 注意

- `readelf -d` 里 **RPATH/NEEDED/FLAGS 必须逐字符相同**，这是主 README 记的成败判据。
  （换 SDK 核那次唯一的故意改动是「新增对 `libslam_sdk.so` 的 NEEDED」，已随回退撤销。）
- 源码树**路径**会经 `__FILE__` 进 `.rodata`，所以 md5 不同**不代表**代码不同。
  判「是不是同一份代码」要看主 README 记的四层比对，不要只看 md5。
- 与其手工跑上面三行，不如直接用 `orb_slam_src/README.md` 里的重编脚本流程；
  本目录只是把「改动前长什么样」钉死。
