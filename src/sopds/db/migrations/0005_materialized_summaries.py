from tortoise import fields, migrations
from tortoise.fields.base import OnDelete
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("catalog", "0004_book_hidden")]

    initial = False

    operations = [
        ops.AddField(
            model_name="CatalogGeneration",
            name="visible_book_count",
            field=fields.BigIntField(default=0, db_default=0),
        ),
        ops.AddField(
            model_name="CatalogGeneration",
            name="hidden_book_count",
            field=fields.BigIntField(default=0, db_default=0),
        ),
        ops.AddField(
            model_name="Archive",
            name="visible_book_count",
            field=fields.BigIntField(default=0, db_default=0),
        ),
        ops.CreateModel(
            name="ArchiveLanguage",
            fields=[
                (
                    "id",
                    fields.BigIntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                (
                    "archive",
                    fields.ForeignKeyField(
                        "catalog.Archive",
                        source_field="archive_id",
                        db_constraint=True,
                        to_field="id",
                        related_name="languages",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
                ("language", fields.CharField(max_length=32)),
            ],
            options={
                "table": "archive_language",
                "app": "catalog",
                "unique_together": (("archive", "language"),),
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="ArchiveOriginalFormat",
            fields=[
                (
                    "id",
                    fields.BigIntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                (
                    "archive",
                    fields.ForeignKeyField(
                        "catalog.Archive",
                        source_field="archive_id",
                        db_constraint=True,
                        to_field="id",
                        related_name="original_formats",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
                ("original_format", fields.CharField(max_length=32)),
            ],
            options={
                "table": "archive_original_format",
                "app": "catalog",
                "unique_together": (("archive", "original_format"),),
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="ArchiveGenre",
            fields=[
                (
                    "id",
                    fields.BigIntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                (
                    "archive",
                    fields.ForeignKeyField(
                        "catalog.Archive",
                        source_field="archive_id",
                        db_constraint=True,
                        to_field="id",
                        related_name="genre_links",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
                (
                    "genre",
                    fields.ForeignKeyField(
                        "catalog.Genre",
                        source_field="genre_id",
                        db_constraint=True,
                        to_field="id",
                        related_name="archive_links",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
            ],
            options={
                "table": "archive_genre",
                "app": "catalog",
                "unique_together": (("archive", "genre"),),
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.RunSQL(
            """
            UPDATE catalog_generation
            SET visible_book_count = (
                    SELECT COUNT(*) FROM book
                    WHERE book.generation_id = catalog_generation.id AND book.hidden = 0
                ),
                hidden_book_count = (
                    SELECT COUNT(*) FROM book
                    WHERE book.generation_id = catalog_generation.id AND book.hidden = 1
                )
            """
        ),
        ops.RunSQL(
            """
            UPDATE archive
            SET visible_book_count = (
                SELECT COUNT(*) FROM book
                WHERE book.archive_id = archive.id AND book.hidden = 0
            )
            """
        ),
        ops.RunSQL(
            """
            INSERT INTO archive_language(archive_id, language)
            SELECT DISTINCT b.archive_id, b.language
            FROM book b
            JOIN archive a ON a.id = b.archive_id
            WHERE a.generation_id = b.generation_id
              AND b.hidden = 0
              AND b.language IS NOT NULL
              AND (b.series_id IS NULL OR EXISTS (
                  SELECT 1 FROM series s
                  WHERE s.id = b.series_id AND s.generation_id = b.generation_id
              ))
            """
        ),
        ops.RunSQL(
            """
            INSERT INTO archive_original_format(archive_id, original_format)
            SELECT DISTINCT b.archive_id, b.original_format
            FROM book b
            JOIN archive a ON a.id = b.archive_id
            WHERE a.generation_id = b.generation_id
              AND b.hidden = 0
              AND (b.series_id IS NULL OR EXISTS (
                  SELECT 1 FROM series s
                  WHERE s.id = b.series_id AND s.generation_id = b.generation_id
              ))
            """
        ),
        ops.RunSQL(
            """
            INSERT INTO archive_genre(archive_id, genre_id)
            SELECT DISTINCT b.archive_id, bg.genre_id
            FROM book b
            JOIN archive a ON a.id = b.archive_id
            JOIN book_genre bg ON bg.book_id = b.id
            JOIN genre g ON g.id = bg.genre_id
            WHERE a.generation_id = b.generation_id
              AND b.hidden = 0
              AND g.generation_id = b.generation_id
              AND (b.series_id IS NULL OR EXISTS (
                  SELECT 1 FROM series s
                  WHERE s.id = b.series_id AND s.generation_id = b.generation_id
              ))
            """
        ),
    ]
