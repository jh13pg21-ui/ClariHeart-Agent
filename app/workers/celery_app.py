from datetime import timedelta

from celery import Celery
from celery.schedules import crontab
from kombu import Exchange, Queue

from app.core.config import get_settings


settings = get_settings()
event_exchange = Exchange(settings.rabbitmq_exchange, type="topic", durable=True)

celery_app = Celery(
    "mindbridge",
    broker=settings.rabbitmq_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks", "app.workers.ingestion_tasks"],
)
celery_app.conf.update(
    broker_transport_options={"confirm_publish": True},
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_default_delivery_mode="persistent",
    worker_prefetch_multiplier=1,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_queues=(
        Queue(
            settings.celery_general_queue,
            exchange=event_exchange,
            routing_key=settings.celery_general_queue,
            durable=True,
        ),
        Queue(
            settings.celery_alert_queue,
            exchange=event_exchange,
            routing_key=settings.celery_alert_queue,
            durable=True,
        ),
        Queue(
            settings.rag_ingestion_queue,
            exchange=event_exchange,
            routing_key=settings.rag_ingestion_queue,
            durable=True,
        ),
    ),
    task_routes={
        "app.workers.tasks.process_excel": {
            "queue": settings.celery_general_queue,
            "routing_key": settings.celery_general_queue,
        },
        "app.workers.tasks.create_case": {
            "queue": settings.celery_general_queue,
            "routing_key": settings.celery_general_queue,
        },
        "app.workers.tasks.extract_long_term_memory": {
            "queue": settings.celery_general_queue,
            "routing_key": settings.celery_general_queue,
        },
        "app.workers.tasks.refresh_conversation_summary": {
            "queue": settings.celery_general_queue,
            "routing_key": settings.celery_general_queue,
        },
        "app.workers.tasks.consolidate_long_term_memory": {
            "queue": settings.celery_general_queue,
            "routing_key": settings.celery_general_queue,
        },
        "app.workers.tasks.scan_memory_dreams": {
            "queue": settings.celery_general_queue,
            "routing_key": settings.celery_general_queue,
        },
        "app.workers.tasks.purge_expired_private_data": {
            "queue": settings.celery_general_queue,
            "routing_key": settings.celery_general_queue,
        },
        "app.workers.tasks.send_high_risk_alert": {
            "queue": settings.celery_alert_queue,
            "routing_key": settings.celery_alert_queue,
        },
        "app.workers.ingestion_tasks.ingest_knowledge_document": {
            "queue": settings.rag_ingestion_queue,
            "routing_key": settings.rag_ingestion_queue,
        },
    },
    beat_schedule={
        "scan-memory-dreams": {
            "task": "app.workers.tasks.scan_memory_dreams",
            "schedule": timedelta(
                minutes=max(1.0, settings.memory_consolidation_beat_interval_minutes)
            ),
        },
        "purge-expired-private-data-daily": {
            "task": "app.workers.tasks.purge_expired_private_data",
            "schedule": crontab(hour=3, minute=20),
        },
    },
)
