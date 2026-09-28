#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抽查报告中各视频的 VLM 场景描述渲染是否为人话（无 markdown/符号堆砌）。"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from desc_text import visible_text, PROBLEM_PATTERNS  # noqa: E402

fp = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "results", "稽核报告_20260911.html")
t = open(fp, encoding="utf-8").read()

print("=" * 78)
print("报告:", os.path.basename(fp))
print("=" * 78)
for m in re.finditer(r'<section class="vid" id="sec-(.*?)">(.*?)</section>', t, re.S):
    key, body = m.group(1), m.group(2)
    print("\n### " + key)
    for hm in re.finditer(r'<h3>([^<]*VLM[^<]*)</h3>', body):
        print("  [标题] " + hm.group(1).strip())
    for nm in re.finditer(r'<div class="vlmnote">(.*?)</div>', body, re.S):
        print("  [提示] " + re.sub(r"<[^>]+>", "", nm.group(1)).strip())
    for pm in re.finditer(r"<pre>(.*?)</pre>", body, re.S):
        txt = pm.group(1)
        print("  [描述] " + txt.replace("\n", " ⏎ ")[:520])
    # 该分区可见文本是否还有问题
    probs = []
    for rx, name in PROBLEM_PATTERNS:
        if rx.search(visible_text(body)):
            probs.append(name)
    print("  [体检] " + ("通过" if not probs else "仍有问题: " + "、".join(probs)))
