# -*- coding: utf-8 -*-
"""
(2)+(3) AI 处理 + 回写：自动轮询 Notion「视频台账库」，
把尚未生成报告的视频下载到本地 → 跑本地 CV/VLM 稽核管线 → 生成单视频 HTML 报告
→ 把报告 HTML 上传到 Notion 本体 → 在「报告库」新建一条关联报告页。

设计要点（贴合 integration 仅 Read+Insert 的约束）：
  * 不更新任何已有页面；报告一律“新建页面”。
  * “待处理/已处理”判定 = 报告库里已有的「关联视频」集合做差集，无需 Update 权限。
  * 本地 .audit_progress.json 记录正在处理中的 page_id，避免重叠轮询重复处理。
"""
import os
import sys
import json
import re
import subprocess
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
import config as C
import notion_client as N

PROGRESS_FILE = os.path.join(os.path.dirname(__file__), ".audit_progress.json")


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            return json.load(open(PROGRESS_FILE))
        except Exception:
            return {}
    return {}


def save_progress(d):
    json.dump(d, open(PROGRESS_FILE, "w"), ensure_ascii=False, indent=2)


def get_title(props):
    t = props.get(C.PROP["title"], {}).get("title", [])
    return "".join(x.get("plain_text", "") for x in t) or "(无标题)"


