# 门店监控视频 → Notion → AI 稽核报告 自动管线

把门店监控视频上传到 Notion 本体，由 AI 读取视频跑 CV/VLM 稽核，生成 HTML 报告后再存回 Notion。
端到端流程（贴合现有 integration 仅 Read + Insert 的权限约束）：

```
门店摄像头(FTP 拉取) ──[店内局域网]──▶ store_capture.py（FTP 拉取·切片·上传，无人值守）
        ──(0) store_capture.py ──▶ 连接录像机 FTP 拉取录像文件，ffmpeg -c copy 切片 → 上传 Notion 本体 + 建「待处理」台账页
        ──(1) upload_video.py ──▶ （手动补传也走这里，与自动 FTP 拉取共用 upload_clip）
        ──(2) 自动化定时轮询 ──▶ process_pending.py 取未处理视频 → 下载 → 跑稽核管线 → 出 HTML
        ──(3) 报告回写 ───────▶ 报告 HTML 上传 Notion + 在「报告库」新建关联报告页
```

> 关键约束（来自本项目的门店现状）：
> - **门店工作人员不会手动下载视频**——视频进入管线的唯一入口是门店侧的 `store_capture.py` FTP 拉取。
> - **AI 只在总部，门店只负责上传**——门店机只做"FTP 拉取 + 切片 + 上传 Notion"，**不跑任何 CV/VLM 推理**
>   （无 GPU / MLX / ultralytics 依赖，任意一台能装 ffmpeg + Python 的机器即可）。所有人数计数、在场时段、
>   语义描述等 AI 稽核都在总部的 `process_pending.py` 完成。

## 1. 准备 Notion 侧

复用现有 integration `workbuddy开发日志写入专用`（id `<NOTION_INTEGRATION_ID>`，
仅有 Read + Insert）。**新建两个专用数据库**并让该 integration 连接（分享给集成）：

### 视频台账库（VIDEO_DB_ID）
| 列名 | 类型 |
|---|---|
| 名称 | Title |
| 店名 | Rich text |
| 摄像头 | Rich text |
| 视频起止 | Rich text |
| 时长 | Rich text |
| 状态 | Select（待处理 / 处理中 / 已生成）|
| 视频文件 | Files & media |

### 报告库（REPORT_DB_ID）
| 列名 | 类型 |
|---|---|
| 名称 | Title |
| 关联视频 | Relation → 视频台账库 |
| 峰值人数 | Number |
| 有人累计(min) | Number |
| 状态 | Select（已生成 / 格式异常）|
| 报告文件 | Files & media |

> 注：因为 integration 没有 Update content，报告一律“新建页面”，不回改视频页；
> “已处理/未处理”靠「报告库里已有的关联视频」做差集判定。

## 2. 配置凭证

在 `notion_audit_pipeline/.env` 写入（不要提交进 git）：

```
NOTION_TOKEN=secret_xxxxxxxx
NOTION_VIDEO_DB_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
NOTION_REPORT_DB_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# 可选：VLM 用的 python 解释器（默认与 CV 同 venv）
VLM_PY=/Users/<用户名>/.workbuddy/binaries/python/envs/cv/bin/python
```

## 3. 用法

### 3.0 门店侧 FTP 拉取（推荐，无人值守）

在**门店局域网内**的一台常驻机器上部署 `store_capture.py` + `cameras.json`：

```bash
# 常驻守护（按 cameras.json 连接录像机 FTP，拉取+切片+上传）
python store_capture.py
# 只跑 1 小时（测试用）
python store_capture.py --once --run-seconds 3600
# 不调 Notion，仅验证“拉取→切片→监听→上传”逻辑（无需凭证，推荐先跑这个）
python store_capture.py --dry-run
# 只打印解析后的 FTP 拉取计划，不启动
python store_capture.py --list
```

`cameras.json` 关键字段（FTP 拉取示例）：

```jsonc
{
  "staging_dir": ".../_capture_staging",   // 切片临时存放，上传后移入 <camdir>/uploaded/
  "segment_seconds": 1800,                // 每段时长（秒），默认 30 分钟
  "ftp_poll_sec": 120,                    // FTP 轮询间隔（秒）
  "cameras": [
    {
      "store": "<门店1>",
      "camera": "<摄像头1>",
      "ftp_host": "192.168.2.10",         // 录像机/监控平台 FTP 地址
      "ftp_port": 21,                     // FTP 端口
      "ftp_user": "nvrftp",               // FTP 账号
      "ftp_password": "<FTP_PASSWORD>",   // FTP 密码（部署时替换）
      "ftp_remote_dir": "/record/通道1",  // 录像文件所在目录（按通道/日期组织）
      "segment_seconds": 1800
    }
  ]
}
```

> 录像机 FTP：`ftp://<user>:<pass>@<ip>:21<remote_dir>`
> （录像文件可以是设备端 FTP 推送产物，也可以是监控平台暴露的 FTP 共享目录；store_capture.py 周期性拉取新增文件）。

