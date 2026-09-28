#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态误检定位器：找出被 YOLO 长期稳定识别为「person」的静止物体（道具骷髅/画报/雕像等）。

原理：真人会移动，同一位置不会被反复命中；而道具静止不动，几乎每一帧都在同一个位置
产生一个 person 框。于是「某位置在采样帧中的命中率」就是区分真人与道具的天然判据。

输出：
  1) 文本表：候选静态目标（命中率高、位置稳定）的中心、框大小、平均置信度、命中率
  2) 可选 --crop 目录：把每个候选框裁出的图像片段各存一张，便于人工确认为道具

用法：
  PYTHONPATH=./_ultra_pkg python find_static_fp.py <视频> \
      --weights ./models/yolov8m_weights/yolov8m.pt --stride 175 --conf 0.15 \
      --device mps [--crop out_dir] [--top 12]
"""
import argparse
import json
import os
import sys

import cv2
import numpy as np


def cluster_boxes(samples, iou_thr=0.25):
    """把跨帧检测框按重叠程度聚成「同一物理目标」。
    samples: list of dict(cx,cy,x1,y1,x2,y2,conf,frame)
    返回 list of cluster dict，含 bbox 中位数、命中帧数、平均置信度。"""
    clusters = []
    for s in samples:
        placed = False
        for c in clusters:
            # 与簇当前代表框中位框比 IoU + 中心距离
            bx = c["box"]
            ix1, iy1 = max(s["x1"], bx[0]), max(s["y1"], bx[1])
            ix2, iy2 = min(s["x2"], bx[2]), min(s["y2"], bx[3])
            iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
            inter = iw * ih
            a1 = (s["x2"] - s["x1"]) * (s["y2"] - s["y1"])
            a2 = (bx[2] - bx[0]) * (bx[3] - bx[1])
            iou = inter / (a1 + a2 - inter) if (a1 + a2 - inter) > 0 else 0
            dc = ((s["cx"] - c["cx"]) ** 2 + (s["cy"] - c["cy"]) ** 2) ** 0.5
            if iou > iou_thr or dc < 0.5 * max(a1 ** 0.5, a2 ** 0.5):
                c["hits"].append(s)
                bx2 = np.median([[h["x1"], h["y1"], h["x2"], h["y2"]] for h in c["hits"]], axis=0)
                c["box"] = [float(v) for v in bx2]
                c["cx"], c["cy"] = (bx2[0] + bx2[2]) / 2, (bx2[1] + bx2[3]) / 2
                placed = True
                break
        if not placed:
            clusters.append({"box": [s["x1"], s["y1"], s["x2"], s["y2"]],
                             "cx": s["cx"], "cy": s["cy"], "hits": [s]})
    return clusters


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--weights", default="./models/yolov8m_weights/yolov8m.pt")
    ap.add_argument("--stride", type=int, default=175)
    ap.add_argument("--conf", type=float, default=0.15)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--device", default="mps")
    ap.add_argument("--crop", default=None, help="候选框裁剪图的输出目录")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    from ultralytics import YOLO

    model = YOLO(args.weights)
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"[error] 无法打开视频: {args.video}", file=sys.stderr)
        sys.exit(1)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    print(f"[info] fps={fps:.2f} 总帧={total} stride={args.stride}", file=sys.stderr)

    samples, n_frames = [], 0
    idx = 0
    while idx < total:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if not ret:
            break
        n_frames += 1
        res = model(frame, classes=[0], conf=args.conf, imgsz=args.imgsz,
                    device=args.device, verbose=False)[0]
        b = res.boxes
        if b is not None and len(b) > 0:
            xyxy = b.xyxy.cpu().numpy()
            cf = b.conf.cpu().numpy()
            for i in range(len(xyxy)):
                x1, y1, x2, y2 = [float(v) for v in xyxy[i]]
                samples.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                "cx": (x1 + x2) / 2, "cy": (y1 + y2) / 2,
                                "conf": float(cf[i]), "t": round(idx / fps, 1)})
        if n_frames % 100 == 0:
            print(f"  ... {n_frames} 帧, 累计 {len(samples)} 个检测", file=sys.stderr)
        idx += args.stride
    cap.release()

    print(f"[info] 共 {n_frames} 个采样帧，{len(samples)} 个 person 检测", file=sys.stderr)
    clusters = cluster_boxes(samples)
    for c in clusters:
        c["hit_frames"] = len({h["t"] for h in c["hits"]})
        c["ratio"] = c["hit_frames"] / max(n_frames, 1)
        c["mean_conf"] = float(np.mean([h["conf"] for h in c["hits"]]))
        c["max_conf"] = float(np.max([h["conf"] for h in c["hits"]]))
        c["area"] = (c["box"][2] - c["box"][0]) * (c["box"][3] - c["box"][1])

    # 只看「长期稳定存在」的：命中率 >= 15%
    static = [c for c in clusters if c["ratio"] >= 0.15]
    static.sort(key=lambda c: (-c["ratio"], -c["area"]))

    print("\n===== 候选静态目标（命中率 >= 15%，极可能是道具） =====")
    print("%5s %6s %6s %8s %8s %7s %7s  %s" %
          ("命中率", "cx", "cy", "w", "h", "面积", "平均conf", "框(x1,y1,x2,y2)"))
    for c in static[:args.top]:
        bx = [round(v) for v in c["box"]]
        print("%5.0f%% %6.0f %6.0f %8.0f %8.0f %7.0f %7.2f  %s" %
              (c["ratio"] * 100, c["cx"], c["cy"], bx[2] - bx[0], bx[3] - bx[1],
               c["area"], c["mean_conf"], bx))

    # 顺带给出"命中率中等"的可疑名单（可能是长期有人停留的岗位，也可能是偶发误检）
    mid = [c for c in clusters if 0.05 <= c["ratio"] < 0.15]
    mid.sort(key=lambda c: -c["ratio"])
    if mid:
        print("\n----- 次可疑（命中率 5%~15%） -----")
        for c in mid[:args.top]:
            bx = [round(v) for v in c["box"]]
            print("%5.0f%% cx=%4.0f cy=%4.0f  %s  conf=%.2f" %
                  (c["ratio"] * 100, c["cx"], c["cy"], bx, c["mean_conf"]))

    if args.crop:
        os.makedirs(args.crop, exist_ok=True)
        cap = cv2.VideoCapture(args.video)
        # 取每个候选"平均帧"位置的实景裁剪：用命中率最高的那一帧时间点回放
        for n, c in enumerate(static[:args.top], 1):
            t = max(h["t"] for h in c["hits"])
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
            ret, frame = cap.read()
            if not ret:
                continue
            x1, y1, x2, y2 = [int(v) for v in c["box"]]
            m = 24
            pad = frame[max(0, y1 - m):min(frame.shape[0], y2 + m),
                        max(0, x1 - m):min(frame.shape[1], x2 + m)]
            if pad.size:
                fn = os.path.join(args.crop, f"cand{n:02d}_r{int(c['ratio']*100)}_c{int(c['cx'])}_{int(c['cy'])}.jpg")
                cv2.imwrite(fn, pad)
        cap.release()
        print(f"\n[info] 候选裁剪图已存至 {args.crop}", file=sys.stderr)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"sampled_frames": n_frames, "n_det": len(samples),
                       "static": [{k: c[k] for k in
                                   ("box", "cx", "cy", "ratio", "mean_conf", "max_conf", "area", "hit_frames")}
                                  for c in static]}, f, ensure_ascii=False, indent=2)
        print(f"[info] JSON 已写入 {args.json_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
