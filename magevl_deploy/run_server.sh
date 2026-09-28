#!/usr/bin/env bash
# 启动 Mage-VL-OptiQ-4bit 推理服务 (OpenAI 兼容接口)
# 注意: mlx 需 Metal, 前台 Bash 沙箱会杀掉 import; 故必须用后台运行本脚本。
# HuggingFace 直连不通, 强制走国内镜像 hf-mirror.com 下载权重。
set -e
cd "$(dirname "$0")"
. .venv/bin/activate
export HF_ENDPOINT="https://hf-mirror.com"
export HF_HUB_ENDPOINT="https://hf-mirror.com"
export PYTHONUNBUFFERED=1
echo ">>> 启动 optiq serve (模型: mlx-community/Mage-VL-OptiQ-4bit)"
echo ">>> 首次运行会自动从镜像下载约 3.7GB 权重, 请耐心等待"
exec python -m optiq.cli serve --no-auth --host 127.0.0.1 --port 8000 --model mlx-community/Mage-VL-OptiQ-4bit
