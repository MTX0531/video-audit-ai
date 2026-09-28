#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""道具护栏复核工具：把「被判为道具而丢弃」的检测逐条裁出来做拼图，供人工确认。

用法：
  python verify_propguard.py <key> [--out 拼图.jpg] [--max 24]

输出：一张拼图，每格左=当前帧该框画面、右=空场参考图同位置画面；
      标题行给出 时间/置信度/mad/ncc。若某格右边没有道具（而是真人），
      说明护栏误删了真人，需要调 --prop-mad/--prop-ncc 或收窄 --prop-roi。
"""
import argparse
import json
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(ROOT, "results")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("key")
    ap.add_argument("--out", default=None)
    ap.add_argument("--max", type=int, default=24)
    ap.add_argument("--cols", type=int, default=6)
    a = ap.parse_args()

    d = os.path.join(RES, a.key)
    s = json.load(open(os.path.join(d, "count_summary.json"), encoding="utf-8"))
    pg = s.get("prop_guard") or {}
    samples = pg.get("dropped_samples") or []
    print(f"[{a.key}] 峰值={s['person_peak']} 平均={s['person_avg']} 采样帧={s['sampled_frames']}")
    print(f"  道具护栏: {pg.get('enabled')} 区域={pg.get('roi')} mad<{pg.get('mad_lt')} ncc>{pg.get('ncc_gt')} "
          f"丢弃总数={pg.get('dropped')}")
    if not samples:
        print("  无可复核样本")
        return
    out = a.out or os.path.join(d, "propguard_复核.jpg")
    video = s["video"]
    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0

    cell_w, cell_h = 168, 236
    cols = a.cols
    n = min(len(samples), a.max)
    rows = (n + cols - 1) // cols
    sheet = np.full((rows * (cell_h + 26), cols * (cell_w * 2 + 8), 3), 255, np.uint8)
    for i, smp in enumerate(samples[:a.max]):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(smp["t_sec"] * fps))
        ok, fr = cap.read()
        if not ok:
            continue
        x1, y1, x2, y2 = smp["box"]
        m = 8
        x1c, y1c = max(0, x1 - m), max(0, y1 - m)
        x2c, y2c = min(fr.shape[1], x2 + m), min(fr.shape[0], y2 + m)
        left = fr[y1c:y2c, x1c:x2c].copy()
        cv2.rectangle(left, (x1 - x1c, y1 - y1c), (x2 - x1c, y2 - y1c), (0, 90, 255), 2)
        left = cv2.resize(left, (cell_w, cell_h))
        r, c = divmod(i, cols)
        y0 = r * (cell_h + 26)
        x0 = c * (cell_w * 2 + 8)
        sheet[y0:y0 + cell_h, x0:x0 + cell_w] = left
        lab = f"t={smp['t_sec']:.0f}s conf={smp['conf']:.2f} mad={smp['mad']:.3f} ncc={smp['ncc']:.2f}"
        cv2.putText(sheet, lab, (x0 + 2, y0 + cell_h + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 0), 1, cv2.LINE_AA)
    cap.release()
    cv2.imwrite(out, sheet)
    print(f"  复核拼图已写出: {out}（{n} 格）")


if __name__ == "__main__":
    main()
