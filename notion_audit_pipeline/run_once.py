# -*- coding: utf-8 -*-
"""
run_once.py — 供 WorkBuddy 自动化定时调用的入口。
逻辑：扫描 Notion「视频台账库」，把还没生成报告的视频拉下来跑稽核管线、回写报告页。
幂等：已生成报告/正在处理的视频会被跳过（差集判定 + 本地进度锁）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import process_pending as P

if __name__ == "__main__":
    P.main()
