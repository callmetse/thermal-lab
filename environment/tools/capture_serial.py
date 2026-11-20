"""抓一段 ESP32 串口日志存盘，用于烧录后自检与留证。

用法：
    python tools/capture_serial.py --port COM6 --seconds 30 --out results/serial.log
    python tools/capture_serial.py --port COM6 --seconds 25 --reset --out results/boot.log

--reset 会用 DTR/RTS 拉一次复位（USB-Serial/JTAG 支持），这样能抓到从启动第一行
开始的日志——I²C 扫描和设备初始化都只在那一段打印。
"""
import argparse
import re
import sys
import time

import serial

ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def reset_board(port):
    """按 ESP32 的 EN/BOOT 电平序列复位，按下 RST 键的等价动作。"""
    port.setDTR(False)
    port.setRTS(True)
    time.sleep(0.1)
    port.setRTS(False)
    time.sleep(0.05)


def main(argv=None):
    parser = argparse.ArgumentParser(description='ESP32 串口日志抓取')
    parser.add_argument('--port', default='COM6')
    parser.add_argument('--baud', type=int, default=115200)
    parser.add_argument('--seconds', type=float, default=30.0)
    parser.add_argument('--reset', action='store_true', help='先复位再抓，能抓到启动日志')
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)

    deadline = time.time() + args.seconds
    lines = []
    with serial.Serial(args.port, args.baud, timeout=0.5) as port:
        # pyserial 打开端口时默认拉高 DTR/RTS，在 ESP32 上等于按住复位；
        # 先都放开，芯片才会正常跑程序并输出日志。
        port.setDTR(False)
        port.setRTS(False)
        time.sleep(0.05)
        port.reset_input_buffer()
        if args.reset:
            reset_board(port)
        print('capturing %s for %.0fs%s ...'
              % (args.port, args.seconds, ' (after reset)' if args.reset else ''),
              flush=True)
        while time.time() < deadline:
            raw = port.readline()
            if not raw:
                continue
            text = ANSI.sub('', raw.decode('utf-8', errors='replace')).rstrip('\r\n')
            print(text, flush=True)
            lines.append(text)

    with open(args.out, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines) + '\n')
    print('\nwrote %d lines to %s' % (len(lines), args.out), flush=True)
    return 0 if lines else 1


if __name__ == '__main__':
    sys.exit(main())
