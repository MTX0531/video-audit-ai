# -*- coding: utf-8 -*-
"""
Notion REST API 轻量封装（不依赖 MCP，纯 requests）。
覆盖本管线需要的 4 类操作：
  1) 文件上传到 Notion 本体  (file_uploads)
  2) 新建页面 / 查询数据库     (pages / databases/query)  —— 仅需 Read + Insert
  3) 读取页面里的文件块 URL    (blocks/children)          —— 用于把视频下载回本地处理
  4) 追加块                    (blocks/children/append)

注意：当前 integration 没有 Update content 权限，因此：
  - 不更新已有页面属性；报告一律“新建页面”实现回写；
  - “待处理/已处理”的判定用「报告库里已存在的关联视频」做差集，而非改视频页状态。
"""
import os
import requests
from config import NOTION_TOKEN, NOTION_VERSION

API = "https://api.notion.com/v1"


def _h(content_type="application/json"):
    h = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
    }
    if content_type:
        h["Content-Type"] = content_type
    return h


def _post(path, json=None, **kw):
    r = requests.post(API + path, headers=_h(), json=json, timeout=60, **kw)
    r.raise_for_status()
    return r.json()


def _get(path, params=None):
    r = requests.get(API + path, headers=_h(), params=params, timeout=60)
    r.raise_for_status()
    return r.json()


# ---------- 1) 文件上传到 Notion 本体 ----------
def upload_file(local_path, filename=None, content_type=None):
    """把本地文件上传到 Notion（single_part 模式），返回 file_upload_id。
    用法：建页面/块时 file={type:'file', file:{file_upload_id: <id>}}。"""
    if filename is None:
        filename = os.path.basename(local_path)
    if content_type is None:
        ext = filename.lower().rsplit(".", 1)[-1]
        content_type = {
            "mp4": "video/mp4", "mov": "video/quicktime", "mkv": "video/x-matroska",
            "html": "text/html", "json": "application/json", "csv": "text/csv",
        }.get(ext, "application/octet-stream")

    # a) 申请上传会话
    created = _post("/file_uploads", json={
        "mode": "single_part",
        "filename": filename,
        "content_type": content_type,
    })
    upload_id = created["id"]
    upload_url = created["upload_url"]

    # b) 把字节 PUT 到返回的 upload_url（这段不需要 Bearer，是预签名地址）
    with open(local_path, "rb") as f:
        up = requests.put(upload_url, data=f, headers={"Content-Type": content_type}, timeout=600)
    up.raise_for_status()
    return upload_id


# ---------- 2) 页面 / 数据库 ----------
def create_page(parent_db_id, properties, children=None, icon=None):
    body = {"parent": {"database_id": parent_db_id}, "properties": properties}
    if children:
        body["children"] = children
    if icon:
        body["icon"] = {"type": "emoji", "emoji": icon}
    return _post("/pages", json=body)


def query_database(db_id, filter_dict=None, page_size=100):
    """返回所有页（自动翻页）。"""
    out, cur = [], None
    while True:
        body = {"page_size": page_size}
        if filter_dict:
            body["filter"] = filter_dict
        if cur:
            body["start_cursor"] = cur
        r = _post(f"/databases/{db_id}/query", json=body)
        out.extend(r.get("results", []))
        if not r.get("has_more"):
            break
        cur = r.get("next_cursor")
    return out


# ---------- 3) 取页面里的视频文件 URL ----------
def get_file_url_from_page(page_id):
    """遍历页面块，返回第一个 file 块的临时下载 URL（预签名，会过期，需尽快下载）。"""
    data = _get(f"/blocks/{page_id}/children", params={"page_size": 100})
    for blk in data.get("results", []):
        if blk.get("type") == "file":
            fu = blk["file"]
            return fu.get("url"), fu.get("expiry_time")
        if blk.get("type") == "video":  # 极少数情况块类型是 video
            fu = blk["video"]
            return fu.get("url"), fu.get("expiry_time")
    return None, None


def download_url(url, dest_path):
    """直接下载预签名 URL（不需要 Bearer）。"""
    r = requests.get(url, timeout=600, stream=True)
    r.raise_for_status()
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            f.write(chunk)
    return dest_path


# ---------- 4) 追加块（如报告页顶部摘要）----------
def append_blocks(page_id, children):
    return _post(f"/blocks/{page_id}/children", json={"children": children})


# ---------- 属性构造小工具 ----------
def rt(text):
    return [{"type": "text", "text": {"content": str(text)}}]


def prop_title(text):
    return {"title": rt(text)}


def prop_rich(text):
    return {"rich_text": rt(text)}


def prop_select(name):
    return {"select": {"name": name}}


def prop_number(n):
    return {"number": n}


def prop_relation(page_ids):
    return {"relation": [{"id": pid} for pid in page_ids]}


def prop_file(file_upload_id, name="file"):
    return {"files": [{"type": "file", "name": name,
                       "file": {"file_upload_id": file_upload_id}}]}
