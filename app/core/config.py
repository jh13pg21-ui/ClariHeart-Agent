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
    agent_framework: str = "event_driven_multi_agent"
    agent_max_rounds: int = 8
    agent_max_claims_per_round: int = 4
    agent_max_claims_per_agent: int = 3
    agent_final_acceptance_min_confidence: float = 0.6
    agent_task_timeout_seconds: float = 70.0
    agent_task_max_attempts: int = 2
    agent_retry_base_seconds: float = 0.2
    agent_retry_max_seconds: float = 2.0
    agent_retry_jitter_ratio: float = 0.25
    agent_model_default_provider: str = ""
    agent_model_default_model: str = ""
    agent_model_coordinator_provider: str = ""
    agent_model_coordinator_model: str = ""
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
    skill_semantic_selection_enabled: bool = True
    skill_semantic_selection_max_optional: int = 2
    long_term_memory_enabled: bool = True
    long_term_memory_max_items: int = 200
    long_term_memory_relevant_items: int = 5
    long_term_memory_extract_messages: int = 10
    long_term_memory_extract_min_new_messages: int = 6
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
