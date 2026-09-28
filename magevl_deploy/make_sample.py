#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成一段用于端到端自测的"假监控"视频 (无需用户提供真实视频即可验证链路)。
画面: 模拟便利店柜台, 几个人形方块在移动/进出。仅用于验证"抽帧->推理->出描述"通路。
输出: sample_clip.mp4
"""
import numpy as np
from PIL import Image, ImageDraw
import imageio.v3 as iio

W, H, FPS, SECS = 640, 360, 10, 12
frames = []
for t in range(FPS * SECS):
    img = Image.new("RGB", (W, H), (30, 30, 35))
    d = ImageDraw.Draw(img)
    # 柜台
    d.rectangle([40, 250, 600, 270], fill=(90, 90, 100))
    d.text((20, 20), f"TEST CAM 01  t={t//FPS}s", fill=(220, 220, 220))
    # 三个人形(用不同颜色方块+头)做进出移动
    people = [
        (50 + t * 8, 200, (200, 60, 60)),
        (300 - t * 5, 180, (60, 180, 90)),
        (W - 80 - t * 3, 210, (70, 120, 220)),
    ]
    for (x, y, c) in people:
        x = max(20, min(W - 40, x))
        d.ellipse([x, y, x + 24, y + 24], fill=c)          # 头
        d.rectangle([x + 4, y + 24, x + 20, y + 70], fill=c)  # 身体
    frames.append(np.asarray(img))

iio.imwrite("sample_clip.mp4", np.stack(frames), fps=FPS, codec="libx264", plugin="FFMPEG")
print("已生成 sample_clip.mp4", frames[0].shape)
