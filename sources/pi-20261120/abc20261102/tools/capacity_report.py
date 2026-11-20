"""P1-07 容量报告：四类占用只报数，不删任何数据。

四类（与审查清单 P1-07 的配额/保留期一一对应）：
  1. 原始环境库  —— 环境网关的滚动原始库（env_sensor.sqlite3 + WAL）
  2. 结构化结果  —— 批次库里的实验记录/环境快照/结束状态（实验证据，不自动删）
  3. 媒体上传    —— data/uploads 的素材（有总量配额）
  4. 临时导出    —— /tmp 下的 experiment-* 导出工作目录

用法（在 Pi 上用 /home/pi/abc20261102/.venv/bin/python 跑）：
  python tools/capacity_report.py
  python tools/capacity_report.py --json
  python tools/capacity_report.py --root /home/pi/abc20261102 \
      --env-db /home/pi/env_sensor_v1/env_sensor.sqlite3
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_UPLOAD_QUOTA_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_ENV_DB = "/home/pi/env_sensor_v1/env_sensor.sqlite3"

RETENTION = {
    "original_environment": "原始环境数据建议滚动保留 30 天：网关目前只在启动时 prune，应改为每日定时清理（保留质量标记与已导出批次的引用）。",
    "structured_results": "结构化结果是实验证据，建议永久保留；批次导出归档后可迁到冷存，删库前必须先确认 experiment.zip 已下载并校验。",
    "media_uploads": "媒体上传有总量配额，按配额滚动清理：已导出批次的上传素材可以删，未导出的批次不能删。",
    "temp_exports": "临时导出目录在下载结束时就该被删除；异常残留建议每日清理 24 小时前的 experiment-* 目录。",
}


def human_bytes(value):
    value = float(max(0, value))
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return "%d %s" % (value, unit) if unit == "B" else "%.1f %s" % (value, unit)
        value /= 1024
    return "%.1f TiB" % value


def sidecar_files(path):
    """SQLite 的 -wal / -shm 与主库一起算占用，否则会系统性低估。"""
    files = []
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.is_file():
            files.append(candidate)
    return files


def group_bytes(paths):
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def read_only_connection(path):
    try:
        return sqlite3.connect("file:%s?mode=ro" % Path(path).as_posix(), uri=True)
    except sqlite3.Error:
        return sqlite3.connect(str(path))


def query(connection, sql, args=()):
    try:
        return connection.execute(sql, args).fetchone()
    except sqlite3.Error:
        return None


def upload_quota_bytes(root):
    override = os.environ.get("ABC_UPLOAD_QUOTA_BYTES")
    if override:
        try:
            value = int(float(override))
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    try:
        value = int(json.loads((Path(root) / "config.json").read_text(encoding="utf-8")).get("upload_quota_bytes") or 0)
        if value > 0:
            return value
    except (OSError, ValueError, TypeError):
        pass
    return DEFAULT_UPLOAD_QUOTA_BYTES


def original_environment(env_db):
    path = Path(env_db)
    files = sidecar_files(path)
    detail = {"rows": None, "oldest_ms": None, "newest_ms": None, "days": None}
    if path.is_file():
        connection = read_only_connection(path)
        try:
            row = query(connection, "SELECT COUNT(*),MIN(received_at_ms),MAX(received_at_ms) FROM measurements")
            if row:
                detail.update(rows=row[0], oldest_ms=row[1], newest_ms=row[2])
                if row[1] and row[2]:
                    detail["days"] = round((row[2] - row[1]) / 86400000.0, 1)
        finally:
            connection.close()
    return {"label": "原始环境库", "path": str(path), "bytes": group_bytes(files),
            "files": [str(p) for p in files], "detail": detail,
            "retention": RETENTION["original_environment"]}


def structured_results(root):
    path = Path(root) / "data" / "collection_batches.sqlite3"
    files = sidecar_files(path)
    detail = {"batches": None, "records": None, "record_bytes": None,
              "environment_rows": None, "environment_bytes": None, "finished": None}
    if path.is_file():
        connection = read_only_connection(path)
        try:
            row = query(connection, "SELECT COUNT(*) FROM batches")
            if row:
                detail["batches"] = row[0]
            row = query(connection, "SELECT COUNT(*),COALESCE(SUM(LENGTH(json)),0) FROM experiment_records")
            if row:
                detail["records"], detail["record_bytes"] = row[0], row[1]
            row = query(connection, "SELECT COUNT(*),COALESCE(SUM(LENGTH(raw_json)),0) FROM batch_environment")
            if row:
                detail["environment_rows"], detail["environment_bytes"] = row[0], row[1]
            row = query(connection, "SELECT status,COUNT(*) FROM batches GROUP BY status ORDER BY 2 DESC")
            if row:
                detail["finished"] = {status: count for status, count in
                                      connection.execute("SELECT status,COUNT(*) FROM batches GROUP BY status")}
        finally:
            connection.close()
    return {"label": "结构化结果", "path": str(path), "bytes": group_bytes(files),
            "files": [str(p) for p in files], "detail": detail,
            "retention": RETENTION["structured_results"]}


def media_uploads(root, quota):
    folder = Path(root) / "data" / "uploads"
    files = []
    videos = metadata = partials = 0
    oldest = newest = None
    if folder.is_dir():
        for path in folder.iterdir():
            try:
                if not path.is_file():
                    continue
                files.append(path)
                if path.name.endswith(".part"):
                    partials += 1
                elif path.name.endswith(".video"):
                    videos += 1
                elif path.name.endswith(".json"):
                    metadata += 1
                stamp = path.stat().st_mtime
                oldest = stamp if oldest is None else min(oldest, stamp)
                newest = stamp if newest is None else max(newest, stamp)
            except OSError:
                continue
    used = group_bytes(files)
    return {"label": "媒体上传", "path": str(folder), "bytes": used,
            "files": [str(p) for p in sorted(files)][:50],
            "detail": {"videos": videos, "metadata": metadata, "partials": partials,
                       "oldest_mtime": oldest, "newest_mtime": newest},
            "quota_bytes": quota, "used_ratio": round(used / quota, 4) if quota else None,
            "retention": RETENTION["media_uploads"]}


def temp_exports(temp_dir):
    base = Path(temp_dir)
    dirs = sorted((p for p in base.glob("experiment-*") if p.is_dir()),
                  key=lambda p: p.stat().st_mtime) if base.is_dir() else []
    detail = {"directories": len(dirs), "oldest_mtime": None, "newest_mtime": None,
              "older_than_24h": 0, "names": [p.name for p in dirs][:50]}
    now = time.time()
    if dirs:
        detail["oldest_mtime"] = dirs[0].stat().st_mtime
        detail["newest_mtime"] = dirs[-1].stat().st_mtime
        detail["older_than_24h"] = sum(1 for p in dirs if now - p.stat().st_mtime > 86400)
    return {"label": "临时导出", "path": str(base), "bytes": group_bytes(dirs),
            "files": [str(p) for p in dirs][:50], "detail": detail,
            "retention": RETENTION["temp_exports"]}


def build_report(root, env_db, temp_dir, quota=None):
    root = Path(root)
    quota = quota or upload_quota_bytes(root)
    categories = {
        "original_environment": original_environment(env_db),
        "structured_results": structured_results(root),
        "media_uploads": media_uploads(root, quota),
        "temp_exports": temp_exports(temp_dir),
    }
    total = sum(item["bytes"] for item in categories.values())
    advice = [item["retention"] for item in categories.values()]
    if categories["media_uploads"]["used_ratio"] is not None and categories["media_uploads"]["used_ratio"] >= 0.8:
        advice.append("媒体上传已用 %.0f%% 配额，请先清理已导出批次的素材。"
                      % (categories["media_uploads"]["used_ratio"] * 100))
    try:
        disk = shutil.disk_usage(str(root))
    except OSError:
        disk = None
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "root": str(root), "env_db": str(env_db), "temp_dir": str(temp_dir),
        "categories": categories, "total_bytes": total,
        "disk": {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free},
        "advice": advice,
        "notes": ["本报告只统计，不删除任何数据。", "四类配额/保留期需人工确认后再清理。"],
    }
    return report


def print_human(report):
    print("容量报告  %s" % report["generated_at"])
    print("根目录 %s" % report["root"])
    disk = report["disk"]
    print("磁盘总 %s / 已用 %s / 可用 %s" % (human_bytes(disk["total_bytes"]),
                                             human_bytes(disk["used_bytes"]),
                                             human_bytes(disk["free_bytes"])))
    print("-" * 96)
    print("%-12s %12s %8s  %s" % ("类别", "占用", "文件数", "路径"))
    for key, item in report["categories"].items():
        extra = ""
        if key == "media_uploads":
            extra = "  配额 %s（已用 %.0f%%）" % (human_bytes(item["quota_bytes"]),
                                                 (item["used_ratio"] or 0) * 100)
        if key == "temp_exports":
            extra = "  目录 %d 个，>24h %d 个" % (item["detail"]["directories"],
                                                item["detail"]["older_than_24h"])
        print("%-12s %12s %8d  %s%s" % (item["label"], human_bytes(item["bytes"]),
                                        len(item["files"]), item["path"], extra))
    print("-" * 96)
    print("合计 %s" % human_bytes(report["total_bytes"]))
    for line in report["advice"]:
        print("· %s" % line)


def main(argv=None):
    parser = argparse.ArgumentParser(description="容量报告：四类占用只报数，不删数据")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                        help="abc20261102 根目录（默认脚本上两级）")
    parser.add_argument("--env-db", default=os.environ.get("ABC_ENV_DB", DEFAULT_ENV_DB),
                        help="环境网关原始库路径")
    parser.add_argument("--temp-dir", default=tempfile.gettempdir(), help="导出临时目录")
    parser.add_argument("--quota", type=int, default=None, help="覆盖媒体上传配额（字节）")
    parser.add_argument("--json", action="store_true", help="输出机读 JSON")
    args = parser.parse_args(argv)
    report = build_report(args.root, args.env_db, args.temp_dir, args.quota)
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
        print_human(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
