#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频内容描述（替代 Mage-VL-4B 视觉前端的本机可用方案）
模型：mlx-community/Qwen2.5-VL-*-Instruct-4bit（经 mlx-vlm 在 Apple Silicon 运行）
能力：输入一段监控/通用视频，输出结构化中文描述（在场人数 / 人员出入 / 主要活动 / 异常 / 场景概述）。

用法：
  . vlm_venv/bin/activate
  python describe_qwen.py <视频路径> [--model ...] [--frames 16] [--mode frames|video] [--max-tokens 700]

说明：
  - 长视频（>几分钟）请用默认 --mode frames：均匀抽 N 帧作为多图输入，稳定且省显存。
  - --mode video 直接把整段视频交给 mlx-vlm 内部抽帧，适合短视频；长视频易抽帧过多导致显存吃紧。
  - 必须在后台运行（MLX 在前台沙箱会被 SIGTERM 137 杀掉）。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from desc_text import normalize, readability, factcheck, known_facts   # 与报告层共用同一套规范化/可读性/事实校验
except Exception:                                   # 兜底：模块缺失时不阻塞
    def normalize(t):
        return (t or "").strip()

    def readability(t):
        return True, ""

    def factcheck(t, summary=None):
        return (t or "").strip(), True, ""

    def known_facts(summary):
        return {}

AUDIT_PROMPT = (
    "你是一名门店/办公室监控视频稽核助手。下面是同一段监控视频按顺序抽取的若干帧画面"
    "（已特意包含该视频人数最多的时刻，以及各个\"有人时段\"的代表画面）。\n"
    "请只输出下面 5 行，每行以固定标签开头、冒号后接**一句简短的话**，整段合计不超过 100 字：\n"
    "场景概述：（这里是什么场所、整体环境大致什么样）\n"
    "人员出入：（有没有人进/出画面、从哪个方向；完全没人就写\"无\"）\n"
    "主要活动：（画面中的人大致在做什么；没人就写\"无\"）\n"
    "异常事件：（跌倒、长时间滞留、打斗、遗留物品、无人值守等；没有就写\"无\"）\n"
    "时间变化：（开头到结尾场景有无明显变化）\n"
    "硬性要求："
    "① **严禁写出任何人数**（如\"3人\"\"两三个人\"）与**任何具体时间**（年月日、钟点、时间戳）"
    "——人数与时间一律由检测数据给出，你只描述场所以及有没有人活动；"
    "② 只做客观描述，看不清就写\"看不清\"，不要编造；"
    "③ 只输出这 5 行，不要写标题、不要编号、不要 Markdown 符号（# * - ` = 等）、"
    "不要连续重复标点、不要括号里再套括号。"
)

# 第一次输出不可读时的加严重试提示
STRICT_RETRY_PROMPT = (
    "\n\n注意：你上一次的回答不合格（{reason}）。请重新回答，"
    "只输出以「场景概述：」「人员出入：」「主要活动：」「异常事件：」「时间变化：」"
    "开头的 5 行，每行一句短话、合计不超过 100 字；不得出现 Markdown 符号"
    "（# * - ` = ~ 等）、不得出现编号或项目符号、不得连续重复标点，直接输出这 5 行。"
)

RES_DIR = "/Users/<用户名>/WorkBuddy/视频稽核/magevl_deploy/results"


def load_summary(key):
    """读取 results/<key>/count_summary.json；读不到返回 None。"""
    if not key:
        return None
    p = os.path.join(RES_DIR, key, "count_summary.json")
    if not os.path.exists(p):
        return None
    try:
        import json as _json
        return _json.load(open(p, encoding="utf-8"))
    except Exception as e:
        print(f"[warn] 读 summary 失败: {e}", file=sys.stderr)
        return None


