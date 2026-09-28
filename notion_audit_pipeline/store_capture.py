# -*- coding: utf-8 -*-
"""
store_capture.py — 门店侧「FTP 拉取视频 → 切片 → 上传 Notion」无人值守代理。

【职责边界】本脚本**只做 FTP 拉取、切片、上传**，不进行任何 AI 推理（无 CV/VLM/MLX/ultralytics 依赖）。
所有人数计数、在场时段、语义描述等稽核都在总部 process_pending.py 完成。门店机只需能装 ffmpeg + Python。

场景：门店录像机/监控平台把录像文件暴露为 FTP 目录（设备端 FTP 推送或共享目录均可），
工作人员不会手动下载视频。本脚本部署在门店局域网内的一台机器上常驻运行：
  1) 按 cameras.json 的 ftp_* 配置，周期性连接 FTP，把远程目录中新增的视频文件拉取到本机；
  2) 用 ffmpeg（-c copy 免转码，首选）把大文件切成固定时长的片段(clip)，落在本机 staging 目录；
  3) 监听切片产出，把“已写完”的片段自动上传到 Notion「视频台账库」(建待处理记录)；
  4) HQ 侧 process_pending.py 继续轮询/下载/跑 CV·VLM/回写报告页（已有，不动）。

特点：
  - 无人值守：无人工下载；FTP 断线自动重连；拉取幂等（远程文件切完移入 processed/ 防重复）。
  - 切片方式：ffmpeg -c copy 免转码（首选）；无 ffmpeg 时整文件作为单片段上传（由上传前归一化兜底）。
  - 幂等：已上传片段记入本地 .capture_state.json，守护进程重启不会重复上传。
  - dry-run：只切片+打印将要上传什么，不真正调 Notion，便于无凭证验证。

用法：
  python store_capture.py                          # 常驻守护（按 cameras.json）
  python store_capture.py --once --run-seconds 3600# 跑 1 小时就停
  python store_capture.py --dry-run                # 不调 Notion，仅验证拉取+切片+上传逻辑
  python store_capture.py --list                   # 打印解析后的 FTP 拉取计划后退出
  python store_capture.py --config my_cameras.json # 指定配置
"""
import os
import sys
import re
import glob
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
CFG = {}

VIDEO_EXTS = (".mp4", ".dav", ".avi", ".mkv", ".ts", ".mov", ".m4v", ".flv")


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


def is_video_name(name):
    """判断远程/本地文件名是否为可拉取、可切片的视频文件。"""
    return (name.lower().endswith(VIDEO_EXTS)
            and not name.startswith(".")
            and name != "processed")


def parse_time_from_name(name):
    """从文件名里提取 14 位时间戳（YYYYMMDDHHMMSS），取不到返回 None。
    用于给切片片段标注真实录像起止时间（录像机类设备命名常带时间戳）。"""
    m = re.search(r"(\d{14})", name)
    if m:
        try:
            datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
            return m.group(1)
        except Exception:
            return None
    return None


# ----------------------------- FTP 拉取 -----------------------------
def ftp_pull_loop(cam, rawdir, cam_key):
    """常驻：连接录像机/监控平台 FTP，把远程目录新增的视频文件拉取到本地 raw 目录。
    断线自动重连；同名文件跳过；下载中断残留的 .part 会被清理重下。"""
    while not STOP.is_set():
        try:
            import ftplib
            host = cam["ftp_host"]
            user = cam.get("ftp_user", "")
            pwd = cam.get("ftp_password", "")
            port = int(cam.get("ftp_port", 21))
            rdir = cam.get("ftp_remote_dir", "/")
            ftp = ftplib.FTP()
            ftp.connect(host, port, timeout=30)
            ftp.login(user, pwd)
            ftp.cwd(rdir)
            names = []
            ftp.retrlines("NLST", names.append)
            for n in names:
                if not is_video_name(n):
                    continue
                local = os.path.join(rawdir, n)
                if os.path.exists(local):
                    continue
                tmp = local + ".part"
                try:
                    with open(tmp, "wb") as f:
                        ftp.retrbinary("RETR " + n, f.write)
                    os.replace(tmp, local)
                    log(f"[ftp:{cam_key}] 已拉取: {n}")
                except Exception as e:
                    log(f"[ftp:{cam_key}] 下载失败 {n}: {e}")
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
            ftp.quit()
        except Exception as e:
            log(f"[ftp:{cam_key}] 连接异常，稍后重试: {e}")
        if STOP.is_set():
            break
        time.sleep(cam.get("ftp_poll_sec", CFG.get("ftp_poll_sec", 120)))


