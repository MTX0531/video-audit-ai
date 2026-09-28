#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描 results/<key>/description*.txt，检查"规范化后是否可读"。
判定逻辑与报告层完全一致（共用 desc_text），避免两套标准打架。"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
sys.path.insert(0, HERE)
from desc_text import humanize_desc, normalize  # noqa: E402


def main():
    rows = []
    for d in sorted(os.listdir(RES)):
        p = os.path.join(RES, d)
        if not os.path.isdir(p):
            continue
        for fn in sorted(os.listdir(p)):
            if not fn.startswith("description"):
                continue
            try:
                raw = open(os.path.join(p, fn), encoding="utf-8", errors="replace").read()
            except Exception as e:
                rows.append((d, fn, 0, "读取失败:%s" % e, ""))
                continue
            sm = None
            jp = os.path.join(p, "count_summary.json")
            if os.path.exists(jp):
                try:
                    import json
                    sm = json.load(open(jp, encoding="utf-8"))
                except Exception:
                    sm = None
            txt, ok, reason = humanize_desc(raw, sm)
            rows.append((d, fn, len(txt), "" if ok else reason, txt))

    print("%-16s %-24s %6s  %s" % ("key", "file", "输出字数", "状态"))
    bad = 0
    for d, fn, ln, reason, txt in rows:
        if reason:
            bad += 1
        print("%-16s %-24s %6d  %s" % (d[:15], fn, ln, reason or "可读"))
    print("\n共 %d 个描述文件；需兜底 %d 个（报告会自动改用检测数据生成的中文说明）"
          % (len(rows), bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
