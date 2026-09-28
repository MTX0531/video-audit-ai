#!/bin/bash
# 一键视频描述：输入视频路径，输出中文结构化描述（Qwen2.5-VL via mlx-vlm）
# 用法： bash run_describe.sh /你的视频路径.mp4
set -e
cd "$(dirname "$0")"
. vlm_venv/bin/activate
export HF_ENDPOINT="https://hf-mirror.com"
export HF_HUB_ENDPOINT="https://hf-mirror.com"
if [ -z "$1" ]; then
  echo "用法: bash run_describe.sh <视频路径> [--max-tokens 600]"
  exit 1
fi
python describe_qwen.py "$1" --model ./models/Qwen2.5-VL-3B-Instruct-4bit --max-tokens 600
