(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.MindBridgeKnowledgeUI = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  const STATUS = {
    PENDING: { label: "排队中", tone: "queued" },
    RUNNING: { label: "处理中", tone: "running" },
    COMPLETED: { label: "已入库", tone: "completed" },
    FAILED: { label: "处理失败", tone: "failed" },
    NEEDS_REVIEW: { label: "等待授权", tone: "review" }
  };

  const STAGES = {
    PENDING: "等待 Worker 接收",
    EXTRACTING: "解析文档与页面",
    ROUTING: "评估页面处理路线",
    OCR_RUNNING: "PaddleOCR 提取文字",
    VISION_RUNNING: "Vision 理解页面结构",
    FUSING: "融合页面证据",
    CHUNKING: "生成结构化分块",
    INDEXING: "写入 Embedding 与向量库",
    ACTIVATING: "校验并激活新版本",
    COMPLETED: "已完成并可用于检索"
  };

  function statusMeta(status) {
    return STATUS[String(status || "").toUpperCase()] || {
      label: status || "未知状态",
      tone: "unknown"
    };
  }

  function stageLabel(stage) {
    return STAGES[String(stage || "").toUpperCase()] || stage || "等待状态更新";
  }

  function progressPercent(job) {
    if (job?.status === "COMPLETED") return 100;
    const current = Number(job?.progressPage || 0);
    const total = Number(job?.totalPages || 0);
    if (total <= 0) return 0;
    return Math.max(0, Math.min(99, Math.round((current / total) * 100)));
  }

  function progressLabel(job) {
    const total = Number(job?.totalPages || 0);
    if (job?.status === "COMPLETED") return "全部页面处理完成";
    if (total <= 0) return stageLabel(job?.stage);
    return `${stageLabel(job?.stage)} · ${Number(job?.progressPage || 0)} / ${total} 页`;
  }

  function canRetry(job) {
    return job?.status === "NEEDS_REVIEW" || (
      job?.status === "FAILED" && job?.error?.retryable === true
    );
  }

  function retryNeedsVision(job) {
    return job?.status === "NEEDS_REVIEW";
  }

  function shouldPoll(jobs) {
    return (jobs || []).some((job) => ["PENDING", "RUNNING"].includes(job.status));
  }

  return {
    statusMeta,
    stageLabel,
    progressPercent,
    progressLabel,
    canRetry,
    retryNeedsVision,
    shouldPoll
  };
});
