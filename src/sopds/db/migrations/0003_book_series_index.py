from tortoise import migrations
from tortoise.indexes import Index
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("catalog", "0002_fts5")]

    initial = False

    operations = [
        ops.AddIndex(
            model_name="Book",
            index=Index(fields=["series_id"]),
        ),
    ]
