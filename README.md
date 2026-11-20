# 室内热环境 · 边云协作实验平台（源码评审包）

ESP32 传感节点 · 树莓派边缘端 · GPU 云端推断 · 控制云 Node-RED 控制台

本仓库把此前分散在多个目录里的源码整理成一棵**可评审的树**，目的是让导师能在一个地方看清三件事：哪一份是基础版、哪一份是历史扩展版、哪一处是真缺口；并直接给出修改意见。

> 现状：本地可运行、测试全过、凭据扫描无泄漏；**但尚未验证任何一台设备跑的就是这份代码**。请勿把本仓库当成"线上版本"。

| 项目 | 状态 |
|---|---|
| 本地测试（331 用例 / 5 跳过 / 0 失败） | ✅ 已执行（2026-11-20，干净克隆） |
| 凭据扫描（gitleaks 8.30.1） | ✅ 工作树 + 提交历史均无泄漏 |
| 设备端部署版本核对 | ⬜ 未做 |
| 硬件/云端运行时验证 | ⬜ 未做 |

---

## 一、目录导览：每个文件夹负责什么

| 目录 | 职责 | 说明 |
|---|---|---|
| `environment/` | 环境感知：HTTP 网关、事件校验、SQLite 存储、ESP32 固件、树莓派启动模板 | 网关有完整回归测试；`firmware/` 是 ESPHome 定义，**未编译、未烧录** |
| `vision/` | 视觉：边缘 worker、摄像头适配、预览与媒体传输、GPU 云端推断、Node-RED 流程、systemd 部署定义 | 基础版视觉源码（805 行 `worker.py`） |
| `acoustic/` | 音频：采集、唤醒词、会话控制、语音识别 worker、仪表盘、Supervisor 模板 | 云端 worker 需要 Sherpa-ONNX 与模型文件 |
| `revisions/vision-extension/` | 历史扩展版视觉快照 | 比基础版多数据集 API、采集监控、知识检索；与基础版有 26 处差异 |
| `revisions/environment-longterm/` | 环境网关的长期记录变体 | 与 `environment/` 不同的另一份网关实现，含长期存储测试 |
| `revisions/collaboration/` | 协作补丁包（worker + 工具 + 测试） | **不是独立包**：测试要叠加基础视觉包才能跑 |
| `docs/` | 评审文档：验证证据、网关说明、评审入口 | 建议先读 `docs/system-verification.md` |

三个"入口文档"：

1. [docs/system-verification.md](docs/system-verification.md) — **最该先看**：测试命令与结果、四个同名 `worker.py` 的身份表、音频缺口、哪些结论已验证/未验证。
2. [docs/README.md](docs/README.md) — 按角色的评审入口（哪块源码在哪、证据状态）。
3. [docs/environment-gateway.md](docs/environment-gateway.md) — 环境网关的行为与可复现示例。

---

## 二、同一文件名不等于同一份代码（重点）

仓库里有 **4 个** `worker.py`，文件名一样，但分属不同子系统、不同版本。请以"路径 + SHA-256"识别，不要按文件名引用：

| 路径 | 行数 | SHA-256（前 16 位） | 身份 |
|---|---|---|---|
| `vision/lab/worker.py` | 805 | `183c179a43ae4c04` | 基础版视觉（边缘/云端角色由参数选择） |
| `revisions/vision-extension/lab/worker.py` | 1074 | `12a305ada8d35fad` | 历史扩展版视觉快照 |
| `revisions/collaboration/worker.py` | 761 | `dfcf77d66dd643e2` | 协作补丁包内的变体 |
| `acoustic/worker.py` | 425 | `dc1f186087bc2930` | 音频云端推理（另一子系统） |

---

## 三、已知缺口（需要导师确认或补材料）

