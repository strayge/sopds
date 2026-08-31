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
    ]
