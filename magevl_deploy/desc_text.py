#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
描述文本规范化（报告渲染统一入口）
==================================
VLM 原始输出常见三类问题，全部在此收敛，报告层只吃"人话"：

1. **Markdown 符号**：`**粗体**`、`# 标题`、`- 列表`、`1. 编号`、`===`/`---` 分隔线、
   代码反引号 —— 一律剥掉，重组为自然段落。
2. **标点堆砌**：`!!!!!!!!!!!!`（模型退化时的复读）—— 折叠为单个标点。
3. **完全不可读**：空输出 / 全是符号 / 汉字过少 —— 判定为退化，改用
   `fallback_desc()` 依据 CV 结构化数据自动生成通俗中文说明。
4. **与检测数据冲突**（`factcheck()`）：场景描述允许模糊，但**时间**与**在场人数**
   是硬数据，必须与逐帧检测一致 —— CV 说全程 0 人时，描述里任何"有人"的句子一律
   剔除；描述里报的时间若落在录制窗口之外，整句剔除。剔除后不成句则改走 3。

对外只暴露一个入口：
    text, ok, reason = humanize_desc(raw_text, summary)
      raw_text : VLM 原始文本
      summary  : results/<key>/count_summary.json 的 dict（可 None）
      -> text  : 保证可读的中文说明（永远不会是符号堆砌，也不会与检测数据矛盾）
         ok    : True=模型原文可用（可能已按事实校验删减）；False=已用 CV 数据兜底
         reason: ok=False 时给出原因短语；ok=True 时若非空，表示已做事实删减
