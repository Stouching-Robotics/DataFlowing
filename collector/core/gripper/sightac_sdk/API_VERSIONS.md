# Sightac API 版本

生产默认继续使用经过现场验证的 `api`（2.4.5）。版本选择必须显式，不能通过
替换目录或修改 `sys.path` 顺序暗中切换。

| 包目录 | 用途 | 默认加载 |
| --- | --- | --- |
| `api/` | 当前生产稳定版 2.4.5 | 是 |
| `api_legacy_2_4_5/` | 2.4.5 完整恢复快照 | 否 |
| `api_v3_2_2_ksq/` | 新兼容版 3.2.3-ksq.1 | 否 |
| `api_new/` | 厂商 3.2.2 原始参考包；目录结构不完整 | 否 |

新兼容版以稳定 2.4.5 上位机契约为基础，保留 `defer_baseline`、
`read_raw_frame`、`preprocess_raw_frame`、`process_raw_frame`、
`update_frame` 和 `update_calculation`，并合入斜面款负 Hue 不参与 Fz
标定的修复。新旧目录携带的 `libSonixCamera.so` 内容相同。

仅在完成 `FAYS_ONLY.md` 的现场顺序验收时，临时选择新版本：

```bash
KSQ_SIGHTAC_API_PACKAGE=api_v3_2_2_ksq \
  /home/admin1/gripper_env/bin/python \
  /home/admin1/KSQ_Gripper/gripper_version1/upper_computer_fays_opencv48.py
```

不设置 `KSQ_SIGHTAC_API_PACKAGE` 时始终回到生产稳定版。紧急恢复快照可显式
设置为 `api_legacy_2_4_5`；不得删除或覆盖恢复快照。
