const test = require("node:test");
const assert = require("node:assert/strict");

const knowledgeUI = require("../../app/static/admin-knowledge.js");

test("任务状态与处理阶段使用管理员可读文案", () => {
  assert.deepEqual(knowledgeUI.statusMeta("RUNNING"), {
    label: "处理中",
    tone: "running"
  });
  assert.equal(knowledgeUI.stageLabel("VISION_RUNNING"), "Vision 理解页面结构");
  assert.equal(knowledgeUI.stageLabel("INDEXING"), "写入 Embedding 与向量库");
});

test("页级进度被限制在激活前最多百分之九十九", () => {
  assert.equal(
    knowledgeUI.progressPercent({ status: "RUNNING", progressPage: 50, totalPages: 100 }),
    50
  );
  assert.equal(
    knowledgeUI.progressPercent({ status: "RUNNING", progressPage: 100, totalPages: 100 }),
    99
  );
  assert.equal(knowledgeUI.progressPercent({ status: "COMPLETED" }), 100);
});

test("仅活动任务触发轮询，失败任务按可重试性展示操作", () => {
  assert.equal(knowledgeUI.shouldPoll([{ status: "PENDING" }]), true);
  assert.equal(knowledgeUI.shouldPoll([{ status: "COMPLETED" }]), false);
  assert.equal(
    knowledgeUI.canRetry({ status: "FAILED", error: { retryable: true } }),
    true
  );
  assert.equal(
    knowledgeUI.canRetry({ status: "FAILED", error: { retryable: false } }),
    false
  );
  assert.equal(knowledgeUI.retryNeedsVision({ status: "NEEDS_REVIEW" }), true);
});
