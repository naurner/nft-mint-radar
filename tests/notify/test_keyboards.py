from wlparser.notify.keyboards import (
    CB_SYS_CLEAR_DRAFTS,
    CB_SYS_DBDUMP,
    CB_SYS_LOGS,
    CB_SYS_TESTPOST,
    CB_SYS_UPDATE,
    main_menu_keyboard,
)


def _callback_data(markup) -> set[str]:
    return {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    }


def test_default_menu_has_no_owner_or_poster_tools():
    data = _callback_data(main_menu_keyboard())
    assert CB_SYS_LOGS not in data
    assert CB_SYS_TESTPOST not in data
    assert CB_SYS_CLEAR_DRAFTS not in data


def test_update_button_only_when_requested():
    assert CB_SYS_UPDATE not in _callback_data(main_menu_keyboard(show_update=False))
    assert CB_SYS_UPDATE in _callback_data(main_menu_keyboard(show_update=True))


def test_owner_tools_shown_to_owner_without_poster():
    data = _callback_data(main_menu_keyboard(show_owner_tools=True, show_poster_tools=False))
    assert CB_SYS_LOGS in data
    assert CB_SYS_TESTPOST not in data
    assert CB_SYS_CLEAR_DRAFTS not in data


def test_poster_tools_require_both_owner_and_poster():
    # Постер включён, но зовущий — не владелец: кнопок с реальными
    # последствиями (твит, списание черновиков) быть не должно.
    data = _callback_data(main_menu_keyboard(show_owner_tools=False, show_poster_tools=True))
    assert CB_SYS_TESTPOST not in data
    assert CB_SYS_CLEAR_DRAFTS not in data


def test_owner_and_poster_together_show_everything():
    data = _callback_data(main_menu_keyboard(show_owner_tools=True, show_poster_tools=True))
    assert CB_SYS_LOGS in data
    assert CB_SYS_TESTPOST in data
    assert CB_SYS_CLEAR_DRAFTS in data


def test_db_dump_button_needs_both_owner_and_local_db():
    assert CB_SYS_DBDUMP not in _callback_data(main_menu_keyboard(show_db_dump=True))
    data = _callback_data(main_menu_keyboard(show_owner_tools=True, show_db_dump=True))
    assert CB_SYS_DBDUMP in data


def test_db_dump_hidden_when_using_a_remote_db():
    data = _callback_data(main_menu_keyboard(show_owner_tools=True, show_db_dump=False))
    assert CB_SYS_DBDUMP not in data
    assert CB_SYS_LOGS in data  # остальные инструменты владельца всё равно есть
