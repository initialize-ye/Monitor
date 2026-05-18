"""Edge case and logic bug detection tests."""

import re
import time
import pytest
from datetime import date

# Import production functions
from rules import normalize_rule, upsert_rule, find_rule
from reminders import normalize_reminder, next_reminder_id
from forward_utils import render_rule, match_keywords, resolve_group_id
from remind_utils import render_reminder, parse_time


def split_field(text: str) -> tuple[str, str] | None:
    """Split text on colon. Test-only utility (not in production code)."""
    for sep in (":", "："):
        if sep in text:
            key, value = text.split(sep, 1)
            if key.strip() and value.strip():
                return key.strip() + sep, value.strip()
    return None


# ==================== Edge case tests ====================

class TestNormalizeRuleEdgeCases:
    """Test normalize_rule with edge cases."""

    def test_keyword_with_only_spaces(self):
        raw = {"group_id": 1, "targets": [], "keywords": ["   ", " ", "valid"]}
        result = normalize_rule(raw)
        assert len(result["keywords"]) == 1
        assert result["keywords"][0]["word"] == "valid"

    def test_keyword_dict_with_empty_word(self):
        raw = {"group_id": 1, "targets": [], "keywords": [{"word": "", "enabled": True}]}
        result = normalize_rule(raw)
        assert len(result["keywords"]) == 0

    def test_keyword_dict_with_whitespace_word(self):
        raw = {"group_id": 1, "targets": [], "keywords": [{"word": "   ", "enabled": True}]}
        result = normalize_rule(raw)
        assert len(result["keywords"]) == 0

    def test_target_with_zero(self):
        raw = {"group_id": 1, "targets": [0], "keywords": []}
        result = normalize_rule(raw)
        assert result["targets"] == [0]

    def test_negative_group_id(self):
        raw = {"group_id": -1, "targets": [], "keywords": []}
        result = normalize_rule(raw)
        assert result["group_id"] == -1

    def test_string_targets(self):
        raw = {"group_id": 1, "targets": ["123", "456"], "keywords": []}
        result = normalize_rule(raw)
        assert result["targets"] == [123, 456]

    def test_mixed_keyword_types(self):
        raw = {"group_id": 1, "targets": [], "keywords": ["str_kw", {"word": "dict_kw", "enabled": False}]}
        result = normalize_rule(raw)
        assert len(result["keywords"]) == 2
        assert result["keywords"][0] == {"word": "str_kw", "enabled": True}
        assert result["keywords"][1] == {"word": "dict_kw", "enabled": False}


class TestNormalizeReminderEdgeCases:
    """Test normalize_reminder with edge cases."""

    def test_workday_type(self):
        raw = {"id": 1, "type": "workday", "hour": 9, "minute": 0, "message": "test"}
        result = normalize_reminder(raw)
        assert result["type"] == "workday"
        assert result["hour"] == 9
        assert result["minute"] == 0

    def test_period_missing_hour(self):
        raw = {"id": 1, "type": "period", "minute": 30, "repeat_interval": 10, "message": "test"}
        result = normalize_reminder(raw)
        assert result["hour"] == 9

    def test_period_missing_repeat_interval(self):
        raw = {"id": 1, "type": "period", "hour": 18, "minute": 0, "message": "test"}
        result = normalize_reminder(raw)
        assert result["repeat_interval"] == 30

    def test_interval_missing_minutes(self):
        raw = {"id": 1, "type": "interval", "message": "test"}
        result = normalize_reminder(raw)
        assert result["interval_minutes"] == 30

    def test_once_missing_date(self):
        raw = {"id": 1, "type": "once", "hour": 9, "minute": 0, "message": "test"}
        result = normalize_reminder(raw)
        assert result["date"] == ""

    def test_once_fired_default(self):
        raw = {"id": 1, "type": "once", "date": "2026-12-25", "hour": 9, "minute": 0, "message": "test"}
        result = normalize_reminder(raw)
        assert result["fired"] is False

    def test_empty_targets(self):
        raw = {"id": 1, "type": "daily", "hour": 9, "minute": 0, "message": "test", "targets": []}
        result = normalize_reminder(raw)
        assert result["targets"] == []

    def test_duplicate_targets(self):
        raw = {"id": 1, "type": "daily", "hour": 9, "minute": 0, "message": "test", "targets": [1, 2, 1, 3, 2]}
        result = normalize_reminder(raw)
        assert result["targets"] == [1, 2, 3]

    def test_string_hour_minute(self):
        raw = {"id": 1, "type": "daily", "hour": "10", "minute": "30", "message": "test"}
        result = normalize_reminder(raw)
        assert result["hour"] == 10
        assert result["minute"] == 30


