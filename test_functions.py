"""Tests for pure functions extracted from the codebase.

This file tests functions that don't depend on NoneBot framework.
Functions are imported from production modules.
"""

import os
import time
import pytest
from collections import deque
from datetime import datetime, date

# Set test environment
os.environ["RULES_FILE"] = ""
os.environ["REMINDERS_FILE"] = ""

# Import production functions
from rules import normalize_rule, upsert_rule, find_rule
from reminders import normalize_reminder, next_reminder_id
from forward_utils import (
    parse_int_set, parse_str_list, normalize_text, match_keywords,
    is_duplicate, check_keyword_cooldown, track_keyword_hit,
    parse_csv_items, parse_indices, resolve_group_id,
    rule_by_index, resolve_rule_reference, rule_index, clean_display_text,
)
from remind_utils import (
    render_reminder, parse_time, parse_id_list,
    should_resume_period_interval, format_schedule_result, REMIND_TYPE_LABELS,
)


# ==================== Tests ====================

class TestParseIntSet:
    """Test parse_int_set function."""

    def test_valid(self):
        assert parse_int_set("1,2,3") == {1, 2, 3}

    def test_with_spaces(self):
        assert parse_int_set("1, 2 , 3") == {1, 2, 3}

    def test_empty(self):
        assert parse_int_set("") == set()

    def test_single(self):
        assert parse_int_set("42") == {42}

    def test_duplicates(self):
        assert parse_int_set("1,1,2,2") == {1, 2}


class TestParseStrList:
    """Test parse_str_list function."""

    def test_valid(self):
        assert parse_str_list("a,b,c") == ["a", "b", "c"]

    def test_with_spaces(self):
        assert parse_str_list(" a , b , c ") == ["a", "b", "c"]

    def test_empty_items(self):
        assert parse_str_list("a,,b,,,c") == ["a", "b", "c"]

    def test_empty(self):
        assert parse_str_list("") == []


class TestMatchKeywords:
    """Test match_keywords function."""

    def test_basic_match(self):
        keywords = [{"word": "test", "enabled": True}]
        assert "test" in match_keywords("this is a test", keywords, False)

    def test_disabled_keyword(self):
        keywords = [{"word": "test", "enabled": False}]
        assert "test" not in match_keywords("this is a test", keywords, False)

    def test_regex_match(self):
        keywords = [{"word": r"\d+", "enabled": True}]
        assert r"\d+" in match_keywords("test 123", keywords, True)

    def test_no_match(self):
        keywords = [{"word": "xyz", "enabled": True}]
        assert match_keywords("hello world", keywords, False) == []

    def test_invalid_regex(self):
        """Invalid regex should not crash."""
        keywords = [{"word": "[invalid", "enabled": True}]
        result = match_keywords("test", keywords, True)
        assert isinstance(result, list)

    def test_case_insensitive(self):
        keywords = [{"word": "TEST", "enabled": True}]
        assert "TEST" in match_keywords("this is a test", keywords, False)

    def test_case_sensitive(self):
        keywords = [{"word": "TEST", "enabled": True}]
        assert match_keywords("this is a test", keywords, False, case_sensitive=True) == []


class TestIsDuplicate:
    """Test is_duplicate function."""

    def test_new_message(self):
        recent_keys = deque(maxlen=1000)
        recent_seen = set()
        assert is_duplicate("group1:msg1", recent_keys, recent_seen) is False

    def test_duplicate_message(self):
        recent_keys = deque(maxlen=1000)
        recent_seen = set()
        is_duplicate("group1:msg1", recent_keys, recent_seen)
        assert is_duplicate("group1:msg1", recent_keys, recent_seen) is True

    def test_expired_cleanup(self):
        recent_keys = deque(maxlen=1000)
        recent_seen = set()
        old_time = time.time() - 31
        recent_keys.append((old_time, "old_key"))
        recent_seen.add("old_key")
        is_duplicate("new_key", recent_keys, recent_seen, dedupe_seconds=30)
        assert "old_key" not in recent_seen
        assert len(recent_keys) == 1


