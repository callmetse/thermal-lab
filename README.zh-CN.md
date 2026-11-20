# 室内热环境 · 边云协作实验平台（中文说明）

ESP32 传感节点 · 树莓派边缘端（环境网关 / 视觉 / 音频）· GPU 云端推断 · 控制云 Node-RED 控制台

英文版见 [README.md](README.md)。

## ⚠️ 项目状态

本仓库是**源码评审快照**，不是已发布系统。本地测试全部通过，但**尚未验证任何一台设备跑的就是这份代码**：2026-11-20 对树莓派做了只读比对，89 个可比文件里 26 个内容不同，设备上跑的是混合构建；GPU 与控制云当时连不上，未比对。部署脚本属于历史材料，使用前请逐条核对主机路径。

## 一、这个仓库是什么

把此前分散在多个目录的源码整理成一棵可评审的树，目的是让人在一个地方看清三件事：哪一份是基础版、哪一份是历史扩展版、哪一处是真缺口。

| 目录 | 职责 |
|---|---|
| `environment/` | 环境感知：HTTP 网关、事件校验、SQLite 存储、ESP32 固件、树莓派启动模板 |
| `vision/` | 视觉：边缘 worker、摄像头、预览与媒体传输、云端推断、Node-RED 流程、systemd 部署定义 |
| `acoustic/` | 音频：采集、唤醒词、会话控制、云端语音识别 worker、仪表盘、Supervisor 模板 |
| `revisions/vision-extension/` | 历史扩展版视觉快照（数据集 API、采集监控、知识检索） |
| `revisions/environment-longterm/` | 环境网关的长期记录变体 |
| `revisions/collaboration/` | 协作补丁包（**不是独立包**，测试需叠加基础视觉包） |
| `docs/` | 验证证据、网关说明、评审入口 |

先读这两份：

1. [docs/system-verification.md](docs/system-verification.md) — 测试命令与结果、四个同名 `worker.py` 的身份、音频缺口、已验证/未验证的边界。
2. [docs/environment-gateway.md](docs/environment-gateway.md) — 环境网关的行为与可复现示例。

## 二、同名文件不等于同一份代码（重点）

仓库里有 **4 个** `worker.py`，请按「路径 + SHA-256」识别，不要按文件名引用：

| 路径 | 行数 | SHA-256（前 16 位） | 身份 |
|---|---|---|---|
| `vision/lab/worker.py` | 805 | `fe10e1e8d14b1c5f` | 基础版视觉 |
| `revisions/vision-extension/lab/worker.py` | 1074 | `4b277770d4b58f13` | 历史扩展版视觉 |
| `revisions/collaboration/worker.py` | 761 | `026d06a8824b44e9` | 协作补丁包变体 |
| `acoustic/worker.py` | 425 | `c4bf1ab017a0d572` | 音频云端推理（另一子系统） |

摘要取**提交里的内容**（LF 换行），和 GitHub 上直接下载的文件一致；早先在 Windows 工作区算出的值不同，比对时以上表为准。Pi 现网的实际版本与这四份都不一样，见 [docs/system-verification.md](docs/system-verification.md) 的"Pi 现网比对"一节。

## 三、已知缺口（需要确认或补材料）

| 缺口 | 现状 | 影响 |
|---|---|---|
| `setup_capture.sh` | **在 Pi 上找到了**（`/home/pi/mm_audio_v1/setup_capture.sh`，2026-11-20 读取），但所有源码目录都没有 | 采集 profile 步骤只能从设备上取回，仓库还复现不了 |
| `gateway.sh` | 由 `acoustic/deploy.py` 部署时生成；生成的这份存在 Pi 上 | 网关程序无法只凭本仓库复现（依赖仓外 `console-auth.env` 与控制云 Caddy） |
| `utt_listener.py` 路径 | 源码在 `acoustic/tools/`，启动脚本从服务根目录启动 | 部署时须复制或改启动路径 |
| 模板 vs 生成器 | 归档模板与 Pi 上的 `supervisord.ini` 一致（都含 `audio-profile`）；`deploy.py` 生成的少这一项 | 生成器是旧版本，不要在已部署机器上重新生成覆盖 |
| `vision/tools/deploy_ui_d.py` | 未收录：原文含明文控制台口令与本机密钥库路径 | 防止把可用口令带进仓库 |
| 现网视觉与仓库不一致 | Pi 上跑的是混合构建（扩展模块 + `dataset-api` 服务），26 个文件内容不同 | 详见 `docs/system-verification.md`，不能把 `vision/` 当成现网源码 |

## 四、本地怎么跑（小白 3 分钟版）

需要 Python 3.12+（本机 3.13 验证，CI 用 3.12/3.13）。

```sh
python -m pip install -r requirements-dev.txt

python -m unittest discover -s environment/tests -v                        # 35
python -m unittest discover -s revisions/environment-longterm/tests -v      # 36
cd acoustic && python -m unittest discover -s tests -v && cd ..             # 67（5 跳过）
cp vision/config.example.json vision/config.json
cd vision && python -m unittest discover -s tests -v && cd ..               # 122
cd revisions/collaboration && PYTHONPATH=../../vision python -m unittest discover -s tests -v  # 71
```

合计 **331 个用例、5 个跳过、0 失败**。跳过的是需要 GPU 主机上 Sherpa-ONNX 模型的 5 个音频用例。

常见问题：

- 视觉测试报 `config.json` 不存在 → 先执行 `cp vision/config.example.json vision/config.json`（该文件不进版本库）。
- 协作包 import 报错 → 忘了 `PYTHONPATH=../../vision`。

## 五、怎么评审、怎么改

**方式 A：网页上直接改（不用装环境）**

1. 打开要改的文件 → 右上角铅笔图标（Edit this file）；
2. 改完在下方选 "Create a new branch for this commit and start a pull request"；
3. 点 Propose changes → Create pull request，写上理由。

**方式 B：提意见，不动代码**

- 文件页某一行左侧行号 → 出现 "+" → 直接写行内评论；
- 整块意见用 Issues → New issue，标题写清"文件 + 位置 + 问题"。

**方式 C：本地改并跑测试**

```sh
git clone https://github.com/callmetse/thermal-lab.git
cd thermal-lab
# 改完务必重跑第四节里对应的测试命令，再提交
git switch -c fix/<short-name>
git add -A && git commit -m "fix: 改了什么、为什么" && git push -u origin fix/<short-name>
```

**改完请顺手确认**：`docs/system-verification.md` 里的用例数如果变了要同步；不要提交模型权重、数据库、录像、`config.json`、`secrets.yaml`。

## 六、仓库约定

- **不提交**：模型权重（`.pt/.onnx/.bin`）、数据库（`.sqlite`）、录像/音频（`.mp4/.wav`）、本地配置（`config.json`、`secrets.yaml`）、密钥（`.pem/.key`）——见 `.gitignore`。
- **地址与凭据均为示例值**：`192.0.2.x` / `gpu.example.invalid`，端口统一为 `22`，账号与密钥名已替换。
- **模型校验**：`vision/MODEL_MANIFEST.json`、`acoustic/MODEL_MANIFEST.json` 记录权重文件名、大小与 SHA-256，部署前核对。
- **提交信息**用英文一句话说明改了什么（`feat:` / `fix:` / `docs:` / `test:` 前缀）。
- **证据写进文档**：新的部署事实写进 `docs/system-verification.md`，不要只留在聊天记录里。

## 七、许可证

BSD-2-Clause，见 [LICENSE](LICENSE)。
