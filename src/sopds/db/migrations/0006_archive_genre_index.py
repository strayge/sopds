from tortoise import migrations
from tortoise.indexes import Index
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("catalog", "0005_materialized_summaries")]

    initial = False

    operations = [
        ops.AddIndex(
            model_name="ArchiveGenre",
            index=Index(fields=["genre_id"]),
        ),
    ]