def facts_prompt(summary):
    """把逐帧检测（CV）得到的**硬数据**作为\"已知客观事实\"写进提示词。

    纪律：场景描述可以模糊，但**时间**与**在场人数**必须精准。3B 级 VLM 没有墙钟
    时间感知、又容易在空场录像里幻觉出\"有人\"，所以把权威事实直接喂给它，并要求
    它不得与之矛盾（生成后再由 desc_text.factcheck() 兜底剔除）。
    """
    f = known_facts(summary) if summary else {}
    if not f:
        return ""
    peak = f.get("peak")
    lines = ["\n\n【已知客观事实（必须以此为准，严禁与之矛盾）】"]
    if f.get("start") and f.get("end"):
        lines.append("· 本段录像的真实起止时间：%s 至 %s。你无需复述具体时间，"
                     "更不得给出任何与之不符的时间。"
                     % (f["start"].strftime("%Y-%m-%d %H:%M:%S"),
                        f["end"].strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("· 人数口径由检测数据给出，**你不需要、也禁止在描述里写出任何人数**。")
    if peak == 0:
        lines.append("· 逐帧检测结果：本段录像「全程无人出现，画面人数恒为 0 人」，"
                     "属于空场录像。因此你的描述里绝对不能出现任何人、人影、顾客，"
                     "也不能出现进出、走动、停留、排队、办公、购物等人员活动；"
                     "「人员出入」「主要活动」两行一律写\"无\"，只能描述场所环境与画面是否变化。")
    elif peak is not None:
        lines.append("· 逐帧检测到画面里是**有人的**，因此不要写成\"空无一人\"；"
                     "人数具体是几由检测数据给出，你只描述人在做什么即可。")
    return "\n".join(lines) if len(lines) > 1 else ""


def sample_frame_paths(video_path, n=8, max_edge=720, out_dir="/tmp/_vlm_frames", key=None):
    """抽帧策略：
    1) 必抽帧（仅在传 --key 时启用）：
       a) CV 峰值时刻 (peak_frame_t_sec) 前后 ±30s 各 1 帧（让 VLM 看到\"人最多\"的时刻）
       b) 每个 attendance.busy_periods 中段 1 帧（让 VLM 看到\"有人在玩\"的画面）
    2) 剩余位用均匀采样填，确保覆盖整段视频
    3) 总数固定到 n（去重后截断，优先保留必抽帧）
    不传 --key 时退化为纯均匀抽帧（向后兼容）。
    返回 (paths, metas)；metas 是 [(frame_idx, label)]，label='' 表示均匀补的帧。
    """
    import imageio.v3 as iio
    from PIL import Image
    os.makedirs(out_dir, exist_ok=True)
    paths, metas = [], []

    s = load_summary(key)

    total = int((s or {}).get("total_frames") or 0)
    fps_meta = float((s or {}).get("fps") or 0)
    if total <= 1:   # 退回 imageio 探测
        try:
            meta = iio.immeta(video_path, plugin="FFMPEG")
            total = int(meta.get("nframes", 0)) or 0
            fps_meta = fps_meta or float(meta.get("fps", 0)) or 0
        except Exception:
            pass

    # === 必抽帧索引（有 summary 时才计算）===
    must_idxs = {}  # frame_idx -> label
    if s is not None:
        try:
            fps_eff = s.get("fps") or fps_meta or 25
            peak_t = s.get("peak_frame_t_sec")
            if peak_t and fps_eff > 0 and total > 0:
                for delta, lbl in [(-30, "peak-30s"), (0, "peak"),
                                   (+30, "peak+30s")]:
                    fi = max(0, min(total - 1,
                                    int(round((peak_t + delta) * fps_eff))))
                    must_idxs[fi] = lbl
            att = s.get("attendance") or {}
            for p in att.get("busy_periods", []) or []:
                ts, te = p.get("t_start_sec"), p.get("t_end_sec")
                if ts is None or te is None or fps_eff <= 0:
                    continue
                mid = (ts + te) / 2.0
                fi = max(0, min(total - 1, int(round(mid * fps_eff))))
                clk = p.get("clock_start", "")
                must_idxs.setdefault(fi, f"busy@{clk}")

            if fps_eff > 0 and total > 0 and len(must_idxs) < n:
                busy = sorted(
                    [(p.get("t_start_sec"), p.get("t_end_sec"))
                     for p in (att.get("busy_periods") or [])
                     if p.get("t_start_sec") is not None and p.get("t_end_sec") is not None],
                    key=lambda x: x[0])
                dur = total / fps_eff
                gaps, cur = [], 0.0
                for bs, be in busy:
                    if bs - cur > 60:          # 超过 1 分钟的空场才算
                        gaps.append((cur, bs))
                    cur = max(cur, be)
                if dur - cur > 60:
                    gaps.append((cur, dur))
                gaps.sort(key=lambda g: g[1] - g[0], reverse=True)
                budget = min(2, n - len(must_idxs))
                for j, (gs, ge) in enumerate(gaps[:budget]):
                    mid = (gs + ge) / 2.0
                    fi = max(0, min(total - 1, int(round(mid * fps_eff))))
                    must_idxs.setdefault(fi, f"empty{j+1}")
        except Exception as e:
            print(f"[warn] 必抽帧计算失败，退化为均匀抽帧: {e}", file=sys.stderr)
    if total > 0:
        print(f"[info] 视频总帧={total} fps={fps_meta} 必抽帧={len(must_idxs)} 个",
              file=sys.stderr)

    # === 合并必抽 + 均匀 ===
    if total > 1:
        if must_idxs:
            remain_n = max(1, n - len(must_idxs))
            uniform = [int(round(i * (total - 1) / max(1, remain_n - 1)))
                       for i in range(remain_n)]
            uniform = sorted(set(uniform) - set(must_idxs.keys()))
            all_idxs = sorted(set(list(must_idxs.keys()) + uniform))
            if len(all_idxs) > n:
                must_set = set(must_idxs.keys())
                keep_must = [i for i in all_idxs if i in must_set]
                keep_uni = [i for i in all_idxs if i not in must_set][:max(0, n - len(keep_must))]
                all_idxs = sorted(keep_must + keep_uni)
        else:
            all_idxs = sorted(set(int(round(i * (total - 1) / max(1, n - 1)))
                                  for i in range(n)))
    else:
        all_idxs = list(range(n))

    # === 读帧写文件 ===
    for i, idx in enumerate(all_idxs):
        try:
            frame = iio.imread(video_path, index=idx, plugin="FFMPEG")
            im = Image.fromarray(frame)
            if max_edge and max(im.size) > max_edge:
                scale = max_edge / max(im.size)
                im = im.resize((int(im.size[0] * scale), int(im.size[1] * scale)))
            label = must_idxs.get(idx, "")
            tag = f"_{label}" if label else ""
            p = os.path.join(out_dir, f"frame_{i:03d}_{idx}{tag}.jpg")
            im.save(p, "JPEG", quality=85)
            paths.append(p)
            metas.append((idx, label))
        except Exception as e:
            print(f"[warn] 抽帧 {idx} 失败: {e}", file=sys.stderr)
    return paths, metas


def describe_frames(video_path, model_id, n_frames, max_tokens, max_edge=720, key=None,
                    max_retry=2, summary=None):
    """抽帧 → 生成 → 规范化 → 可读性校验 →（不合格则加严重试）→ 事实校验。
    返回 (text_to_write, ok, reason)：ok=True 时 text 是规范化后的自然语言；
    ok=False 时 text 是检测数据兜底说明（报告层仍会自动复核一次）。"""
    from mlx_vlm import load, generate
    from PIL import Image

    if summary is None:
        summary = load_summary(key)

    facts = facts_prompt(summary)
    if facts:
        print("[info] 已注入客观事实（时间/人数），模型不得与之矛盾", file=sys.stderr)

    print(f"[info] 抽帧（{n_frames} 张，最长边 {max_edge}px；"
          f"{'含峰值/在场时段必抽帧' if key else '纯均匀采样'}）...",
          file=sys.stderr)
    frames, metas = sample_frame_paths(video_path, n=n_frames, max_edge=max_edge, key=key)
    if not frames:
        raise RuntimeError("抽帧失败，无法继续")
    must = [m for m in metas if m[1]]
    if must:
        print(f"[info] 必抽帧: {must}", file=sys.stderr)
    print(f"[info] 加载模型 {model_id} ...", file=sys.stderr)
    model, processor = load(model_id)

    imgs = [Image.open(f) for f in frames]
    content = [{"type": "image", "image": im} for im in imgs]

    raw_last, norm_last, reason_last = "", "", ""
    for att in range(max_retry + 1):
        prompt = AUDIT_PROMPT + facts
        if att > 0:
            prompt += STRICT_RETRY_PROMPT.format(reason=reason_last or "输出不可读")
        messages = [{"role": "user", "content": content + [{"type": "text", "text": prompt}]}]
        formatted = processor.apply_chat_template(messages, add_generation_prompt=True)
        temp = 0.0 if att == 0 else 0.25 * att       # 首次贪心，重试时加一点随机性以跳出复读
        print(f"[info] 生成描述中（第 {att + 1} 次，temperature={temp}）...", file=sys.stderr)
        try:
            out = generate(model, processor, formatted, image=imgs,
                           max_tokens=max_tokens, temperature=temp, verbose=False)
        except Exception as e:
            print(f"[warn] 第 {att + 1} 次生成抛异常（{type(e).__name__}: {e}），继续重试", file=sys.stderr)
            reason_last = "生成过程异常中断"
            continue
        raw = out if isinstance(out, str) else getattr(out, "text", str(out))
        norm = normalize(raw)
        ok, reason = readability(norm)
        raw_last, norm_last, reason_last = raw, norm, reason
        if ok:
            txt, fok, freason = factcheck(norm, summary)
            if freason:
                print(f"[warn] 已按事实校验修订描述：{freason}", file=sys.stderr)
            if not fok:
                print("[warn] 描述与检测数据冲突严重，改用检测数据说明", file=sys.stderr)
            else:
                print(f"[info] 第 {att + 1} 次输出可用（{len(txt)} 字）", file=sys.stderr)
            return txt, fok, freason
        print(f"[warn] 第 {att + 1} 次输出不可读：{reason}", file=sys.stderr)

    print("[error] 多次重试后仍不可读，保留原始输出（报告将改用检测数据兜底）", file=sys.stderr)
    return raw_last, False, reason_last or "模型输出不可读"


def describe_video(video_path, model_id, max_tokens):
    from mlx_vlm import load, generate
    print(f"[info] 加载模型 {model_id} ...", file=sys.stderr)
    model, processor = load(model_id)
    messages = [{"role": "user", "content": [
        {"type": "video", "video": video_path},
        {"type": "text", "text": AUDIT_PROMPT},
    ]}]
    formatted = processor.apply_chat_template(messages, add_generation_prompt=True)
    print("[info] 生成描述中 ...", file=sys.stderr)
    out = generate(model, processor, formatted, video=video_path,
                   max_tokens=max_tokens, temperature=0.0, verbose=False)
    raw = out if isinstance(out, str) else getattr(out, "text", str(out))
    norm = normalize(raw)
    ok, reason = readability(norm)
    return (norm, True, "") if ok else (raw, False, reason)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--key", help="对应 results/<key>/ 子目录名；传了会启用峰值/在场时段必抽帧并自动写描述文件")
    ap.add_argument("--out", default="description.txt",
                    help="配合 --key 使用：写入 results/<key>/<out>（默认 description.txt；"
                         "Qwen3-VL-4B 请用 description_qwen3vl.txt）")
    ap.add_argument("--model", default="mlx-community/Qwen2.5-VL-3B-Instruct-4bit")
    ap.add_argument("--mode", choices=["frames", "video"], default="frames")
    ap.add_argument("--frames", type=int, default=4, help="提速档(2026-09-17 实测 vs 原8帧省17.5%墙钟，质量等价)")
    ap.add_argument("--resize", type=int, default=640, help="抽帧最长边像素，用于降低显存占用")
    ap.add_argument("--prompt", default=None, help="覆盖默认审计提示词（直接给自定义中文问题）")
    ap.add_argument("--max-tokens", type=int, default=150, help="提速档(2026-09-17 实测：150字足够覆盖场景描述，质量等价)")
    args = ap.parse_args()

    if not os.path.exists(args.video):
        print(f"视频不存在: {args.video}", file=sys.stderr)
        sys.exit(1)

    if args.prompt:
        globals()["AUDIT_PROMPT"] = args.prompt

    try:
        if args.mode == "video":
            text, ok, reason = describe_video(args.video, args.model, args.max_tokens)
        else:
            text, ok, reason = describe_frames(args.video, args.model, args.frames, args.max_tokens,
                                               args.resize, key=args.key)
    except Exception as e:
        print(f"[error] 主路径失败: {e}", file=sys.stderr)
        if args.mode != "frames":
            print("[info] 回退到抽帧图模式 ...", file=sys.stderr)
            text, ok, reason = describe_frames(args.video, args.model, args.frames, args.max_tokens,
                                               args.resize, key=args.key)
        else:
            raise

    print("\n========== 视频内容描述 ==========\n")
    print(text)
    if reason:
        print(f"\n[warn] 事实校验：{reason}", file=sys.stderr)
    if not ok:
        print(f"\n[warn] 描述不可读或与检测数据冲突（{reason}）——"
              f"已改用检测数据生成通俗说明；报告中同样以检测数据为准。", file=sys.stderr)

    if args.key:
        out_dir = (f"/Users/<用户名>/WorkBuddy/视频稽核/magevl_deploy/results/"
                   f"{args.key}")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, args.out)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"\n[info] 已写入: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