**切片方式**：
- **ffmpeg（首选）**：拉取完成后用 `ffmpeg -c copy` 免转码切片（默认 30 分钟/段），CPU 极低。门店机需装 ffmpeg
  （macOS `brew install ffmpeg`；Windows/Linux 下载静态包或 `apt install ffmpeg`）。
- **无 ffmpeg（兜底）**：整文件作为单片段直接上传，由上传前归一化兜底（无额外依赖，但可能传大文件）。

**健壮性**：FTP 断线自动重连；拉取幂等（远程文件切完后源文件移入 `raw/processed/`，不会重复拉取/切片）；
片段写完（大小稳定）才上传；已上传片段记入本地 `.capture_state.json`，守护进程重启不会重复上传；
切片片段自带起止时间戳，保证 HQ 侧能还原水印起止时间。

### 3.2 格式兜底机制（双保险：防总部 AI 无法解码）

**问题**：个别机型录像文件可能是 **H.265/HEVC**，或容器不是 mp4（如 `.dav`/裸 `.h264`/`.ts`/`.mkv`），
上传后总部 `cv2` 读不了首帧 → AI 稽核直接卡死/崩溃。为此在两条边界各加一道防线：

- **门店侧（入口兜底，推荐）**：`store_capture.py` 在上传前用 `ffprobe` 探测片段的容器+编码；
  - 已是 `mp4 + H.264` → 直接传；
  - 非标（非 mp4 / H.265 / 其他）→ 自动 `ffmpeg -c:v libx264 … -an` **转码成 H.264 mp4** 再传（仅小切片，开销低）；
  - 门店机**没装 ffmpeg** 时仅打印告警，转由总部侧兜底。
  - ✅ 因此**门店机务必安装 ffmpeg**，才能启用自动转码兜底（见 3.0 切片方式说明）。

- **总部侧（最后防线）**：`process_pending.py` 下载视频后、跑 CV 前，用 `cv2` 实测能否解出首帧
  （`video_can_decode()`，与后续 CV 同引擎，最准）。解不出则：
  - 在「报告库」新建一条 **`⚠格式异常·<标题>`** 记录页（status=`格式异常`，关联回原视频页）；
  - 用 Insert 权限完成，**不违反 Read+Insert 约束**；差集判定会把它视为“已处理”，该视频不再无限重试；
  - 用户在报告库可一眼看到哪条视频格式异常，去门店排查（多为录像文件 H.265 或私有封装，给门店机装 ffmpeg 转码，或联系设备侧改为 H.264 输出）。

> 注意：报告库的「状态」Select **必须包含「格式异常」这个选项**（见第 1 节表格），否则建异常页时会被 Notion 拒绝
> （代码已做降级：带 status 失败则退化成不含 status 的最小页，但仍能挡住重复轮询）。

### 3.1 上传一条视频（手动补传 / 也走 upload_clip）

上传一条视频（建台账）：
```bash
python upload_video.py /路径/<门店1>_<摄像头1>_20260902165844_20260902171635_device.mp4 \
  --store <门店1> --camera <摄像头1>
```

手动跑一次处理（也可交给自动化）：
```bash
python run_once.py            # 处理所有待处理
python process_pending.py --page-id <视频页id>   # 只处理某一条
python process_pending.py --limit 1              # 本次最多 1 条（先小范围验证）
```

## 4. 自动化（定时轮询）

用 WorkBuddy 自动化每 30 分钟跑一次 `run_once.py`（自包含 prompt）：
“执行门店视频 Notion 稽核管线：用 cv venv 运行
`/Users/<用户名>/WorkBuddy/视频稽核/notion_audit_pipeline/run_once.py`，
扫描 Notion 视频台账库里尚未生成报告的视频，下载、跑 CV/VLM 稽核、生成 HTML 并回写报告库。”

## 5. 每视频计数参数覆盖（可选）

误检多的机位在 `magevl_deploy/video_params.json` 按视频页 id 覆盖：
```json
{"<32hex页id>": {"ignore_roi":"350,80,700,400","min_area":15000,"no_track":true}}
```

## 6. 已知约束 / 注意

- **规模上限**：视频本体走 Notion 仅适合 ≤2-3 店小试点；超出需改回对象存储(COS/OSS/MinIO)+Notion 台账，
  否则撞 Notion API ~3 req/s 限速、单文件 5GB 上限。
- **视频文件 URL 是临时的**：process_pending 取到的 file.url 是预签名地址、会过期，
  已设计为“取到立即下载”，不要长时间缓存该 URL。
- **VLM 步骤**：describe_qwen.py 若依赖 MLX（本机前台沙箱会杀 import mlx 进程），
  自动化里可能失败——建议先在本地手动跑通，再放开自动轮询；或把 VLM 换成非 MLX 后端。
- **本地产物**：视频临时下载到 `AUDIT_TMP_DIR`（默认 /tmp/audit_pipe），结果落在 `magevl_deploy/results/<页id>/`。
