#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核验某个 results/<key>/description.txt 规范化后是否可读（供批跑脚本回看）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from desc_text import humanize_desc  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
for k in sys.argv[1:]:
    p = os.path.join(HERE, "results", k, "description.txt")
    raw = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    txt, ok, reason = humanize_desc(raw)
    print("  [核验] %s  ok=%s  %s" % (k, ok, ("原因：" + reason) if reason else ""))
    print("  " + txt.replace("\n", " ")[:220])
