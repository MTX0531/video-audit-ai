#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描 results/*/description.txt，揪出"退化/不可读"输出并用当前 describe_qwen.py 重跑。

退化判定：统一交给 desc_text.readability()（空文件 / 汉字过少 / 符号堆砌 / 机械复读）。
注意：**不要**再用"(a)/(b) 双口径标记"判断——提示词已改为纯自然语言，不再输出该标记。

用法：
  HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 ./vlm_venv/bin/python repair_vlm.py [--dry-run]

必须在后台运行（MLX 前台会被 SIGTERM）。每个视频独立进程，失败不影响其余。
"""
import json
import os
import re
import subprocess
import sys

ROOT = "/Users/<用户名>/WorkBuddy/视频稽核/magevl_deploy"
BASE = "/Users/<用户名>/WorkBuddy/视频稽核/监控视频"
RES = os.path.join(ROOT, "results")
PY = os.path.join(ROOT, "vlm_venv", "bin", "python")
FRAMES, RESIZE = 10, 640

sys.path.insert(0, ROOT)
from desc_text import normalize, readability  # noqa: E402

_BANNER_RE = re.compile(r"=+\s*视频内容描述\s*=+")


def clean_stdout(text):
    """describe_qwen.py 的 stdout 带标题横幅，去掉后规范化，只留正文。"""
    t = _BANNER_RE.sub("\n", text or "")
    return normalize(t)


def is_degenerate(text):
    """返回 (是否退化, 原因)。"""
    norm = normalize(text or "")
    ok, reason = readability(norm)
    return (not ok), reason or ""


def video_of(key):
    jp = os.path.join(RES, key, "count_summary.json")
    if os.path.exists(jp):
        try:
            return json.load(open(jp, encoding="utf-8")).get("video")
        except Exception:
            return None
    return None


def main():
    dry = "--dry-run" in sys.argv
    targets = []
    for k in sorted(os.listdir(RES)):
        d = os.path.join(RES, k)
        if not os.path.isdir(d):
            continue
        dp = os.path.join(d, "description.txt")
        if not os.path.exists(dp):
            targets.append((k, "缺失"))
            continue
        txt = open(dp, encoding="utf-8").read()
        bad, why = is_degenerate(txt)
        if bad:
            targets.append((k, why))

    print(f"扫描 {len([1 for k in os.listdir(RES) if os.path.isdir(os.path.join(RES,k))])} 个视频，"
          f"退化 {len(targets)} 个：")
    for k, why in targets:
        print(f"  - {k}: {why}")
    if dry or not targets:
        print("(dry-run 或无需修复)")
        return

    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    for i, (k, why) in enumerate(targets, 1):
        v = video_of(k)
        print(f"\n===== [{i}/{len(targets)}] 重跑 {k} ({why}) =====", flush=True)
        if not v or not os.path.exists(v):
            print(f"  跳过：找不到源视频 ({v})", flush=True)
            continue
        dst = os.path.join(RES, k, "description.txt")
        r = subprocess.run(
            [PY, "describe_qwen.py", v, "--key", k,
             "--frames", str(FRAMES), "--resize", str(RESIZE)],
            cwd=ROOT, capture_output=True, text=True, env=env,
        )
        if r.returncode != 0:
            print(f"  exit={r.returncode}（保留原文件，不写入）\n  stderr尾部: "
                  f"{(r.stderr or '')[-300:]}", flush=True)
            continue
        body = clean_stdout(r.stdout)
        ok, reason = readability(body)
        if not ok:
            print(f"  重跑后仍不可读（{reason}），保留原文件；报告将改用检测数据兜底", flush=True)
            continue
        with open(dst, "w", encoding="utf-8") as out:
            out.write(body)
        print(f"  exit=0 已更新 {dst}（{len(body)} 字）", flush=True)


if __name__ == "__main__":
    main()
