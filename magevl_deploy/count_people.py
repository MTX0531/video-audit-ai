#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CV 精确人数计数器（与 VLM 的"粗估"对照）
- 模型：YOLOv8n（默认）/ YOLOv8m（推荐，COCO person 类）做人体检测
- 跟踪：ByteTrack（persist=True 跨帧保持 ID）
- 抽帧：按帧索引 seek，不逐帧解码整段视频（省时）
- 输出：每采样帧在场人数、峰值/均值/最小值、30 秒分桶时间线、唯一个体数(近似)
- 可选：--ignore-roi x1,y1,x2,y2 屏蔽固定误检区域（中心点落入即忽略）
- 可选：--nms-iou 0.5 person 框 NMS 去重阈值（消除同一目标的重复检测框）
- PEAK 证据帧：只画"去重后保留"的框，保证图像框数 == 摘要人数
- 说明：这是"具体人数"，VLM 只做语义描述与标签，精确计数归 CV。

用法：
  PYTHONPATH=./_ultra_pkg python count_people.py <视频> --weights ./models/yolov8m_weights/yolov8m.pt --stride 98 --conf 0.20 --nms-iou 0.5
"""
import sys, os, csv, json, argparse
import cv2
import numpy as np


def _iou(a, b):
    """两个 [x1,y1,x2,y2] 框的 IoU"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    return inter / (area_a + area_b - inter)


def _nms(boxes_xyxy, confs, iou_thr=0.5):
    """按置信度降序的标准 NMS，返回保留的索引列表（针对 person 框）"""
    idxs = sorted(range(len(confs)), key=lambda i: confs[i], reverse=True)
    keep = []
    while idxs:
        i = idxs.pop(0)
        keep.append(i)
        idxs = [j for j in idxs if _iou(boxes_xyxy[i], boxes_xyxy[j]) < iou_thr]
    return keep


def _dedupe_persons(xyxy, confs, ids, nms_iou=0.5, low_conf=0.30, low_iou=0.3):
    """过滤同一目标的重复检测框：
    1) 标准 NMS（iou_thr=nms_iou）
    2) 低置信二次去重：conf<low_conf 且与任一保留框 IoU>low_iou → 视为重复/噪声框丢弃
    返回保留索引列表"""
    keep = _nms(xyxy, confs, nms_iou)
    final = []
    for k in keep:
        if confs[k] < low_conf and any(_iou(xyxy[k], xyxy[m]) > low_iou for m in final):
            continue
        final.append(k)
    return final


def _in_any_roi(cx, cy, rois):
    return any(x1 <= cx <= x2 and y1 <= cy <= y2 for (x1, y1, x2, y2) in rois)


def _area_ok(box, cboxes, lo, hi):
    """检测框面积是否与"道具本体框"面积相当（lo/hi 为相对倍率）。
    用于把"人站在道具前"产生的大框排除在道具判据之外。"""
    a = (box[2] - box[0]) * (box[3] - box[1])
    for (x1, y1, x2, y2) in cboxes:
        ca = (x2 - x1) * (y2 - y1)
        if ca > 0 and lo * ca <= a <= hi * ca:
            return True
    return False


def _build_prop_ref(cap, model, stride, conf, imgsz, device, max_ref=120, min_ref=6):
    """预扫描：用"某帧在道具区内没有任何 person 检测"的帧取逐像素中位数，
    得到一张**空场参考图**（保留静止道具与背景，不含真人）。
    返回灰度 numpy 数组；样本不足时返回 None（此时不使用护栏）。"""
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
    print(f"[info] 道具护栏：预扫描得到 {len(empties)} 张空场帧（步长 {stride}）",
          file=sys.stderr)
    if len(empties) < min_ref:
        print("[warn] 空场帧不足，道具护栏退化：仅按区域屏蔽（不再做外观比对）",
              file=sys.stderr)
        return None
    h = min(e.shape[0] for e in empties)
    w = min(e.shape[1] for e in empties)
    stack = np.stack([e[:h, :w] for e in empties], axis=0)
    return np.median(stack, axis=0).astype(np.uint8)


