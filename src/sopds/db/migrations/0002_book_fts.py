"""Add the explicitly maintained PostgreSQL full-text book projection."""

from tortoise import migrations
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]

    operations = [
        ops.RunSQL(
            """
            CREATE TABLE book_fts (
                book_id BIGINT PRIMARY KEY REFERENCES book(id) ON DELETE CASCADE,
                generation_id BIGINT NOT NULL
                    REFERENCES catalog_generation(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                authors TEXT NOT NULL,
                series TEXT NOT NULL,
                genres TEXT NOT NULL,
                language TEXT NOT NULL,
                all_vector TSVECTOR GENERATED ALWAYS AS (
                    to_tsvector(
                        'simple'::regconfig,
                        title || ' ' || authors || ' ' || series || ' ' || genres || ' ' || language
                    )
                ) STORED,
                title_vector TSVECTOR GENERATED ALWAYS AS (
                    to_tsvector('simple'::regconfig, title)
                ) STORED,
                authors_vector TSVECTOR GENERATED ALWAYS AS (
                    to_tsvector('simple'::regconfig, authors)
                ) STORED,
                series_vector TSVECTOR GENERATED ALWAYS AS (
                    to_tsvector('simple'::regconfig, series)
                ) STORED
            );
            CREATE INDEX book_fts_all_vector_idx ON book_fts USING GIN (all_vector);
            CREATE INDEX book_fts_title_vector_idx ON book_fts USING GIN (title_vector);
            CREATE INDEX book_fts_authors_vector_idx ON book_fts USING GIN (authors_vector);
            CREATE INDEX book_fts_series_vector_idx ON book_fts USING GIN (series_vector);
            CREATE INDEX book_fts_generation_idx ON book_fts (generation_id, book_id);
            """,
            "DROP TABLE book_fts",
        )
    ]
