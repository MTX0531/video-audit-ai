#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在场时段分布生成器
- 输入: results/<key>/counts.csv (逐采样帧 person_count) + count_summary.json (video_start/end)
- 输出: 按监控水印真实时间聚合的"有人/无人"时段
  * busy_periods: 有人时段(起止墙钟、峰值、均值、时长)
  * idle_periods: 无人时段
  * narrative:  自然语言描述(几点到几点有人/无人)
- 写回 count_summary.json 的 "attendance" 字段

设计说明:
- 采样稀疏(stride/fps≈3.7s), 故每个采样点视为该时刻状态, 段边界取采样时刻(±半个采样间隔)
- 允许 GAP_TOL 秒以内的"无人间隔"不切断有人段(避免碎片化)
- 精确人数归 CV; VLM 只做语义描述, 不参与时段划分
"""
import os, sys, csv, json
from datetime import datetime, timedelta

RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
GAP_TOL = 300.0        # 无人间隔 < 5min 视为同一连续有人段内的空档
MIN_BUSY = 3.7         # 单次有人最短时长(≈1个采样间隔), 短于此的孤立有人点降级为噪声? 保留但标注


def parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


def fmt_clock(dt, with_sec=True):
    if not dt:
        return "—"
    return dt.strftime("%H:%M:%S" if with_sec else "%H:%M")


def fmt_min(sec):
    sec = float(sec)
    if sec < 60:
        return f"{sec:.0f}秒"
    m = sec / 60
    if m < 60:
        return f"{m:.1f}分钟"
    return f"{m/60:.1f}小时"


def load_rows(vdir):
    """读 counts.csv → [(t_sec, count), ...] 升序"""
    fp = os.path.join(vdir, "counts.csv")
    rows = []
    if not os.path.exists(fp):
        return rows
    with open(fp, encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                t = float(row["t_sec"])
                n = int(float(row["person_count"]))
            except Exception:
                continue
            rows.append((t, n))
    rows.sort(key=lambda x: x[0])
    return rows


def build_attendance(rows, start_dt, end_dt, gap_tol=GAP_TOL):
    """返回 {busy_periods, idle_periods, stats, narrative}"""
    if not rows:
        return None
    total_dur = (end_dt - start_dt).total_seconds() if (start_dt and end_dt) else rows[-1][0]
    # 采样间隔(中位数)
    diffs = [rows[i+1][0] - rows[i][0] for i in range(len(rows)-1)]
    dt = sorted(diffs)[len(diffs)//2] if diffs else 3.7

    # 状态 RLE: 段以采样时刻为界, 段结束 = 下一段开始
    segs = []   # (state:1/0, t_start, t_end)
    cur_state = 1 if rows[0][1] > 0 else 0
    seg_start = rows[0][0]
    for i in range(1, len(rows)):
        st = 1 if rows[i][1] > 0 else 0
        if st != cur_state:
            segs.append([cur_state, seg_start, rows[i][0]])
            cur_state = st
            seg_start = rows[i][0]
    segs.append([cur_state, seg_start, rows[-1][0] + dt])

    # 合并: busy 段之间的 idle 段若 < gap_tol, 归入 busy(不切断)
    merged = []
    for s in segs:
        if merged and s[0] == 1 and merged[-1][0] == 1:
            pass
        merged.append(s)
    # 找 busy-idle-busy 且 idle 时长 < gap_tol → 合并
    changed = True
    while changed:
        changed = False
        out = []
        i = 0
        while i < len(merged):
            if (i + 2 < len(merged) and merged[i][0] == 1 and merged[i+1][0] == 0
                    and merged[i+2][0] == 1 and (merged[i+1][2] - merged[i+1][1]) < gap_tol):
                out.append([1, merged[i][1], merged[i+2][2]])
                i += 3
                changed = True
            else:
                out.append(merged[i])
                i += 1
        merged = out
    # 再合并相邻同态
    comp = []
    for s in merged:
        if comp and comp[-1][0] == s[0]:
            comp[-1][2] = s[2]
        else:
            comp.append(list(s))

    # 组装结果
    def seg_counts(t0, t1):
        vals = [n for (t, n) in rows if t0 <= t < t1]
        return vals

    busy, idle = [], []
    for state, t0, t1 in comp:
        t1 = min(t1, total_dur)
        if t1 <= t0:
            continue
        rec = {
            "t_start_sec": round(t0, 1),
            "t_end_sec": round(t1, 1),
            "dur_sec": round(t1 - t0, 1),
            "clock_start": fmt_clock(start_dt + timedelta(seconds=t0)) if start_dt else "—",
            "clock_end": fmt_clock(start_dt + timedelta(seconds=t1)) if start_dt else "—",
        }
        vals = seg_counts(t0, t1)
        if vals:
            rec["peak"] = max(vals)
            rec["avg"] = round(sum(vals) / len(vals), 2)
        else:
            rec["peak"] = 0
            rec["avg"] = 0.0
        (busy if state == 1 else idle).append(rec)

    busy_total = sum(p["dur_sec"] for p in busy)
    stats = {
        "total_sec": round(total_dur, 1),
        "busy_total_sec": round(busy_total, 1),
        "busy_ratio": round(busy_total / total_dur, 3) if total_dur else 0,
        "busy_period_count": len(busy),
        "idle_period_count": len(idle),
    }

    # 自然语言
    lines = []
    if start_dt and end_dt:
        lines.append(f"录像全程 {fmt_clock(start_dt)}–{fmt_clock(end_dt)}（约 {fmt_min(total_dur)}）。")
    else:
        lines.append(f"录像全程约 {fmt_min(total_dur)}。")
    if not busy:
        lines.append("全程无人出现。")
    else:
        lines.append(f"其中有人的时段共 {len(busy)} 段，累计约 {fmt_min(busy_total)}"
                     f"（占全程 {stats['busy_ratio']*100:.0f}%）；其余时段无人。")
        for p in busy:
            lines.append(
                f"  · {p['clock_start']}–{p['clock_end']}（约 {fmt_min(p['dur_sec'])}）："
                f"峰值 {p['peak']} 人、平均 {p['avg']} 人")
        if idle:
            lines.append("无人时段：")
            for p in idle:
                lines.append(f"  · {p['clock_start']}–{p['clock_end']}（约 {fmt_min(p['dur_sec'])}）")
    narrative = "\n".join(lines)

    return {"busy_periods": busy, "idle_periods": idle, "stats": stats, "narrative": narrative}


def process(key):
    vdir = os.path.join(RES, key)
    jp = os.path.join(vdir, "count_summary.json")
    if not os.path.exists(jp):
        return None
    d = json.load(open(jp, encoding="utf-8"))
    rows = load_rows(vdir)
    start_dt = parse_dt(d.get("video_start"))
    end_dt = parse_dt(d.get("video_end"))
    if not rows:
        return None
    att = build_attendance(rows, start_dt, end_dt)
    if not att:
        return None
    d["attendance"] = att
    json.dump(d, open(jp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return d, att


def main():
    keys = sorted([d for d in os.listdir(RES) if os.path.isdir(os.path.join(RES, d))])
    for k in keys:
        res = process(k)
        if not res:
            print(f"--- {k}: 跳过(无数据) ---")
            continue
        d, att = res
        st = att["stats"]
        print(f"--- {k} | 峰值{d['person_peak']} 均{d['person_avg']} | "
              f"有人{st['busy_period_count']}段 累计{fmt_min(st['busy_total_sec'])} "
              f"占比{st['busy_ratio']*100:.0f}% ---")
        print(att["narrative"])
        print()


if __name__ == "__main__":
    main()
