# -*- coding: utf-8 -*-
"""
store_capture.py — 门店侧「自动抓流 → 切片 → 上传 Notion」无人值守代理。

【职责边界】本脚本**只做抓流、切片、上传**，不进行任何 AI 推理（无 CV/VLM/MLX/ultralytics 依赖）。
所有人数计数、在场时段、语义描述等稽核都在总部 process_pending.py 完成。门店机只需能装 ffmpeg + Python。

场景：门店摄像头是大华(Dahua) RTSP 流，工作人员不会手动下载视频。
本脚本部署在门店局域网内的一台机器（Mac Mini / 工控机 / 店员电脑）上常驻运行：
  1) 按 cameras.json 连接各摄像头子码流（subtype=1，低码率分析副本）；
  2) 用 ffmpeg（-c copy 免转码，首选）或 OpenCV（无 ffmpeg 时兜底重编码）
     把视频流切成固定时长的片段(clip)，落在本机 staging 目录；
  3) 监听切片产出，把“已写完”的片段自动上传到 Notion「视频台账库」(建待处理记录)；
  4) HQ 侧 process_pending.py 继续轮询/下载/跑 CV·VLM/回写报告页（已有，不动）。

特点：
  - 无人值守：无人工下载；营业时段门控可选；断流自动重连/重启。
  - 两种抓流后端：ffmpeg(首选, 免转码) / opencv(无 ffmpeg 时兜底, 会重编码)。
  - 幂等：已上传片段记入本地 .capture_state.json，守护进程重启不会重复上传。
  - dry-run：只切片+打印将要上传什么，不真正调 Notion，便于无凭证验证。

用法：
  python store_capture.py                          # 常驻守护（按 cameras.json）
  python store_capture.py --once --run-seconds 3600# 跑 1 小时就停
  python store_capture.py --dry-run                # 不调 Notion，仅验证切片+上传逻辑
  python store_capture.py --backend opencv         # 强制 OpenCV 后端（无 ffmpeg 时）
  python store_capture.py --config my_cameras.json # 指定配置
"""
import os
import sys
import re
import json
import time
import shutil
import subprocess
import argparse
import threading
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import upload_video as UV   # 复用 upload_clip()

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "cameras.json")
STATE_FILE = os.path.join(HERE, ".capture_state.json")

# ----------------------------- 全局状态 -----------------------------
STOP = threading.Event()
DONE = {}            # cam_key -> [已上传文件名]，持久化到 STATE_FILE
LAST_SIZE = {}       # 片段路径 -> 上次扫描字节数（判断“写完”）
ALIVE = {}           # cam_key -> 抓流进程/线程是否存活
ACTIVE = {}          # cam_key -> 当前正在写入的片段路径（opencv 用；ffmpeg 用 mtime 推断）
CFG = {}