# ----------------------------- 本地切片 -----------------------------
def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def slice_local(path, camdir, cam, cam_key):
    """把单个已拉取的大文件切成 segment_seconds 片段。
    优先 -c copy 免转码；失败则回退 libx264 重编码。
    片段命名带起止时间戳（与上传/水印还原兼容）：{起}_{止}.mp4。
    返回切出的片段数（0 表示失败/无产出）。"""
    seg = int(cam.get("segment_seconds", CFG.get("segment_seconds", 1800)))
    start_ts = parse_time_from_name(os.path.basename(path))
    tmp_tpl = os.path.join(camdir, "_slice_%04d.mp4")
    cmds = [
        ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
         "-c", "copy", "-f", "segment", "-segment_time", str(seg),
         "-reset_timestamps", "1", tmp_tpl],
        ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-f", "segment", "-segment_time", str(seg),
         "-reset_timestamps", "1", tmp_tpl],
    ]
    ok = False
    for cmd in cmds:
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True, timeout=3600)
            ok = True
            break
        except Exception:
            continue
    if not ok:
        log(f"[slice:{cam_key}] 切片失败: {os.path.basename(path)}")
        return 0
    pieces = sorted(glob.glob(os.path.join(camdir, "_slice_*.mp4")))
    if not pieces:
        log(f"[slice:{cam_key}] 无产出，跳过: {os.path.basename(path)}")
        return 0
    base = datetime.strptime(start_ts, "%Y%m%d%H%M%S") if start_ts else datetime.now()
    for i, p in enumerate(pieces):
        s = base + timedelta(seconds=seg * i)
        e = s + timedelta(seconds=seg)
        dst = os.path.join(camdir,
                           f"{s.strftime('%Y%m%d%H%M%S')}_{e.strftime('%Y%m%d%H%M%S')}.mp4")
        if os.path.exists(dst):
            os.remove(p)
            continue
        os.rename(p, dst)
    return len(pieces)


def slice_loop(cam, rawdir, camdir, cam_key):
    """常驻：扫描 raw 目录中已拉取完成的文件，切成片段放入 camdir 供 watcher 上传。
    切完的源文件移入 rawdir/processed/ 防重复处理；
    无 ffmpeg 时整文件改名 .mp4 直接移入 camdir 作为单片段。"""
    while not STOP.is_set():
        if os.path.isdir(rawdir):
            try:
                files = [f for f in os.listdir(rawdir)
                         if is_video_name(f) and os.path.isfile(os.path.join(rawdir, f))]
            except Exception:
                files = []
            for f in sorted(files):
                p = os.path.join(rawdir, f)
                try:
                    mt = os.path.getmtime(p)
                except Exception:
                    continue
                # 等待文件写稳定（FTP 下载完成后再处理）
                if time.time() - mt < CFG.get("stable_sec", 60):
                    continue
                if not ffmpeg_available():
                    base = f if f.lower().endswith(".mp4") else os.path.splitext(f)[0] + ".mp4"
                    dst = os.path.join(camdir, base)
                    if not os.path.exists(dst):
                        try:
                            shutil.move(p, dst)
                            log(f"[slice:{cam_key}] 无 ffmpeg，整文件作为单片段: {base}")
                        except Exception as e:
                            log(f"[slice:{cam_key}] 移动失败 {f}: {e}")
                    else:
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                    continue
                n = slice_local(p, camdir, cam, cam_key)
                if n:
                    archived = os.path.join(rawdir, "processed")
                    os.makedirs(archived, exist_ok=True)
                    try:
                        shutil.move(p, os.path.join(archived, f))
                        log(f"[slice:{cam_key}] {f} 已切 {n} 段并归档")
                    except Exception as e:
                        log(f"[slice:{cam_key}] 源文件归档失败（稍后重扫）: {e}")
        if STOP.is_set():
            break
        time.sleep(CFG.get("poll_sec", 30))


