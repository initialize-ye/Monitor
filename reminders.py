import json
import os
import tempfile
from pathlib import Path

REMINDERS_FILE = Path(os.getenv("REMINDERS_FILE", "reminders.json"))


def load_reminders() -> list[dict]:
    if not REMINDERS_FILE.exists():
        return []
    try:
        raw = REMINDERS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Failed to read {REMINDERS_FILE}: {exc}") from exc

    reminders = data.get("reminders", [])
    if not isinstance(reminders, list):
        raise ValueError("reminders.json field 'reminders' must be a list")
    return reminders


def save_reminders(reminders: list[dict]) -> None:
    content = json.dumps({"reminders": reminders}, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=str(REMINDERS_FILE.parent),
        prefix=f".{REMINDERS_FILE.name}.tmp.",
    )
    try:
        os.write(fd, content.encode("utf-8"))
    except OSError:
        os.close(fd)
        os.unlink(tmp_path)
        raise
    else:
        os.close(fd)
    os.replace(tmp_path, REMINDERS_FILE)


def normalize_reminder(reminder: dict) -> dict:
    return {
        "id": int(reminder["id"]),
        "hour": int(reminder["hour"]),
        "minute": int(reminder["minute"]),
        "message": str(reminder["message"]).strip(),
        "targets": sorted({int(item) for item in reminder.get("targets", [])}),
        "enabled": bool(reminder.get("enabled", True)),
    }


def next_reminder_id(reminders: list[dict]) -> int:
    if not reminders:
        return 1
    return max(r["id"] for r in reminders) + 1
