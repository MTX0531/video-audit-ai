#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mage-VL-4B 本地视频描述（自包含，后台运行）
直接走 OptiQ 官方 OptiqEngine API，绕过 optiq serve 的 Cloud Boost bug。

用法（在 venv 中、后台运行）:
  python run_vlm.py --video sample_clip.mp4
  python run_vlm.py --video 你的视频.mp4 --frames 8 --max-tokens 400

说明:
- 首次运行会加载约 3.7GB 模型到 Apple Silicon 统一内存，约需 1~2 分钟。
- 视频按均匀抽帧 -> 存为临时 JPEG -> 作为多帧传给 Mage-VL (它本就吃多帧=视频)。
- 仅做语义描述；精确计数请由 CV 层负责。
"""
import argparse
import os
import tempfile
import sys

import imageio.v3 as iio
import numpy as np
from PIL import Image

MODEL_ID = "mlx-community/Mage-VL-OptiQ-4bit"
DEFAULT_PROMPT = (
    "请描述这段监控视频的内容，包括：1) 大致在场人数；"
    "2) 人员出入情况；3) 主要活动与场景；4) 是否有异常。"
)


def sample_frame_paths(path: str, n: int = 8, size: int = 512):
    """均匀抽帧，写临时 JPEG，返回路径列表"""
    meta = iio.immeta(path, plugin="FFMPEG")
    fps = meta.get("fps") or meta.get("sourcefps") or 0
    duration = meta.get("duration") or 0
    total = int(fps * duration) if (fps and duration) else 0
    paths = []
    tmpdir = tempfile.mkdtemp(prefix="magevl_")
    if total and total > n:
        idxs = sorted({int(round(i * (total - 1) / max(1, n - 1))) for i in range(n)})
        for k, idx in enumerate(idxs):
            try:
                fr = iio.imread(path, index=idx, plugin="FFMPEG")
            except Exception:
                continue
            p = os.path.join(tmpdir, f"f{k:03d}.jpg")
            Image.fromarray(fr).convert("RGB").resize(
                (min(size, fr.shape[1]), min(size, fr.shape[0]))
            ).save(p, quality=85)
            paths.append(p)
    else:
        step = max(1, total // n) if total else 1
        seen = cnt = 0
        for fr in iio.imread(path, plugin="FFMPEG"):
            if cnt % step == 0 and seen < n:
                p = os.path.join(tmpdir, f"f{seen:03d}.jpg")
                Image.fromarray(fr).convert("RGB").resize(
                    (min(size, fr.shape[1]), min(size, fr.shape[0]))
                ).save(p, quality=85)
                paths.append(p)
                seen += 1
            cnt += 1
            if seen >= n:
                break
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    args = ap.parse_args()

    print(f"[1/3] 抽帧: {args.video} -> {args.frames} 帧", flush=True)
    frames = sample_frame_paths(args.video, n=args.frames)
    if not frames:
        print("ERROR: 未能从视频采样到任何帧", file=sys.stderr)
        return
    print(f"      采样到 {len(frames)} 帧", flush=True)

    print("[2/3] 加载 Mage-VL-4B (约 1~2 分钟, 首次较慢)...", flush=True)
    from mlx_lm import load
    from optiq.runtime.engine import OptiqEngine

    import optiq  # 注册 mage_vl 架构 + 视觉 sidecar
    model, tok = load(MODEL_ID)
    eng = OptiqEngine.from_loaded(model, tok, MODEL_ID)
    print("      模型就绪", flush=True)

    print("[3/3] 生成描述...", flush=True)
    st = eng.generate(args.prompt, images=frames, max_tokens=args.max_tokens)
    text = getattr(st, "text", str(st))
    print("\n===== Mage-VL-4B 视频内容描述 =====\n")
    print(text)
    print("\n===================================\n")


if __name__ == "__main__":
    main()
