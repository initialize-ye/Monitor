#!/bin/bash
# QQ Bot 启动脚本

cd "$(dirname "$0")"

# 激活虚拟环境
source .venv/bin/activate

# 启动机器人
echo "正在启动 QQ 机器人..."
python bot.py
