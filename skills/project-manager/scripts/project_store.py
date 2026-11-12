#!/usr/bin/env python3
"""Keep one project record local, with atomic writes and stale-write checks."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None


def local_path(root, relative):
    """Reject absolute paths, parent traversal, and symlink escapes."""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("记录路径必须是项目内相对路径")
    target = root / path
    if not target.resolve().is_relative_to(root):
        raise ValueError("记录路径通过符号链接指向项目外")
    for part in [target, *target.parents]:
        if part != root and part.is_relative_to(root) and part.is_symlink():
            raise ValueError("管理文件路径不能包含符号链接")
    return target


def read_config(root):
    config_path = local_path(root, ".project-manager.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("不支持的项目配置")
    uuid.UUID(config["project_id"])
    record = local_path(root, config["record"])
    if record.suffix != ".md":
        raise ValueError("记录必须是 Markdown 文件")
    return config_path, config, record


def snapshot(root):
    _, config, record = read_config(root)
    data = record.read_bytes()
    return {
        "project_root": str(root),
        "project_id": config["project_id"],
        "record_path": str(record),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content": data.decode("utf-8"),
    }


def initial_record():
    reference = Path(__file__).resolve().parents[1] / "references/project-record.md"
    text = reference.read_text(encoding="utf-8")
    return text.split("```markdown\n", 1)[1].split("\n```", 1)[0] + "\n"


def initialize(root):
    config_path = local_path(root, ".project-manager.json")
    if config_path.exists():
        return snapshot(root)
    record = local_path(root, "docs/project-management/PROJECT.md")
    if record.exists():
        raise ValueError("已有管理记录但没有配置；请先确认归属，不覆盖原文件")
    content = initial_record()
    record.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "version": 1,
        "project_id": str(uuid.uuid4()),
        "record": str(record.relative_to(root)),
    }
    # Exclusive creation prevents two initializers from replacing each other.
    with config_path.open("x", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        with record.open("x", encoding="utf-8") as output:
            output.write(content)
    return snapshot(root)


def write_record(root, input_path, expected):
    replacement = input_path.read_bytes()
    if not replacement.decode("utf-8").strip():
        raise ValueError("不能用空内容覆盖项目记录")
    config_path, _, _ = read_config(root)
    # Writers lock the stable config inode; only the Markdown is replaced.
    with config_path.open("r", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = snapshot(root)
        if current["sha256"] != expected:
            raise ValueError("记录已被其他操作更新；重新读取并合并后再保存")
        _, _, record = read_config(root)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=record.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(replacement)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, record.stat().st_mode & 0o777)
            os.replace(temporary, record)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return snapshot(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "read", "write"):
        command = subparsers.add_parser(name)
        command.add_argument("--project-root", type=Path, required=True)
        if name == "write":
            command.add_argument("--input", type=Path, required=True)
            command.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    try:
        if fcntl is None:
            raise ValueError("该辅助脚本需要 macOS/Linux 的 fcntl；请按技能说明维护记录")
        root = args.project_root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("项目根路径必须是已存在的目录")
        if args.command == "init":
            result = initialize(root)
        elif args.command == "read":
            result = snapshot(root)
        else:
            result = write_record(root, args.input, args.expected_sha256)
        if args.command in {"init", "write"}:
            # A hub permission/format failure must not roll back saved project work.
            try:
                from task_hub import sync_project

                hub = sync_project(root)
                entry = next(item for item in hub["projects"] if item["root"] == str(root))
                if entry.get("error"):
                    print(f"警告：本项目已保存，但待办汇总未刷新：{entry['error']}", file=sys.stderr)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                print(f"警告：本项目已保存，但待办汇总未同步：{error}", file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
