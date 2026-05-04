import json
import sys

from rules import find_rule, load_rules, save_rules


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python manage_keywords.py [list|addgroup|delgroup|addkw|delkw|setkw] ...")
        return 1

    command = sys.argv[1]
    try:
        rules = load_rules()
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1

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
        rules = [rule for rule in rules if rule["group_id"] != group_id]
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
