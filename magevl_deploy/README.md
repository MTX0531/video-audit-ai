# magevl_deploy — 本地视频 AI 稽核部署（Apple Silicon / MLX）

在 Mac 上本地部署视觉大模型，用于读取监控视频、输出结构化内容描述（在场人数 / 出入 / 主要活动），配合 YOLO 人数统计与 HTML 报告生成。

## 环境

- Apple Silicon（已验证 M2 / M4），建议 ≥16GB 统一内存，推荐 32GB
- Python venv（`.venv/`）
- HuggingFace 直连不通时设置 `HF_ENDPOINT=https://hf-mirror.com` 走镜像

## 文件

| 文件 | 作用 |
|---|---|
| `run_server.sh` | 启动 VLM 推理服务（OpenAI 兼容接口，端口 8000） |
| `describe_video.py` | 给定视频路径，抽帧并调用服务，输出描述 |
| `count_people.py` | YOLO 人数统计（ByteTrack 跟踪，支持 ROI / min-area / no-track） |
| `describe_qwen.py` | Qwen-VL 视频内容描述（读 `count_summary.json` 抽帧） |
| `describe_attendance.py` | 从 counts.csv 推导在场时段（busy_periods） |
| `build_report.py` | 生成合并稽核报告 HTML（CV 统计 + 峰值时间线 + 证据帧 + VLM 描述） |
| `inspect_report.py` | 检查报告 HTML 结构完整性 |
| `make_sample.py` | 生成自测样本视频 |
| `run_vlm.py` / `repair_vlm.py` | VLM 服务管理与修复 |
| `verify_propguard.py` / `find_static_fp.py` | 误检防护（道具护栏 / 静态误检定位）工具 |

## 运行步骤

1. 启动 VLM 服务（首次自动从镜像下载约 3.7GB 权重，需耐心）：
   ```bash
   bash run_server.sh
   ```
2. 对视频跑 CV 人数统计：
   ```bash
   python count_people.py --weights models/yolov8m_weights/yolov8m.pt \
     --video <视频路径> --stride 142 --conf 0.20 --device mps \
     --nms-iou 0.5 --out-csv counts.csv --out-json count_summary.json
   ```
3. 对视频跑 VLM 描述：
   ```bash
   python describe_video.py --video <视频路径> --frames 12
   ```
4. 生成稽核报告：
   ```bash
   python build_report.py --keys <key1,key2,...>
   ```

## 自测（无需真实视频）

```bash
python make_sample.py                 # 生成 sample_clip.mp4
python describe_video.py --video sample_clip.mp4
```

## 参数说明

- `count_people.py` 关键参数：`--ignore-roi x1,y1,x2,y2`（排除误检区）、`--min-area`（过滤小目标）、`--no-track`（密集遮挡场景关闭跟踪）
- 每视频参数可按 key 在 `video_params.json` 覆盖（ignore_roi / min_area / no_track）
- 道具护栏（prop-guard）：针对海报/展板等静态道具被误识别的场景，见 `build_prop_ref.py`

## 已知限制（2026-09-10 实测）

- 公开 PyPI 的 `mlx-optiq 0.5.6` 不含视觉前端模块 `optiq.vlm`，图片/视频输入在公开包中不可用；文本推理完全可用
- `optiq serve` 对多模态请求存在服务端 bug（Cloud Boost 前缀检测崩溃），根因仍是缺 `optiq.vlm`
- 生产使用建议：换用 Qwen-VL（`describe_qwen.py`）作为 VLM 后端，或等官方补全 vlm 模块

## 输出结构

```
results/<key>/
├── counts.csv             # 逐采样帧人数
├── count_summary.json     # 汇总（窗口/峰值/attendance/prop_guard 等）
├── description.txt        # VLM 描述
└── count_frames/          # 峰值证据帧
results/稽核报告_<YYYYMMDD>.html   # 按日期归档的合并报告
```
