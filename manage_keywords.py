"""CLI 工具：直接管理 rules.json。"""

import json
import sys

from rules import find_rule, load_rules, normalize_rule, save_rules, upsert_rule


def main() -> int:
    """关键词管理 CLI 入口。"""
    if len(sys.argv) < 2:
        print("Usage: python manage_keywords.py [list|addgroup|delgroup|addkw|delkw|setkw] ...")
        return 1

    command = sys.argv[1]
    try:
        rules = [normalize_rule(r) for r in load_rules()]
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1

    if command == "list":
        print(json.dumps({"rules": rules}, ensure_ascii=False, indent=2))
        return 0

    if command == "addgroup":
        if len(sys.argv) < 3:
            print("Usage: python manage_keywords.py addgroup <group_id>")
            return 1
        group_id = int(sys.argv[2])
        if not find_rule(rules, group_id):
            rules = upsert_rule(rules, {"group_id": group_id, "targets": [], "keywords": [], "enabled": True, "use_regex": False})
            save_rules(rules)
        print(json.dumps({"rules": rules}, ensure_ascii=False, indent=2))
        return 0

    if command == "delgroup":
        if len(sys.argv) < 3:
            print("Usage: python manage_keywords.py delgroup <group_id>")
            return 1
        group_id = int(sys.argv[2])
        rules = [rule for rule in rules if rule["group_id"] != group_id]
        save_rules(rules)
        print(json.dumps({"rules": rules}, ensure_ascii=False, indent=2))
        return 0

    if command in {"addkw", "delkw", "setkw"}:
        if len(sys.argv) < 4:
            print(f"Usage: python manage_keywords.py {command} <group_id> <keyword>")
            return 1
        group_id = int(sys.argv[2])
        rule = find_rule(rules, group_id)
        if not rule:
            print(f"group {group_id} not found")
            return 1
        existing_words = {kw["word"] if isinstance(kw, dict) else kw for kw in rule.get("keywords", [])}
        if command == "addkw":
            keyword = sys.argv[3].strip()
            if keyword not in existing_words:
                rule.setdefault("keywords", []).append({"word": keyword, "enabled": True})
        elif command == "delkw":
            keyword = sys.argv[3].strip()
            rule["keywords"] = [kw for kw in rule.get("keywords", []) if (kw["word"] if isinstance(kw, dict) else kw) != keyword]
        else:
            rule["keywords"] = [{"word": item.strip(), "enabled": True} for item in sys.argv[3:] if item.strip()]
        rules = upsert_rule(rules, rule)
        save_rules(rules)
        print(json.dumps(rule, ensure_ascii=False, indent=2))
        return 0

    if command in {"addkwtarget", "delkwtarget"}:
        if len(sys.argv) < 5:
            print(f"Usage: python manage_keywords.py {command} <group_id> <kw_index> <qq>")
            return 1
        group_id = int(sys.argv[2])
        kw_idx = int(sys.argv[3])
        target_qq = int(sys.argv[4])
        rule = find_rule(rules, group_id)
        if not rule:
            print(f"group {group_id} not found")
            return 1
        if kw_idx < 1 or kw_idx > len(rule.get("keywords", [])):
            print(f"keyword index {kw_idx} out of range")
            return 1
        kw = rule["keywords"][kw_idx - 1]
        if command == "addkwtarget":
            targets = kw.get("targets", [])
            if target_qq not in targets:
                targets.append(target_qq)
                kw["targets"] = sorted(targets)
        else:
            targets = kw.get("targets", [])
            if target_qq in targets:
                targets.remove(target_qq)
                if targets:
                    kw["targets"] = targets
                else:
                    kw.pop("targets", None)
        rules = upsert_rule(rules, rule)
        save_rules(rules)
        print(json.dumps(rule, ensure_ascii=False, indent=2))
        return 0

    print(f"Unknown command: {command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
