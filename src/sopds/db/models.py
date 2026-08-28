"""Generation-scoped catalog persistence models."""

from enum import StrEnum

from tortoise import fields
from tortoise.migrations.constraints import CheckConstraint
from tortoise.models import Model

from sopds.imports.status import ImportState, ImportTrigger


class GenerationState(StrEnum):
    IMPORTING = "importing"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class CatalogGeneration(Model):
    id = fields.BigIntField(primary_key=True)
    state = fields.CharEnumField(GenerationState, max_length=16)
    created_at = fields.DatetimeField(auto_now_add=True)
    completed_at = fields.DatetimeField(null=True)
    activated_at = fields.DatetimeField(null=True)
    visible_book_count = fields.BigIntField(default=0, db_default=0)
    hidden_book_count = fields.BigIntField(default=0, db_default=0)

    archives: fields.ReverseRelation[Archive]
    authors: fields.ReverseRelation[Author]
    books: fields.ReverseRelation[Book]
    genres: fields.ReverseRelation[Genre]
    series_entries: fields.ReverseRelation[Series]
    import_runs: fields.ReverseRelation[ImportRun]
    active_catalog_states: fields.ReverseRelation[CatalogState]

    class Meta:
        table = "catalog_generation"
        indexes = (("state", "created_at"),)


class CatalogState(Model):
    id = fields.SmallIntField(primary_key=True)
    active_generation: fields.ForeignKeyNullableRelation[CatalogGeneration] = (
        fields.ForeignKeyField(
            "catalog.CatalogGeneration",
            related_name="active_catalog_states",
            null=True,
            on_delete=fields.SET_NULL,
        )
    )
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "catalog_state"
        constraints = (CheckConstraint(check='"id" = 1', name="catalog_state_singleton"),)


