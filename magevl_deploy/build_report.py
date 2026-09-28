#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 results/<key>/ 下的 description.txt / description_qwen3vl.txt / count_summary.json，
生成合并稽核报告 HTML（含 CV 统计、30s 峰值时间线、峰值时刻证据帧、两段 VLM 描述、常规抽样证据帧）。"""
import os, json, glob, re, html, sys
from datetime import datetime as _dt, timedelta

BASE = "/Users/<用户名>/WorkBuddy/视频稽核/magevl_deploy"
if BASE not in sys.path:
    sys.path.insert(0, BASE)
try:
    from desc_text import (humanize_desc, audit_report_text, facts_brief,
                           fallback_desc)    # 描述规范化 + 事实校验 + CV 兜底说明 + 出报告前体检
except Exception as _e:                          # 兜底：模块缺失时不阻塞出报告
    def humanize_desc(raw, summary=None):
        return (raw or "").strip(), True, ""

    def facts_brief(summary=None):
        return ""

    def fallback_desc(summary=None, reason=""):
        return ""

    def audit_report_text(_html, limit=6):
        return []
    print(f"[warn] desc_text 模块不可用({_e})，描述将原样输出且跳过体检", file=sys.stderr)

RES = os.path.join(BASE, "results")

def fmt_dur(sec):
    try:
        sec = int(sec)
    except Exception:
        return "—"
    m, s = divmod(sec, 60)
    return f"{m}分{s:02d}秒" if m else f"{s}秒"

def parse_dt(s):
    if not s:
        return None
    try:
        return _dt.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def clock_of(start_dt, t_sec):
    """把视频相对秒映射到监控水印真实墙钟时间。"""
    if not start_dt:
        return None
    try:
        return start_dt + timedelta(seconds=int(round(t_sec)))
    except Exception:
        return None

def fmt_clock(dt, with_sec=False):
    if not dt:
        return "—"
    return dt.strftime("%H:%M:%S" if with_sec else "%H:%M")

_CN_DIGIT = {"零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
             "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}


def _cn_seq(s):
    """中文数字 → 阿拉伯数字：四→4、十→10、十二→12、二十→20、一两→1-2。"""
    if s == "十":
        return "10"
    if s.startswith("十") and len(s) == 2 and s[1] in _CN_DIGIT:
        return "1" + _CN_DIGIT[s[1]]
    if s.endswith("十") and len(s) == 2 and s[0] in _CN_DIGIT:
        return _CN_DIGIT[s[0]] + "0"
    if "十" in s and len(s) == 3:
        a, b = s.split("十", 1)
        if a in _CN_DIGIT and b in _CN_DIGIT:
            return _CN_DIGIT[a] + "0" + _CN_DIGIT[b]
    if len(s) == 2 and all(c in _CN_DIGIT for c in s):     # 一两 / 三四 → 1-2 / 3-4
        return _CN_DIGIT[s[0]] + "-" + _CN_DIGIT[s[1]]
    if all(c in _CN_DIGIT for c in s):
        return "".join(_CN_DIGIT[c] for c in s)
    return s


def cn_num_of(text):
    return re.sub(r"([零一二三四五六七八九十两]{1,3})\s*个?\s*人",
                  lambda m: _cn_seq(m.group(1)) + "人", text or "")


def extract_vlm_count(text):
    """从 VLM 描述里抽取人数口径，返回展示用短字符串。"""
    text = cn_num_of(text)
    m = re.search(r"在场人数", text)
    seg = text[m.end(): m.end() + 500] if m else text[:800]

    num = r"([0-9０-９]+(?:\s*[-~～—－–至到]\s*[0-9０-９]+)?\s*人)"
    num_bare = r"([0-9０-９]+(?:\s*[-~～—－–至到]\s*[0-9０-９]+)?)\s*人"

    def _grab(label_re, src=None):
        src = seg if src is None else src
        for gap in (r"[^0-9０-９\n]{0,30}", r"[^0-9０-９]{0,30}"):
            mm = re.search(label_re + gap + num, src)
            if mm:
                return mm.group(1).strip()
        return ""

    def _grab_prose(patterns):
        for p in patterns:
            mm = re.search(p, seg)
            if mm:
                return mm.group(1).strip() + "人"
        return ""

    typ = _grab(r"(?:[（(]\s*[aA]\s*[）)]\s*)?\*{0,2}\s*典型(?:人数)?(?:区间)?\*{0,2}")
    pk = _grab(r"(?:[（(]\s*[bB]\s*[）)]\s*)?\*{0,2}\s*峰值(?:人数)?\*{0,2}")

    if not typ:
        typ = _grab_prose([
            r"通常(?:会)?(?:有|为|是|保持在)?\s*" + num_bare,
            r"一般(?:会)?(?:有|为|是)?\s*" + num_bare,
            r"多数(?:帧|时候|时间)[^0-9０-９\n]{0,10}" + num_bare,
            r"(?:同时|大致|大约|约)(?:在|有|为)?\s*" + num_bare,
            r"平均[^0-9０-９\n]{0,6}" + num_bare,
        ])
    if not pk:
        pk = _grab_prose([
            r"最多(?:时|的时候|一刻)?(?:大约|约)?(?:有|为|达到|是)?\s*" + num_bare,
            r"峰值[^0-9０-９\n]{0,16}" + num_bare,
            r"人数最多[^0-9０-９\n]{0,16}" + num_bare,
        ])

    if typ or pk:
        parts = []
        if typ:
            parts.append("典型 " + typ)
        if pk:
            parts.append("峰值 " + pk)
        return " ／ ".join(parts)[:60]

    mm = re.search(r"([0-9]+)\s*[-~～—－–至到]\s*([0-9]+)\s*人", seg)
    if mm:
        return "≈%s-%s人" % (mm.group(1), mm.group(2))
    mm = re.search(r"([0-9]+)\s*人", seg)
    if mm:
        return "≈%s人" % mm.group(1)

    return "—"

def timeline_svg(timeline, start_dt=None, w=680, h=172):
    """30 秒峰值人数时间线。时间轴按监控水印真实墙钟标定（首尾 + 中间刻度 + 峰值标注）。"""
    if not timeline:
        return "<p style='color:#888'>无时间线数据</p>"
    tmin = timeline[0]["t_start_sec"]
    tmax = timeline[-1]["t_start_sec"]
    span = max(1, tmax - tmin)
    cmax = max(p["peak_count"] for p in timeline) or 1
    pad_l, pad_r, pad_t, pad_b = 40, 14, 24, 34
    plot_w = w - pad_l - pad_r
    bw = max(1.0, plot_w / max(1, len(timeline)))
    def x_of(t):
        return pad_l + (t - tmin) / span * plot_w
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" style="max-width:{w}px;background:#fafbfc;border:1px solid #e3e6ea;border-radius:8px">']
    parts.append(f'<line x1="{pad_l}" y1="{h-pad_b}" x2="{w-pad_r}" y2="{h-pad_b}" stroke="#cfd6dd"/>')
    parts.append(f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{h-pad_b}" stroke="#cfd6dd"/>')
    y_top = pad_t
    y_bot = h - pad_b
    y_span = max(1, y_bot - y_top)
    for k in range(0, int(cmax) + 1):
        yk = y_bot - (k / cmax) * y_span
        parts.append(f'<line x1="{pad_l}" y1="{yk:.1f}" x2="{w-pad_r}" y2="{yk:.1f}" stroke="#eef1f4" stroke-dasharray="2,3"/>')
        parts.append(f'<text x="{pad_l-5}" y="{yk+3:.1f}" font-size="9" fill="#66707a" text-anchor="end">{k}</text>')
    ymid = (y_top + y_bot) / 2
    parts.append(f'<text x="13" y="{ymid:.1f}" font-size="11" fill="#44505a" text-anchor="middle" transform="rotate(-90 13 {ymid:.1f})">人数</text>')
    for p in timeline:
        x = x_of(p["t_start_sec"])
        norm = p["peak_count"] / cmax
        bh = norm * (h - pad_t - pad_b)
        y = (h - pad_b) - bh
        col = "#d64545" if p["peak_count"] >= 4 else ("#e8a33d" if p["peak_count"] >= 2 else "#3d7fe8")
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw-0.6:.1f}" height="{bh:.1f}" fill="{col}" opacity="0.9"/>')
    for frac in [0.0, 0.25, 0.5, 0.75, 1.0]:
        t = tmin + frac * span
        x = x_of(t)
        clk = clock_of(start_dt, t)
        lbl = fmt_clock(clk) if clk else fmt_dur(t)
        anchor = "middle"
        if frac == 0.0:
            anchor = "start"
        elif frac == 1.0:
            anchor = "end"
        parts.append(f'<line x1="{x:.1f}" y1="{h-pad_b}" x2="{x:.1f}" y2="{h-pad_b+4}" stroke="#9aa4ae"/>')
        parts.append(f'<text x="{x:.1f}" y="{h-12}" font-size="10" fill="#66707a" text-anchor="{anchor}">{lbl}</text>')
    peak_i = max(range(len(timeline)), key=lambda i: timeline[i]["peak_count"])
    pp = timeline[peak_i]
    px = x_of(pp["t_start_sec"])
    pclk = clock_of(start_dt, pp["t_start_sec"])
    plbl = fmt_clock(pclk, with_sec=True) if pclk else fmt_dur(pp["t_start_sec"])
    parts.append(f'<text x="{px:.1f}" y="{pad_t-8}" font-size="11" fill="#d64545" text-anchor="middle" font-weight="bold">峰值 {pp["peak_count"]} 人 @ {plbl}</text>')
    parts.append('</svg>')
    return "".join(parts)

def peak_html(summary, vdir, html_dir=RES):
    peaks = summary.get("top_peaks", []) if summary else []
    start_dt = parse_dt(summary.get("video_start")) if summary else None
    if not peaks:
        return '<p style="color:#888">未记录峰值帧</p>'
    cells = []
    for pk in peaks:
        fp = os.path.join(vdir, "count_frames", pk["file"])
        rel = os.path.relpath(fp, html_dir).replace(os.sep, "/")
        clk = clock_of(start_dt, pk.get("t_sec", 0))
        clbl = fmt_clock(clk, with_sec=True) if clk else fmt_dur(pk.get("t_sec", 0))
        if os.path.exists(fp):
            cells.append(
                f'<figure style="margin:0">'
                f'<img src="{html.escape(rel)}" onclick="openImg(\'{rel}\')" '
                f'style="width:200px;height:112px;object-fit:cover;border:2px solid #d64545;border-radius:6px;cursor:zoom-in">'
                f'<figcaption style="font-size:12px;color:#444;text-align:center">{clbl} · {pk["count"]} 人（点击放大）</figcaption></figure>')
    if not cells:
        return '<p style="color:#888">峰值帧图片缺失</p>'
    return '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:6px">' + "".join(cells) + '</div>'

def sample_thumbs(vdir, html_dir=RES):
    frames = sorted(glob.glob(os.path.join(vdir, "count_frames", "count_*.jpg")))
    if not frames:
        return '<p style="color:#888">无抽样帧</p>'
    cells = []
    for fp in frames[:12]:
        rel = os.path.relpath(fp, html_dir).replace(os.sep, "/")
        cells.append(f'<img src="{html.escape(rel)}" onclick="openImg(\'{rel}\')" '
                     f'style="width:120px;height:68px;object-fit:cover;border:1px solid #ddd;border-radius:4px;cursor:zoom-in">')
    return '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">' + "".join(cells) + '</div>'

_DESC_FILE = "description_qwen3vl.txt"


def pick_desc(vdir):
    """挑出该视频用于报告的**唯一**一段场景描述。返回 (path, filename)；无则返回 ("", "")。"""
    p = os.path.join(vdir, _DESC_FILE)
    try:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p, _DESC_FILE
    except OSError:
        pass
    return "", ""


def cv_desc_block(title, summary, color):
    """没有可用的模型描述时，用 CV 结构化数据生成通俗说明（唯一口径的兜底，不回落旧模型文本）。"""
    txt = fallback_desc(summary, "")
    if not txt:
        return ""
    fb = facts_brief(summary)
    bar = (f'<div class="factbar">{html.escape(fb)}</div>') if fb else ""
    note = ('<div class="vlmnote">该视频暂无模型场景描述，'
            '以下由检测数据自动生成，内容通俗可直接阅读。</div>')
    return (f'<h3>{title} <span class="autotag">检测数据自动生成</span></h3>'
            f'<div class="vlm autofix" style="border-left:4px solid {color}">'
            f'{bar}{note}<pre>{html.escape(txt)}</pre></div>')


def vlm_block(title, path, color, summary=None, show_facts=False):
    """渲染一段 VLM 场景描述。
    - 原文先经 desc_text.humanize_desc() 规范化：剥 markdown、折叠重复标点、重组成自然段落；
    - 再做**事实校验**：场景描述允许模糊，但「时间 / 在场人数」是硬数据，任何与逐帧检测
      矛盾的句子都会剔除（如 CV 全程 0 人却写"有人进出"、时间与录制窗口不符）；
    - 若模型输出不可读或与检测数据冲突严重，自动改用检测数据生成的中文说明并标注原因。
    show_facts=True 时在描述上方加「数据基准」条（同一视频只加一次，钉死时间与峰值人数）。
    """
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return ""
    text, ok, reason = humanize_desc(raw, summary)
    if not text:
        return ""
    fb = facts_brief(summary) if show_facts else ""
    bar = (f'<div class="factbar">{html.escape(fb)}</div>') if fb else ""
    note = ""
    if ok:
        h3 = title
        cls = "vlm"
        if reason:
            note = (f'<div class="vlmnote">已按检测数据校验，模型描述中的'
                    f'{html.escape(reason)}已剔除。'
                    f'时间与在场人数一律以逐帧检测为准（见上方数据基准）。</div>')
    else:
        h3 = (title + ' <span class="autotag">检测数据自动生成</span>')
        cls = "vlm autofix"
        note = ('<div class="vlmnote">该模型的画面描述本次未能生成、不可读或与检测数据冲突'
                '（' + html.escape(reason) + '），以下改由检测数据自动生成，内容通俗可直接阅读。</div>')
    return (f'<h3>{h3}</h3><div class="{cls}" style="border-left:4px solid {color}">'
            f'{bar}{note}<pre>{html.escape(text)}</pre></div>')

def fmt_min_brief(sec):
    try:
        sec = float(sec)
    except Exception:
        return "—"
    if sec < 60:
        return f"{sec:.0f}秒"
    m = sec / 60
    return f"{m:.1f}分钟" if m < 60 else f"{m/60:.1f}小时"

def clean_name(video_path, fallback=""):
    """从源视频文件名提炼可读标题（去掉时间戳与 _device 后缀），用于报告显示。"""
    base = os.path.basename(video_path or "")
    if base.lower().endswith(".mp4"):
        base = base[:-4]
    if not base:
        return fallback
    m = re.match(r"^(.*?)_\d{14}_\d{14}(?:_device)?(?:_\d+)?$", base)
    return m.group(1).strip("_") if m else base

def video_report_time(vdir):
    """该视频的『报告产出时间』= results/<key>/ 目录内最新产出文件的修改时间。
    刻意**排除 count_summary.json**：后处理会在同一秒内批量重写它（只补派生字段）。"""
    latest = None
    for root, _dirs, files in os.walk(vdir):
        for fn in files:
            if fn.startswith(".") or fn == "count_summary.json":
                continue
            try:
                mt = os.path.getmtime(os.path.join(root, fn))
            except OSError:
                continue
            if latest is None or mt > latest:
                latest = mt
    if latest is None:   # 兜底：目录里只有 count_summary.json
        jp = os.path.join(vdir, "count_summary.json")
        if os.path.exists(jp):
            try:
                latest = os.path.getmtime(jp)
            except OSError:
                latest = None
    if latest is None:
        return None, "—"
    d = _dt.fromtimestamp(latest)
    return d, d.strftime("%Y-%m-%d %H:%M:%S")


def attendance_html(s):
    """在场时段分布: 几点到几点有人/无人 (由 CV 逐帧计数按水印时间聚合)"""
    att = (s or {}).get("attendance")
    if not att:
        return ""
    busy = att.get("busy_periods", [])
    idle = att.get("idle_periods", [])
    st = att.get("stats", {})
    rows = []
    for p in busy:
        rows.append(
            f'<tr style="background:#fff5f5">'
            f'<td style="white-space:nowrap"><span style="color:#d64545">\u25a0</span> 有人</td>'
            f'<td style="white-space:nowrap">{p["clock_start"]} – {p["clock_end"]}</td>'
            f'<td>{fmt_min_brief(p["dur_sec"])}</td>'
            f'<td><b>{p["peak"]}</b></td></tr>')
    for p in idle:
        rows.append(
            f'<tr style="background:#f8f9fa;color:#7b8794">'
            f'<td style="white-space:nowrap"><span style="color:#b8c0c8">\u25a1</span> 无人</td>'
            f'<td style="white-space:nowrap">{p["clock_start"]} – {p["clock_end"]}</td>'
            f'<td>{fmt_min_brief(p["dur_sec"])}</td><td>0</td></tr>')
    tbl = ('<table style="margin-top:6px"><tr><th>状态</th><th>时段（监控水印时间）</th>'
           '<th>时长</th><th>峰值人数</th></tr>' + "".join(rows) + '</table>')
    nar = att.get("narrative", "")
    nar = re.sub(r"[、,，]?\s*平均\s*[0-9]+(?:\.[0-9]+)?\s*人", "", nar)
    nar = html.escape(nar)
    head = (f'共 <b>{st.get("busy_period_count",0)}</b> 个有人时段，累计约 '
            f'<b>{fmt_min_brief(st.get("busy_total_sec",0))}</b>'
            f'（占全程 {st.get("busy_ratio",0)*100:.0f}%）；其余时段无人。')
    return (f'<h3>\U0001f551 在场时段分布（几点有人 / 几点无人，按监控水印真实时间）</h3>'
            f'<p class="sub">{head}</p>'
            f'<div class="vlm" style="border-left:4px solid #e8a33d"><pre>{nar}</pre></div>'
            f'{tbl}')

def video_section(key, vdir, html_dir=RES):
    jp = os.path.join(vdir, "count_summary.json")
    summary = {}
    if os.path.exists(jp):
        with open(jp, encoding="utf-8") as f:
            summary = json.load(f)
    s = summary
    if s:
        wm_start = parse_dt(s.get("video_start"))
        wm_end = parse_dt(s.get("video_end"))
        wm_date = wm_start.strftime("%Y-%m-%d") if wm_start else "—"
        stats = (f'<div style="display:flex;gap:10px;flex-wrap:wrap;margin:6px 0">'
            f'<div class="stat"><b>{s.get("person_peak","-")}</b><span>峰值人数</span></div>'
            f'<div class="stat"><b>{fmt_dur(s.get("total_frames",0)/s.get("fps",25))}</b><span>录制时长</span></div>'
            f'<div class="stat"><b>{wm_date}</b><span>水印日期</span></div>'
            f'<div class="stat"><b>{fmt_clock(wm_start, with_sec=False)}–{fmt_clock(wm_end, with_sec=False)}</b><span>水印时段</span></div>'
            f'<div class="stat"><b>{s.get("sampled_frames","-")}</b><span>采样帧数</span></div>'
            f'</div>')
        start_dt = parse_dt(s.get("video_start")) if s else None
        tl = timeline_svg(s.get("timeline_30s_peak", []), start_dt)
        pk = peak_html(s, vdir, html_dir=html_dir)
        peak_clk = clock_of(start_dt, s.get("peak_frame_t_sec") or 0)
        ptime = fmt_clock(peak_clk, with_sec=True) if peak_clk else fmt_dur(s.get("peak_frame_t_sec") or 0)
    else:
        stats = '<p style="color:#c0392b">CV 计数结果缺失</p>'; tl = ""; pk = ""; ptime = "—"

    att = attendance_html(s)
    _dpath, _dsrc = pick_desc(vdir)
    if _dpath:
        b3 = vlm_block("场景描述 · AI 视觉理解（仅供参考）", _dpath, "#2257c9",
                       summary=s, show_facts=True)
    else:
        b3 = cv_desc_block("场景描述 · AI 视觉理解（仅供参考）", s, "#2257c9")
    thumbs = sample_thumbs(vdir, html_dir=html_dir)

    return f"""
    <section class="vid" id="sec-{html.escape(key)}">
      <h2>{html.escape(clean_name(s.get('video','') if s else '', key))}</h2>
      <p class="sub">{html.escape(s.get('video','') if s else '')}</p>
      {stats}
      <h3>在场人数 · 30秒峰值时间线</h3>
      {tl}
      {att}
      <h3>🔴 峰值时刻证据帧（人数最多前 3 帧，带检测框）</h3>
      {('<p class="sub">峰值出现在监控水印时间 ' + ptime + ' 左右</p>') if (s and s.get('peak_frame_t_sec')) else ''}
      {pk}
      {b3}
      <h3>常规抽样证据帧（带检测框）</h3>
      {thumbs}
    </section>
    """

def write_history_index(index_path):
    """扫描 results/稽核报告_YYYYMMDD.html 历史归档，按日期倒序生成索引页（卡片式入口）。"""
    import re as _re
    cards = []
    for fn in sorted(os.listdir(RES)):
        m = _re.match(r"^稽核报告_(\d{8})\.html$", fn)
        if not m:
            continue
        date_lbl = m.group(1)
        date_pretty = f"{date_lbl[0:4]}-{date_lbl[4:6]}-{date_lbl[6:8]}"
        fp = os.path.join(RES, fn)
        v_count = "—"; max_peak = "—"
        try:
            txt = open(fp, encoding="utf-8").read()
            m2 = _re.search(r"共\s*(\d+)\s*个视频", txt)
            if m2: v_count = m2.group(1)
            for x in _re.findall(r'<b>([0-9.]+)</b><span>峰值人数</span>', txt):
                try:
                    p = float(x)
                    if int(p) > int(max_peak) if str(max_peak).isdigit() else True:
                        max_peak = str(int(p))
                except Exception:
                    pass
        except Exception:
            pass
        try:
            st = os.stat(fp)
            mtime = _dt.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            size_kb = st.st_size // 1024
        except Exception:
            mtime = "—"; size_kb = 0
        cards.append((date_lbl, date_pretty, v_count, max_peak, mtime, size_kb, fn))
    cards.sort(reverse=True)  # 日期倒序

    rows = []
    for d_lbl, d_pretty, v_count, max_peak, mtime, size_kb, fn in cards:
        rows.append(
            f'<tr><td style="white-space:nowrap"><b>{d_pretty}</b><br><span style="color:#7b8794;font-size:12px">{d_lbl}</span></td>'
            f'<td style="white-space:nowrap"><b>{v_count}</b> 个</td>'
            f'<td><b style="color:#d64545">{max_peak}</b></td>'
            f'<td style="font-size:12px;color:#7b8794">{mtime}</td>'
            f'<td style="font-size:12px;color:#7b8794">{size_kb} KB</td>'
            f'<td><a class="vlink" href="{html.escape(fn)}">打开报告 →</a></td></tr>')

    total_videos = len([d for d in os.listdir(RES) if os.path.isdir(os.path.join(RES, d))])
    sum_videos = 0
    for c in cards:
        try:
            sum_videos += int(c[2])
        except (TypeError, ValueError):
            pass

    html_out = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>稽核报告历史索引</title>
<style>
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f7fa;color:#1f2933;margin:0;padding:24px}}
.wrap{{max-width:1100px;margin:0 auto;background:#fff;padding:24px;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.06)}}
h1{{font-size:22px;margin:0 0 4px}} .meta{{color:#7b8794;font-size:13px;margin-bottom:18px}}
table{{border-collapse:collapse;width:100%;margin:10px 0 20px;font-size:14px}}
th,td{{border:1px solid #e3e6ea;padding:8px 10px;text-align:left}}
th{{background:#f0f3f7;text-align:center}}
.banner{{background:linear-gradient(135deg,#2257c9,#1f9d55);color:#fff;padding:14px 18px;border-radius:10px;margin-bottom:14px}}
.banner .dt{{font-size:20px;font-weight:700;letter-spacing:.5px}}
.banner .stamp{{font-size:12px;opacity:.85}}
.vlink{{color:#2257c9;font-weight:600;border-bottom:1px dashed #9db8ea}}
.vlink:hover{{color:#16389a;background:#eef3ff}}
.empty{{color:#7b8794;text-align:center;padding:30px}}
#toTop{{position:fixed!important;right:24px!important;bottom:24px!important;z-index:2147483647!important;width:52px!important;height:52px!important;border-radius:50%!important;padding:0!important;border:none!important;background:linear-gradient(160deg,#3f6fe4 0%,#2257c9 62%,#1b48b0 100%)!important;display:flex!important;align-items:center!important;justify-content:center!important;cursor:pointer!important;box-shadow:0 6px 16px rgba(34,87,201,.34),0 2px 5px rgba(0,0,0,.16)!important;transition:transform .22s cubic-bezier(.2,.8,.3,1.2),box-shadow .22s ease,filter .22s ease}}
#toTop:hover{{transform:translateY(-3px) scale(1.06);box-shadow:0 10px 22px rgba(34,87,201,.46),0 3px 7px rgba(0,0,0,.18);filter:brightness(1.05)}}
#toTop:active{{transform:translateY(-1px) scale(.96)}}
#toTop svg{{display:block;width:24px;height:24px}}
</style></head>
<body><div class="wrap">
<div class="banner">
  <div class="dt">📚 稽核报告历史索引</div>
  <div class="stamp">每次 build_report.py 都会归档到 results/稽核报告_YYYYMMDD.html，共 {len(cards)} 份</div>
</div>
<div class="meta">
  <b>总览</b>：results/ 目录内共 <b>{total_videos}</b> 个视频，已归档 <b>{len(cards)}</b> 天、报告去重合计 <b>{sum_videos}</b> 个视频。<br>
  「报告内视频数」=<b>该份报告里包含几个视频</b>（按处理日期切分，例如 15 + 9 = 24），不是目录里的视频总数。
  最新一份指向"稽核报告_最新.html"（与最新日期归档同内容）；点表行"打开报告"进入当日完整报告；当日最大峰值取当天所有视频的最大值。
</div>
<h3>按日期倒序</h3>
{('<table><thead><tr><th>日期</th><th>报告内视频数</th><th>当日最大峰值</th><th>生成时间</th><th>大小</th><th>入口</th></tr></thead><tbody>' + ''.join(rows) +
  f'<tr style="background:#f0f3f7"><td><b>合计</b></td><td><b>{sum_videos}</b> 个</td><td>—</td><td>—</td><td>—</td><td style="color:#7b8794;font-size:12px">共 {len(cards)} 份归档</td></tr>'
  + '</tbody></table>') if cards else '<div class="empty">尚无归档报告</div>'}
<p style="color:#7b8794;font-size:12px;margin-top:20px">使用：<code>python build_report.py --date 20260910</code> 生成指定日期归档（默认今天北京时间）；<code>--all</code> 生成全量报告。</p>
<script></script>
<button id="toTop" title="返回顶部" aria-label="返回顶部" style="position:fixed;right:24px;bottom:24px;z-index:2147483647;width:52px;height:52px;border-radius:50%;padding:0;border:none;background:#2257c9;cursor:pointer;box-shadow:0 8px 20px rgba(34,87,201,.42)" onclick="window.scrollTo({{top:0,behavior:'smooth'}})"><svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 15l6-6 6 6"/></svg></button>
</div></body></html>"""
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html_out)

