from unittest.mock import patch

from app.workers import ingestion_tasks
from app.workers.celery_app import celery_app


def test_ingestion_task_uses_pipeline_runner():
    with patch.object(ingestion_tasks, "SessionLocal") as sessions, patch.object(
        ingestion_tasks, "run_ingestion_job"
    ) as run:
        db = sessions.return_value
        result = ingestion_tasks.ingest_knowledge_document.run("job_1")
    run.assert_called_once_with(db, ingestion_tasks.worker_settings, "job_1")
    db.close.assert_called_once()
    assert result == {"jobId": "job_1", "status": "COMPLETED"}


def test_ingestion_task_is_routed_to_dedicated_queue():
    route = celery_app.conf.task_routes[
        "app.workers.ingestion_tasks.ingest_knowledge_document"
    ]
    assert route["queue"] == "mindbridge.ingestion"