def _appearance_match(frame_gray, ref_gray, box):
    """把 box 区域在"当前帧"与"空场参考图"里各裁一份，判断两者是否几乎一致。

    返回 (mad, ncc)：
      mad = 归一化平均灰度差（0=逐像素相同）
      ncc = 梯度图（Sobel）归一化互相关（1=结构完全一致）
    两者同时满足阈值 ⇒ 该框看到的就是**静止道具**（空场时它也在，且长得一样）。
    真人只要出现，区域内灰度/结构就会偏离空场参考 ⇒ 不会被误判。
    """
    x1, y1, x2, y2 = [int(round(v)) for v in box]
    x1 = max(0, x1); y1 = max(0, y1)
    x2 = min(frame_gray.shape[1], x2); y2 = min(frame_gray.shape[0], y2)
    if x2 - x1 < 12 or y2 - y1 < 12:
        return None
    a = cv2.resize(frame_gray[y1:y2, x1:x2], (64, 128))
    b = cv2.resize(ref_gray[y1:y2, x1:x2], (64, 128))
    d = np.abs(a.astype(np.int16) - b.astype(np.int16))
    mad = float(d.mean() / 255.0)
    ab = cv2.GaussianBlur(a, (5, 5), 0).astype(np.float32)
    bb = cv2.GaussianBlur(b, (5, 5), 0).astype(np.float32)
    ga = cv2.Sobel(ab, cv2.CV_32F, 1, 1, ksize=3)
    gb = cv2.Sobel(bb, cv2.CV_32F, 1, 1, ksize=3)
    ga = (ga - ga.mean()) / (ga.std() + 1e-6)
    gb = (gb - gb.mean()) / (gb.std() + 1e-6)
    return mad, float((ga * gb).mean())