def get_processed_date(vdir):
    """读取 results/<key>/ 的处理日期（北京时间 YYYY-MM-DD）。
    优先取 count_summary.json.processed_at（精确 ISO 时间戳，由 count_people.py 写入）；
    否则退回『CV 检测帧 count_frames/*.jpg 的最早产出时间』；
    最后才用 count_summary.json 的 mtime 兜底。"""
    jp = os.path.join(vdir, "count_summary.json")
    if not os.path.exists(jp):
        return None
    try:
        with open(jp, encoding="utf-8") as f:
            s = json.load(f)
        pa = s.get("processed_at")
        if pa and isinstance(pa, str) and len(pa) >= 10:
            return pa[:10]  # "2026-09-11T..." -> "2026-09-11"
    except Exception:
        pass
    frames = glob.glob(os.path.join(vdir, "count_frames", "*.jpg"))
    if frames:
        try:
            mt = min(os.path.getmtime(fp) for fp in frames)
            return _dt.fromtimestamp(mt).strftime("%Y-%m-%d")
        except Exception:
            pass
    try:
        mt = os.path.getmtime(jp)
        return _dt.fromtimestamp(mt).strftime("%Y-%m-%d")
    except Exception:
        return None

def main(only_key=None, out_path=None, date_str=None, all_videos=False, only_keys=None):
    keys_all = sorted([d for d in os.listdir(RES) if os.path.isdir(os.path.join(RES, d))])
    html_dir = os.path.dirname(out_path) if out_path else RES
    index_path = os.path.join(RES, "稽核报告_历史.html")
    history_href = os.path.relpath(index_path, html_dir)
    scope_note = ""
    if only_keys:
        want = [k for k in only_keys if k]
        keys = [k for k in keys_all if k in set(want)]
        miss = [k for k in want if k not in keys]
        if miss:
            print(f"--keys 未找到（已忽略）: {miss}")
        if not keys:
            print("--keys 指定的 key 均不存在，跳过生成。")
            return None
        print(f"--keys 定向模式: {len(keys)} 个指定视频进入报告")
    elif all_videos:
        keys = keys_all
        print(f"--all 模式: {len(keys)} 个视频全量进入报告")
    else:
        from datetime import datetime as _dd
        if date_str:
            tgt = date_str if "-" in date_str else f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"
        else:
            tgt = _dd.now().strftime("%Y-%m-%d")
        keys = [k for k in keys_all if get_processed_date(os.path.join(RES, k)) == tgt]
        if not keys:
            print(f"日期 {tgt} 没有当日处理的视频，跳过生成。")
            print(f"  提示：处理时间取自 results/<key>/count_summary.json.processed_at（缺则用文件 mtime）。")
            print(f"  提示：要看历史归档请 --date YYYY-MM-DD；要看全量请 --all。")
            return None
        print(f"日期过滤 {tgt}: {len(keys)}/{len(keys_all)} 个视频进入报告")
        if len(keys) < len(keys_all):
            scope_note = (f'（results/ 目录内全量 <b>{len(keys_all)}</b> 个视频，'
                          f'另 <b>{len(keys_all)-len(keys)}</b> 个属其他处理日期、见'
                          f'<a class="vlink" href="{history_href}">历史索引</a>）')
    if only_key:
        keys = [k for k in keys if k == only_key]
        if not keys:
            print(f"未找到 key={only_key}")
            return None
    now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    html_dir = os.path.dirname(out_path) if out_path else RES
    rtime = {k: video_report_time(os.path.join(RES, k)) for k in keys}
    keys = sorted(keys, key=lambda k: (rtime[k][0].timestamp() if rtime[k][0] else 0.0))
    sections, rows = [], []
    for k in keys:
        vdir = os.path.join(RES, k)
        s = {}
        jp = os.path.join(vdir, "count_summary.json")
        if os.path.exists(jp):
            with open(jp, encoding="utf-8") as f:
                s = json.load(f)
        dur = fmt_dur(s.get("total_frames",0)/s.get("fps",25)) if s else "—"
        if s:
            _sd = parse_dt(s.get("video_start"))
            _pc = clock_of(_sd, s.get("peak_frame_t_sec") or 0)
            ptime = fmt_clock(_pc, with_sec=True) if _pc else fmt_dur(s.get("peak_frame_t_sec") or 0)
        else:
            ptime = "—"
        name = clean_name(s.get("video","") or k, k)
        _sd = parse_dt(s.get("video_start")) if s else None
        _ed = parse_dt(s.get("video_end")) if s else None
        if _sd and _ed:
            win = f'{_sd.strftime("%Y-%m-%d %H:%M:%S")} ~ {_ed.strftime("%H:%M:%S")}'
        elif _sd:
            win = _sd.strftime("%Y-%m-%d %H:%M:%S")
        else:
            win = "—"
        rows.append((k, name, win, dur, s.get("person_peak","—"), ptime, rtime[k][1]))
        sections.append(video_section(k, vdir, html_dir=html_dir))

    _trs = []
    for r in rows:
        try:
            _zero = int(r[4]) == 0
        except (TypeError, ValueError):
            _zero = False
        if _zero:
            pk_cell = "<b>0</b> <span class='zerotag'>无人</span>"
        else:
            pk_cell = f"<b>{r[4]}</b>"
        _trs.append(
            f"<tr><td><a class='vlink' href='#sec-{html.escape(r[0])}'>"
            f"{html.escape(str(r[1]))}</a></td>"
            f"<td>{r[2]}</td><td>{r[3]}</td><td>{pk_cell}</td>"
            f"<td>{r[5]}</td>"
            f'<td style="white-space:nowrap;color:#7b8794">{r[6]}</td></tr>')
    table_rows = "".join(_trs)

    _ds = (date_str or _dt.now().strftime("%Y%m%d"))
    date_lbl = _ds.replace("-", "")
    date_pretty = (f"{date_lbl[0:4]}-{date_lbl[4:6]}-{date_lbl[6:8]}"
                   if len(date_lbl) == 8 else _ds)
    out = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>门店监控视频 AI 稽核报告 · {date_pretty}</title>
