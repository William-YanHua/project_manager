#!/usr/bin/env python3
"""Collect project todos without modifying source projects; store reminder preferences."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from zoneinfo import ZoneInfo

from project_store import fcntl, local_path, snapshot


def hub_directory():
    override = os.environ.get("PROJECT_MANAGER_HUB_DIR")
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return Path(override).expanduser().resolve() if override else (home / "project-manager").expanduser().resolve()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def empty_hub():
    return {
        "version": 1,
        "projects": {},
        "reminder": {
            "time": None, "timezone": "Asia/Shanghai",
            "status": "not_configured", "automation_id": None,
        },
    }


@contextmanager
def hub_state(directory=None):
    """Serialize all hub mutations; never silently replace a corrupt registry."""
    if fcntl is None:
        raise ValueError("汇总脚本需要 macOS/Linux 的 fcntl")
    directory = (directory or hub_directory()).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    registry = local_path(directory, "hub.json")
    summary = local_path(directory, "TODOS.md")
    lock_path = local_path(directory, ".lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else empty_hub()
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("不支持的汇总配置，保留原文件")
        if not isinstance(state.get("projects"), dict) or not isinstance(state.get("reminder"), dict):
            raise ValueError("汇总配置损坏，保留原文件")
        yield state
        state["updated_at"] = now()
        atomic_write(registry, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        atomic_write(summary, render_summary(state))


def cells(line):
    """Split GFM table cells, honoring backslash-escaped pipes."""
    result, current = [], []
    escaped = False
    for char in line.strip().strip("|"):
        if escaped:
            current.append(char if char in ("|", "\\") else "\\" + char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            result.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    return result + ["".join(current).strip()]


def extract_tasks(content):
    """Only collect the explicit todo table, not examples or unapproved prose."""
    required = ["ID", "待办及完成条件", "状态", "优先级", "依赖", "结果依据或阻塞原因"]
    section, found, header, separator = False, False, None, False
    fence = None
    tasks, identifiers = [], set()
    for line in content.splitlines():
        stripped = line.strip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        if re.match(r"^#{1,2}\s", stripped):
            if stripped == "## 待办事项":
                if found:
                    raise ValueError("待办事项章节重复")
                section, found = True, True
            else:
                section = False
            continue
        if not section or not stripped.startswith("|"):
            continue
        row = cells(stripped)
        if header is None:
            if len(row) != len(required) or set(row) != set(required):
                raise ValueError("待办表头不符合模板，无法可靠汇总")
            header = row
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in row):
            if len(row) != len(header):
                raise ValueError("待办表格分隔列数不一致")
            separator = True
            continue
        if not separator or len(row) != len(header):
            raise ValueError("待办表格格式错误，检查未转义的竖线")
        record = dict(zip(header, row))
        task_id = record["ID"]
        if not task_id or task_id in identifiers:
            raise ValueError("待办 ID 为空或重复")
        identifiers.add(task_id)
        status = record["状态"]
        if status not in {"待办", "进行中", "受阻", "已完成", "已取消"}:
            raise ValueError(f"待办 {task_id} 状态无法识别：{status}")
        if status in {"已完成", "已取消"}:
            continue
        tasks.append({
            "id": task_id, "title": record["待办及完成条件"], "status": status,
            "priority": record["优先级"], "depends_on": record["依赖"],
            "note": record["结果依据或阻塞原因"],
        })
    if not found or header is None or not separator:
        raise ValueError("缺少完整待办表格；不能据此认定没有待办")
    return tasks


def project_entry(root, previous=None):
    """Read a source once; unavailable/invalid sources retain a marked stale cache."""
    timestamp = now()
    previous = previous or {}
    entry = {**previous, "root": str(root), "checked_at": timestamp}
    try:
        if root.resolve(strict=True) != root:
            raise ValueError("项目根路径已改变指向，请重新确认并登记")
        source = snapshot(root)
        if previous.get("project_id") and previous["project_id"] != source["project_id"]:
            raise ValueError("项目标识已改变，请重新确认归属")
        tasks = extract_tasks(source["content"])
        name = re.search(r"^- 项目名称[：:]\s*(.+)$", source["content"], re.MULTILINE)
        entry.update({
            "project_id": source["project_id"],
            "name": name.group(1).strip() if name and name.group(1).strip() != "待确认" else root.name,
            "record_path": source["record_path"], "source_sha256": source["sha256"],
            "tasks": tasks, "synced_at": timestamp, "error": None,
        })
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        entry["error"] = str(error)
        entry.setdefault("name", root.name)
        entry.setdefault("tasks", [])
    return entry


def report(state, directory=None):
    projects = list(state["projects"].values())
    valid = [project for project in projects if not project.get("error")]
    return {
        "summary_path": str((directory or hub_directory()) / "TODOS.md"),
        "project_count": len(projects),
        "incomplete_project_count": len(projects) - len(valid),
        "open_task_count": sum(len(project["tasks"]) for project in valid),
        "blocked_task_count": sum(task["status"] == "受阻" for project in valid for task in project["tasks"]),
        "projects": projects, "reminder": state["reminder"],
    }


def render_summary(state):
    stats = report(state)
    reminder = state["reminder"]
    labels = {"not_configured": "未设置，未启用", "pending": "时间已保存，等待调度确认",
              "active": "已启用", "paused": "已关闭"}
    display_zone = ZoneInfo(reminder["timezone"])
    updated = datetime.fromisoformat(state.get("updated_at", now())).astimezone(display_zone)
    lines = ["# 各项目待办汇总", "", f"汇总更新：{updated:%Y-%m-%d %H:%M:%S}（{reminder['timezone']}）",
             f"已登记 {stats['project_count']} 个项目；可读取记录中有 {stats['open_task_count']} 项未完成，"
             f"其中 {stats['blocked_task_count']} 项受阻。", "",
             f"每日提醒：{labels.get(reminder['status'], reminder['status'])}"
             + (f"；{reminder['time']}（{reminder['timezone']}）" if reminder.get('time') else ""), "",
             "本文件是自动生成的摘要，不直接编辑；进度以各项目原始记录为准。", ""]
    if stats["incomplete_project_count"]:
        lines += [f"注意：{stats['incomplete_project_count']} 个项目未能刷新，总数不完整。", ""]
    for entry in sorted(state["projects"].values(), key=lambda item: (item["name"], item["root"])):
        synced = entry.get("synced_at")
        synced_display = (datetime.fromisoformat(synced).astimezone(display_zone).strftime("%Y-%m-%d %H:%M:%S")
                          + f"（{reminder['timezone']}）") if synced else "无"
        lines += [f"## {entry['name']}", "", f"项目目录：{entry['root']}",
                  f"来源：{entry.get('record_path', '尚未成功读取')}",
                  f"最近成功同步：{synced_display}", ""]
        if entry.get("error"):
            lines += [f"未能刷新：{entry['error']}。以下为旧缓存，不计入当前总数。", ""]
        if not entry["tasks"]:
            lines += ["无缓存待办。" if entry.get("error") else "已读取的待办表中没有未完成项；不代表项目已经验收结束。", ""]
        for task in entry["tasks"]:
            lines += [f"- [{task['status']}] {task['id']}：{task['title']}",
                      f"  优先级：{task['priority']}；依赖：{task['depends_on']}。",
                      f"  说明：{task['note']}"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def sync_project(root, directory=None):
    root = root.resolve(strict=True)
    # Register only an enabled project. A missing config must not create a project.
    if not (root / ".project-manager.json").is_file():
        raise ValueError("该项目尚未启用 project-manager")
    with hub_state(directory) as state:
        key = str(root)
        state["projects"][key] = project_entry(root, state["projects"].get(key))
        result = report(state, directory)
    return result


def refresh(directory=None):
    with hub_state(directory) as state:
        for key, previous in list(state["projects"].items()):
            state["projects"][key] = project_entry(Path(key), previous)
        result = report(state, directory)
    return result


def set_reminder(time, zone=None, directory=None):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time):
        raise ValueError("提醒时间必须为 24 小时制 HH:MM")
    with hub_state(directory) as state:
        reminder = state["reminder"]
        zone = zone or reminder["timezone"]
        ZoneInfo(zone)
        if reminder["time"] != time or reminder["timezone"] != zone or reminder["status"] != "active":
            reminder.update(time=time, timezone=zone, status="pending")
        result = dict(reminder)
    return result


def bind_reminder(automation_id, status, expected_time, expected_zone, directory=None):
    """Record a successful scheduler response, never create a schedule ourselves."""
    if not automation_id.strip():
        raise ValueError("需要调度工具返回的 automation ID")
    with hub_state(directory) as state:
        reminder = state["reminder"]
        if not reminder["time"] or (reminder["time"], reminder["timezone"]) != (expected_time, expected_zone):
            raise ValueError("提醒设置已变化或未设置时间，请重新核对调度结果")
        reminder.update(automation_id=automation_id, status=status)
        result = dict(reminder)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync = subparsers.add_parser("sync")
    sync.add_argument("--project-root", type=Path, required=True)
    subparsers.add_parser("refresh")
    subparsers.add_parser("reminder-status")
    settings = subparsers.add_parser("reminder-set")
    settings.add_argument("--time", required=True)
    settings.add_argument("--timezone")
    bind = subparsers.add_parser("reminder-bind")
    bind.add_argument("--automation-id", required=True)
    bind.add_argument("--status", choices=["active", "paused"], required=True)
    bind.add_argument("--expected-time", required=True)
    bind.add_argument("--expected-timezone", required=True)
    args = parser.parse_args()
    try:
        if args.command == "sync":
            result = sync_project(args.project_root)
        elif args.command == "refresh":
            result = refresh()
        elif args.command == "reminder-set":
            result = set_reminder(args.time, args.timezone)
        elif args.command == "reminder-bind":
            result = bind_reminder(args.automation_id, args.status, args.expected_time, args.expected_timezone)
        else:
            with hub_state() as state:
                result = dict(state["reminder"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
