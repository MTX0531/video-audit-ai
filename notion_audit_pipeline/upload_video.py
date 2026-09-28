# -*- coding: utf-8 -*-
"""
(1) 上传：把门店监控视频本体上传到 Notion，并在「视频台账库」建一条记录。
用法：
  python upload_video.py <视频路径> --store 店名 --camera 摄像头
说明：
  - 视频字节通过 file_uploads 接口传到 Notion 本体（不走对象存储）。
  - 水印起止时间从文件名里的 _YYYYMMDDHHMMSS_YYYYMMDDHHMMSS_ 解析。
  - 自动建台账页（状态=待处理），供 process_pending.py 自动轮询处理。
"""
import os
import re
import sys
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
import config as C
import notion_client as N


def parse_window(fn):
    ms = re.findall(r"(\d{14})", fn)
    if len(ms) >= 2:
        try:
            s = datetime.strptime(ms[0], "%Y%m%d%H%M%S")
            e = datetime.strptime(ms[1], "%Y%m%d%H%M%S")
            return s.strftime("%Y-%m-%d %H:%M:%S"), e.strftime("%Y-%m-%d %H:%M:%S"), int((e - s).total_seconds())
        except Exception:
            pass
    return "", "", 0


def upload_clip(path, store="", camera="", window=None):
    """通用：把一个本地视频片段上传到 Notion 本体并建「视频台账库」记录(状态=待处理)。
    返回新建页面 id。store_capture.py 与手动 upload_video.py 共用本函数。"""
    if not C.NOTION_TOKEN or not C.VIDEO_DB_ID:
        raise RuntimeError("缺少 NOTION_TOKEN / NOTION_VIDEO_DB_ID（检查 .env）")
    fn = os.path.basename(path)
    ws, we, dur = (window if window else parse_window(fn))
    title = f"{store or '店'}·{camera or 'cam'}·{ws[:10] if ws else fn}"

    print(f"[upload] {fn} ({os.path.getsize(path)/1e6:.1f}MB) → Notion 台账 ...")
    fid = N.upload_file(path, filename=fn, content_type="video/mp4")
    props = {
        C.PROP["title"]: N.prop_title(title),
        C.PROP["store"]: N.prop_rich(store),
        C.PROP["camera"]: N.prop_rich(camera),
        C.PROP["window"]: N.prop_rich(f"{ws} – {we}" if ws else ""),
        C.PROP["duration"]: N.prop_rich(f"{dur//60}分{dur%60:02d}秒" if dur else ""),
        C.PROP["status"]: N.prop_select("待处理"),
        C.PROP["videofile"]: N.prop_file(fid, name=fn),
    }
    page = N.create_page(C.VIDEO_DB_ID, props, icon="🎥")
    print("      台账页:", page.get("url"))
    return page["id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--store", default="")
    ap.add_argument("--camera", default="")
    a = ap.parse_args()

    if not C.NOTION_TOKEN or not C.VIDEO_DB_ID:
        print("缺少环境变量 NOTION_TOKEN / NOTION_VIDEO_DB_ID"); sys.exit(2)

    try:
        pid = upload_clip(a.video, store=a.store, camera=a.camera)
    except Exception as e:
        print("上传失败:", e); sys.exit(1)
    print("      页面 id:", pid)


if __name__ == "__main__":
    main()
