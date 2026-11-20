"""在 ASCII 路径下编译/烧录 env01 固件。

为什么要这么绕：ESP-IDF 的 CMake/ninja 会把绝对路径写进编译参数，而当前工程的
目录名是中文（`D:\\thermal-lab-workspace\\...`）。xtensa-esp-elf-gcc 读 .rsp 时按 ANSI
码页解释这些路径，`sdkconfig.h: No such file or directory` 就是这个原因（源文件
都读得到，只有带中文的 -I 路径打不开）。把构建目录放到纯 ASCII 路径即可绕开，
源码仍以项目目录为准。

用法：
    python tools/build_firmware.py compile
    python tools/build_firmware.py flash --port COM6
    python tools/build_firmware.py logs  --port COM6 --seconds 30

默认构建目录 D:\\env01_build，可用 --build-dir 覆盖。
"""
import argparse
import filecmp
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FIRMWARE = HERE / 'firmware'
SOURCES = ('env01.yaml', 'env_publish.h', 'secrets.yaml')
DEFAULT_BUILD = Path(os.environ.get('ENV01_BUILD_DIR', r'D:\env01_build'))
ESPHOME = Path(os.environ.get(
    'ESPHOME_BIN', r'C:\Users\operator\env_sensor_tools\.venv\Scripts\esphome.exe'))
RESULTS = HERE / 'results'


def sync_sources(build_dir):
    build_dir.mkdir(parents=True, exist_ok=True)
    missing = []
    for name in SOURCES:
        source = FIRMWARE / name
        if not source.exists():
            missing.append(name)
            continue
        target = build_dir / name
        if target.exists() and filecmp.cmp(source, target, shallow=False):
            continue
        shutil.copy2(source, target)
        print('synced %s -> %s' % (source.name, target))
    if missing:
        raise SystemExit('缺少固件源文件：%s（secrets.yaml 需从 secrets.yaml.example 复制）'
                         % ', '.join(missing))


def run(build_dir, args, log_name, subs=None):
    RESULTS.mkdir(parents=True, exist_ok=True)
    log_path = RESULTS / log_name
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    env['NO_COLOR'] = '1'
    cli = [str(ESPHOME)]
    for key, value in (subs or {}).items():
        cli += ['-s', key, value]
    cli += args
    print('running: %s' % ' '.join(cli), flush=True)
    with open(log_path, 'w', encoding='utf-8', errors='replace') as log:
        proc = subprocess.run(cli, cwd=str(build_dir), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        text = proc.stdout.decode('utf-8', errors='replace')
        log.write(text)
        tail = '\n'.join(text.splitlines()[-25:])
        print(tail)
    print('\nfull log: %s (exit=%d)' % (log_path, proc.returncode))
    return proc.returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description='env01 固件构建/烧录')
    parser.add_argument('action', choices=('compile', 'flash', 'logs'))
    parser.add_argument('--build-dir', type=Path, default=DEFAULT_BUILD)
    parser.add_argument('--port', default='COM6')
    parser.add_argument('--seconds', type=float, default=30.0)
    parser.add_argument('--gateway-base', default=None,
                        help='覆盖固件里的网关地址，例如 http://192.0.2.12:28984')
    args = parser.parse_args(argv)

    if not ESPHOME.exists():
        raise SystemExit('找不到 esphome: %s' % ESPHOME)

    if args.action == 'logs':
        sys.path.insert(0, str(HERE / 'tools'))
        import capture_serial
        stamp = time.strftime('%Y%m%d_%H%M%S')
        out = RESULTS / ('serial_%s.log' % stamp)
        return capture_serial.main(['--port', args.port, '--seconds', str(args.seconds),
                                    '--out', str(out)])

    sync_sources(args.build_dir)
    subs = {'gateway_base': args.gateway_base} if args.gateway_base else None
    stamp = time.strftime('%Y%m%d_%H%M%S')
    if args.action == 'compile':
        return run(args.build_dir, ['compile', 'env01.yaml'],
                   'compile_%s.log' % stamp, subs)
    # 先编译再上传：upload 只发上一次编译产物，改了 --gateway-base 不重编译就会
    # 把旧地址的固件再刷一遍（曾因此白刷两次）。
    rc = run(args.build_dir, ['compile', 'env01.yaml'],
             'compile_%s.log' % stamp, subs)
    if rc != 0:
        print('编译失败，未上传')
        return rc
    return run(args.build_dir, ['upload', 'env01.yaml', '--device', args.port],
               'flash_%s.log' % stamp, subs)


if __name__ == '__main__':
    sys.exit(main())