"""

import re
from datetime import datetime as _dt, timedelta as _td

# ---------------------------------------------------------------- 常量

_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"[-*+•·▪◦‣∙]"
    r"|\d{1,2}\s*[.、)）]"
    r"|[（(]\s*\d{1,2}\s*[）)]"
    r"|[（(]?\s*[a-zA-Z]\s*[)）.、]"
    r"|[（(]\s*[a-zA-Z]\s*[）)]"
    r")\s*"
)
_RULE_RE = re.compile(r"^\s*(?:[-*_=~—–•·]{3,}|=+\s*\S*\s*=+)\s*$")
_WRAPPED_RULE_RE = re.compile(r"^\s*=+\s*[^=]*\s*=+\s*$")
_EMPH_RE = re.compile(r"\*\*|__|~~|`")
_HASH_RE = re.compile(r"^\s{0,3}#{1,6}\s*")
_PUNCT_RUN_RE = re.compile(r"([!！?？。，,、；;：:~～\-_…])\1{1,}")
_DUP_PHRASE_RE = re.compile(r"([^\s，。；！？]{2,12}?)\1{2,}")
_INLINE_MARKER_RE = re.compile(r"(?:(?<=[；;。，,、])|^)\s*[（(]?\s*[a-fA-F]\s*[)）]\s*")
_SENT_END = "。！？；"


def _collapse_punct(t):
    return _PUNCT_RUN_RE.sub(r"\1", t)


def _strip_markdown(t):
    out_lines = []
    for raw in t.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            out_lines.append("")
            continue
        if _RULE_RE.match(line) or _WRAPPED_RULE_RE.match(line):
            out_lines.append("")
            continue
        line = _HASH_RE.sub("", line)
        for _ in range(4):
            before = line
            line = _EMPH_RE.sub("", line)
            line = _MARKER_RE.sub("", line)
            if line == before:
                break
        line = line.replace("|", " ").replace("\\", "")
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        if line:
            out_lines.append(line)
        else:
            out_lines.append("")
    return out_lines


def _labelize(text):
    def wrap(item):
        m = re.match(r"^(.{2,14}?)[：:]\s*(.+)$", item)
        if m and m.group(2):
            inner = m.group(1)
            content = m.group(2).strip().strip("。；;，, ")
            return "%s（%s）" % (inner, content) if content else inner
        return item

    body = text.strip()
    m = re.match(r"^(.{2,10}?)[：:](.+)$", body)
    if m and re.search(r"[：:]", m.group(2)):
        items = [x for x in re.split(r"[；;]", m.group(2)) if x.strip()]
        if len(items) >= 2 and all(re.match(r"^.{2,14}?[：:]", x.strip()) for x in items):
            return m.group(1) + "：" + "，".join(wrap(x.strip()) for x in items) + \
                   ("" if body.endswith(("。", "！", "？")) else "。")
    return body


_LABEL_LINE_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z·]{1,9}[：:]\s*\S")


def _assemble(lines):
    blocks = []
    cur = []
    for ln in lines:
        if not ln:
            if cur:
                blocks.append(cur)
                cur = []
            continue
        if _LABEL_LINE_RE.match(ln) and cur:
            blocks.append(cur)
            cur = [ln]
            continue
        cur.append(ln)
    if cur:
        blocks.append(cur)

    paras = []
    for blk in blocks:
        head = blk[0] if (len(blk) > 1 and blk[0].endswith(("：", ":")) and len(blk[0]) <= 16) else None
        if head:
            parts = []
            for item in blk[1:]:
                m = re.match(r"^(.{2,14}?)[：:]\s*(.+)$", item)
                if m and m.group(2):
                    inner = m.group(2).strip().strip("。；;，, ")
                    parts.append("%s（%s）" % (m.group(1), inner) if inner else m.group(1))
                else:
                    parts.append(item)
            body = head + "，".join(parts)
        else:
            body = "".join(blk) if len(blk) > 1 else blk[0]
        body = body.strip("，、； ")
        body = _labelize(body)
        if body and body[-1] not in _SENT_END and body[-1] not in "：:":
            body += "。"
        if body:
            paras.append(body)
    return "\n".join(paras)


def normalize(text):
    """markdown 剥离 + 标点折叠 + 段落重组。永不抛异常，输入 None 返回 ''。"""
    if not text:
        return ""
    t = _strip_markdown(text)
    t = _assemble(t)
    t = _INLINE_MARKER_RE.sub("", t)
    t = _collapse_punct(t)
    t = _DUP_PHRASE_RE.sub(r"\1", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


# ---------------------------------------------------------------- 可读性判定

def readability(text):
    """返回 (ok, reason)。ok=False 时 reason 为中文原因短语。"""
    t = (text or "").strip()
    if not t:
        return False, "模型没有返回内容"
    han = len(re.findall(r"[\u4e00-\u9fff]", t))
    if han < 20:
        return False, "模型返回的内容几乎是符号、读不出完整句子"
    total = len(t)
    if han / max(total, 1) < 0.35:
        return False, "模型返回的内容符号太多、不成句"
    if t:
        from collections import Counter
        c, n = Counter(t).most_common(1)[0]
        if n / total > 0.35 and c not in "，。、":
            return False, "模型返回的内容高度重复、无法理解"
    m = _DUP_PHRASE_RE.search(t)
    if m and len(m.group(0)) > max(30, total * 0.5):
        return False, "模型返回的内容为机械复读"
    return True, ""


# ---------------------------------------------------------------- 兜底描述

def _fmt_dur(sec):
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return ""
    if sec < 60:
        return "%.0f秒" % sec
    m = sec / 60.0
    return "%.1f分钟" % m if m < 60 else "%.1f小时" % (m / 60.0)


def fallback_desc(summary, reason=""):
    """用 CV 结构化数据生成通俗中文描述，替代不可读的模型输出。"""
    s = summary or {}
    att = s.get("attendance") or {}
    st = att.get("stats") or {}

    dur_sec = st.get("total_sec")
    if not dur_sec:
        try:
            if s.get("total_frames") and s.get("fps"):
                dur_sec = float(s["total_frames"]) / float(s["fps"])
        except (TypeError, ValueError, ZeroDivisionError):
            dur_sec = None
    durtxt = _fmt_dur(dur_sec)
    n = s.get("sampled_frames")

    bits = []
    if durtxt:
        bits.append("画面为一段时长约%s的监控录像" % durtxt)
    else:
        bits.append("画面为一段监控录像")
    if n:
        bits.append("（系统逐帧抽检了%s帧画面）" % n)
    bits.append("。")

    try:
        peak = int(s.get("person_peak"))
    except (TypeError, ValueError):
        peak = None

    busy_ratio = st.get("busy_ratio")
    busy_sec = st.get("busy_total_sec")
    busy_cnt = st.get("busy_period_count")

    if peak == 0:
        bits.append("整段录像里始终没有出现人员，属于无人经过的机位与时段，也没有人员进出。")
    elif peak is not None:
        seg = "画面中最多同时出现%d人" % peak
        pt = s.get("peak_frame_t_sec")
        if pt:
            seg += "，人数最多的时刻出现在全片第%s" % _fmt_dur(pt)
        bits.append(seg + "。")
        if isinstance(busy_ratio, (int, float)):
            pct = busy_ratio * 100
            if isinstance(busy_cnt, int) and busy_cnt:
                head = "有人出现过的时段共%d段" % busy_cnt
                if busy_sec:
                    head += "、合计约%s" % _fmt_dur(busy_sec)
                head += "，占全片约%.0f%%" % pct
            else:
                head = "有人出现过的时间占全片约%.0f%%" % pct
            if pct >= 95:
                bits.append(head + "，几乎全程都有人在场。")
            else:
                bits.append(head + "，其余时间为空场。")

    note = "（说明：以上内容由检测数据自动生成；画面语义描述未能生成"
    note += ("，原因：%s）" % reason.strip().rstrip("。）)").rstrip("）")) if reason else "）"
    return "".join(bits) + note


def humanize_desc(raw_text, summary=None):
    """报告渲染统一入口。返回 (text, ok, reason)。"""
    clean = normalize(raw_text)
    ok, reason = readability(clean)
    if not ok:
        return fallback_desc(summary, reason), False, reason
    return factcheck(clean, summary)


# ---------------------------------------------------------------- 事实校验（硬数据必须精准）

_TIME_RE = re.compile(
    r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"
    r"|\d{1,2}\s*[:：]\s*\d{2}(?:\s*[:：]\s*\d{2})?")
_DATE_RE = re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})")
_TIME_ASSERT_RE = re.compile(r"时间|时点|时段|时间戳|起止|时长|拍摄|录制|录像|实拍")
_RANGE_HINT_RE = re.compile(r"至|到|~|～|—|–|之间|起止")
_CLAUSE_SPLIT_RE = re.compile(r"[，,、；;]")
_NEG_STRONG_RE = re.compile(
    r"无人|没有人|无人员|没人员|未见人|未出现人|不曾出现"
    r"|空无一人|看不到人|空场|空无|静止"
    r"|(?:没有|未见|未出现|无)[^。，；]{0,6}?有人"
    r"|[：:]\s*(?:无|没有|没有人员|未见|未发现|不适用)[。！？]?\s*$")
_EMPTY_ASSERT_RE = re.compile(
    r"空无一人|不见人影|无人在场|没有人在场|根本无人|无人出现|没有人物"
    r"|没有(?:看到|见到|发现|出现)?[^。，；]{0,4}任何人物?"
    r"|没有人(?:在场|出现)"
    r"|(?:整个|整体)(?:场景|空间|画面|环境|走廊|房间|现场)[^。，；]{0,12}?(?:无人|没有人|空无)"
    r"|(?:画面|场景|空间|走廊|房间|现场|环境)[中内里]?(?:无人|没有人)(?![人员活动进出值守异常影])"
)
_PEOPLE_RE = re.compile(r"人|员|顾客|游客|客人|身影|群众")
_MOVE_RE = re.compile(r"进(?:出|入)画面|进出|走入|走向|走动|经过|徘徊|排队|办公(?!室)"
                      r"|作业|购物|闲逛|滞留|逗留|等待|交谈|浏览")
_TIME_TOL_SEC = 120


def _sentences(line):
    return [s.strip() for s in re.findall(r"[^。！？；!?;]+[。！？；!?;]?", line or "")
            if s.strip()]


def _parse_clock(tok):
    m = re.match(r"(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?$", (tok or "").strip())
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 23 or mi > 59 or s > 59:
        return None
    return h * 3600 + mi * 60 + s


def _parse_dt_str(s):
    if not s:
        return None
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return _dt.strptime(str(s).strip(), f)
        except ValueError:
            continue
    return None


def known_facts(summary):
    """从 count_summary.json 提炼硬数据（时间窗口 / 峰值人数），供校验与展示。"""
    s = summary or {}
    att = s.get("attendance") or {}
    st = att.get("stats") or {}
    f = {"start": _parse_dt_str(s.get("video_start")),
         "end": _parse_dt_str(s.get("video_end")),
         "peak": None,
         "peak_t": s.get("peak_frame_t_sec"),
         "dur_sec": st.get("total_sec")}
    try:
        f["peak"] = int(s.get("person_peak"))
    except (TypeError, ValueError):
        f["peak"] = None
    if not f["dur_sec"]:
        try:
            if s.get("total_frames") and s.get("fps"):
                f["dur_sec"] = float(s["total_frames"]) / float(s["fps"])
        except (TypeError, ValueError, ZeroDivisionError):
            f["dur_sec"] = None
    return f


def facts_brief(summary):
    f = known_facts(summary)
    bits = []
    if f["start"] and f["end"]:
        bits.append("录像时段 %s–%s" % (f["start"].strftime("%Y-%m-%d %H:%M:%S"),
                                       f["end"].strftime("%H:%M:%S")))
        d = _fmt_dur(f["dur_sec"])
        if d:
            bits.append("全程约%s" % d)
    if f["peak"] is not None:
        if f["peak"] == 0:
            bits.append("画面人数峰值 0 人（全程无人员出现）")
        else:
            seg = "画面人数峰值 %d 人" % f["peak"]
            try:
                if f["start"] and f["peak_t"]:
                    clk = f["start"] + _td(seconds=int(round(float(f["peak_t"]))))
                    seg += "（出现在 %s）" % clk.strftime("%H:%M:%S")
            except (TypeError, ValueError):
                pass
            bits.append(seg)
    if not bits:
        return ""
    return "数据基准（逐帧检测）：" + "；".join(bits) + "。"


def _asserts_people(sent):
    if _NEG_STRONG_RE.search(sent):
        return False
    if _PEOPLE_RE.search(sent):
        return True
    return bool(_MOVE_RE.search(sent))


def _clock_dt(sec, base):
    return base.replace(hour=sec // 3600, minute=(sec % 3600) // 60, second=sec % 60)


def _clause_time_conflict(clause, f):
    if not _TIME_ASSERT_RE.search(clause):
        return False
    toks = _TIME_RE.findall(clause)
    if not toks:
        return False
    for y, mo, d in _DATE_RE.findall(clause):
        try:
            if _dt(int(y), int(mo), int(d)).date() != f["start"].date():
                return True
        except ValueError:
            return True
    clocks = [_parse_clock(t) for t in toks]
    clocks = [c for c in clocks if c is not None]
    if not clocks:
        return False
    tol = _td(seconds=_TIME_TOL_SEC)
    lo, hi = f["start"] - tol, f["end"] + tol
    if len(clocks) >= 2 and _RANGE_HINT_RE.search(clause):
        first, last = _clock_dt(clocks[0], f["start"]), _clock_dt(clocks[-1], f["start"])
        near = lambda a, b: abs((a - b).total_seconds()) <= _TIME_TOL_SEC
        if not ((near(first, f["start"]) or near(first, f["end"])) and
                (near(last, f["end"]) or near(last, f["start"]))):
            return True
        return False
    for c in clocks:
        if not (lo <= _clock_dt(c, f["start"]) <= hi):
            return True
    return False


def _asserts_empty(sent):
    return bool(_EMPTY_ASSERT_RE.search(sent))


_COUNT_CLAIM_RE = re.compile(r"人数|\d+\s*个?\s*人|\d+\s*[-~～—–至到]\s*\d+\s*个?\s*人")


def _strip_count_clauses(sent):
    if not _COUNT_CLAIM_RE.search(sent):
        return sent, 0
    clauses = [c for c in _CLAUSE_SPLIT_RE.split(sent) if c.strip()]
    keep = [c for c in clauses if not _COUNT_CLAIM_RE.search(c)]
    if not keep:
        return "", len(clauses)
    return "，".join(c.strip() for c in keep).rstrip("，") + "。", len(clauses) - len(keep)


def _strip_time_clauses(sent, f):
    if not _TIME_ASSERT_RE.search(sent) or not _TIME_RE.search(sent):
        return sent, 0
    clauses = [c for c in _CLAUSE_SPLIT_RE.split(sent) if c.strip()]
    if len(clauses) <= 1:
        return ("", 1) if _clause_time_conflict(sent, f) else (sent, 0)
    keep = [c for c in clauses if not _clause_time_conflict(c, f)]
    n = len(clauses) - len(keep)
    if n == 0:
        return ("", 1) if _clause_time_conflict(sent, f) else (sent, 0)
    tail = sent.rstrip()[-1] if sent.rstrip()[-1:] in "。！？" else ""
    return "，".join(c.strip() for c in keep).rstrip("，") + tail, n


def factcheck(text, summary=None):
    """事实校验：剔除与逐帧检测（CV）相矛盾的硬数据断言（人数 / 时间）。"""
    f = known_facts(summary)
    txt = (text or "").strip()
    if not txt:
        return fallback_desc(summary, "模型没有返回内容"), False, "模型没有返回内容"
    has_window = bool(f["start"] and f["end"])
    if f["peak"] is None and not has_window:
        return txt, True, ""

    out_lines = []
    drop_people = drop_empty = drop_time = drop_count = 0
    for line in txt.split("\n"):
        if not line.strip():
            out_lines.append("")
            continue
        keep = []
        for sent in _sentences(line):
            if f["peak"] and f["peak"] > 0:
                sent, n = _strip_count_clauses(sent)
                drop_count += n
                if not sent.strip():
                    continue
            if f["peak"] == 0 and _asserts_people(sent):
                drop_people += 1
                continue
            if f["peak"] and _asserts_empty(sent):
                drop_empty += 1
                continue
            if has_window:
                sent, n = _strip_time_clauses(sent, f)
                drop_time += n
                if not sent.strip():
                    continue
            keep.append(sent)
        if keep:
            out_lines.append("".join(keep))
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip()

    reasons = []
    if drop_people:
        reasons.append("与检测结果（画面全程 0 人）矛盾的「有人」描述 %d 处" % drop_people)
    if drop_count:
        reasons.append("模型自述的人数口径 %d 处" % drop_count)
    if drop_empty:
        reasons.append("与检测结果（峰值 %d 人）矛盾的「空场」描述 %d 处" % (f["peak"], drop_empty))
    if drop_time:
        reasons.append("与监控记录时段不符的时间 %d 处" % drop_time)
    reason = "；".join(reasons)

    han = len(re.findall(r"[\u4e00-\u9fff]", body))
    if han < 20:
        r = reason or "剔除冲突内容后已无有效描述"
        return fallback_desc(summary, r), False, r
    return body, True, reason


# ---------------------------------------------------------------- 出报告前体检

_STYLE_RE = re.compile(r"<(style|script)\b.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY = (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
           ("&quot;", '"'), ("&#39;", "'"))

PROBLEM_PATTERNS = [
    (re.compile(r"\*\*"), "Markdown 粗体标记 **"),
    (re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S"), "Markdown 标题 #"),
    (re.compile(r"(?m)^\s*[-*+]\s+\S"), "Markdown 列表符号"),
    (re.compile(r"={3,}"), "分隔线 ======"),
    (re.compile(r"`"), "反引号代码标记"),
    (re.compile(r"([!！?？。，,、；;])\1{2,}"), "重复/堆砌标点"),
    (re.compile(r"([^\s，。；！？]{2,12}?)\1{4,}"), "机械复读"),
]


def visible_text(html_text):
    t = _STYLE_RE.sub(" ", html_text or "")
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</(p|div|li|tr|h[1-6]|section)>", "\n", t, flags=re.I)
    t = _TAG_RE.sub(" ", t)
    for a, b in _ENTITY:
        t = t.replace(a, b)
    return t


def audit_report_text(html_text, limit=6):
    vis = visible_text(html_text)
    problems = []
    for rx, name in PROBLEM_PATTERNS:
        for m in rx.finditer(vis):
            ctx = re.sub(r"\s+", " ", vis[max(0, m.start() - 28):m.end() + 28]).strip()
            problems.append((name, ctx))
            if len(problems) >= limit:
                return problems
    return problems


if __name__ == "__main__":
    import json
    import os
    import sys
    RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    bad = 0
    for d in sorted(os.listdir(RES)):
        p = os.path.join(RES, d)
        if not os.path.isdir(p):
            continue
        for fn in sorted(os.listdir(p)):
            if not fn.startswith("description"):
                continue
            raw = open(os.path.join(p, fn), encoding="utf-8", errors="replace").read()
            sm = None
            jp = os.path.join(p, "count_summary.json")
            if os.path.exists(jp):
                try:
                    sm = json.load(open(jp, encoding="utf-8"))
                except Exception:
                    sm = None
            txt, ok, reason = humanize_desc(raw, sm)
            flag = "OK " if ok else "FIX"
            if not ok:
                bad += 1
            print("[%s] %-16s %-22s %s" % (flag, d[:15], fn, (reason or "原样可用")))
            print("      -> " + txt.replace("\n", " / ")[:300])
    print("\n共 %d 个退化描述已可兜底" % bad)
    sys.exit(0)