class TestCheckKeywordCooldown:
    """Test check_keyword_cooldown function."""

    def test_no_cooldown(self):
        cooldown_dict = {}
        assert check_keyword_cooldown(123, "test", cooldown_dict) is False

    def test_active_cooldown(self):
        cooldown_dict = {}
        check_keyword_cooldown(123, "test", cooldown_dict)
        assert check_keyword_cooldown(123, "test", cooldown_dict) is True

    def test_different_groups(self):
        cooldown_dict = {}
        check_keyword_cooldown(1, "test", cooldown_dict)
        assert check_keyword_cooldown(2, "test", cooldown_dict) is False

    def test_different_keywords(self):
        cooldown_dict = {}
        check_keyword_cooldown(123, "test1", cooldown_dict)
        assert check_keyword_cooldown(123, "test2", cooldown_dict) is False


class TestTrackKeywordHit:
    """Test track_keyword_hit function."""

    def test_track_hit(self):
        stats = {}
        track_keyword_hit(123, "test", stats)
        today = time.strftime("%Y-%m-%d")
        key = f"{today}:123:test"
        assert stats[key] == 1

    def test_multiple_hits(self):
        stats = {}
        track_keyword_hit(123, "test", stats)
        track_keyword_hit(123, "test", stats)
        today = time.strftime("%Y-%m-%d")
        key = f"{today}:123:test"
        assert stats[key] == 2


class TestResolveGroupId:
    """Test resolve_group_id function."""

    def test_with_group(self):
        group_id, keyword = resolve_group_id("add 123456 test")
        assert group_id == 123456
        assert keyword == "test"

    def test_without_group(self):
        group_id, keyword = resolve_group_id("add test")
        assert group_id is None
        assert keyword == "test"

    def test_no_keyword(self):
        group_id, keyword = resolve_group_id("add 123456")
        assert group_id == 123456
        assert keyword is None

    def test_single_command(self):
        group_id, keyword = resolve_group_id("status")
        assert group_id is None
        assert keyword is None


class TestRuleByIndex:
    """Test rule_by_index function."""

    def test_valid(self):
        rules = [{"group_id": 1}, {"group_id": 2}]
        assert rule_by_index(rules, 1)["group_id"] == 1

    def test_out_of_range(self):
        rules = [{"group_id": 1}]
        assert rule_by_index(rules, 5) is None

    def test_zero(self):
        rules = [{"group_id": 1}]
        assert rule_by_index(rules, 0) is None


class TestResolveRuleReference:
    """Test resolve_rule_reference function."""

    def test_by_group_id(self):
        rules = [{"group_id": 123}, {"group_id": 456}]
        assert resolve_rule_reference(rules, 123)["group_id"] == 123

    def test_by_index(self):
        rules = [{"group_id": 123}, {"group_id": 456}]
        assert resolve_rule_reference(rules, 1)["group_id"] == 123

    def test_not_found(self):
        rules = [{"group_id": 123}]
        assert resolve_rule_reference(rules, 999) is None


class TestRuleIndex:
    """Test rule_index function."""

    def test_found(self):
        rules = [{"group_id": 100}, {"group_id": 200}]
        assert rule_index(rules, 200) == 2

    def test_not_found(self):
        rules = [{"group_id": 100}]
        assert rule_index(rules, 999) is None


class TestCleanDisplayText:
    """Test clean_display_text function."""

    def test_with_emoji(self):
        assert clean_display_text("Hello 😀 World 🎉") == "Hello World"

    def test_empty_after_clean(self):
        assert clean_display_text("😀🎉") == "未知"

    def test_normal(self):
        assert clean_display_text("测试群名") == "测试群名"

    def test_multiple_spaces(self):
        assert clean_display_text("hello   world") == "hello world"


class TestParseCsvItems:
    """Test parse_csv_items function."""

    def test_chinese_comma(self):
        assert parse_csv_items("a，b，c") == ["a", "b", "c"]

    def test_english_comma(self):
        assert parse_csv_items("a,b,c") == ["a", "b", "c"]

    def test_mixed(self):
        assert parse_csv_items("a，b,c") == ["a", "b", "c"]


class TestParseIndices:
    """Test parse_indices function."""

    def test_valid(self):
        indices, error = parse_indices("1,2,3")
        assert indices == [1, 2, 3]
        assert error is None

    def test_invalid(self):
        indices, error = parse_indices("a,b")
        assert indices == []
        assert error is not None

    def test_empty(self):
        indices, error = parse_indices("")
        assert indices == []
        assert error is not None

    def test_dedup_and_sort(self):
        indices, error = parse_indices("3,1,2,1,3")
        assert indices == [1, 2, 3]
        assert error is None


