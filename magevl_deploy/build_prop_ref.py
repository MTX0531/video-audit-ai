#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""道具护栏（prop-guard）——从源头上避免把静止道具（骷髅/画报/雕像/猪头等）计成人数。

思路
----
密室/展厅类机位里，道具是**永久静止**的，而真人是流动的。于是：
  1) 先用"无人帧"（YOLO 在该帧一个 person 都没检出的帧）取逐像素中位数，
     得到一张**空场参考图** ref —— 它保留了房间里所有静止物件（含道具），
     但不含任何真人（真人不会在所有无人帧里都出现）。
  2) 计数时，对每一个 person 检测框，把该框在"当前帧"与"参考图"同一位置裁剪出来比对：
       · 若两者几乎一致  → 该框看到的就是**静止道具** → 丢弃（不计入人数）
       · 若差异明显      → 说明框里是**临时出现的东西（真人）** → 保留
  3) 这样"人站在道具前面"也不会误删：人在时该区域与参考图不同，照常计入。

本脚本负责第 1 步（生成空场参考图）；计数脚本 count_people.py 用 --prop-ref 使用它。

用法：
  PYTHONPATH=./_ultra_pkg python build_prop_ref.py <视频> --out <ref.jpg> \
      [--weights ./models/yolov8m_weights/yolov8m.pt] [--stride 300] [--max-ref 120]
"""
import argparse
import os
import sys

import cv2
import numpy as np


def build_ref(video, weights, out_path, stride=300, conf=0.20, imgsz=640,
              device="mps", max_ref=120, min_ref=8):
    from ultralytics import YOLO

    model = YOLO(weights)
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        print(f"[error] 无法打开视频: {video}", file=sys.stderr)
        return False
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    empties = []
    idx = 0
    while idx < total and len(empties) < max_ref:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if not ret:
            break
        res = model(frame, classes=[0], conf=conf, imgsz=imgsz, device=device,
                    verbose=False)[0]
        b = res.boxes
        if b is None or len(b) == 0:
            empties.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).copy())
        idx += stride
    cap.release()

    print(f"[info] 无人帧 {len(empties)} 张（步长 {stride}）", file=sys.stderr)
    if len(empties) < min_ref:
        print(f"[error] 无人帧不足 {min_ref} 张，无法生成可靠的空场参考图。"
              f"可调大 --stride 或降低 --conf。", file=sys.stderr)
        return False

    h = min(e.shape[0] for e in empties)
    w = min(e.shape[1] for e in empties)
    stack = np.stack([e[:h, :w] for e in empties], axis=0)
    ref = np.median(stack, axis=0).astype(np.uint8)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    cv2.imwrite(out_path, ref)
    print(f"[ok] 空场参考图已写入 {out_path}  ({w}x{h}, 由 {len(empties)} 张无人帧取中位数)",
          file=sys.stderr)
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", required=True)
    ap.add_argument("--weights", default="./models/yolov8m_weights/yolov8m.pt")
    ap.add_argument("--stride", type=int, default=300)
    ap.add_argument("--conf", type=float, default=0.20)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--device", default="mps")
    ap.add_argument("--max-ref", type=int, default=120)
    a = ap.parse_args()
    ok = build_ref(a.video, a.weights, a.out, a.stride, a.conf, a.imgsz,
                   a.device, a.max_ref)
    sys.exit(0 if ok else 1)
