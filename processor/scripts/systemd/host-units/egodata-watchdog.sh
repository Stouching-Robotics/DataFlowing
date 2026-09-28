#!/bin/bash
# EgoData 看门狗:挂载掉了重挂,API 掉了重启。
# 由 egodata-watchdog.timer 每分钟调用一次;只做本地检查(不读 NAS 数据),
# 开销可忽略。systemctl --user 在用户单元里可用,输出进 journal。
set -u

MOUNT=/home/stouching/.cache/egodata-data
API_URL=http://127.0.0.1:8000/health
STATE_FILE=/run/user/1000/egodata-watchdog.fails
FAIL_THRESHOLD=2      # 连续 2 次不健康才动手,避开"启动中"的误判

# 1) 挂载在不在(mountpoint 查内核挂载表,零网络开销)。
#    断线可能留下"僵尸挂载"(Transport endpoint is not connected):
#    mountpoint 判为未挂载,但残留条目会让 mkdir/sshfs 全部失败 ——
#    先强制卸载清掉,再重启挂载单元(单元里也有同样的 ExecStartPre 兜底)。
if ! mountpoint -q "$MOUNT"; then
    echo "storage 未挂载,清理残留并重启 egodata-storage"
    fusermount3 -u -z "$MOUNT" 2>/dev/null
    systemctl --user reset-failed egodata-storage.service 2>/dev/null
    systemctl --user restart egodata-storage.service
    sleep 5
fi

# 2) API 健不健康。启动中(activating)直接放行 —— API 冷启要约 20s,
#    正好落在两次巡检之间会被误判成宕机并反复重启。
state=$(systemctl --user is-active egodata-api.service 2>/dev/null)
if [ "$state" = "activating" ] || [ "$state" = "deactivating" ]; then
    echo "API 正在启动/停止中($state),跳过本次巡检"
    exit 0
fi

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$API_URL" 2>/dev/null)
if [ "$code" = "200" ]; then
    rm -f "$STATE_FILE"
    exit 0
fi

fails=$(( $(cat "$STATE_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE_FILE"
if [ "$fails" -lt "$FAIL_THRESHOLD" ]; then
    echo "API health=$code(连续第 $fails 次),再观察一轮"
    exit 0
fi

echo "API health=$code(连续 $fails 次),重启 egodata-api"
rm -f "$STATE_FILE"
systemctl --user restart egodata-api.service