class TestParseTime:
    """Test parse_time function."""

    def test_valid(self):
        hour, minute, error = parse_time("09:30")
        assert hour == 9
        assert minute == 30
        assert error is None

    def test_single_digit_hour(self):
        hour, minute, error = parse_time("9:30")
        assert hour == 9
        assert minute == 30

    def test_invalid_hour(self):
        hour, minute, error = parse_time("25:00")
        assert error is not None

    def test_invalid_minute(self):
        hour, minute, error = parse_time("12:60")
        assert error is not None

    def test_no_colon(self):
        hour, minute, error = parse_time("1230")
        assert error is not None


class TestParseIdList:
    """Test parse_id_list function."""

    def test_single(self):
        ids, error = parse_id_list("1")
        assert ids == [1]
        assert error is None

    def test_multiple(self):
        ids, error = parse_id_list("1,2,3")
        assert ids == [1, 2, 3]
        assert error is None

    def test_chinese_comma(self):
        ids, error = parse_id_list("1，2，3")
        assert ids == [1, 2, 3]
        assert error is None

    def test_invalid(self):
        ids, error = parse_id_list("a,b")
        assert ids == []
        assert error is not None

    def test_empty(self):
        ids, error = parse_id_list("")
        assert ids == []
        assert error is not None

    def test_dedup_and_sort(self):
        ids, error = parse_id_list("3,1,2,1,3")
        assert ids == [1, 2, 3]
        assert error is None


class TestRenderReminder:
    """Test render_reminder function."""

    def test_daily(self):
        rem = {"id": 1, "type": "daily", "hour": 10, "minute": 30, "message": "背单词"}
        result = render_reminder(rem)
        assert "提醒 #1" in result
        assert "每天" in result
        assert "10:30" in result
        assert "背单词" in result

    def test_once(self):
        rem = {"id": 2, "type": "once", "date": "2026-05-15", "hour": 9, "minute": 0, "message": "开会"}
        result = render_reminder(rem)
        assert "单次" in result
        assert "2026-05-15" in result

    def test_interval(self):
        rem = {"id": 3, "type": "interval", "interval_minutes": 30, "message": "休息"}
        result = render_reminder(rem)
        assert "间隔" in result
        assert "30 分钟" in result

    def test_period(self):
        rem = {"id": 4, "type": "period", "hour": 18, "minute": 0, "repeat_interval": 10, "message": "背单词"}
        result = render_reminder(rem)
        assert "周期催促" in result
        assert "18:00" in result
        assert "10 分钟" in result

    def test_quote(self):
        rem = {"id": 5, "type": "daily", "hour": 9, "minute": 0, "auto_generate": "quote"}
        result = render_reminder(rem)
        assert "每日一言" in result

    def test_period_done_today(self):
        rem = {
            "id": 6, "type": "period", "hour": 18, "minute": 0,
            "repeat_interval": 10, "message": "背单词",
            "last_done_date": date.today().isoformat()
        }
        result = render_reminder(rem)
        assert "已完成" in result


class TestShouldResumePeriodInterval:
    """Test should_resume_period_interval function."""

    def test_not_period(self):
        rem = {"type": "daily", "enabled": True}
        assert should_resume_period_interval(rem) is False

    def test_disabled(self):
        rem = {"type": "period", "enabled": False, "hour": 9, "minute": 0}
        assert should_resume_period_interval(rem) is False

    def test_done_today(self):
        rem = {"type": "period", "enabled": True, "hour": 9, "minute": 0, "last_done_date": date.today().isoformat()}
        assert should_resume_period_interval(rem) is False

    def test_before_start(self):
        now = datetime.now()
        future_hour = (now.hour + 2) % 24
        rem = {"type": "period", "enabled": True, "hour": future_hour, "minute": 0}
        assert should_resume_period_interval(rem) is False


