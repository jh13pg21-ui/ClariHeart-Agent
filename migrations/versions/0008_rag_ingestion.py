"""add versioned RAG ingestion schema

Revision ID: 0008_rag_ingestion
Revises: 0007_structured_summary
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0008_rag_ingestion"
down_revision = "0007_structured_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.String(length=512), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("access_class", sa.String(length=32), nullable=False),
        sa.Column("cloud_vision_allowed", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("active_version_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_key"),
    )
    op.create_index("ix_knowledge_documents_access_class", "knowledge_documents", ["access_class"])
    op.create_index("ix_knowledge_documents_active_version_id", "knowledge_documents", ["active_version_id"])

    op.create_table(
        "knowledge_document_versions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("document_id", sa.String(length=32), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("pipeline_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("canonical_artifact_path", sa.String(length=1024), nullable=True),
        sa.Column("previous_version_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "sha256", name="uq_knowledge_doc_version_hash"),
    )
    op.create_index("ix_knowledge_document_versions_document_id", "knowledge_document_versions", ["document_id"])
    op.create_index("ix_knowledge_document_versions_status", "knowledge_document_versions", ["status"])

    op.create_table(
        "knowledge_ingestion_jobs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("document_version_id", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("progress_page", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_pages", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_retryable", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("trigger_actor", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["document_version_id"], ["knowledge_document_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_ingestion_jobs_document_version_id", "knowledge_ingestion_jobs", ["document_version_id"])
    op.create_index("ix_knowledge_ingestion_jobs_stage", "knowledge_ingestion_jobs", ["stage"])
    op.create_index("ix_knowledge_ingestion_jobs_status", "knowledge_ingestion_jobs", ["status"])
    op.create_index("ix_knowledge_ingestion_jobs_version_status", "knowledge_ingestion_jobs", ["document_version_id", "status"])

    op.create_table(
        "knowledge_pages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_version_id", sa.String(length=32), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=False),
        sa.Column("height_px", sa.Integer(), nullable=False),
        sa.Column("rotation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("text_strategy", sa.String(length=32), nullable=False),
        sa.Column("structure_strategy", sa.String(length=32), nullable=False),
        sa.Column("route_reasons_json", sa.Text(), nullable=True),
        sa.Column("quality_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("image_artifact_path", sa.String(length=1024), nullable=True),
        sa.Column("canonical_json", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["document_version_id"], ["knowledge_document_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_version_id", "page_number", name="uq_knowledge_page_version_number"),
    )
    op.create_index("ix_knowledge_pages_document_version_id", "knowledge_pages", ["document_version_id"])
    op.create_index("ix_knowledge_pages_status", "knowledge_pages", ["status"])
    op.create_index("ix_knowledge_pages_structure_strategy", "knowledge_pages", ["structure_strategy"])
    op.create_index("ix_knowledge_pages_text_strategy", "knowledge_pages", ["text_strategy"])

    with op.batch_alter_table("knowledge_chunks") as batch_op:
        batch_op.add_column(sa.Column("stable_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("document_id", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("document_version_id", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("parent_chunk_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("chunk_kind", sa.String(length=32), server_default="LEGACY_TEXT", nullable=False))
        batch_op.add_column(sa.Column("page_start", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("page_end", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("section_path_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("block_ids_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("content_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("embedding_model", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("embedding_dimension", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("active", sa.Boolean(), server_default="1", nullable=False))
        batch_op.create_foreign_key("fk_knowledge_chunks_document_id", "knowledge_documents", ["document_id"], ["id"])
        batch_op.create_foreign_key("fk_knowledge_chunks_document_version_id", "knowledge_document_versions", ["document_version_id"], ["id"])
        batch_op.create_foreign_key("fk_knowledge_chunks_parent_chunk_id", "knowledge_chunks", ["parent_chunk_id"], ["id"])
        batch_op.create_index("ix_knowledge_chunks_stable_id", ["stable_id"], unique=True)
        batch_op.create_index("ix_knowledge_chunks_document_id", ["document_id"])
        batch_op.create_index("ix_knowledge_chunks_document_version_id", ["document_version_id"])
        batch_op.create_index("ix_knowledge_chunks_parent_chunk_id", ["parent_chunk_id"])
        batch_op.create_index("ix_knowledge_chunks_chunk_kind", ["chunk_kind"])
        batch_op.create_index("ix_knowledge_chunks_active", ["active"])


def downgrade() -> None:
    with op.batch_alter_table("knowledge_chunks") as batch_op:
        for name in (
            "ix_knowledge_chunks_active",
            "ix_knowledge_chunks_chunk_kind",
            "ix_knowledge_chunks_parent_chunk_id",
            "ix_knowledge_chunks_document_version_id",
            "ix_knowledge_chunks_document_id",
            "ix_knowledge_chunks_stable_id",
        ):
            batch_op.drop_index(name)
        batch_op.drop_constraint("fk_knowledge_chunks_parent_chunk_id", type_="foreignkey")
        batch_op.drop_constraint("fk_knowledge_chunks_document_version_id", type_="foreignkey")
        batch_op.drop_constraint("fk_knowledge_chunks_document_id", type_="foreignkey")
        for name in (
            "active", "embedding_dimension", "embedding_model", "content_hash", "block_ids_json",
            "section_path_json", "page_end", "page_start", "chunk_kind", "parent_chunk_id",
            "document_version_id", "document_id", "stable_id",
        ):
            batch_op.drop_column(name)
    op.drop_table("knowledge_pages")
    op.drop_table("knowledge_ingestion_jobs")
    op.drop_table("knowledge_document_versions")
    op.drop_table("knowledge_documents")