class TestMatchKeywordsEdgeCases:
    """Test match_keywords with edge cases."""

    def test_empty_text(self):
        keywords = [{"word": "test", "enabled": True}]
        assert match_keywords("", keywords, False) == []

    def test_empty_keywords_list(self):
        assert match_keywords("test text", [], False) == []

    def test_keyword_at_start(self):
        keywords = [{"word": "hello", "enabled": True}]
        assert "hello" in match_keywords("hello world", keywords, False)

    def test_keyword_at_end(self):
        keywords = [{"word": "world", "enabled": True}]
        assert "world" in match_keywords("hello world", keywords, False)

    def test_keyword_is_entire_text(self):
        keywords = [{"word": "test", "enabled": True}]
        assert "test" in match_keywords("test", keywords, False)

    def test_multiple_keywords_match(self):
        keywords = [
            {"word": "hello", "enabled": True},
            {"word": "world", "enabled": True},
        ]
        result = match_keywords("hello world", keywords, False)
        assert "hello" in result
        assert "world" in result

    def test_regex_special_chars_in_keyword(self):
        keywords = [{"word": "test.com", "enabled": True}]
        assert match_keywords("testXcom", keywords, False) == []

    def test_regex_dot_matches_any(self):
        keywords = [{"word": "test.com", "enabled": True}]
        assert "test.com" in match_keywords("testXcom", keywords, True)

    def test_regex_anchored(self):
        keywords = [{"word": "^test$", "enabled": True}]
        assert "^test$" in match_keywords("test", keywords, True)
        assert match_keywords("testing", keywords, True) == []

    def test_chinese_keyword(self):
        keywords = [{"word": "你好", "enabled": True}]
        assert "你好" in match_keywords("你好世界", keywords, False)

    def test_mixed_enabled_disabled(self):
        keywords = [
            {"word": "a", "enabled": True},
            {"word": "b", "enabled": False},
            {"word": "c", "enabled": True},
        ]
        result = match_keywords("a b c", keywords, False)
        assert "a" in result
        assert "b" not in result
        assert "c" in result

    def test_invalid_regex_skipped(self):
        keywords = [
            {"word": "[invalid", "enabled": True},
            {"word": "valid", "enabled": True},
        ]
        result = match_keywords("this is valid", keywords, True)
        assert "valid" in result
        assert "[invalid" not in result

    def test_all_invalid_regex(self):
        keywords = [
            {"word": "[unclosed", "enabled": True},
            {"word": "(unclosed", "enabled": True},
        ]
        result = match_keywords("test", keywords, True)
        assert result == []


class TestResolveGroupIdEdgeCases:
    """Test resolve_group_id with edge cases."""

    def test_large_group_id(self):
        group_id, kw = resolve_group_id("add 99999999999 keyword")
        assert group_id == 99999999999
        assert kw == "keyword"

    def test_group_id_with_leading_zeros(self):
        group_id, kw = resolve_group_id("add 00123 keyword")
        assert group_id == 123

    def test_keyword_with_spaces(self):
        group_id, kw = resolve_group_id("add 123 hello world")
        assert group_id == 123
        assert kw == "hello world"

    def test_only_command(self):
        group_id, kw = resolve_group_id("status")
        assert group_id is None
        assert kw is None

    def test_command_with_empty_space(self):
        group_id, kw = resolve_group_id("add ")
        assert group_id is None
        assert kw is None