def log(*a):
    print(f"[{datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)


def slug(s):
    """把店名/摄像头名变成安全的目录片段（保留中文，去掉 / : 等非法字符）。"""
    return "".join(c if (c.isalnum() or c in " _-") else "_" for c in s).strip()


def load_state():
    global DONE
    if os.path.exists(STATE_FILE):
        try:
            DONE = json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            DONE = {}


def save_state():
    json.dump(DONE, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def in_business(hours):
    if not hours:
        return True
    try:
        s = datetime.strptime(hours[0], "%H:%M").time()
        e = datetime.strptime(hours[1], "%H:%M").time()
    except Exception:
        return True
    now = datetime.now().time()
    return s <= now <= e


# ----------------------------- 抓流后端 -----------------------------
def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def build_ffmpeg_cmd(cam, camdir):
    """ffmpeg 免转码切片：直读大华子码流(subtype=1)，-c copy 不重编码，
    用 segment muxer 按 segment_seconds 切出以起始时间命名的 mp4。"""
    seg = int(cam.get("segment_seconds", CFG.get("segment_seconds", 1800)))
    url = cam["rtsp_url"]
    extra = cam.get("ffmpeg_extra", "")
    out = os.path.join(camdir, "%Y%m%d_%H%M%S.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-rtsp_transport", "tcp",
           "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
           "-i", url,
           "-c", "copy",
           "-f", "segment", "-segment_time", str(seg),
           "-reset_timestamps", "1", "-strftime", "1", out]
    if extra:
        # 允许在 rtsp_url 之后插入额外参数（如 "-use_wallclock_as_timestamps 1"）
        # 简单做法：把 extra 拼到 -i 之前
        cmd[5:5] = extra.split()
    return cmd


def capture_ffmpeg(cam, camdir, cam_key):
    """常驻：ffmpeg 进程退出就重启（断流自愈）。"""
    while not STOP.is_set():
        if not in_business(cam.get("business_hours")):
            time.sleep(30)
            continue
        cmd = build_ffmpeg_cmd(cam, camdir)
        log(f"[ffmpeg:{cam_key}] 启动抓流:", cam["rtsp_url"])
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ALIVE[cam_key] = True
            p.wait()
            ALIVE[cam_key] = False
        except Exception as e:
            ALIVE[cam_key] = False
            log(f"[ffmpeg:{cam_key}] 启动失败: {e}")
        if STOP.is_set():
            break
        log(f"[ffmpeg:{cam_key}] 进程退出，5s 后重启")
        time.sleep(5)


def capture_opencv(cam, camdir, cam_key):
    """兜底：用 OpenCV 读 RTSP/文件，按 segment_seconds 重编码切片（无需 ffmpeg）。
    注意：这是纯“读帧→写文件”的封装/转码，不做任何目标检测或 AI 推理。"""
    import cv2
    seg = int(cam.get("segment_seconds", CFG.get("segment_seconds", 1800)))
    cap = cv2.VideoCapture(cam["rtsp_url"])
    if not cap.isOpened():
        log(f"[opencv:{cam_key}] 无法打开: {cam['rtsp_url']}")
        return
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = None
    frames = 0
    seq = 0
    active_path = None
    ALIVE[cam_key] = True
    try:
        while not STOP.is_set():
            if not in_business(cam.get("business_hours")):
                time.sleep(30)
                continue
            ret, frame = cap.read()
            if not ret:
                time.sleep(1)
                continue
            if writer is None:
                start = datetime.now()
                end = start + timedelta(seconds=seg)
                # 加序列号后缀，避免高速回放/同秒内切段导致文件名碰撞互相覆盖
                name = f"{start.strftime('%Y%m%d%H%M%S')}_{end.strftime('%Y%m%d%H%M%S')}_s{seq}.mp4"
                seq += 1
                active_path = os.path.join(camdir, name)
                ACTIVE[cam_key] = active_path
                writer = cv2.VideoWriter(active_path, fourcc, fps, (w, h))
            writer.write(frame)
            frames += 1
            if frames >= int(seg * fps):
                writer.release()
                writer = None
                frames = 0
                active_path = None
                ACTIVE[cam_key] = None
    finally:
        if writer:
            writer.release()
        cap.release()
        ALIVE[cam_key] = False
        ACTIVE[cam_key] = None


# ----------------------------- 片段监听/上传 -----------------------------
def upload_if_done(cam, camdir, cam_key):
    if not os.path.isdir(camdir):
        return
    alive = ALIVE.get(cam_key, False)
    done_set = set(DONE.get(cam_key, []))
    try:
        files = [f for f in os.listdir(camdir)
                 if f.endswith(".mp4") and f not in done_set]
    except Exception:
        return
    if not files:
        return
    # 当前正在写入的片段：opencv 后端看 ACTIVE（writer 当前打开的文件）；
    # ffmpeg 后端看 mtime 最新者（segment muxer 同一时刻只持有一个打开文件）。
    if CFG.get("backend") == "opencv":
        active_path = ACTIVE.get(cam_key)
    else:
        active_path = os.path.join(
            camdir, max(files, key=lambda f: os.path.getmtime(os.path.join(camdir, f))))
    for f in files:
        p = os.path.join(camdir, f)
        try:
            sz = os.path.getsize(p)
            mt = os.path.getmtime(p)
        except Exception:
            continue
        stable = (time.time() - mt) > CFG.get("stable_sec", 60)
        size_unchanged = (LAST_SIZE.get(p) == sz)
        LAST_SIZE[p] = sz
        is_active = (p == active_path)
        # 已完成条件：写完(stable) 且 大小不再变 且 (不是活跃片段 或 抓流已死)
        if stable and size_unchanged and (not is_active or not alive):
            upload_one(cam, camdir, cam_key, p, f)


def ensure_two_timestamps(path, cam):
    """ffmpeg -strftime 文件名只带起始时间戳；HQ 侧依赖“起止”两个 14 位时间戳还原水印时间，
    故片段写完补上结束时间戳（start+segment_seconds）再上传。opencv 片段已自带，无需处理。"""
    d = os.path.dirname(path)
    base = os.path.basename(path)
    m = re.match(r"^(\d{14})\.mp4$", base)
    if not m:
        return path  # 已有起止时间戳（如 *_sN.mp4）
    seg = int(cam.get("segment_seconds", CFG.get("segment_seconds", 1800)))
    try:
        start = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
        end = start + timedelta(seconds=seg)
        new_base = f"{m.group(1)}_{end.strftime('%Y%m%d%H%M%S')}.mp4"
    except Exception:
        return path
    new_path = os.path.join(d, new_base)
    if os.path.exists(new_path):
        return path
    os.rename(path, new_path)
    return new_path


def ffprobe_streams(path):
    """返回 (container, video_codec)。无 ffmpeg 时返回 (None, None)。"""
    if not ffmpeg_available():
        return (None, None)
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=format_name:stream=codec_name,codec_type",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30)
        info = json.loads(out.stdout or "{}")
        fmt = (info.get("format", {}).get("format_name") or "").lower()
        vcodec = ""
        for s in info.get("streams", []):
            if s.get("codec_type") == "video":
                vcodec = (s.get("codec_name") or "").lower()
                break
        return (fmt, vcodec)
    except Exception as e:
        log(f"[probe] ffprobe 失败: {e}")
        return (None, None)


def normalize_clip_for_upload(path, cam):
    """上传前格式兜底：确保出口是「mp4 容器 + H.264 编码」，否则总部 AI 无法解码。
    - 已是 mp4+h264 → 原样返回；
    - 非 mp4 / H.265(hevc) / 其他 → 用 ffmpeg 转码成 H.264 mp4（仅小切片，开销低）；
    - 无 ffmpeg 环境 → 仅告警，最终由总部侧 cv2 解码兜底。
    转码产物落在同目录；原非标文件移入 uploaded/ 避免被重复扫描。"""
    d = os.path.dirname(path)
    base = os.path.basename(path)
    if CFG.get("dry_run"):
        log(f"[dry-run] 将探测片段格式并(必要时)归一化为 mp4/H.264: {base}")
        return path
    if not ffmpeg_available():
        if not base.lower().endswith(".mp4"):
            log(f"[warn] 无 ffmpeg，无法校验/转码 {base}；若总部解码失败将由总部侧标记异常")
        return path
    fmt, vcodec = ffprobe_streams(path)
    is_mp4 = (fmt and "mp4" in fmt) or base.lower().endswith(".mp4")
    is_h264 = (vcodec == "h264")
    if is_mp4 and is_h264:
        return path  # 已合规，跳过转码
    stem = re.sub(r"\.mp4$", "", base)
    out_path = os.path.join(d, f"{stem}__h264.mp4")
    log(f"[transcode] 片段格式={fmt or '?'} 编码={vcodec or '?'} 非标，转码为 H.264 mp4: {base}")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", "-an", out_path],
            check=True, capture_output=True, text=True, timeout=600)
        # 原非标文件移入 uploaded/，避免被 watcher 再次扫到重传
        rej = os.path.join(d, "uploaded")
        os.makedirs(rej, exist_ok=True)
        try:
            shutil.move(path, os.path.join(rej, base))
        except Exception:
            pass
        return out_path
    except Exception as e:
        log(f"[transcode] 转码失败，仍按原文件上传（{e}）")
        return path


def upload_one(cam, camdir, cam_key, path, fn):
    uploaded_dir = os.path.join(camdir, "uploaded")
    # ffmpeg 片段补结束时间戳（不影响 opencv 片段）
    path = ensure_two_timestamps(path, cam)
    fn = os.path.basename(path)
    # 格式兜底：确保出口为 mp4(H.264)，避免总部 AI 无法解码
    path = normalize_clip_for_upload(path, cam)
    fn = os.path.basename(path)
    if CFG.get("dry_run"):
        log(f"[dry-run] 将上传片段: {fn}  (店={cam['store']} 摄像头={cam['camera']})")
        DONE.setdefault(cam_key, []).append(fn)
        save_state()
        return
    try:
        UV.upload_clip(path, store=cam["store"], camera=cam["camera"])
    except Exception as e:
        log(f"[upload:{cam_key}] 上传失败，稍后重试: {e}")
        return
    os.makedirs(uploaded_dir, exist_ok=True)
    shutil.move(path, os.path.join(uploaded_dir, fn))
    DONE.setdefault(cam_key, []).append(fn)
    save_state()
    log(f"[upload:{cam_key}] 已上传并归档: {fn}")


def watcher_loop():
    while not STOP.is_set():
        for cam in CFG["cameras"]:
            cam_key = f"{slug(cam['store'])}__{slug(cam['camera'])}"
            camdir = os.path.join(CFG["staging_dir"], cam_key)
            upload_if_done(cam, camdir, cam_key)
        time.sleep(CFG.get("poll_sec", 30))


# ----------------------------- 主流程 -----------------------------
def resolve_backend(preferred):
    if preferred == "opencv":
        return "opencv"
    if preferred == "ffmpeg":
        if ffmpeg_available():
            return "ffmpeg"
        log("未检测到 ffmpeg，回退到 opencv 后端")
        return "opencv"
    # auto
    return "ffmpeg" if ffmpeg_available() else "opencv"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--backend", default="auto", choices=["auto", "ffmpeg", "opencv"])
    ap.add_argument("--once", action="store_true", help="跑 run-seconds 秒后停")
    ap.add_argument("--run-seconds", type=int, default=3600)
    ap.add_argument("--dry-run", action="store_true", help="不调 Notion，仅验证切片+上传逻辑")
    ap.add_argument("--list", action="store_true", help="打印解析后的抓流计划后退出")
    a = ap.parse_args()

    if not os.path.exists(a.config):
        log(f"找不到配置文件: {a.config}"); sys.exit(2)
    glo = json.load(open(a.config, encoding="utf-8"))
    CFG.update(glo)
    CFG["dry_run"] = a.dry_run
    CFG.setdefault("staging_dir", os.path.join(HERE, "_capture_staging"))
    CFG.setdefault("segment_seconds", 1800)
    CFG.setdefault("stable_sec", 60)
    CFG.setdefault("poll_sec", 30)
    CFG["cameras"] = glo.get("cameras", [])
    load_state()

    backend = resolve_backend(a.backend)
    CFG["backend"] = backend
    log(f"后端={backend}  片段时长={CFG['segment_seconds']}s  staging={CFG['staging_dir']}"
        f"  dry_run={a.dry_run}")

    if a.list:
        for cam in CFG["cameras"]:
            log(f"  - {cam['store']} / {cam['camera']}  {cam['rtsp_url']}  "
                f"营业={cam.get('business_hours','全天')}")
        return

    os.makedirs(CFG["staging_dir"], exist_ok=True)
    threads = []
    for cam in CFG["cameras"]:
        cam_key = f"{slug(cam['store'])}__{slug(cam['camera'])}"
        camdir = os.path.join(CFG["staging_dir"], cam_key)
        os.makedirs(camdir, exist_ok=True)
        if backend == "ffmpeg":
            t = threading.Thread(target=capture_ffmpeg, args=(cam, camdir, cam_key), daemon=True)
        else:
            t = threading.Thread(target=capture_opencv, args=(cam, camdir, cam_key), daemon=True)
        t.start()
        threads.append(t)

    wt = threading.Thread(target=watcher_loop, daemon=True)
    wt.start()

    try:
        if a.once:
            log(f"单次模式，运行 {a.run_seconds}s ...")
            STOP.wait(a.run_seconds)
        else:
            log("守护模式（Ctrl+C 退出）...")
            while not STOP.is_set():
                time.sleep(1)
    except KeyboardInterrupt:
        log("收到中断，停止抓流...")
    finally:
        STOP.set()
        for t in threads:
            t.join(timeout=5)
        wt.join(timeout=5)
        log("已停止。")


if __name__ == "__main__":
    main()
