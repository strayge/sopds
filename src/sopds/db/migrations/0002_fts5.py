"""Add the explicitly maintained full-text book projection."""

from tortoise import migrations
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]

    operations = [
        ops.RunSQL(
            """
            CREATE VIRTUAL TABLE book_fts USING fts5(
                book_id UNINDEXED,
                generation_id UNINDEXED,
                title,
                authors,
                series,
                genres,
                language,
                tokenize = 'unicode61 remove_diacritics 2'
            )
            """,
            "DROP TABLE book_fts",
        )
    ]
