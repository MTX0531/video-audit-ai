# -*- coding: utf-8 -*-
"""
Notion 视频稽核管线 — 全局配置
所有敏感信息走环境变量，不在代码里硬编码。
"""
import os


def _load_env():
    """从同目录 .env 读取 KEY=VALUE（不依赖 python-dotenv），
    让自动化任务也能拿到 NOTION_TOKEN，而不必依赖 shell 环境。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

# ===== Notion 鉴权 =====
# integration secret，形如 secret_xxx。在 Notion → 设置 → 集成 里创建/查看。
# 当前项目已有的 integration: workbuddy开发日志写入专用
#   id = <NOTION_INTEGRATION_ID> （仅 Read + Insert，没有 Update content）
# 视频稽核建议：用同一个 integration，但连到一个【新建的专用数据库】。
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
# 不同接口对 Notion-Version 要求不同；文件上传(file_uploads)较新，若报错可升到 "2025-05-14"
NOTION_VERSION = os.environ.get("NOTION_VERSION", "2022-06-28")

# ===== 数据库 =====
# 视频台账库（存放视频本体 + 元数据）。需要你先在 Notion 建好并让 integration 连上。
VIDEO_DB_ID = os.environ.get("NOTION_VIDEO_DB_ID", "")
# 报告库（每个视频一条报告页，含关联视频 + 报告 HTML 文件块）。
REPORT_DB_ID = os.environ.get("NOTION_REPORT_DB_ID", "")

# ===== 本地工作区 =====
WORK_DIR = os.environ.get("AUDIT_WORK_DIR", "/Users/<用户名>/WorkBuddy/视频稽核/magevl_deploy")
RESULTS_DIR = os.path.join(WORK_DIR, "results")          # count_people 输出根目录
TMP_DIR = os.environ.get("AUDIT_TMP_DIR", "/tmp/audit_pipe")  # 下载的视频临时存放

# ===== 本地 AI 稽核管线（复用现有脚本）=====
CV_PY = "/Users/<用户名>/.workbuddy/binaries/python/envs/cv/bin/python"
VLM_PY = os.environ.get("VLM_PY", "/Users/<用户名>/.workbuddy/binaries/python/envs/cv/bin/python")
COUNT_SCRIPT = os.path.join(WORK_DIR, "count_people.py")
VLM_SCRIPT = os.path.join(WORK_DIR, "describe_qwen.py")
ATTENDANCE_SCRIPT = os.path.join(WORK_DIR, "describe_attendance.py")
REPORT_SCRIPT = os.path.join(WORK_DIR, "build_report.py")
WEIGHTS = os.path.join(WORK_DIR, "models/yolov8m_weights/yolov8m.pt")

# 默认计数参数（可被 video_params.json 按 key 覆盖）
DEFAULT_COUNT_ARGS = [
    "--weights", WEIGHTS,
    "--stride", "142",
    "--conf", "0.20",
    "--device", "mps",
    "--nms-iou", "0.5",
    "--annotate-every", "25",
]
# 默认不传 ROI/min-area；对误检多的机位在 video_params.json 里加
DEFAULT_EXTRA = []   # 例如 ["--ignore-roi","350,80,700,400","--min-area","15000","--no-track"]

# 视频台账库 / 报告库的属性名（与你在 Notion 里建的列名保持一致）
PROP = {
    "title": "名称",
    "store": "店名",
    "camera": "摄像头",
    "window": "视频起止",
    "duration": "时长",
    "status": "状态",          # select: 待处理 / 处理中 / 已生成
    "videofile": "视频文件",   # files
    "rel_video": "关联视频",   # relation -> VIDEO_DB
    "peak": "峰值人数",        # number
    "busy_min": "有人累计(min)",  # number
    "reportfile": "报告文件",  # files
}
