# 室内热环境：边云协作的视觉—环境配对观测平台

室内热舒适研究要求把空间状态与人员状态记录在同一条时间轴上，并且能够在事后复查。本仓库记录
为此搭建的一套实验平台：一个传感器节点、一级边缘计算、一级 GPU 推理与一台控制主机，以及
2026 年 11 月 20 日从三台在役机器上回收的源码树。

本仓库是源码与证据的评审件。它收录代码、部署记录与验证材料；模型权重、数据库、录像与凭据不在
其中，公开的主机、账号与端口均为示例值。

## 一、现状

`vision/lab/` 是现网观测到的最新构建：`worker.py` 与 `llm.py` 取自 GPU 实例 2026 年 11 月 18 日
17:03 的写入版本，该包其余 26 个模块在树莓派与 GPU 上逐字节一致。

| 结论 | 证据 | 状态 |
|---|---|---|
| 七个常驻服务执行的即本仓库记录的代码 | 进程启动时间晚于各包最后写入时间，2026-11-20 22:16（JST） | 已核，`docs/runtime-verification.md` |
| 树莓派源码自抓取后未变 | 50 个文件按摘要比对 | 已核，`docs/runtime-verification.md` |
| 测试通过 | 330 个用例、12 个跳过、0 失败；CI 覆盖 Python 3.12 与 3.13 | 已核，第五节 |
| 三端运行同一构建 | 并非如此：现网存在五个 `worker.py` 构建与两个 `llm.py` 构建 | 差异已记录，第四节 |
| ESP32 可由本树复现 | 仅有 ESPHome 定义，镜像未在此编译或烧录 | 未完成 |

## 二、系统构成

平台采集实验室房间的环境、视觉与声学证据，供占用状态、热舒适与 HVAC 控制实验使用，分为四级。

**传感器节点。** ESP32-S3 板卡（`env01`）搭载 SHT4x 温湿度传感器与 SCD41 二氧化碳传感器，以
HTTP 上报测量值，附带序号、时钟状态与心跳，定义见 `environment/firmware/env01.yaml`。

**边缘层。** 树莓派校验并存储传感器事件（SQLite），采集画面与音频，执行端侧检测，并把会话媒体
转发至云端。环境网关监听 28984 端口，视觉工作进程在 28990 提供预览套接字，数据面运行于
28992，Caddy 在 28880 作为浏览器实验台的唯一局域网入口。

**GPU 层。** 租用的 GPU 实例承担姿态与动作推理，以及音频推理（语音识别、声音标注）。其访问端口
在每次实例启动时重新分配，经 SSH 隧道接入。

**控制主机。** 云端主机运行 MQTT broker、Node-RED 流程、Caddy 控制台与会话编排；树莓派经第二条
隧道以 28881 访问。

环境数据经 HTTP 传输，视觉与音频经 MQTT 传输。实验台对外提供两个界面：`/lab` 用于实验控制，
`/data` 用于数据检索，路由划分见 `docs/frontend-20261120.md`。

## 三、源码结构

```
thermal-lab/
├── environment/                  # 网关、事件契约、SQLite 存储、ESPHome 固件
│   ├── env_gateway.py            # HTTP 入口、校验、重放防护
│   ├── env_schema.py             # 事件契约：时钟、序号、量程、质量
│   ├── env_store.py              # 存储、裁剪、陈旧判定
│   ├── env_longterm.py           # 长期区间记录
│   ├── firmware/                 # env01.yaml、HTTP 上报头文件
│   └── tests/                    # 35 个用例
├── vision/
│   ├── lab/                      # 28 个模块：工作进程、相机、预览、传输、归档
│   ├── deploy/                   # systemd 单元、Node-RED 流程、compose、Caddy
│   ├── tools/                    # 实验运行器、导出器、置备脚本
│   ├── vendor/algorithm/         # 时序动作模型
│   ├── tests/                    # 121 个用例
│   ├── config.example.json
│   └── MODEL_MANIFEST.json
├── acoustic/                     # 采集、语音识别、声音标注；67 个用例
├── revisions/
│   ├── vision-base-20261120/     # 早期视觉构建，供对照
│   ├── vision-extension/         # 同包的扩展变体
│   ├── environment-longterm/     # 长期记录修订；36 个用例
│   └── collaboration/            # 协作叠加层；71 个用例
├── sources/                      # 2026-11-20 回收的源码树
│   ├── pi-20261120/              # 152 个文件，附 SHA256SUMS
│   ├── gpu-20261120/             # 41 个文件，附 SHA256SUMS
│   └── control-20261120/         # 69 个文件，附 SHA256SUMS
├── docs/                         # 验证、架构、部署与前端记录
│   └── figures/                  # 架构图与九张实验台截图
├── .github/workflows/tests.yml   # Linux 上并行运行五套测试，Python 3.12 与 3.13
├── requirements-dev.txt
├── CONTRIBUTING.md
├── LICENSE
├── README.md
└── README.zh-CN.md
```

本树是多组件源码树，不是单一可安装包；各组件自带依赖与测试。

## 四、构建对应关系

现网共有四份源码。其 `worker.py` 与 `llm.py` 的摘要区分彼此，其余 26 个视觉模块由树莓派与 GPU
共用。