<style>
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f7fa;color:#1f2933;margin:0;padding:24px}}
h1{{font-size:22px;margin:0 0 4px}} .meta{{color:#7b8794;font-size:13px;margin-bottom:18px}}
.wrap{{max-width:960px;margin:0 auto;background:#fff;padding:24px;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.06)}}
table{{border-collapse:collapse;width:100%;margin:10px 0 20px;font-size:14px}}
th,td{{border:1px solid #e3e6ea;padding:8px 10px;text-align:center}}
th{{background:#f0f3f7}}
.stat{{background:#f0f3f7;border-radius:8px;padding:8px 14px;text-align:center;min-width:72px}}
.stat b{{display:block;font-size:20px;color:#2257c9}}
.stat span{{font-size:12px;color:#7b8794}}
.vid{{border-top:2px solid #eef1f4;padding-top:14px;margin-top:18px}}
.zerotag{{display:inline-block;font-size:11px;color:#b97a09;background:#fff6e5;border:1px solid #f0d199;border-radius:8px;padding:0 5px;vertical-align:middle}}
.vid h2{{font-size:18px;margin:0}} .sub{{color:#7b8794;font-size:12px;word-break:break-all}}
.vid h3{{font-size:15px;margin:14px 0 4px;color:#34495e}}
.vlm{{background:#f8fafc;border:1px solid #e3e6ea;border-radius:8px;padding:10px;margin-bottom:10px}}
.vlm pre{{white-space:pre-wrap;word-break:break-word;font-family:inherit;margin:0;font-size:13.5px;line-height:1.6}}
.vlm.autofix{{background:#fffdf6;border:1px dashed #e8c07a}}
.vlmnote{{font-size:12.5px;color:#b97a09;background:#fff7e6;border-radius:6px;padding:6px 9px;margin-bottom:8px;line-height:1.55}}
.factbar{{font-size:12.5px;color:#1f4f9c;background:#eef3ff;border:1px solid #c9d9f7;border-radius:6px;padding:6px 9px;margin-bottom:8px;line-height:1.55;font-weight:600}}
.autotag{{display:inline-block;background:#e8a33d;color:#fff;font-size:11.5px;font-weight:700;padding:1px 8px;border-radius:9px;margin-left:6px;vertical-align:middle;white-space:nowrap}}
a{{text-decoration:none}}
.vlink{{color:#2257c9;font-weight:600;cursor:pointer;border-bottom:1px dashed #9db8ea}}
.vlink:hover{{color:#16389a;background:#eef3ff}}
#imgModal{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.86);z-index:9999;align-items:center;justify-content:center;cursor:zoom-out}}
#imgModal img{{max-width:94%;max-height:94%;border:2px solid #fff;border-radius:6px;box-shadow:0 4px 24px rgba(0,0,0,.5);cursor:default}}
#imgModal .x{{position:fixed;top:14px;right:22px;color:#fff;font-size:32px;font-weight:bold;cursor:pointer;line-height:1}}
.banner{{background:linear-gradient(135deg,#2257c9,#1f9d55);color:#fff;padding:14px 18px;border-radius:10px;margin-bottom:14px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}}
.banner .dt{{font-size:20px;font-weight:700;letter-spacing:.5px}}
.banner .stamp{{font-size:12px;opacity:.85}}
.banner a{{color:#fff;border:1px solid rgba(255,255,255,.45);padding:4px 10px;border-radius:14px;font-size:12px}}
.banner a:hover{{background:rgba(255,255,255,.18)}}
#toTop{{position:fixed!important;right:24px!important;bottom:24px!important;z-index:2147483647!important;width:52px!important;height:52px!important;border-radius:50%!important;padding:0!important;border:none!important;background:linear-gradient(160deg,#3f6fe4 0%,#2257c9 62%,#1b48b0 100%)!important;display:flex!important;align-items:center!important;justify-content:center!important;cursor:pointer!important;box-shadow:0 6px 16px rgba(34,87,201,.34),0 2px 5px rgba(0,0,0,.16)!important;transition:transform .22s cubic-bezier(.2,.8,.3,1.2),box-shadow .22s ease,filter .22s ease}}
#toTop:hover{{transform:translateY(-3px) scale(1.06);box-shadow:0 10px 22px rgba(34,87,201,.46),0 3px 7px rgba(0,0,0,.18);filter:brightness(1.05)}}
#toTop:active{{transform:translateY(-1px) scale(.96)}}
#toTop svg{{display:block;width:24px;height:24px}}
.back-compare{{position:fixed;right:24px;bottom:88px;z-index:2147483647;display:none;align-items:center;gap:6px;background:#1f9d55;color:#fff;text-decoration:none;font-size:14px;font-weight:600;padding:11px 16px;border-radius:24px;box-shadow:0 6px 16px rgba(31,157,85,.38);cursor:pointer}}
.back-compare:hover{{background:#178a48}}
</style></head>
<body><div class="wrap">
<div id="imgModal" onclick="closeImg()"><span class="x" onclick="closeImg()">&times;</span><img id="imgModalImg" onclick="event.stopPropagation()"></div>
<div class="banner">
  <div><div class="dt">📋 稽核报告 · {date_pretty}</div><div class="stamp">报告归档号 {date_lbl} ｜ 生成于 {now}</div></div>
  <div><a href="{history_href}">📚 历史报告索引</a></div>
</div>
<div class="meta">CV=YOLOv8m+ByteTrack（精确人数） · 场景描述=Qwen3-VL-4B（全报告单一来源，仅作语义参考）；在场时段分布由 CV 计数聚合 ｜ 时间按监控水印真实时间(录制窗口)标定 ｜ 共 {len(keys)} 个视频{scope_note}</div>
<h3>跨视频对比</h3>
<table><thead><tr><th>视频（文件名）</th><th>监控时间（水印）</th><th>时长</th><th>峰值人数</th><th>峰值时刻</th><th title="该视频结果目录中最新的产出文件时间（counts.csv / count_frames / description 等；不含会被批量补跑重写的 count_summary.json）">报告产出时间</th></tr></thead>
<tbody>{table_rows}</tbody></table>
{''.join(sections)}
<p style="color:#7b8794;font-size:12px;margin-top:20px"><b>排序</b>：下表与各视频分区均按「报告产出时间」<b>升序</b>排列（产出时间 = 该视频结果目录内 counts.csv / count_frames / description 等<b>实质产出</b>文件的最新时间，见末列；不含会被批量后处理重写的 count_summary.json）。说明：精确人数（峰值）由 CV 检测计数得出；场景描述由视觉模型生成，仅作语义参考，不参与计数，一切数字以 CV 为准。时间轴与峰值时刻均按<b>监控视频内水印真实时间</b>（录制窗口，映射自文件名中的起止时间戳，监控录像 1x 实时）标定，非视频相对秒；轴上除首尾外还在 25%/50%/75% 处标注中间时刻，峰值柱上方标注具体时刻，便于判断何人最多。红色边框为人数最多的峰值时刻证据帧，点击图片可在本页放大查看，点空白处或右上角 × 关闭。<b>在场时段分布</b>（几点有人/几点无人）同样基于 CV 逐帧计数按水印时间聚合：以采样帧的在场状态做分段，5 分钟以内的短暂无人间隔不切断同一段有人时段，边界精度约 ±1 个采样间隔（≈4 秒）。跨视频对比中『峰值人数』列为 <b>0 无人</b> 的视频，表示 CV 逐帧检测人数<b>恒为 0</b>（无真人出现，属无人经过的机位/时段，非漏检）；这类视频的场景描述只谈场所环境与画面变化，不得出现任何人。<b>场景描述</b>全报告只保留<b>一段</b>（避免多模型描述互相打架），统一为固定 5 行标签式短句：<b>场景概述 / 人员出入 / 主要活动 / 异常事件 / 时间变化</b>，每行一句话、整段不超过 100 字；自动去除 markdown 符号与冗余标点，并做<b>事实校验</b>——场景描述允许笼统，但<b>时间</b>与<b>在场人数</b>属于硬数据，一律以逐帧检测与监控记录为准：<b>描述中不保留模型自述的人数</b>（人数见各视频统计卡、峰值时间线与本表『峰值人数』列），模型自述的时间若与录制窗口不符也会剔除，CV 判定全程 0 人的视频还会剔除其"有人/有人进出"的描述。被剔除的内容会在描述上方以「数据基准」与提示条标明；若模型描述未能生成、不可读或与检测数据冲突严重，则改用检测数据自动生成通俗说明，并标注「检测数据自动生成」。</p>
<script>
function openImg(src){{document.getElementById('imgModalImg').src=src;document.getElementById('imgModal').style.display='flex';}}
function closeImg(){{document.getElementById('imgModal').style.display='none';document.getElementById('imgModalImg').src='';}}
document.addEventListener('keydown',function(e){{if(e.key==='Escape')closeImg();}});
</script>
<button id="toTop" title="返回顶部" aria-label="返回顶部" style="position:fixed;right:24px;bottom:24px;z-index:2147483647;width:52px;height:52px;border-radius:50%;padding:0;border:none;background:#2257c9;cursor:pointer;box-shadow:0 8px 20px rgba(34,87,201,.42)" onclick="window.scrollTo({{top:0,behavior:'smooth'}})"><svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 15l6-6 6 6"/></svg></button>
<a id="backToCompare" class="back-compare" href="人工AI峰值核对对比报告.html">↩ 返回人工核对报告</a>
<script>
(function(){{
  try{{
    var p=new URLSearchParams(location.search);
    if(p.get('from')==='compare'){{
      var b=document.getElementById('backToCompare');
      if(b) b.style.display='flex';
    }}
  }}catch(e){{}}
}})();
</script>
</div></body></html>"""
    # ===== 出报告前体检（强制）=====
    def _audit(label, text):
        probs = audit_report_text(text)
        if probs:
            print(f"[体检][失败] {label}：可见文本仍有 {len(probs)} 处不规范内容 —— 需修正！")
            for nm, ctx in probs:
                print(f"    - {nm}：…{ctx}…")
        else:
            print(f"[体检][通过] {label}：无 markdown 符号 / 重复标点 / 机械复读")
        return not probs

    if out_path:
        dated_path = out_path
    else:
        dated_path = os.path.join(RES, f"稽核报告_{date_lbl}.html")
    latest_path = os.path.join(RES, "稽核报告_最新.html")
    legacy_path = os.path.join(RES, "稽核报告_全部视频.html")  # 兼容旧链接

    if only_keys and out_path:
        with open(dated_path, "w", encoding="utf-8") as f:
            f.write(out)
        _audit(os.path.basename(dated_path), out)
        print(f"报告已生成(定向 --keys): {dated_path}")
        print(f"视频数: {len(keys)}")
        return dated_path

    with open(dated_path, "w", encoding="utf-8") as f:
        f.write(out)
    try:
        with open(latest_path, "w", encoding="utf-8") as f:
            f.write(out)
    except Exception as e:
        print(f"[warn] 写最新副本失败: {e}")
    try:
        with open(legacy_path, "w", encoding="utf-8") as f:
            f.write(out)
    except Exception as e:
        print(f"[warn] 写旧链接兼容文件失败: {e}")
    index_path = os.path.join(RES, "稽核报告_历史.html")
    write_history_index(index_path)

    print(f"报告已生成(本日归档): {dated_path}")
    print(f"报告已同步(快速入口): {latest_path}")
    print(f"报告已同步(旧链接兼容): {legacy_path}")
    print(f"历史索引已更新: {index_path}")
    _audit(os.path.basename(dated_path), out)
    print(f"视频数: {len(keys)}")

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", help="只生成该 key 的单视频报告")
    ap.add_argument("--out", help="输出 HTML 路径")
    ap.add_argument("--date", help="日期 YYYY-MM-DD 或 YYYYMMDD，默认今天")
    ap.add_argument("--all", dest="all_videos", action="store_true", help="不看日期过滤，全量输出")
    ap.add_argument("--keys", help="只生成这些 key（逗号分隔），不受日期过滤；配 --out 使用（定向测试用）")
    a = ap.parse_args()
    main(only_key=a.key, out_path=a.out, date_str=a.date, all_videos=a.all_videos,
         only_keys=(a.keys.split(",") if a.keys else None))
