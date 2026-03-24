"""Tests for the patch route."""
import pytest
from server import apply_patch


class TestInsert:
    def test_insert_at_start(self):
        html, n = apply_patch("world", [{"op": "insert", "pos": 0, "text": "hello "}])
        assert html == "hello world"
        assert n == 1

    def test_insert_at_end(self):
        html, n = apply_patch("hello", [{"op": "insert", "pos": 999, "text": " world"}])
        assert html == "hello world"

    def test_insert_in_middle(self):
        html, n = apply_patch("helo", [{"op": "insert", "pos": 2, "text": "l"}])
        assert html == "hello"

    def test_insert_negative_pos_clamps_to_zero(self):
        html, n = apply_patch("world", [{"op": "insert", "pos": -5, "text": "hello "}])
        assert html == "hello world"


class TestDelete:
    def test_delete_range(self):
        html, n = apply_patch("hello world", [{"op": "delete", "start": 5, "end": 11}])
        assert html == "hello"
        assert n == 1

    def test_delete_from_start(self):
        html, n = apply_patch("hello world", [{"op": "delete", "start": 0, "end": 6}])
        assert html == "world"

    def test_delete_clamps(self):
        html, n = apply_patch("hi", [{"op": "delete", "start": 0, "end": 999}])
        assert html == ""


class TestReplace:
    def test_replace_first(self):
        html, n = apply_patch("aaa", [{"op": "replace", "find": "a", "text": "b", "count": 1}])
        assert html == "baa"
        assert n == 1

    def test_replace_n(self):
        html, n = apply_patch("aaa", [{"op": "replace", "find": "a", "text": "b", "count": 2}])
        assert html == "bba"

    def test_replace_all(self):
        html, n = apply_patch("aaa", [{"op": "replace_all", "find": "a", "text": "b"}])
        assert html == "bbb"

    def test_replace_empty_find_noop(self):
        html, n = apply_patch("hello", [{"op": "replace", "find": "", "text": "x"}])
        assert html == "hello"
        assert n == 0


class TestSlice:
    def test_slice(self):
        html, n = apply_patch("hello world", [{"op": "slice", "start": 6, "end": 11}])
        assert html == "world"
        assert n == 1

    def test_slice_from_start(self):
        html, n = apply_patch("hello world", [{"op": "slice", "start": 0, "end": 5}])
        assert html == "hello"


class TestPrepend:
    def test_prepend(self):
        html, n = apply_patch("world", [{"op": "prepend", "text": "hello "}])
        assert html == "hello world"
        assert n == 1


class TestRegex:
    def test_regex_replace(self):
        html, n = apply_patch("foo123bar456", [{"op": "regex_replace", "pattern": r"\d+", "text": "#"}])
        assert html == "foo#bar#"
        assert n == 1

    def test_regex_replace_count(self):
        html, n = apply_patch("foo123bar456", [{"op": "regex_replace", "pattern": r"\d+", "text": "#", "count": 1}])
        assert html == "foo#bar456"


class TestMultiOps:
    def test_chained_ops(self):
        html, n = apply_patch("<div>old</div>", [
            {"op": "replace", "find": "old", "text": "new"},
            {"op": "prepend", "text": "<!-- patched -->"},
            {"op": "insert", "pos": 999, "text": "\n<!-- end -->"}
        ])
        assert "new" in html
        assert html.startswith("<!-- patched -->")
        assert html.endswith("<!-- end -->")
        assert n == 3

    def test_empty_ops(self):
        html, n = apply_patch("hello", [])
        assert html == "hello"
        assert n == 0

    def test_unknown_op_skipped(self):
        html, n = apply_patch("hello", [{"op": "destroy_everything"}])
        assert html == "hello"
        assert n == 0