def _annotate_kept(frame, xyxy, confs, kept_idx):
    """只画"去重后保留"的 person 框，避免 PEAK 帧的视觉框数与摘要人数不一致。
    与 res.plot() 的区别：本函数严格只绘制计票时保留下来的框，框数 == count。"""
    img = frame.copy()
    for k in kept_idx:
        x1, y1, x2, y2 = [int(round(v)) for v in xyxy[k]]
        c = float(confs[k])
        cv2.rectangle(img, (x1, y1), (x2, y2), (255, 90, 30), 2)
        label = f"person {c:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        ty = max(th + 2, y1 - 4)
        cv2.rectangle(img, (x1, max(0, y1 - th - 6)), (x1 + tw + 4, ty + 4),
                      (255, 90, 30), -1)
        cv2.putText(img, label, (x1 + 2, ty),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return img



def count(video, weights, stride=30, conf=0.30, imgsz=640,
          device="mps", out_csv="counts.csv", out_json="count_summary.json",
          sample_dir=None, annotate_every=60, ignore_roi=None, nms_iou=0.5,
          min_area=0, no_track=False, prop_roi=None, prop_guard=False,
          prop_ref_path=None, prop_mad=0.10, prop_ncc=0.80,
          prop_ref_stride=260, prop_ref_gap=600.0,
          prop_core_roi=None, prop_min_conf=0.45, prop_core_mad=0.16,
          prop_core_area_lo=0.15, prop_core_area_hi=1.5):
    from ultralytics import YOLO

    # 归一化 ignore_roi：支持单个 (x1,y1,x2,y2) 或多个 [(..),(..)]，统一成区域列表
    rois = []
    if ignore_roi is not None:
        if isinstance(ignore_roi[0], (list, tuple)):
            rois = [tuple(int(v) for v in r) for r in ignore_roi]
        else:
            rois = [tuple(int(v) for v in ignore_roi)]

    # 归一化 prop_roi（道具区域，供道具护栏使用）
    prois = []
    if prop_roi is not None:
        if isinstance(prop_roi[0], (list, tuple)):
            prois = [tuple(int(v) for v in r) for r in prop_roi]
        else:
            prois = [tuple(int(v) for v in prop_roi)]
    # 道具"核心区"（道具本体所在的紧致区域）：该区内的检测需要更高置信度才算人
    pcores = []
    if prop_core_roi is not None:
        if isinstance(prop_core_roi[0], (list, tuple)):
            pcores = [tuple(int(v) for v in r) for r in prop_core_roi]
        else:
            pcores = [tuple(int(v) for v in prop_core_roi)]
    if not pcores:
        pcores = list(prois)      # 未单独指定时，等于道具区

    model = YOLO(weights)
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        print(f"[error] 无法打开视频: {video}", file=sys.stderr)
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    print(f"[info] fps={fps:.2f} 总帧≈{total} 采样步长={stride} (≈{fps/stride:.2f} 采样/秒)", file=sys.stderr)

    # ---------- 道具护栏（prop-guard）准备 ----------
    # 目标：避免把静止道具（骷髅/画报/雕像/猪头等）计成人数。
    # 判据：检测框落在"道具区"内 **且** 该区域外观与"空场参考图"几乎一致 ⇒ 判为道具，丢弃。
    prop_ref = None
    if prop_guard and prois:
        if prop_ref_path and os.path.exists(prop_ref_path):
            prop_ref = cv2.imread(prop_ref_path, cv2.IMREAD_GRAYSCALE)
            print(f"[info] 道具护栏：使用外部空场参考图 {prop_ref_path}", file=sys.stderr)
        else:
            prop_ref = _build_prop_ref(cap, model, prop_ref_stride, conf, imgsz, device)
        print(f"[info] 道具护栏已启用，道具区={prois} 阈值 mad<{prop_mad} 且 ncc>{prop_ncc}",
              file=sys.stderr)
    last_roi_empty = None     # 最近一帧"道具区内无任何检测"的灰度帧（就近参考，光照最接近）
    last_empty_t = None
    prop_dropped = []         # 被护栏判定为道具而丢弃的记录（供人工复核）

    if sample_dir:
        os.makedirs(sample_dir, exist_ok=True)

    rows = []          # (sample_idx, t_sec, n_persons, n_ids)
    uniq_ids = set()
    sample_i = 0
    last_save = -999
    top_peaks = []      # 人数最多的前 3 帧（含检测框标注图），用于"峰值时刻"证据

    def _update_top(n, t, ann):
        rec = {"n": n, "t": t, "ann": ann}
        if len(top_peaks) < 3:
            top_peaks.append(rec)
        else:
            mi = min(range(len(top_peaks)), key=lambda i: top_peaks[i]["n"])
            if n > top_peaks[mi]["n"]:
                top_peaks[mi] = rec

    idx = 0
    while idx < total:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if not ret:
            break
        t_sec = idx / fps
        try:
            if no_track:
                res = model(frame, classes=[0], conf=conf, imgsz=imgsz,
                            device=device, verbose=False)[0]
            else:
                res = model.track(frame, classes=[0], conf=conf, imgsz=imgsz,
                                  device=device, persist=True, verbose=False)[0]
        except Exception:
            if "mps" in str(device).lower():
                print("[warn] MPS 失败，退回 CPU", file=sys.stderr)
                device = "cpu"
                res = model.track(frame, classes=[0], conf=conf, imgsz=imgsz,
                                  device=device, persist=True, verbose=False)[0]
            else:
                raise
        boxes = res.boxes
        n = 0
        ids_this = set()
        p_xyxy = []
        p_conf = []
        p_id = []
        final = []
        frame_gray = None
        if boxes is not None and len(boxes) > 0:
            cls = boxes.cls.cpu().numpy()
            xyxy_all = boxes.xyxy.cpu().numpy()
            conf_all = boxes.conf.cpu().numpy() if boxes.conf is not None else np.ones(len(cls))
            # 收集 person 框
            p_idx = [b for b in range(len(cls)) if int(cls[b]) == 0]
            p_xyxy = [xyxy_all[b].tolist() for b in p_idx]
            p_conf = [float(conf_all[b]) for b in p_idx]
            p_id = [int(boxes.id[b].item()) if boxes.id is not None else -1 for b in p_idx]
            # 去重:标准 NMS + 低置信二次去重,消除同一目标的重复检测框
            final = _dedupe_persons(p_xyxy, p_conf, p_id, nms_iou=nms_iou)
            # 再叠加 道具护栏 + ROI + 最小面积过滤,得到"最终计票列表" roi_final
            roi_final = []
            in_prop_roi = False
            if prop_guard and prois and final:
                frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            for k in final:
                cx = (p_xyxy[k][0] + p_xyxy[k][2]) / 2
                cy = (p_xyxy[k][1] + p_xyxy[k][3]) / 2
                if prop_guard and prois and _in_any_roi(cx, cy, prois):
                    in_prop_roi = True
                    drop_reason = None
                    # 判据一（外观）：该框看起来与"空场"完全一致 ⇒ 静止道具
                    ref_use = None
                    if (last_roi_empty is not None and last_empty_t is not None
                            and abs(t_sec - last_empty_t) <= prop_ref_gap):
                        ref_use = last_roi_empty
                    elif prop_ref is not None:
                        ref_use = prop_ref
                    m = _appearance_match(frame_gray, ref_use, p_xyxy[k]) if ref_use is not None else None
                    if m is not None and m[0] < prop_mad and m[1] > prop_ncc:
                        drop_reason = "appearance"
                    # 判据二（本体区）：道具本体所在的紧致区域内，若该框
                    #   ① 尺寸与道具本体相当（排除"人站在道具前"的大框）
                    #   ② 置信度偏低（静止道具只会被低置信度误判为 person，真人更高）
                    #   ③ 与空场参考差异也不大（mad 放宽到 prop_core_mad）
                    # 则判为道具。用于兜住"拥挤时刻骨架与人重叠、外观比对失效"的场景。
                    elif (m is not None and prop_core_mad > 0
                          and _in_any_roi(cx, cy, pcores)
                          and _area_ok(p_xyxy[k], pcores, prop_core_area_lo, prop_core_area_hi)
                          and p_conf[k] < prop_min_conf
                          and m[0] < prop_core_mad):
                        drop_reason = "core-appearance"
                    if drop_reason:
                        prop_dropped.append({"t_sec": round(t_sec, 1),
                                             "box": [round(v) for v in p_xyxy[k]],
                                             "conf": round(p_conf[k], 2),
                                             "mad": (round(m[0], 3) if m else None),
                                             "ncc": (round(m[1], 3) if m else None),
                                             "why": drop_reason})
                        continue
                if any(x1 <= cx <= x2 and y1 <= cy <= y2 for (x1, y1, x2, y2) in rois):
                    continue               # 落入任一忽略区域 → 跳过
                if min_area > 0:
                    bw = p_xyxy[k][2] - p_xyxy[k][0]
                    bh = p_xyxy[k][3] - p_xyxy[k][1]
                    if bw * bh < min_area:
                        continue               # 面积过小 → 视为噪声/误检
                roi_final.append(k)
                n += 1
                if p_id[k] >= 0:
                    ids_this.add(p_id[k])
            # 本帧道具区内没有任何检测 → 可作为"就近空场参考"（该区域无临时物体）
            if prop_guard and prois and not in_prop_roi:
                last_roi_empty = frame_gray if frame_gray is not None else \
                    cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                last_empty_t = t_sec
        uniq_ids.update(ids_this)
        rows.append((sample_i, round(t_sec, 1), n, len(ids_this)))

        try:
            # PEAK 帧只画"最终计票后保留"的框(NMS+ROI+min_area),保证图与数一致
            ann = _annotate_kept(frame, p_xyxy, p_conf, roi_final)
        except Exception:
            ann = frame
        _update_top(n, t_sec, ann)

        if sample_dir and (sample_i - last_save) >= annotate_every:
            cv2.imwrite(os.path.join(sample_dir, f"count_{sample_i:04d}_{int(t_sec)}s.jpg"), ann)
            last_save = sample_i

        sample_i += 1
        idx += stride

    cap.release()

    # 峰值帧证据（采样帧中人数最多的前 3 帧，带检测框）
    peak_files = []
    top_peaks.sort(key=lambda r: r["n"], reverse=True)
    for rec in top_peaks:
        if not sample_dir:
            break
        fn = os.path.join(sample_dir, f"PEAK_{rec['n']}人_{int(rec['t'])}s.jpg")
        try:
            cv2.imwrite(fn, rec["ann"])
            peak_files.append({"t_sec": round(rec["t"], 1), "count": rec["n"],
                               "file": os.path.basename(fn)})
        except Exception as e:
            print(f"[warn] 保存峰值帧失败: {e}", file=sys.stderr)
    top_peaks.clear()

    if not rows:
        print("[error] 未读到任何采样帧", file=sys.stderr)
        sys.exit(1)

    counts = [r[2] for r in rows]
    peak = max(counts)
    avg = sum(counts) / len(counts)
    mn = min(counts)
    buckets = {}
    for _, t, n, _ in rows:
        b = int(t // 30) * 30
        buckets[b] = max(buckets.get(b, 0), n)
    timeline = [{"t_start_sec": b, "peak_count": buckets[b]} for b in sorted(buckets)]

    # 处理时间戳（北京时间 ISO8601，build_report.py 据此按日过滤视频）
    from datetime import datetime, timezone, timedelta
    beijing_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    summary = {
        "video": video,
        "weights": weights,
        "fps": round(fps, 2),
        "total_frames": total,
        "sampled_frames": len(rows),
        "stride": stride,
        "conf": conf,
        "nms_iou": nms_iou,
        "min_area": min_area,
        "no_track": no_track,
        "person_peak": peak,
        "person_avg": round(avg, 2),
        "person_min": mn,
        "unique_ids_seen": len(uniq_ids),
        "timeline_30s_peak": timeline,
        "peak_frame_t_sec": peak_files[0]["t_sec"] if peak_files else None,
        "top_peaks": peak_files,
        "ignore_roi": [list(r) for r in rois] if rois else None,
        "prop_guard": ({
            "enabled": True,
            "roi": [list(r) for r in prois],
            "core_roi": [list(r) for r in pcores],
            "mad_lt": prop_mad, "ncc_gt": prop_ncc, "min_conf_in_core": prop_min_conf,
            "core_mad_lt": prop_core_mad,
            "core_area_ratio": [prop_core_area_lo, prop_core_area_hi],
            "ref": (prop_ref_path if (prop_ref_path and prop_ref is not None
                                      and not str(prop_ref_path).startswith("auto"))
                    else "auto"),
            "dropped": len(prop_dropped),
            "dropped_samples": prop_dropped[:20],
        } if (prop_guard and prois) else None),
        "processed_at": beijing_now.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sample_idx", "t_sec", "person_count", "track_ids"])
        for r in rows:
            w.writerow(r)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n===== CV 精确人数统计 =====", file=sys.stderr)
    print(f"峰值在场人数 : {peak}", file=sys.stderr)
    print(f"平均在场人数 : {avg:.1f}", file=sys.stderr)
    print(f"最少在场人数 : {mn}", file=sys.stderr)
    print(f"出现过的唯一个体(近似): {len(uniq_ids)}", file=sys.stderr)
    print(f"采样帧数     : {len(rows)}", file=sys.stderr)
    if prop_guard and prois:
        print(f"道具护栏丢弃 : {len(prop_dropped)} 个检测被判定为静止道具（不计入人数）",
              file=sys.stderr)
    print(f"结果已写入   : {out_csv} / {out_json}", file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--weights", default="./models/yolov8n_weights/yolov8n.pt")
    ap.add_argument("--stride", type=int, default=30, help="每 N 帧采样一次")
    ap.add_argument("--conf", type=float, default=0.30)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--device", default="mps")
    ap.add_argument("--out-csv", default="counts.csv")
    ap.add_argument("--out-json", default="count_summary.json")
    ap.add_argument("--sample-dir", default=None, help="存带框截图证据")
    ap.add_argument("--annotate-every", type=int, default=60)
    ap.add_argument("--ignore-roi", default=None,
                    help="屏蔽固定误检区域。格式 x1,y1,x2,y2(像素),中心点落入即忽略;"
                         "多个区域用分号分隔,如 'x1,y1,x2,y2;x1,y1,x2,y2'")
    ap.add_argument("--nms-iou", type=float, default=0.5,
                    help="person 框去重 NMS 的 IoU 阈值(默认 0.5)，用于消除同一目标的重复检测框")
    ap.add_argument("--no-track", action="store_true",
                    help="用纯 detect (非 track) 统计人数，可避免 ByteTrack 对低置信度框的抑制副作用")
    ap.add_argument("--min-area", type=float, default=0,
                    help="person 框最小面积(像素²),0=不过滤;用于消除小面积噪声/误检(如天花板、反射)")
    ap.add_argument("--prop-roi", default=None,
                    help="道具区域(静止道具所在),格式同 --ignore-roi,可用分号分隔多个。"
                         "配合 --prop-guard 使用：落在该区域内**且**外观与空场参考一致才丢弃，"
                         "因此人站在道具前面不会被误删。")
    ap.add_argument("--prop-guard", action="store_true",
                    help="启用道具护栏：避免把骷髅/画报/雕像/猪头等静止道具识别成人。"
                         "需同时给 --prop-roi；会先自动预扫描生成空场参考图（或由 --prop-ref 指定）。")
    ap.add_argument("--prop-ref", default=None,
                    help="外部空场参考图路径（可选）。不给则自动预扫描生成。")
    ap.add_argument("--prop-mad", type=float, default=0.10,
                    help="道具护栏灰度差阈值：低于该值视为与空场一致（默认 0.10）")
    ap.add_argument("--prop-ncc", type=float, default=0.80,
                    help="道具护栏结构相似度阈值：高于该值视为与空场一致（默认 0.80）")
    ap.add_argument("--prop-ref-stride", type=int, default=260,
                    help="自动预扫描空场参考图时的采样步长（默认 260）")
    ap.add_argument("--prop-ref-gap", type=float, default=600.0,
                    help="就近空场参考的最大时间跨度(秒)，超过则用全局参考图（默认 600）")
    ap.add_argument("--prop-core-roi", default=None,
                    help="道具本体所在的紧致区域（默认等于 --prop-roi）。该区域内的检测需达到 "
                         "--prop-min-conf 才算人，用于兜住\"拥挤时刻人与道具重叠、外观比对失效\"的情况。")
    ap.add_argument("--prop-min-conf", type=float, default=0.45,
                    help="道具核心区内判定为人的最低置信度（默认 0.45）。静止道具只会被"
                         "低置信度误判为 person；真人即使站在该区域置信度也更高。")
    ap.add_argument("--prop-core-mad", type=float, default=0.16,
                    help="道具核心区判据的灰度差上限（默认 0.16，比全局的 --prop-mad 宽松），"
                         "配合尺寸/置信度条件使用；0=关闭该判据。")
    ap.add_argument("--prop-core-area-lo", type=float, default=0.15,
                    help="核心区判据的框面积下限（相对道具本体框，默认 0.15 倍）")
    ap.add_argument("--prop-core-area-hi", type=float, default=1.5,
                    help="核心区判据的框面积上限（相对道具本体框，默认 1.5 倍；"
                         "人站在道具前会形成明显更大的框，从而被排除）")
    args = ap.parse_args()
    roi = None
    if args.ignore_roi:
        try:
            roi = []
            for grp in args.ignore_roi.split(";"):
                grp = grp.strip()
                if not grp:
                    continue
                t = tuple(int(float(x)) for x in grp.split(","))
                assert len(t) == 4
                roi.append(t)
            assert roi
        except Exception:
            print(f"[error] --ignore-roi 格式应为 x1,y1,x2,y2 (多个用分号分隔),实际收到: {args.ignore_roi}",
                  file=sys.stderr); sys.exit(2)
        print(f"[info] 屏蔽 ROI: {roi}", file=sys.stderr)
    proi = None
    if args.prop_roi:
        try:
            proi = []
            for grp in args.prop_roi.split(";"):
                grp = grp.strip()
                if not grp:
                    continue
                t = tuple(int(float(x)) for x in grp.split(","))
                assert len(t) == 4
                proi.append(t)
            assert proi
        except Exception:
            print(f"[error] --prop-roi 格式应为 x1,y1,x2,y2 (多个用分号分隔),实际收到: {args.prop_roi}",
                  file=sys.stderr); sys.exit(2)
    if args.prop_guard and not proi:
        print("[error] --prop-guard 需要同时给出 --prop-roi（道具所在区域）", file=sys.stderr)
        sys.exit(2)
    pcore = None
    if args.prop_core_roi:
        try:
            pcore = []
            for grp in args.prop_core_roi.split(";"):
                grp = grp.strip()
                if not grp:
                    continue
                t = tuple(int(float(x)) for x in grp.split(","))
                assert len(t) == 4
                pcore.append(t)
            assert pcore
        except Exception:
            print(f"[error] --prop-core-roi 格式应为 x1,y1,x2,y2 (多个用分号分隔),实际收到: {args.prop_core_roi}",
                  file=sys.stderr); sys.exit(2)
    count(args.video, args.weights, args.stride, args.conf, args.imgsz,
          args.device, args.out_csv, args.out_json, args.sample_dir,
          args.annotate_every, roi, args.nms_iou, args.min_area, args.no_track,
          proi, args.prop_guard, args.prop_ref, args.prop_mad, args.prop_ncc,
          args.prop_ref_stride, args.prop_ref_gap, pcore, args.prop_min_conf,
          args.prop_core_mad, args.prop_core_area_lo, args.prop_core_area_hi)