| 源码集 | 位置 | `worker.py` | `llm.py` | 写入时间 |
|---|---|---|---|---|
| 本仓库主树 | `vision/lab/` | `19030c2a` | `19e20583` | 2026-11-18 17:03（GPU） |
| GPU 实例 | `sources/gpu-20261120/` | `19030c2a` | `19e20583` | 2026-11-18 17:03 |
| 树莓派 | `sources/pi-20261120/` | `c394fad5` | `48f5d2f2` | 2026-11-18 10:55 |
| 控制主机 | `sources/control-20261120/` | `9d48941e` | `48f5d2f2` | 更早的基础构建 |
| 早期 GPU 基线 | `revisions/vision-base-20261120/` | `fe10e1e8` | — | 2026-11-16 |

摘要按提交对象（LF 归一化）计算。逐文件比对（含前端）见 `docs/deployments-20261120.md` 与
`docs/frontend-20261120.md`。

## 五、验证

### 5.1 测试

| 套件 | 用例 | 结果 | 命令 |
|---|---|---|---|
| 环境网关 | 35 | 通过 | `python -m unittest discover -s environment/tests` |
| 环境长期记录修订 | 36 | 通过 | `python -m unittest discover -s revisions/environment-longterm/tests` |
| 声学 | 67 | 通过，5 跳过 | `cd acoustic && python -m unittest discover -s tests` |
| 视觉 | 121 | 通过，7 跳过 | `cp vision/config.example.json vision/config.json && cd vision && python -m unittest discover -s tests` |
| 协作叠加层 | 71 | 通过 | `cd revisions/collaboration && PYTHONPATH=../../vision python -m unittest discover -s tests` |
| 合计 | 330 | 0 失败，12 跳过 | CI：`.github/workflows/tests.yml` |

5 个声学用例跳过，是因为所需 Sherpa-ONNX 模型仅存在于 GPU 主机；7 个视觉用例针对早期工作进程的
夹具，跳过理由写在代码中。

### 5.2 运行态核验

2026 年 11 月 20 日 22:16（JST）对树莓派、GPU 与控制主机上的七个常驻服务作了核验：各进程启动时间
均晚于其执行包的最后写入时间，树莓派 50 个源文件与抓取摘要一致。28 个视觉模块中，23 个与归档逐
字节相同，5 个仅差脱敏替换。过程与原始输出见 `docs/runtime-verification.md`。

### 5.3 复现

```bash
git clone https://github.com/callmetse/thermal-lab.git
cd thermal-lab
python -m pip install -r requirements-dev.txt
```

需要 Python 3.12 或更高版本；测试可在 Linux、macOS 与 Windows 上运行，部署目标为 Linux。

## 六、文档

| 文档 | 内容 |
|---|---|
| [`docs/system-verification.md`](docs/system-verification.md) | 测试证据、构建身份、证据状态与待办核查项 |
| [`docs/runtime-verification.md`](docs/runtime-verification.md) | 三端进程级核验 |
| [`docs/deployments-20261120.md`](docs/deployments-20261120.md) | 四端部署矩阵与逐文件差异 |
| [`docs/frontend-20261120.md`](docs/frontend-20261120.md) | 实验台路由与前端三份拷贝 |
| [`docs/architecture.md`](docs/architecture.md) | A/B/C/D 四种采集方案与共享云侧管线 |
| [`docs/environment-gateway.md`](docs/environment-gateway.md) | 网关契约与可复现示例 |
| [`docs/gpu-endpoint.md`](docs/gpu-endpoint.md) | GPU 端口变更原因与找回方式 |
| [`docs/deployed-baseline.md`](docs/deployed-baseline.md) | 早期基线构建，存于 `revisions/` |
| [`docs/pi-diff-20261120.md`](docs/pi-diff-20261120.md) | 仓库与树莓派的逐文件比对 |
| [`docs/figures/bench/README.md`](docs/figures/bench/README.md) | 九张实验台截图与抓取条件 |

## 七、边界与待定事项

- **非部署镜像。** 本仓库是源码与证据记录，不含模型权重、数据库、录像、日志或运行态。
- **已脱敏。** 主机、账号、端口与密钥名替换为示例值，本地副本无法接入实验室设备。
- **GPU 端点不稳定。** 实例按次租用，SSH 端口每次启动重新分配；本文记录的端口仅对抓取当日有效，
  找回方式见 `docs/gpu-endpoint.md`。
- **前端存在分叉。** 实验台页面有三份：本仓库的本地修改（2026-11-16，51,882 B）、两台设备实际
  提供的页面（2026-11-03，35,206 B），以及控制主机上更新的 Node-RED 流程（2026-11-17，
  102,105 B）。以哪份为准尚未确定。
- **ESP32。** 仅有 ESPHome 定义，未在此编译或烧录，也没有端到端的传感器验证记录。
- **部署脚本。** 属历史材料，使用前需按现场重新核对主机路径。

## 八、协作方式

每项改动对应一个分支、一次提交与一个拉取请求；测试套件应保持通过；新的部署事实写入
`docs/system-verification.md`，不停留在通信记录中。约定详见 `CONTRIBUTING.md`。

## 九、许可证

BSD-2-Clause，见 [LICENSE](LICENSE)。

## 十、联系

维护者：[@callmetse](https://github.com/callmetse)。English version: [README.md](README.md)。