class CatalogSource(Model):
    id = fields.SmallIntField(primary_key=True)
    namespace = fields.CharField(max_length=64, unique=True)
    path = fields.TextField()
    fingerprint_size = fields.BigIntField(null=True)
    fingerprint_mtime_ns = fields.BigIntField(null=True)
    fingerprint_sha256 = fields.CharField(max_length=64, null=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "catalog_source"
        constraints = (CheckConstraint(check='"id" = 1', name="catalog_source_singleton"),)


class ImportRun(Model):
    id = fields.BigIntField(primary_key=True)
    trigger = fields.CharEnumField(ImportTrigger, max_length=16)
    state = fields.CharEnumField(ImportState, max_length=16)
    started_at = fields.DatetimeField(auto_now_add=True)
    finished_at = fields.DatetimeField(null=True)
    attempted_size = fields.BigIntField(null=True)
    attempted_mtime_ns = fields.BigIntField(null=True)
    attempted_sha256 = fields.CharField(max_length=64, null=True)
    records_read = fields.BigIntField(default=0)
    records_imported = fields.BigIntField(default=0)
    records_deleted = fields.BigIntField(default=0)
    records_rejected = fields.BigIntField(default=0)
    error_summary = fields.TextField(null=True)
    staging_generation: fields.ForeignKeyNullableRelation[CatalogGeneration] = (
        fields.ForeignKeyField(
            "catalog.CatalogGeneration",
            related_name="import_runs",
            null=True,
            on_delete=fields.SET_NULL,
        )
    )

    class Meta:
        table = "import_run"
        indexes = (("state", "started_at"), ("started_at", "id"))


class Archive(Model):
    id = fields.BigIntField(primary_key=True)
    generation: fields.ForeignKeyRelation[CatalogGeneration] = fields.ForeignKeyField(
        "catalog.CatalogGeneration", related_name="archives", on_delete=fields.CASCADE
    )
    relative_path = fields.TextField()
    available = fields.BooleanField(default=True)
    visible_book_count = fields.BigIntField(default=0, db_default=0)

    books: fields.ReverseRelation[Book]
    languages: fields.ReverseRelation[ArchiveLanguage]
    original_formats: fields.ReverseRelation[ArchiveOriginalFormat]
    genre_links: fields.ReverseRelation[ArchiveGenre]

    class Meta:
        table = "archive"
        unique_together = (("generation", "relative_path"),)
        indexes = (("generation_id", "available"),)


class Author(Model):
    id = fields.BigIntField(primary_key=True)
    generation: fields.ForeignKeyRelation[CatalogGeneration] = fields.ForeignKeyField(
        "catalog.CatalogGeneration", related_name="authors", on_delete=fields.CASCADE
    )
    name = fields.CharField(max_length=512)
    name_sort = fields.CharField(max_length=512)

    book_links: fields.ReverseRelation[BookAuthor]

    class Meta:
        table = "author"
        unique_together = (("generation", "name_sort", "name"),)
        indexes = (("generation_id", "name_sort", "id"),)


class Genre(Model):
    id = fields.BigIntField(primary_key=True)
    generation: fields.ForeignKeyRelation[CatalogGeneration] = fields.ForeignKeyField(
        "catalog.CatalogGeneration", related_name="genres", on_delete=fields.CASCADE
    )
    code = fields.CharField(max_length=128)
    label = fields.CharField(max_length=256)
    label_sort = fields.CharField(max_length=256)

    book_links: fields.ReverseRelation[BookGenre]
    archive_links: fields.ReverseRelation[ArchiveGenre]

    class Meta:
        table = "genre"
        unique_together = (("generation", "code"),)
        indexes = (("generation_id", "label_sort", "id"),)


class ArchiveLanguage(Model):
    id = fields.BigIntField(primary_key=True)
    archive: fields.ForeignKeyRelation[Archive] = fields.ForeignKeyField(
        "catalog.Archive", related_name="languages", on_delete=fields.CASCADE
    )
    language = fields.CharField(max_length=32)

    class Meta:
        table = "archive_language"
        unique_together = (("archive", "language"),)


class ArchiveOriginalFormat(Model):
    id = fields.BigIntField(primary_key=True)
    archive: fields.ForeignKeyRelation[Archive] = fields.ForeignKeyField(
        "catalog.Archive", related_name="original_formats", on_delete=fields.CASCADE
    )
    original_format = fields.CharField(max_length=32)

    class Meta:
        table = "archive_original_format"
        unique_together = (("archive", "original_format"),)


class ArchiveGenre(Model):
    id = fields.BigIntField(primary_key=True)
    archive: fields.ForeignKeyRelation[Archive] = fields.ForeignKeyField(
        "catalog.Archive", related_name="genre_links", on_delete=fields.CASCADE
    )
    genre: fields.ForeignKeyRelation[Genre] = fields.ForeignKeyField(
        "catalog.Genre", related_name="archive_links", on_delete=fields.CASCADE
    )

    class Meta:
        table = "archive_genre"
        unique_together = (("archive", "genre"),)
        indexes = (("genre_id",),)


class Series(Model):
    id = fields.BigIntField(primary_key=True)
    generation: fields.ForeignKeyRelation[CatalogGeneration] = fields.ForeignKeyField(
        "catalog.CatalogGeneration", related_name="series_entries", on_delete=fields.CASCADE
    )
    name = fields.CharField(max_length=512)
    name_sort = fields.CharField(max_length=512)

    books: fields.ReverseRelation[Book]

    class Meta:
        table = "series"
        unique_together = (("generation", "name_sort", "name"),)
        indexes = (("generation_id", "name_sort", "id"),)


class Book(Model):
    id = fields.BigIntField(primary_key=True)
    generation: fields.ForeignKeyRelation[CatalogGeneration] = fields.ForeignKeyField(
        "catalog.CatalogGeneration", related_name="books", on_delete=fields.CASCADE
    )
    public_id = fields.CharField(max_length=64)
    archive: fields.ForeignKeyRelation[Archive] = fields.ForeignKeyField(
        "catalog.Archive", related_name="books", on_delete=fields.CASCADE
    )
    member_filename = fields.TextField()
    title = fields.CharField(max_length=1024)
    title_sort = fields.CharField(max_length=1024)
    series: fields.ForeignKeyNullableRelation[Series] = fields.ForeignKeyField(
        "catalog.Series", related_name="books", null=True, on_delete=fields.SET_NULL
    )
    series_number = fields.TextField(null=True)
    size = fields.BigIntField()
    libid = fields.CharField(max_length=128, null=True)
    published_date = fields.DateField(null=True)
    language = fields.CharField(max_length=32, null=True)
    original_format = fields.CharField(max_length=32)
    rating = fields.SmallIntField(null=True)
    keywords = fields.TextField(null=True)
    hidden = fields.BooleanField(default=False, db_default=False)

    author_links: fields.ReverseRelation[BookAuthor]
    genre_links: fields.ReverseRelation[BookGenre]

    class Meta:
        table = "book"
        unique_together = (
            ("generation", "public_id"),
            ("archive", "member_filename"),
        )
        indexes = (
            ("series_id",),
            ("generation_id", "title_sort", "public_id"),
            ("generation_id", "series_id", "series_number", "public_id"),
            ("generation_id", "language", "title_sort", "public_id"),
            ("generation_id", "libid"),
            ("generation_id", "hidden", "title_sort", "public_id"),
        )


class BookAuthor(Model):
    id = fields.BigIntField(primary_key=True)
    book: fields.ForeignKeyRelation[Book] = fields.ForeignKeyField(
        "catalog.Book", related_name="author_links", on_delete=fields.CASCADE
    )
    author: fields.ForeignKeyRelation[Author] = fields.ForeignKeyField(
        "catalog.Author", related_name="book_links", on_delete=fields.CASCADE
    )
    position = fields.SmallIntField()

    class Meta:
        table = "book_author"
        unique_together = (("book", "author"), ("book", "position"))
        indexes = (("author_id", "book_id"),)


class BookGenre(Model):
    id = fields.BigIntField(primary_key=True)
    book: fields.ForeignKeyRelation[Book] = fields.ForeignKeyField(
        "catalog.Book", related_name="genre_links", on_delete=fields.CASCADE
    )
    genre: fields.ForeignKeyRelation[Genre] = fields.ForeignKeyField(
        "catalog.Genre", related_name="book_links", on_delete=fields.CASCADE
    )

    class Meta:
        table = "book_genre"
        unique_together = (("book", "genre"),)
        indexes = (("genre_id", "book_id"),)
