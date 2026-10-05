from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1160a000001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents", sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
        sa.UniqueConstraint("name", name=op.f("uq_agents_name")))
    op.create_table(
        "analyses", sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("portfolio_id", sa.UUID(), nullable=True),
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analyses")))
    op.create_index(
        "ix_analyses_ticker_created_at", "analyses", ["ticker", "created_at"],
        unique=False)
    op.create_index(
        "ix_analyses_user_id_created_at", "analyses",
        ["user_id", "created_at"], unique=False)
    op.create_table(
        "backtests", sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("strategy_name", sa.String(length=100), nullable=False),
        sa.Column("benchmark_symbol", sa.String(length=10), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column(
            "initial_capital", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column(
            "ending_capital", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column(
            "strategy_return", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column(
            "benchmark_return", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtests")))
    op.create_index(
        "ix_backtests_user_id_created_at", "backtests",
        ["user_id", "created_at"], unique=False)
    op.create_table(
        "benchmarks",
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("asset_type", sa.String(length=20), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_benchmarks")),
        sa.UniqueConstraint("symbol", name=op.f("uq_benchmarks_symbol")))
    op.create_table(
        "document_chunks", sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("qdrant_point_id", sa.String(length=64), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint(
            "qdrant_point_id",
            name=op.f("uq_document_chunks_qdrant_point_id")))
    op.create_index(
        "ix_document_chunks_document_id", "document_chunks", ["document_id"],
        unique=False)
    op.create_table(
        "embedding_jobs", sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embedding_jobs")))
    op.create_index(
        "ix_embedding_jobs_document_id", "embedding_jobs", ["document_id"],
        unique=False)
    op.create_table(
        "instruments",
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("asset_type", sa.String(length=20), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("sector", sa.String(length=100), nullable=True),
        sa.Column("exchange", sa.String(length=50), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instruments")),
        sa.UniqueConstraint("ticker", name=op.f("uq_instruments_ticker")))
    op.create_table(
        "kg_entities",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column("entity_type", sa.String(length=30), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kg_entities")),
        sa.UniqueConstraint(
            "normalized_name", "entity_type", name="uq_kg_entities_norm_type"))
    op.create_table(
        "rag_collections",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rag_collections")),
        sa.UniqueConstraint("name", name=op.f("uq_rag_collections_name")))
    op.create_table(
        "ticker_names",
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("official_name", sa.String(length=300), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticker_names")),
        sa.UniqueConstraint("ticker", name=op.f("uq_ticker_names_ticker")))
    op.create_table(
        "agent_memories", sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=True),
        sa.Column("memory_type", sa.String(length=30), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column(
            "content", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"],
                                name=op.f("fk_agent_memories_agent_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_memories")))
    op.create_index(
        "ix_agent_memories_agent_id_score", "agent_memories",
        ["agent_id", "score"], unique=False)
    op.create_table(
        "agent_metrics", sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("avg_score", sa.Numeric(), nullable=True),
        sa.Column("success_rate", sa.Numeric(), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"],
                                name=op.f("fk_agent_metrics_agent_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_metrics")),
        sa.UniqueConstraint(
            "agent_id", name=op.f("uq_agent_metrics_agent_id")))
    op.create_table(
        "agent_reports", sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("agent_role", sa.String(length=30), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=True),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"],
                                name=op.f("fk_agent_reports_analysis_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_reports")))
    op.create_index(
        "ix_agent_reports_analysis_id", "agent_reports", ["analysis_id"],
        unique=False)
    op.create_index(
        "ix_agent_reports_ticker", "agent_reports", ["ticker"], unique=False)
    op.create_table(
        "agent_runs", sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"],
                                name=op.f("fk_agent_runs_agent_id")),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"],
                                name=op.f("fk_agent_runs_analysis_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")))
    op.create_index(
        "ix_agent_runs_agent_id", "agent_runs", ["agent_id"], unique=False)
    op.create_index(
        "ix_agent_runs_analysis_id", "agent_runs", ["analysis_id"],
        unique=False)
    op.create_table(
        "analysis_citations",
        sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("chunk_id", sa.UUID(), nullable=True),
        sa.Column("citation_text", sa.Text(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_id"], ["analyses.id"],
            name=op.f("fk_analysis_citations_analysis_id")),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunks.id"],
                                name=op.f("fk_analysis_citations_chunk_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_citations")))
    op.create_index(
        "ix_analysis_citations_analysis_id", "analysis_citations",
        ["analysis_id"], unique=False)
    op.create_table(
        "analysis_sources",
        sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"],
                                name=op.f("fk_analysis_sources_analysis_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_sources")))
    op.create_index(
        "ix_analysis_sources_analysis_id", "analysis_sources", ["analysis_id"],
        unique=False)
    op.create_table(
        "backtest_metrics",
        sa.Column("backtest_id", sa.UUID(), nullable=False),
        sa.Column("sharpe_ratio", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column("sortino_ratio", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column("drawdown", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column("win_rate", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["backtest_id"], ["backtests.id"],
                                name=op.f("fk_backtest_metrics_backtest_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_metrics")),
        sa.UniqueConstraint(
            "backtest_id", name=op.f("uq_backtest_metrics_backtest_id")))
    op.create_table(
        "backtest_positions",
        sa.Column("backtest_id", sa.UUID(), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("entry_price", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(
            ["backtest_id"], ["backtests.id"],
            name=op.f("fk_backtest_positions_backtest_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_positions")))
    op.create_index(
        "ix_backtest_positions_backtest_id", "backtest_positions",
        ["backtest_id"], unique=False)
    op.create_table(
        "backtest_trades", sa.Column("backtest_id", sa.UUID(), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("price", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("trade_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["backtest_id"], ["backtests.id"],
                                name=op.f("fk_backtest_trades_backtest_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backtest_trades")))
    op.create_index(
        "ix_backtest_trades_backtest_id", "backtest_trades", ["backtest_id"],
        unique=False)
    op.create_table(
        "benchmark_results",
        sa.Column("backtest_id", sa.UUID(), nullable=False),
        sa.Column("benchmark_id", sa.UUID(), nullable=False),
        sa.Column(
            "strategy_return", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column(
            "benchmark_return", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column("alpha", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column("beta", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column(
            "information_ratio", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column(
            "tracking_error", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column("outperformed", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["backtest_id"], ["backtests.id"],
                                name=op.f("fk_benchmark_results_backtest_id")),
        sa.ForeignKeyConstraint(
            ["benchmark_id"], ["benchmarks.id"],
            name=op.f("fk_benchmark_results_benchmark_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_benchmark_results")))
    op.create_index(
        "ix_benchmark_results_backtest_id", "benchmark_results",
        ["backtest_id"], unique=False)
    op.create_table(
        "debate_sessions", sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"],
                                name=op.f("fk_debate_sessions_analysis_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_debate_sessions")))
    op.create_index(
        "ix_debate_sessions_analysis_id", "debate_sessions", ["analysis_id"],
        unique=False)
    op.create_table(
        "episodic_memories", sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("recommendation", sa.String(length=10), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("outcome_return", sa.Float(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"],
                                name=op.f("fk_episodic_memories_analysis_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_episodic_memories")))
    op.create_index(
        "ix_episodic_memories_ticker_created_at", "episodic_memories",
        ["ticker", "created_at"], unique=False)
    op.create_table(
        "price_bars", sa.Column("instrument_id", sa.UUID(), nullable=False),
        sa.Column("interval", sa.String(length=5), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(), nullable=False),
        sa.Column("high", sa.Numeric(), nullable=False),
        sa.Column("low", sa.Numeric(), nullable=False),
        sa.Column("close", sa.Numeric(), nullable=False),
        sa.Column("volume", sa.Numeric(), nullable=False),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"],
                                name=op.f("fk_price_bars_instrument_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_bars")),
        sa.UniqueConstraint(
            "instrument_id", "interval", "timestamp",
            name="uq_price_bars_instrument_interval_timestamp"))
    op.create_table(
        "recommendations", sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("recommendation", sa.String(length=10), nullable=False),
        sa.Column("confidence", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("confidence_adjustment_note", sa.Text(), nullable=True),
        sa.Column("tier_generation_note", sa.Text(), nullable=True),
        sa.Column("debate_converged", sa.Boolean(), nullable=False),
        sa.Column(
            "window_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"],
                                name=op.f("fk_recommendations_analysis_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendations")))
    op.create_index(
        "ix_recommendations_analysis_id", "recommendations", ["analysis_id"],
        unique=False)
    op.create_table(
        "reflections", sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column(
            "issues", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "retry_agents", postgresql.JSONB(astext_type=sa.Text()),
            nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"],
                                name=op.f("fk_reflections_analysis_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reflections")))
    op.create_index(
        "ix_reflections_analysis_id", "reflections", ["analysis_id"],
        unique=False)
    op.create_table(
        "retrieval_logs", sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("retrieved_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"],
                                name=op.f("fk_retrieval_logs_analysis_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retrieval_logs")))
    op.create_index(
        "ix_retrieval_logs_analysis_id", "retrieval_logs", ["analysis_id"],
        unique=False)
    op.create_table(
        "technical_indicator_cache",
        sa.Column("instrument_id", sa.UUID(), nullable=False),
        sa.Column("indicator", sa.String(length=20), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instruments.id"],
            name=op.f("fk_technical_indicator_cache_instrument_id")),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_technical_indicator_cache")))
    op.create_table(
        "agent_results", sa.Column("agent_run_id", sa.UUID(), nullable=False),
        sa.Column("score", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"],
                                name=op.f("fk_agent_results_agent_run_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_results")),
        sa.UniqueConstraint(
            "agent_run_id", name=op.f("uq_agent_results_agent_run_id")))
    op.create_index(
        "ix_agent_results_agent_run_id", "agent_results", ["agent_run_id"],
        unique=False)
    op.create_table(
        "debate_arguments",
        sa.Column("debate_session_id", sa.UUID(), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column(
            "citations", postgresql.JSONB(astext_type=sa.Text()),
            nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(
            ["debate_session_id"], ["debate_sessions.id"],
            name=op.f("fk_debate_arguments_debate_session_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_debate_arguments")))
    op.create_index(
        "ix_debate_arguments_debate_session_id", "debate_arguments",
        ["debate_session_id"], unique=False)
    op.create_table(
        "investment_decisions",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("portfolio_id", sa.UUID(), nullable=True),
        sa.Column("recommendation_id", sa.UUID(), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("action_taken", sa.String(length=10), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(
            ["recommendation_id"], ["recommendations.id"],
            name=op.f("fk_investment_decisions_recommendation_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investment_decisions")),
        sa.UniqueConstraint(
            "user_id", "recommendation_id",
            name="uq_investment_decisions_user_recommendation"))
    op.create_index(
        "ix_investment_decisions_recommendation_id", "investment_decisions",
        ["recommendation_id"], unique=False)
    op.create_index(
        op.f("ix_investment_decisions_user_id"), "investment_decisions",
        ["user_id"], unique=False)
    op.create_table(
        "kg_relations",
        sa.Column("source_entity_id", sa.UUID(), nullable=False),
        sa.Column("target_entity_id", sa.UUID(), nullable=False),
        sa.Column("relation_type", sa.String(length=100), nullable=False),
        sa.Column("source_report_id", sa.UUID(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["source_entity_id"], ["kg_entities.id"],
                                name=op.f("fk_kg_relations_source_entity_id")),
        sa.ForeignKeyConstraint(["source_report_id"], ["agent_reports.id"],
                                name=op.f("fk_kg_relations_source_report_id")),
        sa.ForeignKeyConstraint(["target_entity_id"], ["kg_entities.id"],
                                name=op.f("fk_kg_relations_target_entity_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kg_relations")))
    op.create_index(
        "ix_kg_relations_source_entity_id", "kg_relations",
        ["source_entity_id"], unique=False)
    op.create_index(
        "ix_kg_relations_target_entity_id", "kg_relations",
        ["target_entity_id"], unique=False)
    op.create_table(
        "memory_references", sa.Column("memory_id", sa.UUID(), nullable=False),
        sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"],
                                name=op.f("fk_memory_references_analysis_id")),
        sa.ForeignKeyConstraint(["memory_id"], ["agent_memories.id"],
                                name=op.f("fk_memory_references_memory_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_references")))
    op.create_index(
        "ix_memory_references_analysis_id", "memory_references",
        ["analysis_id"], unique=False)
    op.create_table(
        "recommendation_metrics",
        sa.Column("recommendation_id", sa.UUID(), nullable=False),
        sa.Column("accuracy_score", sa.Numeric(), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(
            ["recommendation_id"], ["recommendations.id"],
            name=op.f("fk_recommendation_metrics_recommendation_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation_metrics")),
        sa.UniqueConstraint(
            "recommendation_id",
            name="uq_recommendation_metrics_recommendation_id"))
    op.create_table(
        "recommendation_tiers",
        sa.Column("recommendation_id", sa.UUID(), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("target_price", sa.Numeric(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triggered_price", sa.Numeric(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(
            ["recommendation_id"], ["recommendations.id"],
            name=op.f("fk_recommendation_tiers_recommendation_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation_tiers")))
    op.create_index(
        "ix_recommendation_tiers_recommendation_id", "recommendation_tiers",
        ["recommendation_id"], unique=False)
    op.create_index(
        "ix_recommendation_tiers_status", "recommendation_tiers", ["status"],
        unique=False)
    op.create_table(
        "decision_outcomes",
        sa.Column("decision_id", sa.UUID(), nullable=False),
        sa.Column("evaluation_days", sa.Integer(), nullable=False),
        sa.Column("strategy_return", sa.Numeric(), nullable=True),
        sa.Column("benchmark_return", sa.Numeric(), nullable=True),
        sa.Column("alpha", sa.Numeric(), nullable=True),
        sa.Column("outperformed", sa.Boolean(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["decision_id"], ["investment_decisions.id"],
                                name=op.f("fk_decision_outcomes_decision_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decision_outcomes")),
        sa.UniqueConstraint(
            "decision_id", "evaluation_days",
            name="uq_decision_outcomes_decision_window"))
    op.create_index(
        "ix_decision_outcomes_decision_id", "decision_outcomes",
        ["decision_id"], unique=False)
    op.create_table(
        "decision_reviews",
        sa.Column("decision_id", sa.UUID(), nullable=False),
        sa.Column("review_text", sa.Text(), nullable=True),
        sa.Column("lessons_learned", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False),
        sa.ForeignKeyConstraint(["decision_id"], ["investment_decisions.id"],
                                name=op.f("fk_decision_reviews_decision_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decision_reviews")))
    op.create_index(
        "ix_decision_reviews_decision_id", "decision_reviews", ["decision_id"],
        unique=False)


def downgrade() -> None:
    op.drop_index(
        "ix_decision_reviews_decision_id", table_name="decision_reviews")
    op.drop_table("decision_reviews")
    op.drop_index(
        "ix_decision_outcomes_decision_id", table_name="decision_outcomes")
    op.drop_table("decision_outcomes")
    op.drop_index(
        "ix_recommendation_tiers_status", table_name="recommendation_tiers")
    op.drop_index(
        "ix_recommendation_tiers_recommendation_id",
        table_name="recommendation_tiers")
    op.drop_table("recommendation_tiers")
    op.drop_table("recommendation_metrics")
    op.drop_index(
        "ix_memory_references_analysis_id", table_name="memory_references")
    op.drop_table("memory_references")
    op.drop_index(
        "ix_kg_relations_target_entity_id", table_name="kg_relations")
    op.drop_index(
        "ix_kg_relations_source_entity_id", table_name="kg_relations")
    op.drop_table("kg_relations")
    op.drop_index(
        op.f("ix_investment_decisions_user_id"),
        table_name="investment_decisions")
    op.drop_index(
        "ix_investment_decisions_recommendation_id",
        table_name="investment_decisions")
    op.drop_table("investment_decisions")
    op.drop_index(
        "ix_debate_arguments_debate_session_id", table_name="debate_arguments")
    op.drop_table("debate_arguments")
    op.drop_index("ix_agent_results_agent_run_id", table_name="agent_results")
    op.drop_table("agent_results")
    op.drop_table("technical_indicator_cache")
    op.drop_index("ix_retrieval_logs_analysis_id", table_name="retrieval_logs")
    op.drop_table("retrieval_logs")
    op.drop_index("ix_reflections_analysis_id", table_name="reflections")
    op.drop_table("reflections")
    op.drop_index(
        "ix_recommendations_analysis_id", table_name="recommendations")
    op.drop_table("recommendations")
    op.drop_table("price_bars")
    op.drop_index(
        "ix_episodic_memories_ticker_created_at",
        table_name="episodic_memories")
    op.drop_table("episodic_memories")
    op.drop_index(
        "ix_debate_sessions_analysis_id", table_name="debate_sessions")
    op.drop_table("debate_sessions")
    op.drop_index(
        "ix_benchmark_results_backtest_id", table_name="benchmark_results")
    op.drop_table("benchmark_results")
    op.drop_index(
        "ix_backtest_trades_backtest_id", table_name="backtest_trades")
    op.drop_table("backtest_trades")
    op.drop_index(
        "ix_backtest_positions_backtest_id", table_name="backtest_positions")
    op.drop_table("backtest_positions")
    op.drop_table("backtest_metrics")
    op.drop_index(
        "ix_analysis_sources_analysis_id", table_name="analysis_sources")
    op.drop_table("analysis_sources")
    op.drop_index(
        "ix_analysis_citations_analysis_id", table_name="analysis_citations")
    op.drop_table("analysis_citations")
    op.drop_index("ix_agent_runs_analysis_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_agent_id", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_agent_reports_ticker", table_name="agent_reports")
    op.drop_index("ix_agent_reports_analysis_id", table_name="agent_reports")
    op.drop_table("agent_reports")
    op.drop_table("agent_metrics")
    op.drop_index(
        "ix_agent_memories_agent_id_score", table_name="agent_memories")
    op.drop_table("agent_memories")
    op.drop_table("ticker_names")
    op.drop_table("rag_collections")
    op.drop_table("kg_entities")
    op.drop_table("instruments")
    op.drop_index("ix_embedding_jobs_document_id", table_name="embedding_jobs")
    op.drop_table("embedding_jobs")
    op.drop_index(
        "ix_document_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_table("benchmarks")
    op.drop_index("ix_backtests_user_id_created_at", table_name="backtests")
    op.drop_table("backtests")
    op.drop_index("ix_analyses_user_id_created_at", table_name="analyses")
    op.drop_index("ix_analyses_ticker_created_at", table_name="analyses")
    op.drop_table("analyses")
    op.drop_table("agents")