| 缺口 | 现状 | 影响 |
|---|---|---|
| `setup_capture.sh` | 所有源码目录均无此文件，只在归档 Supervisor 模板和台账里被引用 | 树莓派音频采集前的 profile 步骤无法复现；照归档模板启动会有一个程序起不来 |
| `gateway.sh` | 不是源码，由 `acoustic/deploy.py` 部署时生成；内部依赖 `/home/pi/.config/abc20261102/console-auth.env` 与 `/home/pi/abc20261102/runtime/viewer/caddy`（均在仓外） | 网关程序无法只凭本仓库复现 |
| `utt_listener.py` 路径 | 源码在 `acoustic/tools/`，但启动脚本从服务根目录 `/home/pi/mm_audio_v1` 启动它 | 部署时须复制或改启动路径，否则找不到文件 |
| 模板 vs 生成器 | 归档模板比 `deploy.py` 生成的多一个 `audio-profile` 程序 | 两份都是历史快照，无法确定设备上装的是哪一份 |
| `vision/tools/deploy_ui_d.py` | **未收录**：原文含明文控制台登录口令与本机密钥库路径 | 防止把可用口令带进仓库 |

---

## 四、本地怎么跑（小白 3 分钟版）

需要 Python 3.10+（本机用 3.13.16 验证）。在仓库根目录依次执行：

```sh
python -m unittest discover -s environment/tests -v                       # 35 个
python -m unittest discover -s revisions/environment-longterm/tests -v     # 36 个
cd acoustic && python -m unittest discover -s tests -v                     # 67 个（5 个跳过）
cd ../vision && cp config.example.json config.json && python -m unittest discover -s tests -v   # 122 个
cd ../revisions/collaboration && PYTHONPATH=../../vision python -m unittest discover -s tests -v # 71 个
```

合计 **331 个用例，5 个跳过，0 失败**。跳过的是需要 GPU 主机上 Sherpa-ONNX 模型的 5 个音频用例。

常见问题：

- 视觉测试报 `config.json` 不存在 → 先执行上面的 `cp config.example.json config.json`（该文件不进版本库）。
- 协作包测试 import 报错 → 忘了 `PYTHONPATH=../../vision`，它必须叠加基础视觉包。

---

## 五、怎么评审、怎么改（导师专用）

**方式 A：网页上直接改（不用装环境）**

1. 打开要改的文件 → 右上角铅笔图标（Edit this file）；
2. 改完在下方选 "Create a new branch for this commit and start a pull request"；
3. 点 Propose changes → Create pull request，写上理由即可。

适合改文档、补注释、改参数默认值。

**方式 B：提意见，不动代码**

- 文件页某一行左侧行号 → 出现 "+" → 直接写行内评论；
- 整块意见用 Issues → New issue，标题写清"文件 + 位置 + 问题"。

**方式 C：本地改并跑测试**

```sh
git clone --branch review/full-system-source https://github.com/callmetse/thermal-lab.git
cd thermal-lab
# 改完务必重跑第四节里对应的测试命令，再提交
git add -A && git commit -m "改了什么、为什么" && git push origin <你的分支>
```

**改完请顺手确认**：`docs/system-verification.md` 里的用例数如果变了，要同步更新；不要提交模型权重、数据库、录像、`config.json`、`secrets.yaml`。

---

## 六、仓库约定

- **不提交**：模型权重（`.pt/.onnx/.bin`）、数据库（`.sqlite`）、录像/音频（`.mp4/.wav`）、本地配置（`config.json`、`secrets.yaml`）、密钥（`.pem/.key`）——已写进 `.gitignore`。
- **地址与凭据均为示例值**：主机地址是 `192.0.2.x` / `gpu.example.invalid`，端口统一为 `22`，账号与密钥名已替换。复现时请按现场实际值改回。
- **模型校验**：`vision/MODEL_MANIFEST.json`、`acoustic/MODEL_MANIFEST.json` 记录了权重文件名、大小与 SHA-256，部署前请核对。
- **提交信息**用英文一句话说明改了什么；文档中英文混排，以中文说明为主。

---

## 七、English summary

This repository consolidates the thermal-lab edge/cloud system into one reviewable tree: the
environment gateway with ESP32 firmware, the base vision and audio pipelines, the Node-RED
control flows, and three archived revision snapshots under `revisions/`. The local suites pass
(331 tests, 5 skipped) and a credential scan reports no leaks, but no device is verified to run
this revision. Start with [docs/system-verification.md](docs/system-verification.md); deployment
gaps and evidence status are recorded there. Provision hosts, identities and weights locally;
the tree ships example addresses only.
