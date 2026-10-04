"""Ensure vector searches use the cosine HNSW index."""

from alembic import op
from sqlalchemy import text

revision = "0002_vector_index"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    for table, index in [("files", "file_vector_idx"), ("file_chunks", "file_chunk_vector_idx")]:
        definition = conn.scalar(text("SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND tablename=:table AND indexname=:index"), {"table": table, "index": index})
        if definition and "vector_cosine_ops" in definition:
            continue
        if definition:
            op.execute(f"DROP INDEX {index}")
        op.execute(f"CREATE INDEX {index} ON {table} USING hnsw (vector_info vector_cosine_ops)")


def downgrade():
    # Both index variants remain valid; retain the cosine index on downgrade.
    pass
