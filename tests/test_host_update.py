import json

from wlparser.host_update import HostUpdater, format_result


def test_request_writes_the_flag_file(tmp_path):
    updater = HostUpdater(tmp_path)
    assert updater.request(chat_id=111, message_id=42) is True

    flag = tmp_path / "update_requested.json"
    assert flag.exists()
    data = json.loads(flag.read_text(encoding="utf-8"))
    assert data == {"chat_id": 111, "message_id": 42}


def test_second_request_is_refused_while_one_is_pending(tmp_path):
    updater = HostUpdater(tmp_path)
    assert updater.request(chat_id=111, message_id=42) is True
    assert updater.request(chat_id=111, message_id=99) is False


def test_request_is_refused_while_inflight(tmp_path):
    # Хост уже переименовал запрос — значит взялся за дело.
    updater = HostUpdater(tmp_path)
    (tmp_path / "update_inflight.json").write_text('{"chat_id": 1, "message_id": 2}', encoding="utf-8")
    assert updater.has_pending() is True
    assert updater.request(chat_id=111, message_id=42) is False


def test_poll_result_returns_none_without_a_result_file(tmp_path):
    updater = HostUpdater(tmp_path)
    assert updater.poll_result() is None


def test_poll_result_combines_result_and_inflight_then_cleans_up(tmp_path):
    updater = HostUpdater(tmp_path)
    (tmp_path / "update_inflight.json").write_text(
        '{"chat_id": 111, "message_id": 42}', encoding="utf-8"
    )
    (tmp_path / "update_result.txt").write_text(
        "status=ok\nfrom=abc123\nto=def456\ndetail=обновлено и пересобрано\n",
        encoding="utf-8",
    )

    result = updater.poll_result()

    assert result == {
        "status": "ok",
        "from": "abc123",
        "to": "def456",
        "detail": "обновлено и пересобрано",
        "chat_id": 111,
        "message_id": 42,
    }
    assert not (tmp_path / "update_result.txt").exists()
    assert not (tmp_path / "update_inflight.json").exists()


def test_poll_result_without_inflight_still_returns_the_status(tmp_path):
    # На случай, если inflight потерялся — статус хотя бы виден в логах.
    updater = HostUpdater(tmp_path)
    (tmp_path / "update_result.txt").write_text(
        "status=error\ndetail=что-то пошло не так\n", encoding="utf-8"
    )

    result = updater.poll_result()

    assert result["status"] == "error"
    assert "chat_id" not in result


# --- format_result ---


def test_format_result_ok():
    text = format_result({"status": "ok", "from": "abc123", "to": "def456", "detail": "готово"})
    assert "Обновлено" in text
    assert "abc123 → def456" in text
    assert "готово" in text


def test_format_result_unchanged():
    text = format_result({"status": "unchanged", "from": "abc123", "to": "abc123", "detail": "уже на последнем"})
    assert "последнем коммите" in text


def test_format_result_error():
    text = format_result({"status": "error", "detail": "docker не найден в PATH"})
    assert "Не получилось" in text
    assert "docker не найден в PATH" in text


def test_format_result_escapes_detail():
    text = format_result({"status": "error", "detail": "<script>alert(1)</script>"})
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