class TestNormalizeRule:
    """Test normalize_rule from rules.py."""

    def test_string_keywords(self):
        raw = {"group_id": 1, "targets": [3, 1, 2], "keywords": ["word1", "word2"]}
        result = normalize_rule(raw)
        assert result["group_id"] == 1
        assert result["targets"] == [1, 2, 3]
        assert len(result["keywords"]) == 2
        assert result["keywords"][0] == {"word": "word1", "enabled": True}

    def test_dict_keywords(self):
        raw = {"group_id": 1, "targets": [], "keywords": [{"word": "active", "enabled": True}, {"word": "disabled", "enabled": False}]}
        result = normalize_rule(raw)
        assert result["keywords"][0]["enabled"] is True
        assert result["keywords"][1]["enabled"] is False

    def test_empty_keywords(self):
        raw = {"group_id": 1, "targets": [], "keywords": []}
        result = normalize_rule(raw)
        assert result["keywords"] == []

    def test_blank_keyword_filtered(self):
        raw = {"group_id": 1, "targets": [], "keywords": ["valid", "", "  ", "also_valid"]}
        result = normalize_rule(raw)
        assert len(result["keywords"]) == 2
        assert result["keywords"][0]["word"] == "valid"
        assert result["keywords"][1]["word"] == "also_valid"

    def test_deduplicate_targets(self):
        raw = {"group_id": 1, "targets": [1, 1, 2, 2, 3], "keywords": []}
        result = normalize_rule(raw)
        assert result["targets"] == [1, 2, 3]

    def test_per_keyword_targets(self):
        """Keywords with targets should preserve them."""
        raw = {"group_id": 1, "targets": [100], "keywords": [
            {"word": "a", "enabled": True, "targets": [200, 300]},
            {"word": "b", "enabled": True}
        ]}
        result = normalize_rule(raw)
        assert result["keywords"][0]["targets"] == [200, 300]
        assert "targets" not in result["keywords"][1]


class TestUpsertRule:
    """Test upsert_rule from rules.py."""

    def test_add_new(self):
        existing = [{"group_id": 1, "targets": [], "keywords": []}]
        new_rule = {"group_id": 2, "targets": [111], "keywords": ["new"]}
        result = upsert_rule(existing, new_rule)
        assert len(result) == 2
        assert result[1]["group_id"] == 2

    def test_update_existing(self):
        existing = [
            {"group_id": 1, "targets": [], "keywords": ["old"]},
            {"group_id": 2, "targets": [], "keywords": []}
        ]
        updated = {"group_id": 1, "targets": [111], "keywords": ["new"]}
        result = upsert_rule(existing, updated)
        assert len(result) == 2
        rule1 = next(r for r in result if r["group_id"] == 1)
        assert rule1["keywords"][0]["word"] == "new"


class TestNormalizeReminder:
    """Test normalize_reminder from reminders.py."""

    def test_daily(self):
        raw = {"id": 1, "type": "daily", "hour": "10", "minute": "30", "message": "test", "targets": [3, 1, 2]}
        result = normalize_reminder(raw)
        assert result["id"] == 1
        assert result["type"] == "daily"
        assert result["hour"] == 10
        assert result["minute"] == 30
        assert result["targets"] == [1, 2, 3]

    def test_once(self):
        raw = {"id": 2, "type": "once", "date": "2026-05-15", "hour": 9, "minute": 0, "message": "once task"}
        result = normalize_reminder(raw)
        assert result["type"] == "once"
        assert result["date"] == "2026-05-15"
        assert result["fired"] is False

    def test_interval(self):
        raw = {"id": 3, "type": "interval", "interval_minutes": "15", "message": "interval task"}
        result = normalize_reminder(raw)
        assert result["type"] == "interval"
        assert result["interval_minutes"] == 15

    def test_period(self):
        raw = {"id": 4, "type": "period", "hour": 18, "minute": 0, "repeat_interval": "10", "message": "period task", "last_done_date": "2026-05-14"}
        result = normalize_reminder(raw)
        assert result["type"] == "period"
        assert result["repeat_interval"] == 10
        assert result["last_done_date"] == "2026-05-14"

    def test_invalid_type(self):
        raw = {"id": 5, "type": "invalid", "message": "test"}
        result = normalize_reminder(raw)
        assert result["type"] == "daily"

    def test_missing_id(self):
        raw = {"type": "daily", "message": "test"}
        with pytest.raises((KeyError, TypeError)):
            normalize_reminder(raw)

    def test_missing_creator_qq(self):
        raw = {"id": 1, "type": "daily", "message": "test"}
        result = normalize_reminder(raw)
        assert result["creator_qq"] == 0

    def test_auto_generate(self):
        raw = {"id": 6, "type": "daily", "hour": 9, "minute": 0, "auto_generate": "quote"}
        result = normalize_reminder(raw)
        assert result["auto_generate"] == "quote"


class TestNextReminderId:
    """Test next_reminder_id from reminders.py."""

    def test_empty(self):
        assert next_reminder_id([]) == 1

    def test_existing(self):
        existing = [{"id": 1}, {"id": 5}, {"id": 3}]
        assert next_reminder_id(existing) == 6


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
