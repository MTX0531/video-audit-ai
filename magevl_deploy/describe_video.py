#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mage-VL-4B 视频内容描述客户端
用法:
  python describe_video.py --video /path/to/clip.mp4 [--frames 12] [--host 127.0.0.1] [--port 8000]

流程: 均匀抽帧 -> 缩放 -> JPEG -> base64 -> 调用 optiq serve 的 OpenAI 兼容接口(多帧 image_url)
输出: 模型生成的中文视频内容描述(在场人数 / 出入 / 主要活动)
"""
import argparse
import base64
import io
import json
import sys
import urllib.request

import imageio.v3 as iio
import numpy as np
from PIL import Image


def pil_to_b64(frame: np.ndarray, size: int) -> str:
    """numpy RGB 帧 -> 缩放 -> JPEG -> base64 data uri"""
    img = Image.fromarray(frame).convert("RGB")
    w, h = img.size
    scale = min(size / w, size / h, 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def sample_frames(path: str, n: int = 12, size: int = 512):
    b64_list = []
    meta = iio.immeta(path, plugin="FFMPEG")
    fps = meta.get("fps") or meta.get("sourcefps") or 0
    duration = meta.get("duration") or 0
    total = int(fps * duration) if (fps and duration) else 0
    if total and total > n:
        idxs = sorted({int(round(i * (total - 1) / max(1, n - 1))) for i in range(n)})
        for idx in idxs:
            try:
                fr = iio.imread(path, index=idx, plugin="FFMPEG")
            except Exception:
                continue
            b64_list.append(pil_to_b64(fr, size))
    else:
        step = max(1, total // n) if total else 1
        seen = 0
        cnt = 0
        for fr in iio.imread(path, plugin="FFMPEG"):
            if cnt % step == 0 and seen < n:
                b64_list.append(pil_to_b64(fr, size))
                seen += 1
            cnt += 1
            if seen >= n:
                break
    return b64_list


def describe(video, frames=12, host="127.0.0.1", port=8000, max_tokens=512,
             prompt="请描述这段监控视频的内容，包括：1) 大致在场人数；2) 人员出入情况；3) 主要活动与场景。"):
    b64s = sample_frames(video, n=frames)
    if not b64s:
        print("ERROR: 未从视频中采样到任何帧，请检查视频文件。", file=sys.stderr)
        return None
    content = [{"type": "text", "text": prompt}]
    for b in b64s:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}})
    payload = {
        "model": "mlx-community/Mage-VL-OptiQ-4bit",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    url = f"http://{host}:{port}/v1/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer EMPTY"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"ERROR: 调用推理服务失败: {e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--frames", type=int, default=12)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--prompt", default=None)
    args = ap.parse_args()
    out = describe(args.video, args.frames, args.host, args.port, args.max_tokens, args.prompt)
    if out:
        print("\n===== Mage-VL-4B 视频内容描述 =====\n")
        print(out)
        print("\n===================================\n")


if __name__ == "__main__":
    main()
