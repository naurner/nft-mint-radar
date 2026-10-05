from wlparser.notify.logs import filter_log, read_log


def test_no_search_returns_the_tail():
    text = "\n".join(f"line {i}" for i in range(10))
    assert filter_log(text, search=None, max_lines=3, context=40) == "line 7\nline 8\nline 9"


def test_no_search_returns_everything_when_shorter_than_max_lines():
    text = "a\nb\nc"
    assert filter_log(text, search=None, max_lines=100, context=40) == "a\nb\nc"


def test_search_includes_matched_line_and_context_after_it():
    text = "\n".join(
        ["noise", "не удалось опубликовать пост 1", "Traceback...", "Exception: boom", "noise"]
    )
    result = filter_log(text, search="не удалось опубликовать", max_lines=100, context=2)
    assert "не удалось опубликовать пост 1" in result
    assert "Traceback..." in result
    assert "Exception: boom" in result
    assert "noise" not in result  # как у grep -A: не включает то, что до совпадения


def test_search_with_no_match_returns_empty_string():
    text = "all good\nnothing to see"
    assert filter_log(text, search="error", max_lines=100, context=40) == ""


def test_search_is_case_insensitive():
    text = "ERROR: something broke"
    result = filter_log(text, search="error", max_lines=100, context=0)
    assert "ERROR: something broke" in result


def test_search_respects_max_lines_by_keeping_the_tail():
    lines = []
    for i in range(5):
        lines += [f"match {i}", f"detail {i}"]
    text = "\n".join(lines)
    result = filter_log(text, search="match", max_lines=6, context=1)
    assert "match 4" in result
    assert "match 0" not in result


def test_read_log_returns_empty_for_a_missing_file(tmp_path):
    assert read_log(tmp_path / "absent.log") == ""


def test_read_log_reads_and_filters_a_real_file(tmp_path):
    path = tmp_path / "bot.log"
    path.write_text("a\nERROR boom\nb\n", encoding="utf-8")
    assert read_log(path, search="error", context=0) == "ERROR boom\n---"
