#!/usr/bin/env bash
# DAQ 程序启动脚本 —— 使用项目自带 venv 运行
# 用法: ./run.sh
cd "$(dirname "$0")"

if [ ! -x venv/bin/python ]; then
    echo "[错误] 还没部署: 找不到 venv/bin/python"
    echo "  请先运行一键部署脚本（自动建 venv、装依赖、展开手套工具包）:"
    echo "      ./start.sh"
    echo "  不要手工 python -m venv —— 那样会绕过离线 wheels/ 与依赖签名，"
    echo "  装完仍可能缺包。部署过一次之后，日常用本脚本启动最快。"
    exit 1
fi

exec venv/bin/python main.py "$@"
