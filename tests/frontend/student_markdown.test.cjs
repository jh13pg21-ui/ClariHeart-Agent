const assert = require("node:assert/strict");
const test = require("node:test");

const markdown = require("../../app/static/markdown.js");

test("渲染加粗、编号列表和无序列表", () => {
  const html = markdown.render("1. **任务分解**\n   - 研究目标公司\n   - 完善简历");

  assert.match(html, /<strong>任务分解<\/strong>/);
  assert.match(html, /<ol>/);
  assert.match(html, /<ul>/);
  assert.match(html, /研究目标公司/);
});

test("转义原始 HTML 并拒绝危险链接", () => {
  const html = markdown.render(
    '<img src=x onerror=alert(1)> [危险](javascript:alert(1)) [安全](https://example.com)'
  );

  assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(html, /href="javascript:/);
  assert.match(html, /&lt;img/);
  assert.match(html, /href="https:\/\/example\.com"/);
  assert.match(html, /rel="noopener noreferrer"/);
});

test("编号列表被空行分隔时仍保留原始序号", () => {
  const html = markdown.render("1. 第一步\n\n2. 第二步\n\n3. 第三步");

  assert.match(html, /<ol><li>第一步<\/li><\/ol>/);
  assert.match(html, /<ol start="2"><li>第二步<\/li><\/ol>/);
  assert.match(html, /<ol start="3"><li>第三步<\/li><\/ol>/);
});
