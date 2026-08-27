from tortoise import fields, migrations
from tortoise.indexes import Index
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("catalog", "0003_book_series_index")]

    initial = False

    operations = [
        ops.AddField(
            model_name="Book",
            name="hidden",
            field=fields.BooleanField(default=False, db_default=False),
        ),
        ops.AddIndex(
            model_name="Book",
            index=Index(fields=["generation_id", "hidden", "title_sort", "public_id"]),
        ),
    ]
