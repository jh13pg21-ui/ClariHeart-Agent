"""离线 RAG 摄取评测工具。"""

from app.rag_ingestion.evaluation.dataset import load_deep_pages, load_routing_gold
from app.rag_ingestion.evaluation.metrics import evaluate_deep_pages, evaluate_routing

__all__ = ["load_deep_pages", "load_routing_gold", "evaluate_deep_pages", "evaluate_routing"]
