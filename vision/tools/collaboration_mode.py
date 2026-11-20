"""切换边云协作策略的运行模式：唯一入口，避免手改 config.json 出错。

放在工程的 tools/ 下，在 Pi 上运行：

    .venv/bin/python tools/collaboration_mode.py status
    .venv/bin/python tools/collaboration_mode.py shadow  --restart
    .venv/bin/python tools/collaboration_mode.py active  --budget-bps 4000000 --restart
    .venv/bin/python tools/collaboration_mode.py off     --restart

语义：
- `off`    ：策略完全不启用（不进入判定路径，行为与改动前逐字节一致）。
- `shadow` ：只计算、只记账，不改变发送行为；用于观察"如果启用会怎样"。
- `active` ：按判定执行；必须显式给 >= 1 Mbps 的预算，否则拒绝（防止"预算未标定就接管"）。

默认只改配置文件、不重启；加 `--restart` 才会重启 vision-edge 让改动生效。
写配置用"临时文件 + 原子替换"，重启失败时配置仍是完整的。
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODES = ("off", "shadow", "active")
DEFAULT_BUDGET_BPS = 4000000


def apply_mode(config, mode, budget_bps=None):
    """纯函数：返回 (新配置, 错误信息或 None)。不写盘、不重启、不改入参。"""
    config = json.loads(json.dumps(config))
    if mode not in MODES:
        return config, f"mode 必须是 {MODES} 之一"
    block = dict(config.get("collaboration") or {})
    if mode == "off":
        block["enabled"] = False
        block["mode"] = "shadow"
    elif mode == "shadow":
        block["enabled"] = True
        block["mode"] = "shadow"
    else:
        budget = budget_bps if budget_bps is not None else block.get("media_budget_bps") or 0
        if budget < 1_000_000:
            return config, "active 需要显式预算（>= 1 Mbps）；预算未标定时不能接管"
        block["media_budget_bps"] = int(budget)
        block["enabled"] = True
        block["mode"] = "active"
    if budget_bps is not None and mode != "active":
        block["media_budget_bps"] = int(budget_bps)
    block.setdefault("alpha", 0.8)
    block.setdefault("action_floor_fps", 8)
    block.setdefault("pacing", True)
    block.setdefault("quality_tiers", [])
    config["collaboration"] = block
    return config, None


def write_atomic(path, config):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def supervisor(*args):
    cmd = [str(ROOT / ".venv/bin/supervisorctl"), "-c", str(ROOT / "deploy/supervisord.ini"), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=MODES + ("status",))
    parser.add_argument("--budget-bps", type=int, default=None)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args(argv)

    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.mode == "status":
        print(json.dumps({"collaboration": config.get("collaboration"),
                          "config": str(args.config)}, ensure_ascii=False, indent=2))
        if (ROOT / ".venv/bin/supervisorctl").exists():
            print(supervisor("status", "vision-edge").stdout.strip() or "(vision-edge 未在 supervisor 中)")
        return 0

    config, error = apply_mode(config, args.mode, args.budget_bps)
    if error:
        print(f"拒绝执行：{error}", file=sys.stderr)
        return 2
    write_atomic(args.config, config)
    print(json.dumps({"applied": config["collaboration"]}, ensure_ascii=False, indent=2))

    if args.restart:
        result = supervisor("restart", "vision-edge")
        print(result.stdout.strip() or result.stderr.strip())
        if result.returncode != 0:
            print("重启失败：配置已写入但可能未生效，请检查 supervisor", file=sys.stderr)
            return 3
        print(supervisor("status", "vision-edge").stdout.strip())
    else:
        print("配置已写入；未加 --restart，改动要等 vision-edge 下次重启才生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
