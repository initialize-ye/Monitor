import json
import sys
from pathlib import Path


RULES_FILE = Path("rules.json")


def load_rules() -> list[dict]:
    if not RULES_FILE.exists():
        return []
    data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    rules = data.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("rules.json field 'rules' must be a list")
    return rules


def save_rules(rules: list[dict]) -> None:
    RULES_FILE.write_text(
        json.dumps({"rules": rules}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def find_rule(rules: list[dict], group_id: int) -> dict | None:
    for rule in rules:
        if int(rule["group_id"]) == group_id:
            return rule
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python manage_keywords.py [list|addgroup|delgroup|addkw|delkw|setkw] ...")
        return 1

    command = sys.argv[1]
    rules = load_rules()

    if command == "list":
        print(json.dumps({"rules": rules}, ensure_ascii=False, indent=2))
        return 0

    if command == "addgroup":
        group_id = int(sys.argv[2])
        if not find_rule(rules, group_id):
            rules.append({"group_id": group_id, "targets": [], "keywords": [], "enabled": True, "use_regex": False})
            save_rules(rules)
        print(json.dumps({"rules": rules}, ensure_ascii=False, indent=2))
        return 0

    if command == "delgroup":
        group_id = int(sys.argv[2])
        rules = [rule for rule in rules if int(rule["group_id"]) != group_id]
        save_rules(rules)
        print(json.dumps({"rules": rules}, ensure_ascii=False, indent=2))
        return 0

    if command in {"addkw", "delkw", "setkw"}:
        group_id = int(sys.argv[2])
        rule = find_rule(rules, group_id)
        if not rule:
            print(f"group {group_id} not found")
            return 1
        if command == "addkw":
            keyword = sys.argv[3].strip()
            if keyword not in rule["keywords"]:
                rule["keywords"].append(keyword)
        elif command == "delkw":
            keyword = sys.argv[3].strip()
            rule["keywords"] = [item for item in rule["keywords"] if item != keyword]
        else:
            rule["keywords"] = [item.strip() for item in sys.argv[3:] if item.strip()]
        save_rules(rules)
        print(json.dumps(rule, ensure_ascii=False, indent=2))
        return 0

    print(f"Unknown command: {command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
