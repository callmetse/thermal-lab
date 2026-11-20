# 采集设备适配与实测（2026-11-09）

## 1. 设备画像（来自 `/proc/asound`，不是型号推测）

```
C-Media Electronics Inc. USB PnP Sound Device (08bb:2902, PCM2902 类)
USB 全速 12 Mbps -- 不是高速设备
Capture: S16_LE / 1 channel / 48000, 44100 Hz / ACCES ADAPTIVE / MONO
```

能被内核接受的采集格式只有一种组合（单声道 16 bit），采样率二选一。**没有 96/192 kHz，没有 24 bit，没有立体声采集** —— 这台设备的"优势"是 48 kHz 原生、单声道链路干净、走全速 USB 带宽占用极低（48 kHz×16 bit 只需 96 kB/s），而不是高规格。

## 2. 改了什么

| 文件 | 作用 |
|---|---|
| `audio_device.py`（新） | 从 `/proc/asound` 解析采集卡与能力，返回可直接喂给 `arecord`/`ffmpeg` 的设备对象，并用 1 秒实采自检参数 |
| `edge.py`（改） | 删除硬编码 `hw:CARD=Device,DEV=0`，改用 `resolve_capture()`；ffmpeg 输入侧按设备实际通道/采样率构造；头里 `hardware_sample_rate` 写真值；`main()` 预热一次探测 |
| `setup_capture.sh`（新） | 关闭 AGC、固定增益、采 3 秒验证电平 |
| `calibrate_gain.py`（新） | 用使用者的真实语音测增益，先两点探测出这个卡的实际 dB/步 |
| `supervisord.ini`（改） | 增加 `audio-profile` 程序，优先级 1，开机先把混音器配好再起 edge |

旧的 `edge.py` 备份在 `results/backup_audio_device_20261109-*/edge.py`。

## 3. 实测数据

### 3.1 采集管线的首块延迟（arecord → ffmpeg → 0.25 s 块读出）

| ALSA 参数 | 首块 | 块节拍 | 最大间隔 | 丢帧 |
|---|---|---|---|---|
| 默认（周期 125 ms / 缓冲 500 ms） | **388–393 ms** | 248 ms | 251 ms | 否 |
| 周期 25 ms / 缓冲 100 ms | 289–299 ms | 250 ms | 276–281 ms | 否 |
| **周期 10 ms / 缓冲 100 ms（采用）** | **270–275 ms** | 250 ms | 261–266 ms | 否 |
| 周期 5 ms / 缓冲 40 ms | 274–279 ms | 251 ms | 261–265 ms | 否 |
| 周期 10 ms + soxr 重采样 | 364–367 ms | 249 ms | 341 ms | 否 |

结论：**延迟由周期决定，不由缓冲决定**（缓冲只影响余量）。10 ms 周期把首块提前约 115 ms；再往下压到 5 ms 没有收益，soxr 反而多 90 ms 且抖动变大，故不采用 soxr。

### 3.2 90 秒浸泡 + 4 核满载 + 直写盘

| 配置 | 产出 | 块数 | overrun |
|---|---|---|---|
| 周期 10 ms / 缓冲 60 ms | 90.00/90 s | 360 | 0 |
| 周期 10 ms / 缓冲 100 ms | 90.00/90 s | 360 | 0 |
| 默认 125/500 | 90.00/90 s | 360 | 0 |

三种都零丢帧，小缓冲不引入 overrun —— 这是选 10 ms 周期的依据。

### 3.3 增益控件

- 简单控件名是 `Mic`（不是 contents 里显示的 `Mic Capture Volume`，直接拿全名 `sset` 会报 "Unable to find simple control"）。
- 实测 **1.500 dB/步**（0–16 → 0–24 dB），与标称 1.49 一致。
- 静音底噪：增益 0 → −61.0 dBFS，增益 16 → −37.0 dBFS，当前（12）约 −46.8 dBFS。
- **AGC 关闭**：开着时静音段被抬到 −35.0 dBFS（比关掉高约 10 dB），说话时又压回来 —— 输入电平不再是场景的函数，对 ASR/事件识别是干扰项。

## 4. 复跑方法

```bash
cd /home/pi/mm_audio_v1

# 看设备解析成什么
.venv/bin/python -c "import audio_device as a; print(a.resolve().describe())"

# 固定混音器（AGC 关、增益 12）；换增益用 MM_CAPTURE_GAIN=16
bash setup_capture.sh

# 用真实语音校准增益（说话时运行，默认只建议不改动）
.venv/bin/python calibrate_gain.py
.venv/bin/python calibrate_gain.py --apply

# 指定设备（例如双声卡时钉死某一张）
MM_AUDIO_DEVICE=hw:2,0 .venv/bin/python calibrate_gain.py
```

## 5. 尚未验证 / 风险

1. **端到端未验证**：MQTT 已能连上（24/24 包 publish accepted），但 worker 侧不在线，"转写回到 Pi" 这一半没有数据。按项目纪律，这不叫通过。
2. **增益仍待语音标定**：当前 12（18 dB）是保守值，静音底噪 −46.8 dBFS。要定值必须让人说一句，跑 `calibrate_gain.py`。
3. **USB 供电额度 600 mA**：本轮仍是充电宝供电，未握手 5V/5A PD。这张卡是低功耗全速设备，加进来后 5V 轨从 4.955 V 变到 4.945 V（噪声级），但**不要在同一条线上再加 SSD / 大功率 USB 设备**。
4. **换设备时**：`audio_device.py` 会按能力自适应；若插了两张同类卡，用 `MM_AUDIO_DEVICE` 钉死。