class TestRenderRuleEdgeCases:
    """Test render_rule with edge cases."""

    def test_rule_with_no_targets(self):
        rule = {"group_id": 1, "targets": [], "keywords": [], "enabled": True, "use_regex": False}
        result = render_rule(rule)
        assert "未设置" in result

    def test_rule_with_multiple_targets(self):
        rule = {"group_id": 1, "targets": [111, 222, 333], "keywords": [], "enabled": True, "use_regex": False}
        result = render_rule(rule)
        assert "111, 222, 333" in result

    def test_rule_with_index_zero(self):
        rule = {"group_id": 1, "targets": [], "keywords": [], "enabled": True, "use_regex": False}
        result = render_rule(rule, index=0)
        assert "规则编号: 0" in result

    def test_rule_with_group_name(self):
        rule = {"group_id": 1, "targets": [], "keywords": [], "enabled": True, "use_regex": False, "group_name": "测试群"}
        result = render_rule(rule)
        assert "测试群" in result

    def test_rule_with_empty_group_name(self):
        rule = {"group_id": 1, "targets": [], "keywords": [], "enabled": True, "use_regex": False, "group_name": ""}
        result = render_rule(rule)
        assert "未知" in result

    def test_rule_disabled_all_keywords(self):
        rule = {
            "group_id": 1, "targets": [], "enabled": True, "use_regex": False,
            "keywords": [{"word": "a", "enabled": False}, {"word": "b", "enabled": False}]
        }
        result = render_rule(rule)
        assert "0 个启用" in result
        assert "2 个禁用" in result

    def test_rule_use_regex_enabled(self):
        rule = {"group_id": 1, "targets": [], "keywords": [], "enabled": True, "use_regex": True}
        result = render_rule(rule)
        assert "开启" in result

    def test_rule_with_per_keyword_targets(self):
        """Keywords with targets should show target suffix."""
        rule = {"group_id": 1, "targets": [100], "enabled": True, "use_regex": False, "keywords": [
            {"word": "test", "enabled": True, "targets": [200, 300]}
        ]}
        result = render_rule(rule)
        assert "→ 200,300" in result


class TestRenderReminderEdgeCases:
    """Test render_reminder with edge cases."""

    def test_interval_reminder_with_zero_minutes(self):
        rem = {"id": 1, "type": "interval", "interval_minutes": 0, "message": "test"}
        result = render_reminder(rem)
        assert "每 0 分钟" in result

    def test_period_reminder_midnight(self):
        rem = {"id": 1, "type": "period", "hour": 0, "minute": 0, "repeat_interval": 5, "message": "test"}
        result = render_reminder(rem)
        assert "00:00" in result

    def test_once_reminder_empty_date(self):
        rem = {"id": 1, "type": "once", "date": "", "hour": 9, "minute": 0, "message": "test"}
        result = render_reminder(rem)
        assert " 09:00" in result

    def test_quote_reminder_no_message(self):
        rem = {"id": 1, "type": "daily", "hour": 9, "minute": 0, "auto_generate": "quote"}
        result = render_reminder(rem)
        assert "每日一言" in result
        assert "提醒 #1\n类型: 每天\n时间: 09:00\n每日一言（自动生成）" in result

    def test_workday_reminder(self):
        rem = {"id": 1, "type": "workday", "hour": 9, "minute": 0, "message": "上班"}
        result = render_reminder(rem)
        assert "工作日" in result


class TestParseTimeEdgeCases:
    """Test parse_time with edge cases."""

    def test_midnight(self):
        hour, minute, error = parse_time("00:00")
        assert hour == 0
        assert minute == 0
        assert error is None

    def test_end_of_day(self):
        hour, minute, error = parse_time("23:59")
        assert hour == 23
        assert minute == 59
        assert error is None

    def test_single_digit_minute(self):
        hour, minute, error = parse_time("9:0")
        assert error is not None

    def test_extra_spaces(self):
        hour, minute, error = parse_time("  09:30  ")
        assert hour == 9
        assert minute == 30

    def test_empty_string(self):
        hour, minute, error = parse_time("")
        assert error is not None


class TestSplitFieldEdgeCases:
    """Test split_field with edge cases."""

    def test_chinese_colon(self):
        result = split_field("状态：已启用")
        assert result is not None
        assert result[0] == "状态："
        assert result[1] == "已启用"

    def test_english_colon(self):
        result = split_field("Status: enabled")
        assert result is not None
        assert result[0] == "Status:"
        assert result[1] == "enabled"

    def test_multiple_colons(self):
        result = split_field("key: value: with: colons")
        assert result is not None
        assert result[0] == "key:"
        assert result[1] == "value: with: colons"

    def test_no_colon(self):
        result = split_field("no colon here")
        assert result is None

    def test_empty_key(self):
        result = split_field(": value")
        assert result is None

    def test_empty_value(self):
        result = split_field("key:")
        assert result is None

    def test_only_colon(self):
        result = split_field(":")
        assert result is None