# ----------------------------- 片段监听/上传 -----------------------------
def upload_if_done(cam, camdir, cam_key):
    if not os.path.isdir(camdir):
        return
    done_set = set(DONE.get(cam_key, []))
    try:
        files = [f for f in os.listdir(camdir)
                 if f.endswith(".mp4") and not f.startswith("_") and f not in done_set]
    except Exception:
        return
    if not files:
        return
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
        # 已完成条件：写完(stable) 且 大小不再变（连续两轮扫描确认）
        if stable and size_unchanged:
            upload_one(cam, camdir, cam_key, p, f)


def ensure_two_timestamps(path, cam):
    """兼容旧逻辑：若片段只有起始时间戳，补上结束时间戳（start+segment_seconds）再上传。
    当前 FTP 切片产物已自带“起止”两个 14 位时间戳，本函数会直接返回原路径。"""
    d = os.path.dirname(path)
    base = os.path.basename(path)
    m = re.match(r"^(\d{14})\.mp4$", base)
    if not m:
        return path  # 已有起止时间戳
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
    # 片段若只有起始时间戳则补结束时间戳（FTP 切片产物已自带，通常跳过）
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
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--once", action="store_true", help="跑 run-seconds 秒后停")
    ap.add_argument("--run-seconds", type=int, default=3600)
    ap.add_argument("--dry-run", action="store_true", help="不调 Notion，仅验证拉取+切片+上传逻辑")
    ap.add_argument("--list", action="store_true", help="打印解析后的 FTP 拉取计划后退出")
    a = ap.parse_args()

    if not os.path.exists(a.config):
        log(f"找不到配置文件: {a.config}")
        sys.exit(2)
    glo = json.load(open(a.config, encoding="utf-8"))
    CFG.update(glo)
    CFG["dry_run"] = a.dry_run
    CFG.setdefault("staging_dir", os.path.join(HERE, "_capture_staging"))
    CFG.setdefault("segment_seconds", 1800)
    CFG.setdefault("stable_sec", 60)
    CFG.setdefault("poll_sec", 30)
    CFG.setdefault("ftp_poll_sec", 120)
    CFG["cameras"] = glo.get("cameras", [])
    load_state()

    log(f"FTP 拉取模式  片段时长={CFG['segment_seconds']}s  staging={CFG['staging_dir']}"
        f"  dry_run={a.dry_run}")

    if a.list:
        for cam in CFG["cameras"]:
            log(f"  - {cam['store']} / {cam['camera']}  "
                f"ftp://{cam.get('ftp_user', '')}@{cam['ftp_host']}:{cam.get('ftp_port', 21)}"
                f"{cam.get('ftp_remote_dir', '/')}")
        return

    os.makedirs(CFG["staging_dir"], exist_ok=True)
    threads = []
    for cam in CFG["cameras"]:
        cam_key = f"{slug(cam['store'])}__{slug(cam['camera'])}"
        camdir = os.path.join(CFG["staging_dir"], cam_key)
        rawdir = os.path.join(camdir, "raw")
        os.makedirs(camdir, exist_ok=True)
        os.makedirs(rawdir, exist_ok=True)
        t1 = threading.Thread(target=ftp_pull_loop, args=(cam, rawdir, cam_key), daemon=True)
        t2 = threading.Thread(target=slice_loop, args=(cam, rawdir, camdir, cam_key), daemon=True)
        t1.start()
        t2.start()
        threads += [t1, t2]

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
        log("收到中断，停止拉取...")
    finally:
        STOP.set()
        for t in threads:
            t.join(timeout=5)
        wt.join(timeout=5)
        log("已停止。")


if __name__ == "__main__":
    main()
