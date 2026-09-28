#!/usr/bin/env bash
# 安装本目录下的 systemd 用户单元(生产机实际配置)。
#
# 覆盖内容:NAS 挂载(egodata-storage)、API(egodata-api)、worker(egodata-worker)、
# 看门狗(每分钟巡检挂载+API 并自动恢复)。
#
# 用法:bash install.sh
# 卸载:systemctl --user disable --now egodata-storage egodata-api egodata-worker egodata-watchdog.timer
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
BIN_DIR="${HOME}/.local/bin"

mkdir -p "${UNIT_DIR}" "${BIN_DIR}"

install -m 644 "${HERE}"/egodata-storage.service \
               "${HERE}"/egodata-api.service \
               "${HERE}"/egodata-worker.service \
               "${HERE}"/egodata-watchdog.service \
               "${HERE}"/egodata-watchdog.timer "${UNIT_DIR}/"
install -m 755 "${HERE}/egodata-watchdog.sh" "${BIN_DIR}/egodata-watchdog.sh"

# worker 的 API key 文件(含密钥,故意不入库):不存在时建占位,需人工填。
ENV_FILE="${HOME}/.config/egodata-worker-env"
if [ ! -f "${ENV_FILE}" ]; then
    umask 077
    echo "EGODATA_WORKER_API_KEY=change-me" > "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    echo "⚠️  已生成 ${ENV_FILE} 占位,请填入真实 WORKER_API_KEY(取值见 processor/.env)"
fi

systemctl --user daemon-reload
systemctl --user enable --now egodata-storage.service \
                            egodata-api.service \
                            egodata-worker.service \
                            egodata-watchdog.timer

echo "✅ 安装完成。状态:"
systemctl --user is-active egodata-storage egodata-api egodata-worker egodata-watchdog.timer
