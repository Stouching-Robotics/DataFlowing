# 生产机 systemd 用户单元(实际运行配置)

本目录是生产机 `192.168.110.58` 上**实际在跑**的那套 systemd 用户单元,入库是为了
换机/重装时可直接复用。与上级目录的 `*.service.in`(deploy.py 的模板,另一套命名
`egodata-backend` / `egodata-workers`)并存,互不影响 —— 当前运行的是本目录这套。

## 包含什么

| 文件 | 作用 |
|---|---|
| `egodata-storage.service` | sshfs 挂载 NAS 数据(`~/.cache/egodata-data`)。含**僵尸挂载自愈**:启动前先 `fusermount3 -u -z` 清理残留,再重挂 |
| `egodata-api.service` | uvicorn 后端 + 前端网页(同一进程,0.0.0.0:8000)。`Requires`+`After` 挂载,挂载没就绪不会启动 |
| `egodata-worker.service` | 异步处理 worker(领工作流任务) |
| `egodata-watchdog.sh` + `.service` + `.timer` | 每分钟巡检:挂载掉了重挂、API 不健康重启。启动中(activating)跳过、连续 2 次不健康才动手,避开 API 冷启约 20s 的误判 |
| `install.sh` | 一键安装到 `~/.config/systemd/user/` + `~/.local/bin/` |

## 为什么这样配(踩过的坑)

1. **换网络/休眠后服务全挂**:sshfs 断线 → 挂载单元失败 → API 启动时扫描数据目录撞上
   死挂载崩溃 → systemd 因"正常退出码"不重启。已用 `Restart=always` + `Requires=` 修正。
2. **僵尸挂载**(`Transport endpoint is not connected`):断线后挂载点残留,`mkdir`/`sshfs`
   全失败,重启重试 73 次也挂不上。已用 `ExecStartPre=fusermount3 -u -z` 自愈。
3. **API 冷启约 20s**:看门狗若在这期间判为宕机就会反复重启,故加 `activating` 跳过 +
   连续 2 次阈值。

## 安装

```bash
bash install.sh
```

依赖:
- `sshfs`(`processor/.tools/sshfs/usr/bin/sshfs`,单元里写死此路径)
- NAS 的 SSH 私钥 `~/.ssh/id_rsa`
- `~/.config/egodata-worker-env`(含 `EGODATA_WORKER_API_KEY`,**不入库**,install.sh 会
  生成占位,取值见 `processor/.env`)

## 常用操作

```bash
systemctl --user status egodata-api          # 状态
journalctl --user -u egodata-api -f          # 实时日志
systemctl --user restart egodata-api         # 重启后端(前端同一个进程)
systemctl --user list-timers egodata-watchdog.timer   # 看门狗巡检计划
```

> 注意:前端与后端是**同一个服务**(uvicorn 同时提供 API 和网页模板),没有独立的前端进程。
