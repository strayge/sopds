from sopds.catalog.genre_names import GENRE_NAMES, genre_label


def test_genre_names_include_current_dump_and_legacy_entries() -> None:
    assert len(GENRE_NAMES) == 279
    assert genre_label("sf_litrpg") == "ЛитРПГ"
    assert genre_label("comp_osnet") == "ОС и Сети"  # noqa: RUF001
    assert genre_label("thriller_mystery") == "Триллеры"


def test_unknown_genre_code_is_preserved() -> None:
    assert genre_label("future_genre") == "future_genre"
