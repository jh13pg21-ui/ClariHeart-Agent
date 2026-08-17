from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_environment: str = "production"
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    auth_secure_cookie: bool = True
    auth_cookie_samesite: str = "lax"
    langgraph_retry_base_seconds: float = 0.2
    langgraph_retry_max_seconds: float = 2.0
    langgraph_retry_jitter_ratio: float = 0.25
    langgraph_checkpointer: str = "disabled"
    langgraph_checkpoint_database_url: str = ""
    langgraph_aes_key: str = ""
    langgraph_checkpoint_auto_setup: bool = False
    langgraph_checkpoint_pool_min_size: int = 1
    langgraph_checkpoint_pool_max_size: int = 10
    langgraph_checkpoint_ttl_seconds: int | None = 604800
    langgraph_node_timeout_seconds: float = 70.0
    langgraph_node_max_attempts: int = 2
    langsmith_tracing_enabled: bool = False
    langsmith_project: str = "mindbridge"
    langsmith_allow_content: bool = False
    sse_resume_enabled: bool = False
    agent_model_default_provider: str = ""
    agent_model_default_model: str = ""
    agent_model_understanding_provider: str = ""
    agent_model_understanding_model: str = ""
    agent_model_safety_provider: str = ""
    agent_model_safety_model: str = ""
    agent_model_context_provider: str = ""
    agent_model_context_model: str = ""
    agent_model_response_provider: str = ""
    agent_model_response_model: str = ""
    ai_provider: str = "ollama"
    ai_temperature: float = 0.35
    ai_max_tokens: int = 512
    model_context_window_default: int = 8192
    model_ollama_context_window: int = 32768
    model_ollama_max_output_tokens: int = 4096
    model_ollama_tokenizer_kind: str = "conservative"
    model_ollama_tokenizer_path: str = ""
    model_openai_context_window: int = 128000
    model_openai_max_output_tokens: int = 16384
    model_openai_tokenizer_kind: str = "tiktoken"
    model_recovery_reserve_tokens: int = 1024
    model_provider_safety_margin_ratio: float = 0.10
    model_token_estimator_safety_multiplier: float = 1.05
    model_cloud_fallback_enabled: bool = True
    model_recovery_max_transient_retries: int = 2
    model_recovery_max_stream_retries: int = 1
    model_recovery_deadline_seconds: float = 65.0
    model_recovery_base_delay_seconds: float = 0.5
    model_recovery_max_delay_seconds: float = 32.0
    model_recovery_jitter_ratio: float = 0.25
    model_recovery_max_continuations: int = 2
    model_stream_release_chars: int = 256
    model_gateway_enabled: bool = True
    recovery_orchestrator_enabled: bool = True
    prompt_registry_enabled: bool = True
    context_planner_enabled: bool = True
    context_planner_shadow_mode: bool = False
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "mindbridge-qwen2.5-7b-ft:latest"
    finetuned_model_name: str = "mindbridge-qwen2.5-7b-ft:latest"
    finetuned_model_dir: str = "models/mindbridge-qwen2.5-7b-ft"
    finetuned_model_file: str = "mindbridge-qwen2.5-7b-ft-q4_k_m.gguf"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    database_url: str = "mysql+pymysql://mindbridge:mindbridge@127.0.0.1:3306/mindbridge?charset=utf8mb4"
    chat_history_limit: int = 10
    knowledge_top_k: int = 4
    knowledge_candidate_k: int = 16
    knowledge_chunk_size: int = 512
    knowledge_chunk_overlap: int = 64
    knowledge_hybrid_vector_weight: float = 0.65
    knowledge_hybrid_bm25_weight: float = 0.35
    knowledge_rerank_enabled: bool = True
    knowledge_vector_enabled: bool = True
    knowledge_vector_required: bool = False
    chroma_persist_dir: str = "data/chroma"
    chroma_collection_name: str = "mindbridge_knowledge"
    chroma_snapshot_dir: str = "data/chroma-snapshots"
    chroma_snapshot_keep: int = 5
    embedding_timeout_seconds: float = 30.0
    rag_eval_dataset: str = "app/rag_eval/mindbridge-rag-eval.json"
    rag_eval_output: str = "target/rag-eval-report.json"
    rag_eval_enabled: bool = False
    rag_eval_exit_after_run: bool = False
    rag_ingestion_enabled: bool = True
    rag_ingestion_queue: str = "mindbridge.ingestion"
    rag_ingestion_max_file_size_bytes: int = 52_428_800
    rag_ingestion_max_pages: int = 500
    rag_ingestion_max_attempts: int = 3
    rag_pipeline_fingerprint: str = "rag-v3"
    rag_artifact_dir: str = "data/knowledge-artifacts"
    rag_artifact_temp_retention_hours: int = 24
    rag_parser_provider: str = "liteparse"
    rag_liteparse_ocr_enabled: bool = False
    rag_page_render_dpi: int = 150
    rag_ocr_enabled: bool = True
    rag_ocr_provider: str = "paddleocr"
    rag_ocr_device: str = "cpu"
    rag_ocr_worker_concurrency: int = 1
    rag_vision_enabled: bool = True
    rag_vision_provider: str = "openai_compatible"
    rag_vision_model: str = "gpt-5.6-luna"
    rag_vision_detail: str = "original"
    rag_vision_base_url: str = ""
    rag_vision_api_key: str = ""
    rag_vision_timeout_seconds: float = 90.0
    rag_vision_max_attempts: int = 2
    rag_private_cloud_vision_default: bool = False
    rag_child_target_tokens: int = 400
    rag_child_min_tokens: int = 120
    rag_child_max_tokens: int = 650
    rag_child_overlap_tokens: int = 60
    rag_parent_target_tokens: int = 1200
    rag_parent_max_tokens: int = 1800
    risk_eval_dataset: str = "app/risk_eval/mindbridge-risk-eval.json"
    risk_eval_output: str = "target/risk-eval-report.json"
    excel_path: str = "data/mindbridge-risk-ledger.xlsx"
    redis_url: str = "redis://127.0.0.1:6379/0"
    rabbitmq_url: str = "amqp://mindbridge:mindbridge@127.0.0.1:5672//"
    rabbitmq_exchange: str = "mindbridge.events"
    celery_result_backend: str = "redis://127.0.0.1:6379/1"
    celery_general_queue: str = "mindbridge.general"
    celery_alert_queue: str = "mindbridge.alert"
    outbox_publisher_batch_size: int = 50
    outbox_publisher_poll_seconds: float = 1.0
    outbox_publisher_max_attempts: int = 10
    redis_memory_ttl_seconds: int = 86400
    redis_memory_max_messages: int = 40
    redis_socket_timeout_seconds: float = 2.0
    memory_compaction_enabled: bool = True
    memory_compaction_recent_messages: int = 8
    memory_summary_refresh_messages: int = 4
    memory_summary_max_chars: int = 500
    memory_summary_llm_enabled: bool = True
    memory_summary_llm_attempts: int = 2
    memory_summary_max_source_messages: int = 40
    memory_summary_input_max_chars: int = 12000
    skill_semantic_selection_enabled: bool = True
    skill_semantic_selection_max_optional: int = 2
    long_term_memory_enabled: bool = True
    long_term_memory_max_items: int = 200
    long_term_memory_relevant_items: int = 5
    long_term_memory_extract_messages: int = 10
    long_term_memory_extract_min_new_messages: int = 6
    memory_v2_enabled: bool = True
    memory_v2_shadow_mode: bool = False
    memory_consolidation_enabled: bool = True
    memory_consolidation_min_interval_hours: float = 24.0
    memory_consolidation_scan_interval_minutes: float = 60.0
    memory_consolidation_min_active_memories: int = 10
    memory_consolidation_min_modified_sessions: int = 5
    memory_consolidation_lease_seconds: int = 3600
    memory_consolidation_scan_batch_size: int = 100
    memory_consolidation_beat_interval_minutes: float = 15.0
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: float = 10.0
    alert_email_delivery_mode: str = "log"
    alert_email_from: str = ""
    alert_email_to: str = ""
    alert_email_subject_prefix: str = "[MindBridge 高风险预警]"
    alert_email_rate_limit_per_minute: int = 30
    tool_task_max_attempts: int = 5
    mcp_allowed_actors: str = "admin,counselor"
    privacy_store_original_input: bool = False
    privacy_require_encryption_at_rest: bool = False
    sensitive_data_encryption_key: str = ""
    chat_data_retention_days: int = 365
    risk_data_retention_days: int = 1095
    privacy_retention_enabled: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    @property
    def effective_rag_vision_base_url(self) -> str:
        return (self.rag_vision_base_url or self.openai_base_url).rstrip("/")

    @property
    def effective_rag_vision_api_key(self) -> str:
        return self.rag_vision_api_key or self.openai_api_key

    def validate_auth_configuration(self) -> None:
        if self.jwt_algorithm != "HS256":
            raise ValueError("JWT_ALGORITHM 必须为 HS256")
        if self.app_environment.lower() != "test" and len(self.jwt_secret_key.encode("utf-8")) < 32:
            raise ValueError("非测试环境必须通过 JWT_SECRET_KEY 提供至少 32 字节的 JWT 密钥")
        if self.privacy_require_encryption_at_rest and not self.sensitive_data_encryption_key:
            raise ValueError("启用静态加密要求后必须配置 SENSITIVE_DATA_ENCRYPTION_KEY")
        if self.sensitive_data_encryption_key:
            from app.services.data_protection import SensitiveTextProtector

            SensitiveTextProtector(self)
        if self.langgraph_checkpointer.strip().lower() == "postgres":
            if not self.langgraph_checkpoint_database_url:
                raise ValueError("生产 LangGraph Checkpointer 必须配置 LANGGRAPH_CHECKPOINT_DATABASE_URL")
            if len(self.langgraph_aes_key.encode("utf-8")) not in {16, 24, 32}:
                raise ValueError("LANGGRAPH_AES_KEY 必须为 16、24 或 32 字节")


@lru_cache
def get_settings() -> Settings:
    return Settings()