def inject_timestamps(summary_path, filename):
    """从文件名水印解析起止时间，写回 count_summary.json（describe_attendance 需要）。"""
    ms = re.findall(r"(\d{14})", filename)
    if len(ms) < 2:
        return
    try:
        s = datetime.strptime(ms[0], "%Y%m%d%H%M%S")
        e = datetime.strptime(ms[1], "%Y%m%d%H%M%S")
    except Exception:
        return
    d = json.load(open(summary_path, encoding="utf-8"))
    d["video_start"] = s.strftime("%Y-%m-%d %H:%M:%S")
    d["video_end"] = e.strftime("%Y-%m-%d %H:%M:%S")
    d["video_filename"] = filename
    json.dump(d, open(summary_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def run_cmd(cmd, out_path=None):
    print("  $ " + " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    kw = {}
    if out_path:
        kw["stdout"] = open(out_path, "w")
        kw["stderr"] = subprocess.STDOUT
    subprocess.run(cmd, check=True, **kw)
    if out_path:
        kw["stdout"].close()


def video_can_decode(path):
    """格式兜底（总部侧最后防线）：用 cv2 实测能否打开并解出首帧。
    与后续 CV 同引擎，最能反映『总部 AI 能否读取』。解不出则视为格式异常。"""
    try:
        import cv2
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            cap.release()
            return False
        ret, frame = cap.read()
        cap.release()
        return bool(ret) and frame is not None and getattr(frame, "size", 0) > 0
    except Exception:
        return False


def mark_format_error(pid, title):
    """视频无法解码：在『报告库』新建一条『格式异常』记录页（Insert，不违反 Read+Insert 约束）。
    这样差集判定会把它视为『已处理』，不再无限重试；用户在报告库可看到哪条异常。
    注意：报告库的 status 需含『格式异常』这个 select 选项（见 README）。"""
    props = {
        C.PROP["title"]: N.prop_title(f"⚠格式异常·{title}"),
        C.PROP["rel_video"]: N.prop_relation([pid]),
        C.PROP["status"]: N.prop_select("格式异常"),
    }
    try:
        page = N.create_page(C.REPORT_DB_ID, props, icon="⚠️")
        print("  ✓ 已建格式异常记录页:", page.get("url"))
    except Exception as e:
        print(f"  ! 建异常页(带status)失败，尝试最小页: {e}")
        try:
            page = N.create_page(C.REPORT_DB_ID, {
                C.PROP["title"]: N.prop_title(f"⚠格式异常·{title}"),
                C.PROP["rel_video"]: N.prop_relation([pid]),
            }, icon="⚠️")
            print("  ✓ 已建最小格式异常记录页:", page.get("url"))
        except Exception as e2:
            print(f"  ! 建异常页也失败: {e2}")


def process_one(page, params_map):
    pid = page["id"]
    props = page["properties"]
    title = get_title(props)
    print(f"\n=== 处理: {title} ({pid}) ===")

    # 1) 取视频文件临时 URL 并下载
    url, _ = N.get_file_url_from_page(pid)
    if not url:
        print("  ! 该页没有视频文件块，跳过")
        return False
    key = pid.replace("-", "")
    vpath = os.path.join(C.TMP_DIR, key + ".mp4")
    print("  ↓ 下载视频 ...")
    N.download_url(url, vpath)

    # 0.5) 解码能力校验（格式兜底最后防线）：解不出首帧则标记格式异常并跳过，
    #      避免整条轮询因单个坏文件崩溃
    if not video_can_decode(vpath):
        print("  ! 视频无法解码（编码/容器不被支持），标记格式异常并跳过")
        mark_format_error(pid, title)
        return False

    rdir = os.path.join(C.RESULTS_DIR, key)
    os.makedirs(rdir, exist_ok=True)
    count_frames = os.path.join(rdir, "count_frames")
    os.makedirs(count_frames, exist_ok=True)

    # 2) CV 精确计数
    p = params_map.get(key, {})
    extra = []
    if p.get("ignore_roi"):
        extra += ["--ignore-roi", p["ignore_roi"]]
    if p.get("min_area"):
        extra += ["--min-area", str(p["min_area"])]
    if p.get("no_track"):
        extra += ["--no-track"]
    print("  ▶ CV 计数 (YOLOv8m) ...")
    run_cmd([
        C.CV_PY, C.COUNT_SCRIPT, vpath,
        "--weights", C.WEIGHTS, "--stride", "142", "--conf", "0.20",
        "--device", "mps", "--nms-iou", "0.5", "--annotate-every", "25",
        *extra,
        "--out-csv", os.path.join(rdir, "counts.csv"),
        "--out-json", os.path.join(rdir, "count_summary.json"),
        "--sample-dir", count_frames,
    ])
    inject_timestamps(os.path.join(rdir, "count_summary.json"), os.path.basename(vpath))

    # 3) 在场时段分布（扫描全部 results/，幂等）
    print("  ▶ 在场时段分布 ...")
    run_cmd([C.CV_PY, C.ATTENDANCE_SCRIPT])

    # 4) VLM 语义描述
    print("  ▶ VLM 描述 ...")
    run_cmd([C.VLM_PY, C.VLM_SCRIPT, vpath],
            out_path=os.path.join(rdir, "description.txt"))

    # 5) 生成单视频 HTML 报告
    report_html = os.path.join(rdir, f"稽核报告_{key}.html")
    print("  ▶ 生成报告 HTML ...")
    run_cmd([C.CV_PY, C.REPORT_SCRIPT, "--key", key, "--out", report_html])

    # 6) 上传报告 HTML 到 Notion + 新建报告页
    s = json.load(open(os.path.join(rdir, "count_summary.json"), encoding="utf-8"))
    att = (s.get("attendance") or {})
    stats = att.get("stats", {})
    peak = s.get("person_peak", 0)
    busy_min = round((stats.get("busy_total_sec", 0) or 0) / 60, 1)
    narrative = att.get("narrative", "")

    fid = N.upload_file(report_html, filename=os.path.basename(report_html),
                        content_type="text/html")

    children = []
    if narrative:
        children.append({"object": "block", "type": "paragraph",
                         "paragraph": {"rich_text": N.rt("🕑 " + narrative)}})
    children.append({"object": "block", "type": "paragraph",
                     "paragraph": {"rich_text": N.rt(
                         f"峰值人数 {peak} ｜ 有人累计 {busy_min} 分钟。完整交互报告见上方「报告文件」可下载打开。")}})

    props_report = {
        C.PROP["title"]: N.prop_title(f"稽核报告·{title}"),
        C.PROP["rel_video"]: N.prop_relation([pid]),
        C.PROP["peak"]: N.prop_number(peak),
        C.PROP["busy_min"]: N.prop_number(busy_min),
        C.PROP["status"]: N.prop_select("已生成"),
        C.PROP["reportfile"]: N.prop_file(fid, name=os.path.basename(report_html)),
    }
    page = N.create_page(C.REPORT_DB_ID, props_report, children=children, icon="📊")
    print("  ✓ 报告页已建:", page.get("url"))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理几条（0=全部）")
    ap.add_argument("--page-id", default="", help="只处理指定视频页（手动单条）")
    a = ap.parse_args()

    if not C.NOTION_TOKEN or not C.VIDEO_DB_ID or not C.REPORT_DB_ID:
        print("缺少环境变量 NOTION_TOKEN / NOTION_VIDEO_DB_ID / NOTION_REPORT_DB_ID")
        sys.exit(2)

    # 每视频计数参数覆盖（可选）
    params_map = {}
    pp = os.path.join(C.WORK_DIR, "video_params.json")
    if os.path.exists(pp):
        params_map = json.load(open(pp, encoding="utf-8"))

    # 已生成报告的视频页 id 集合（差集判定，无需 Update 权限）
    done_ids = set()
    for rp in N.query_database(C.REPORT_DB_ID):
        rel = rp["properties"].get(C.PROP["rel_video"], {}).get("relation", [])
        for r in rel:
            done_ids.add(r["id"])

    progress = load_progress()

    if a.page_id:
        targets = [a.page_id]
    else:
        targets = []
        for vp in N.query_database(C.VIDEO_DB_ID):
            pid = vp["id"]
            if pid in done_ids or pid in progress:
                continue
            targets.append(pid)

    if a.limit:
        targets = targets[: a.limit]

    print(f"待处理视频页数: {len(targets)}")
    for pid in targets:
        progress[pid] = True
        save_progress(progress)
        try:
            import requests
            r = requests.get(f"{N.API}/pages/{pid}", headers=N._h(), timeout=60)
            r.raise_for_status()
            process_one(r.json(), params_map)
        except Exception as e:
            print(f"  ! 处理失败: {e}")
        finally:
            progress.pop(pid, None)
            save_progress(progress)


if __name__ == "__main__":
    main()