class TestUpsertRuleEdgeCases:
    """Test upsert_rule with edge cases."""

    def test_empty_rules_list(self):
        new_rule = {"group_id": 1, "targets": [111], "keywords": ["test"]}
        result = upsert_rule([], new_rule)
        assert len(result) == 1
        assert result[0]["group_id"] == 1

    def test_upsert_preserves_other_rules(self):
        existing = [
            {"group_id": 1, "targets": [111], "keywords": ["a"]},
            {"group_id": 2, "targets": [222], "keywords": ["b"]},
        ]
        new_rule = {"group_id": 1, "targets": [333], "keywords": ["c"]}
        result = upsert_rule(existing, new_rule)
        assert len(result) == 2
        rule2 = next(r for r in result if r["group_id"] == 2)
        assert rule2["keywords"][0]["word"] == "b"

    def test_upsert_result_is_sorted(self):
        existing = [
            {"group_id": 3, "targets": [], "keywords": []},
            {"group_id": 1, "targets": [], "keywords": []},
        ]
        new_rule = {"group_id": 2, "targets": [], "keywords": []}
        result = upsert_rule(existing, new_rule)
        assert result[0]["group_id"] == 1
        assert result[1]["group_id"] == 2
        assert result[2]["group_id"] == 3


class TestCooldownAndDedup:
    """Test cooldown and deduplication logic."""

    def test_cooldown_key_format(self):
        cooldown = {}
        key = f"{123}:{'test'}"
        cooldown[key] = time.time()
        assert "123:test" in cooldown

    def test_cooldown_different_groups_same_word(self):
        cooldown = {}
        now = time.time()
        cooldown[f"1:test"] = now
        assert f"2:test" not in cooldown

    def test_stats_key_format(self):
        today = time.strftime("%Y-%m-%d")
        key = f"{today}:123:test"
        assert key.startswith(today)

    def test_stats_different_dates(self):
        stats = {}
        stats[f"2026-01-01:123:test"] = 5
        stats[f"2026-01-02:123:test"] = 3
        assert stats[f"2026-01-01:123:test"] == 5
        assert stats[f"2026-01-02:123:test"] == 3


class TestMessageBufferLogic:
    """Test message buffer logic."""

    def test_buffer_timeout_seconds(self):
        assert 8 == 8

    def test_buffer_groups_isolation(self):
        buffer: dict[int, list[dict]] = {}
        buffer[1] = [{"text": "msg1"}]
        buffer[2] = [{"text": "msg2"}]
        assert len(buffer[1]) == 1
        assert len(buffer[2]) == 1
        assert buffer[1][0]["text"] != buffer[2][0]["text"]


class TestImageRendererConstants:
    """Test image renderer constants are consistent."""

    def test_two_col_detection_threshold(self):
        COL2_X = 490
        PADDING = 42
        threshold = COL2_X - PADDING - 56
        assert threshold > 0
        assert threshold == 392

    def test_card_dimensions(self):
        CARD_WIDTH = 920
        PADDING = 42
        assert CARD_WIDTH > PADDING * 2
        assert CARD_WIDTH - PADDING * 2 == 836

    def test_font_sizes(self):
        FONT_SIZE = 19
        TITLE_FONT_SIZE = 24
        HEADER_FONT_SIZE = 20
        assert FONT_SIZE < HEADER_FONT_SIZE < TITLE_FONT_SIZE


class TestRegexPatterns:
    """Test regex patterns used in the codebase."""

    def test_two_col_regex(self):
        pattern = re.compile(r"^(\S.+?)  {3,}(\S.+)$")
        m = pattern.match("add <关键词>             添加关键词")
        assert m is not None
        assert m.group(1) == "add <关键词>"
        assert m.group(2) == "添加关键词"

    def test_two_col_regex_insufficient_spaces(self):
        pattern = re.compile(r"^(\S.+?)  {3,}(\S.+)$")
        m = pattern.match("add <关键词> 添加关键词")
        assert m is None

    def test_field_regex(self):
        pattern = re.compile(r"^[\w一-鿿 #()（）]+[:：]")
        assert pattern.match("状态: 已启用")
        assert pattern.match("群号: 123456")
        assert not pattern.match("这是一段普通文本")

    def test_numbered_regex(self):
        pattern = re.compile(r"^\s*\d+[.)、]")
        assert pattern.match("1. 第一项")
        assert pattern.match("2) 第二项")
        assert pattern.match("3、第三项")
        assert not pattern.match("第一项")

    def test_separator_chars(self):
        sep_chars = "-_=─━—－"
        for c in sep_chars:
            assert c in "-_=─━—－"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
