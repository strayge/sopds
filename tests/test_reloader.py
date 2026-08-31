"""Development reload scheduling and file selection tests."""

from pathlib import Path

import pytest

from sopds.reloader import RestartSchedule, _snapshot_files


def test_restart_schedule_coalesces_changes_without_losing_trailing_restart() -> None:
    schedule = RestartSchedule(minimum_interval_seconds=10.0)

    assert schedule.notify_change(0.0) == 0.0
    assert schedule.take_due(0.0)

    assert schedule.notify_change(2.0) == 10.0
    assert schedule.notify_change(4.0) == 10.0
    assert schedule.notify_change(9.0) == 10.0
    assert not schedule.take_due(9.99)
    assert schedule.take_due(10.0)

    assert schedule.notify_change(11.0) == 20.0
    assert schedule.take_due(20.0)


def test_restart_schedule_limits_wait_to_poll_interval() -> None:
    schedule = RestartSchedule(minimum_interval_seconds=10.0)

    assert schedule.wait_seconds(0.0, maximum=0.5) == 0.5
    schedule.last_restart_started_at = 0.0
    schedule.notify_change(9.8)

    assert schedule.wait_seconds(9.8, maximum=0.5) == pytest.approx(0.2)
    assert schedule.wait_seconds(10.0, maximum=0.5) == 0.0


def test_reload_snapshot_watches_python_translations_and_config_but_not_web_assets(
    tmp_path: Path,
) -> None:
    package_path = tmp_path / "sopds"
    translations_path = package_path / "web" / "translations" / "ru" / "LC_MESSAGES"
    translations_path.mkdir(parents=True)
    module_path = package_path / "app.py"
    catalog_path = translations_path / "messages.po"
    template_path = package_path / "page.html"
    config_path = tmp_path / "config.toml"
    module_path.write_text("value = 1\n", encoding="utf-8")
    catalog_path.write_text('msgid "Catalog"\nmsgstr "Каталог"\n', encoding="utf-8")
    template_path.write_text("first\n", encoding="utf-8")
    config_path.write_text("first = true\n", encoding="utf-8")
    initial = _snapshot_files(package_path, config_path)

    template_path.write_text("second and ignored\n", encoding="utf-8")
    assert _snapshot_files(package_path, config_path) == initial

    catalog_path.write_text('msgid "Catalog"\nmsgstr "Библиотека"\n', encoding="utf-8")
    translation_changed = _snapshot_files(package_path, config_path)
    assert translation_changed != initial

    module_path.write_text("value = 200\n", encoding="utf-8")
    python_changed = _snapshot_files(package_path, config_path)
    assert python_changed != translation_changed

    config_path.unlink()
    assert _snapshot_files(package_path, config_path) != python_changed
