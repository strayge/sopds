from tortoise import fields, migrations
from tortoise.fields.base import OnDelete
from tortoise.indexes import Index
from tortoise.migrations import operations as ops
from tortoise.migrations.constraints import CheckConstraint

from sopds.db.models import GenerationState
from sopds.imports.status import ImportState, ImportTrigger


class Migration(migrations.Migration):
    initial = True

    operations = [
        ops.CreateModel(
            name="CatalogGeneration",
            fields=[
                (
                    "id",
                    fields.BigIntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                (
                    "state",
                    fields.CharEnumField(
                        description="IMPORTING: importing\nACTIVE: active\nSUPERSEDED: superseded\nFAILED: failed",
                        enum_type=GenerationState,
                        max_length=16,
                    ),
                ),
                ("created_at", fields.DatetimeField(auto_now=False, auto_now_add=True)),
                (
                    "completed_at",
                    fields.DatetimeField(null=True, auto_now=False, auto_now_add=False),
                ),
                (
                    "activated_at",
                    fields.DatetimeField(null=True, auto_now=False, auto_now_add=False),
                ),
            ],
            options={
                "table": "catalog_generation",
                "app": "catalog",
                "indexes": [Index(fields=["state", "created_at"])],
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="Archive",
            fields=[
                (
                    "id",
                    fields.BigIntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                (
                    "generation",
                    fields.ForeignKeyField(
                        "catalog.CatalogGeneration",
                        source_field="generation_id",
                        db_constraint=True,
                        to_field="id",
                        related_name="archives",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
                ("relative_path", fields.TextField(unique=False)),
                ("available", fields.BooleanField(default=True)),
            ],
            options={
                "table": "archive",
                "app": "catalog",
                "unique_together": (("generation", "relative_path"),),
                "indexes": [Index(fields=["generation_id", "available"])],
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="Author",
            fields=[
                (
                    "id",
                    fields.BigIntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                (
                    "generation",
                    fields.ForeignKeyField(
                        "catalog.CatalogGeneration",
                        source_field="generation_id",
                        db_constraint=True,
                        to_field="id",
                        related_name="authors",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
                ("name", fields.CharField(max_length=512)),
                ("name_sort", fields.CharField(max_length=512)),
            ],
            options={
                "table": "author",
                "app": "catalog",
                "unique_together": (("generation", "name_sort", "name"),),
                "indexes": [Index(fields=["generation_id", "name_sort", "id"])],
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="CatalogSource",
            fields=[
                (
                    "id",
                    fields.SmallIntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                ("namespace", fields.CharField(unique=True, max_length=64)),
                ("path", fields.TextField(unique=False)),
                ("fingerprint_size", fields.BigIntField(null=True)),
                ("fingerprint_mtime_ns", fields.BigIntField(null=True)),
                ("fingerprint_sha256", fields.CharField(null=True, max_length=64)),
                ("updated_at", fields.DatetimeField(auto_now=True, auto_now_add=False)),
            ],
            options={"table": "catalog_source", "app": "catalog", "pk_attr": "id"},
            bases=["Model"],
        ),
        ops.CreateModel(
            name="CatalogState",
            fields=[
                (
                    "id",
                    fields.SmallIntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                (
                    "active_generation",
                    fields.ForeignKeyField(
                        "catalog.CatalogGeneration",
                        source_field="active_generation_id",
                        null=True,
                        db_constraint=True,
                        to_field="id",
                        related_name="active_catalog_states",
                        on_delete=OnDelete.SET_NULL,
                    ),
                ),
                ("updated_at", fields.DatetimeField(auto_now=True, auto_now_add=False)),
            ],
            options={"table": "catalog_state", "app": "catalog", "pk_attr": "id"},
            bases=["Model"],
        ),
        ops.CreateModel(
            name="Genre",
            fields=[
                (
                    "id",
                    fields.BigIntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                (
                    "generation",
                    fields.ForeignKeyField(
                        "catalog.CatalogGeneration",
                        source_field="generation_id",
                        db_constraint=True,
                        to_field="id",
                        related_name="genres",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
                ("code", fields.CharField(max_length=128)),
                ("label", fields.CharField(max_length=256)),
                ("label_sort", fields.CharField(max_length=256)),
            ],
            options={
                "table": "genre",
                "app": "catalog",
                "unique_together": (("generation", "code"),),
                "indexes": [Index(fields=["generation_id", "label_sort", "id"])],
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="ImportRun",
            fields=[
                (
                    "id",
                    fields.BigIntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                (
                    "trigger",
                    fields.CharEnumField(
                        description="SCHEDULED: scheduled\nMANUAL: manual",
                        enum_type=ImportTrigger,
                        max_length=16,
                    ),
                ),
                (
                    "state",
                    fields.CharEnumField(
                        description="RUNNING: running\nSUCCEEDED: succeeded\nFAILED: failed\nINTERRUPTED: interrupted",
                        enum_type=ImportState,
                        max_length=16,
                    ),
                ),
                ("started_at", fields.DatetimeField(auto_now=False, auto_now_add=True)),
                (
                    "finished_at",
                    fields.DatetimeField(null=True, auto_now=False, auto_now_add=False),
                ),
                ("attempted_size", fields.BigIntField(null=True)),
                ("attempted_mtime_ns", fields.BigIntField(null=True)),
                ("attempted_sha256", fields.CharField(null=True, max_length=64)),
                ("records_read", fields.BigIntField(default=0)),
                ("records_imported", fields.BigIntField(default=0)),
                ("records_deleted", fields.BigIntField(default=0)),
                ("records_rejected", fields.BigIntField(default=0)),
                ("error_summary", fields.TextField(null=True, unique=False)),
                (
                    "staging_generation",
                    fields.ForeignKeyField(
                        "catalog.CatalogGeneration",
                        source_field="staging_generation_id",
                        null=True,
                        db_constraint=True,
                        to_field="id",
                        related_name="import_runs",
                        on_delete=OnDelete.SET_NULL,
                    ),
                ),
            ],
            options={
                "table": "import_run",
                "app": "catalog",
                "indexes": [
                    Index(fields=["state", "started_at"]),
                    Index(fields=["started_at", "id"]),
                ],
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="Series",
            fields=[
                (
                    "id",
                    fields.BigIntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                (
                    "generation",
                    fields.ForeignKeyField(
                        "catalog.CatalogGeneration",
                        source_field="generation_id",
                        db_constraint=True,
                        to_field="id",
                        related_name="series_entries",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
                ("name", fields.CharField(max_length=512)),
                ("name_sort", fields.CharField(max_length=512)),
            ],
            options={
                "table": "series",
                "app": "catalog",
                "unique_together": (("generation", "name_sort", "name"),),
                "indexes": [Index(fields=["generation_id", "name_sort", "id"])],
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="Book",
            fields=[
                (
                    "id",
                    fields.BigIntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                (
                    "generation",
                    fields.ForeignKeyField(
                        "catalog.CatalogGeneration",
                        source_field="generation_id",
                        db_constraint=True,
                        to_field="id",
                        related_name="books",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
                ("public_id", fields.CharField(max_length=64)),
                (
                    "archive",
                    fields.ForeignKeyField(
                        "catalog.Archive",
                        source_field="archive_id",
                        db_constraint=True,
                        to_field="id",
                        related_name="books",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
                ("member_filename", fields.TextField(unique=False)),
                ("title", fields.CharField(max_length=1024)),
                ("title_sort", fields.CharField(max_length=1024)),
                (
                    "series",
                    fields.ForeignKeyField(
                        "catalog.Series",
                        source_field="series_id",
                        null=True,
                        db_constraint=True,
                        to_field="id",
                        related_name="books",
                        on_delete=OnDelete.SET_NULL,
                    ),
                ),
                ("series_number", fields.TextField(null=True, unique=False)),
                ("size", fields.BigIntField()),
                ("libid", fields.CharField(null=True, max_length=128)),
                ("published_date", fields.DateField(null=True)),
                ("language", fields.CharField(null=True, max_length=32)),
                ("original_format", fields.CharField(max_length=32)),
                ("rating", fields.SmallIntField(null=True)),
                ("keywords", fields.TextField(null=True, unique=False)),
            ],
            options={
                "table": "book",
                "app": "catalog",
                "unique_together": (("generation", "public_id"), ("archive", "member_filename")),
                "indexes": [
                    Index(fields=["generation_id", "title_sort", "public_id"]),
                    Index(fields=["generation_id", "series_id", "series_number", "public_id"]),
                    Index(fields=["generation_id", "language", "title_sort", "public_id"]),
                    Index(fields=["generation_id", "libid"]),
                ],
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="BookAuthor",
            fields=[
                (
                    "id",
                    fields.BigIntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                (
                    "book",
                    fields.ForeignKeyField(
                        "catalog.Book",
                        source_field="book_id",
                        db_constraint=True,
                        to_field="id",
                        related_name="author_links",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
                (
                    "author",
                    fields.ForeignKeyField(
                        "catalog.Author",
                        source_field="author_id",
                        db_constraint=True,
                        to_field="id",
                        related_name="book_links",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
                ("position", fields.SmallIntField()),
            ],
            options={
                "table": "book_author",
                "app": "catalog",
                "unique_together": (("book", "author"), ("book", "position")),
                "indexes": [Index(fields=["author_id", "book_id"])],
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.CreateModel(
            name="BookGenre",
            fields=[
                (
                    "id",
                    fields.BigIntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                (
                    "book",
                    fields.ForeignKeyField(
                        "catalog.Book",
                        source_field="book_id",
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
                        related_name="book_links",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
            ],
            options={
                "table": "book_genre",
                "app": "catalog",
                "unique_together": (("book", "genre"),),
                "indexes": [Index(fields=["genre_id", "book_id"])],
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
        ops.AddConstraint(
            model_name="CatalogSource",
            constraint=CheckConstraint(check='"id" = 1', name="catalog_source_singleton"),
        ),
        ops.AddConstraint(
            model_name="CatalogState",
            constraint=CheckConstraint(check='"id" = 1', name="catalog_state_singleton"),
        ),
        ops.RunSQL(
            "INSERT INTO catalog_state(id, active_generation_id, updated_at) "
            "VALUES (1, NULL, CURRENT_TIMESTAMP)",
            "DELETE FROM catalog_state WHERE id = 1",
        ),
    ]
