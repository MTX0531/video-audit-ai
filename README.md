# 门店视频 AI 稽核系统

面向连锁门店的监控视频 AI 稽核方案与实现：对门店监控视频进行**人数统计（CV）+ 视频内容描述（VLM）+ 报告生成（Notion）** 的自动化流水线。

## 仓库结构

```
video-audit-ai/
├── 使用须知.md                  # 部署 / 配置 / 运行 / FAQ
├── docs/项目流程.md             # 业务流程与数据流
├── magevl_deploy/             # 本地视频 AI 稽核部署（Apple Silicon / MLX）
│   ├── count_people.py        # YOLO 人数统计（ByteTrack 跟踪）
│   ├── describe_qwen.py       # Qwen-VL 视频内容描述
│   ├── describe_video.py      # Mage-VL-4B 本地推理服务调用
│   ├── build_report.py        # 稽核报告生成
│   └── process_*.sh           # 批量处理脚本
└── notion_audit_pipeline/     # Notion 视频台账流水线
    ├── store_capture.py       # 门店侧 FTP 拉取 + 切片 + 上传
    ├── process_pending.py     # 待处理视频 → 本地稽核
    ├── notion_client.py       # Notion API 封装
    └── config.py              # 全局配置（敏感信息走环境变量）
```

## 流水线概览

```
门店摄像头 FTP 拉取视频切片
        ↓
上传视频到 Notion 台账库（视频本体 + 元数据）
        ↓
待处理队列 → 本地 AI 稽核（人数统计 / 内容描述）
        ↓
生成报告页（关联视频 + 报告 HTML）写回 Notion
```

## 配置说明

所有敏感信息（Notion Token、数据库 ID、摄像头凭据）**不硬编码**，一律通过环境变量或 `.env` 提供：

```bash
export NOTION_TOKEN=secret_xxx          # Notion integration secret
export NOTION_VIDEO_DB_ID=...           # 视频台账库 ID
export NOTION_REPORT_DB_ID=...          # 报告库 ID
```

`notion_audit_pipeline/cameras.json` 中 FTP 地址与密码为脱敏占位示例，部署时替换为真实录像机 FTP 配置。

## 依赖

- Python 3.10+，Apple Silicon（已验证 M2 / M4），建议 ≥16GB 统一内存
- YOLOv8m 权重、Mage-VL-4B（OptiQ 4-bit 量化）或 Qwen-VL 模型
- Notion 集成（integration）及对应数据库
