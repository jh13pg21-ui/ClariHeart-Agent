# Production RAG Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 LiteParse、PaddleOCR、OpenAI-compatible Vision、Canonical Document JSON 和结构感知父子分块，完整替换 MindBridge 的 toy PDF 摄取链路，同时保持现有混合检索兼容。

**Architecture:** 新建 `app/rag_ingestion` 独立包，解析器只生成页面证据，二维 Router 独立决定文字与结构策略，OCR/Vision 作为可替换 Provider，Fusion 产出唯一 Canonical Document，再由 Chunker、持久化服务和异步任务完成索引及原子激活。现有 `KnowledgeService` 收缩为查询与兼容入口，只检索 active child chunk，并在命中后返回 parent 内容及页码引用。

**Tech Stack:** Python 3.12/3.13、FastAPI、SQLAlchemy 2、Alembic、Celery/RabbitMQ、LiteParse 2.11.1、PaddleOCR 3.7.0、PaddlePaddle 3.3.1、Pydantic 2、httpx、ChromaDB、OpenAI-compatible Chat Completions。

## Global Constraints

- LiteParse 固定为 2.11.1，并关闭其 Tesseract OCR；它只提供底层页面证据，不负责最终路由或语义真相。
- PaddleOCR 固定为 `paddleocr==3.7.0` 与 `paddlepaddle==3.3.1`；OCR 模型只在 ingestion worker 中延迟加载一次。
- Vision 默认模型为 `gpt-5.6-luna`，调用 `/v1/chat/completions`，图片 detail 为 `original`，最多两次尝试。
- 中转接口返回的 JSON 必须由本地 Pydantic 严格校验；HTTP 200 与接受 `json_schema` 参数均不表示 Schema 合法。
- `BUILTIN_PUBLIC` 默认允许云 Vision；`ADMIN_PRIVATE` 与 `RESTRICTED` 默认禁止，只有管理员显式授权才能覆盖前者。
- 学生聊天、心理报告、长期记忆和用户个人材料不得进入知识摄取 Vision Provider。
- Canonical Document JSON 是唯一真相；Markdown、纯文本预览和 chunk 均从它派生。
- child chunk 目标/最小/最大为 400/120/650 tokens，重叠不超过 60；parent 目标/最大为 1200/1800。
- 新版本完成索引校验前不得替换 active 版本；任一步失败时旧版本继续服务。
- 文件修改使用 `apply_patch`；不提交 `app/knowledge/pdf` 中的用户 PDF，也不提交 `.env`、模型缓存或运行产物。

---

## File Structure

- `app/rag_ingestion/schema.py`：Canonical、路由、证据、chunk、Vision 响应模型和枚举。
- `app/rag_ingestion/ids.py`：文档、版本、block、chunk 的稳定 ID 与 SHA-256。
- `app/rag_ingestion/errors.py`：可重试/不可重试/人工复核错误分类与脱敏。
- `app/rag_ingestion/artifacts.py`：安全路径、原子 JSON/二进制写入和页面缓存。
- `app/rag_ingestion/parsers/*`：Parser 协议、Markdown/TXT 和 LiteParse no-OCR 适配器。
- `app/rag_ingestion/routing.py`：页面特征、二维路由与版本化阈值。
- `app/rag_ingestion/ocr/*`：OCR 协议与 Paddle 延迟加载适配器。
- `app/rag_ingestion/vision/*`：Vision 协议、提示协议、本地校验与重试。
- `app/rag_ingestion/fusion.py`：原生/OCR/Vision 证据对齐与 Canonical Page 生成。
- `app/rag_ingestion/chunking.py`：中文 token 估算、结构分组、表格按行和父子 chunk。
- `app/rag_ingestion/repository.py`：文档、版本、页面、任务与 chunk 的事务持久化。
- `app/rag_ingestion/pipeline.py`：幂等 stage 编排、检查点恢复、索引校验和原子激活。
- `app/rag_ingestion/service.py`：上传、内置知识同步、状态查询、重试与任务派发。
- `app/rag_ingestion/evaluation/*`：黄金数据加载、指标和离线评测入口。
- `app/models/entities.py`、`migrations/versions/0008_rag_ingestion.py`：数据库模型与兼容迁移。
- `app/services/knowledge.py`、`app/services/vector_store.py`：active child 检索、parent 展开和 citation metadata。
- `app/workers/ingestion_tasks.py`、`app/workers/celery_app.py`：独立 ingestion queue。
- `app/api/knowledge_routes.py`、`app/api/routes.py`：异步管理 API 与旧接口兼容。
- `tests/rag_ingestion/*`：单元、集成和 Provider fixture 测试。
- `app/rag_eval/gold/*`：270 页路由标注与 30 页深度标注的数据格式及种子清单。

### Task 1: 配置、依赖与错误边界

**Files:**
- Modify: `requirements.txt`
- Create: `requirements-dev.txt`
- Modify: `app/core/config.py`
- Create: `requirements-ingestion.txt`
- Create: `app/rag_ingestion/__init__.py`
- Create: `app/rag_ingestion/errors.py`
- Test: `tests/rag_ingestion/test_config_and_errors.py`

**Interfaces:**
- Produces: `Settings.effective_rag_vision_base_url`、`Settings.effective_rag_vision_api_key`、`IngestionError`、`RetryableIngestionError`、`NeedsReviewError`、`sanitize_error(exc) -> tuple[str, str]`。

- [ ] **Step 1: 写失败测试**

```python
def test_vision_credentials_fall_back_to_openai_settings():
    settings = Settings(openai_base_url="https://proxy.example/v1", openai_api_key="secret")
    assert settings.effective_rag_vision_base_url == "https://proxy.example/v1"
    assert settings.effective_rag_vision_api_key == "secret"

def test_sanitize_error_never_returns_secret_or_authorization_header():
    code, message = sanitize_error(RuntimeError("Authorization: Bearer secret-value"))
    assert code == "RUNTIME_ERROR"
    assert "secret-value" not in message
    assert "Authorization" not in message
```

- [ ] **Step 2: 运行测试并确认因配置字段和错误类型不存在而失败**

Run: `.venv\Scripts\python.exe -m unittest tests.rag_ingestion.test_config_and_errors -v`

- [ ] **Step 3: 添加设计稿中的全部 RAG 配置，创建异常分类与脱敏函数**

```python
class IngestionError(RuntimeError):
    code = "INGESTION_ERROR"
    retryable = False

class RetryableIngestionError(IngestionError):
    retryable = True

class NeedsReviewError(IngestionError):
    code = "NEEDS_REVIEW"

def sanitize_error(exc: Exception) -> tuple[str, str]:
    code = getattr(exc, "code", type(exc).__name__.upper())
    message = re.sub(r"(?i)(authorization|api[_ -]?key)\s*[:=]\s*\S+", r"\1=[REDACTED]", str(exc))
    return code, message[:1000]
```

`requirements-ingestion.txt` 精确加入 `liteparse==2.11.1`、`paddleocr==3.7.0`、`paddlepaddle==3.3.1`，主 `requirements.txt` 不强制 Web 镜像安装 Paddle；`requirements-dev.txt` 引用主依赖并加入固定版本的 `pytest` 与 `pytest-httpx`。

- [ ] **Step 4: 运行配置测试**

Run: `.venv\Scripts\python.exe -m unittest tests.rag_ingestion.test_config_and_errors -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add requirements.txt requirements-dev.txt requirements-ingestion.txt app/core/config.py app/rag_ingestion tests/rag_ingestion/test_config_and_errors.py
git commit -m "feat: configure production RAG ingestion"
```

### Task 2: Canonical Schema 与稳定 ID

**Files:**
- Create: `app/rag_ingestion/schema.py`
- Create: `app/rag_ingestion/ids.py`
- Test: `tests/rag_ingestion/test_schema_and_ids.py`

**Interfaces:**
- Produces: `ParseEvidence`、`PageEvidence`、`CanonicalDocument`、`CanonicalPage`、`CanonicalBlock`、`ChunkDraft`、`VisionAnalysisRequest`、`VisionPageResult`、`stable_document_id`、`stable_block_id`、`stable_chunk_id`。

- [ ] **Step 1: 写失败测试，覆盖非法 bbox、重复 reading order、未知 Vision 字段和 ID 幂等性**

```python
def test_block_rejects_out_of_range_bbox():
    with pytest.raises(ValidationError):
        CanonicalBlock(block_id="b1", type="paragraph", reading_order=0,
                       bbox_norm=[0.0, 0.0, 1.2, 1.0], text="正文", provenance=[])

def test_stable_chunk_id_is_repeatable_and_content_sensitive():
    first = stable_chunk_id("doc1", "v1", "child", ["b1"], "心理支持")
    assert first == stable_chunk_id("doc1", "v1", "child", ["b1"], "心理支持")
    assert first != stable_chunk_id("doc1", "v1", "child", ["b1"], "危机干预")
```

- [ ] **Step 2: 运行测试并确认导入失败**

Run: `.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_schema_and_ids.py -q`

- [ ] **Step 3: 用 Pydantic `extra="forbid"` 实现全部 Canonical 模型和枚举**

关键枚举固定为：`TextStrategy(NATIVE, PADDLE_OCR, HYBRID)`、`StructureStrategy(LOCAL, VISION)`、`AccessClass(BUILTIN_PUBLIC, ADMIN_PRIVATE, RESTRICTED)`、`ChunkKind(PARENT, CHILD)`。`bbox_norm` 验证 `0 <= x0 < x1 <= 1` 与 `0 <= y0 < y1 <= 1`；页面验证 reading order 唯一且连续。

- [ ] **Step 4: 用规范化 JSON 与 SHA-256 实现稳定 ID**

```python
def stable_digest(prefix: str, payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"
```

- [ ] **Step 5: 运行 Schema 测试并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_schema_and_ids.py -q`

```powershell
git add app/rag_ingestion/schema.py app/rag_ingestion/ids.py tests/rag_ingestion/test_schema_and_ids.py
git commit -m "feat: add canonical RAG document schema"
```

### Task 3: 数据库模型与兼容迁移

**Files:**
- Modify: `app/models/entities.py`
- Create: `migrations/versions/0008_rag_ingestion.py`
- Modify: `tests/test_migrations.py`
- Create: `tests/rag_ingestion/test_repository_models.py`

**Interfaces:**
- Produces: `KnowledgeDocument`、`KnowledgeDocumentVersion`、`KnowledgeIngestionJob`、`KnowledgePage` ORM 模型；扩展后的 `KnowledgeChunk`。

- [ ] **Step 1: 扩展迁移测试的预期 head 和表/字段断言**

```python
RAG_TABLES = {"knowledge_documents", "knowledge_document_versions", "knowledge_ingestion_jobs", "knowledge_pages"}
assert RAG_TABLES <= set(inspector.get_table_names())
assert {"stable_id", "document_id", "document_version_id", "parent_chunk_id",
        "chunk_kind", "page_start", "page_end", "active"} <= chunk_columns
```

- [ ] **Step 2: 运行迁移测试并确认失败**

Run: `.venv\Scripts\python.exe -m unittest tests.test_migrations.MigrationWorkflowTests -v`

- [ ] **Step 3: 添加 ORM 模型和 0008 迁移**

迁移创建四张新表、唯一约束 `knowledge_documents.source_key`、`(document_id, sha256)`、`(document_version_id, page_number)`、单 active job 的查询索引；为 `knowledge_chunks` 新增 nullable 兼容字段。旧行迁移为 `chunk_kind='LEGACY_TEXT'`、`active=1`，不删除或改写原正文。

- [ ] **Step 4: 添加 SQLite ORM 往返测试，验证 LONGTEXT 在 MySQL 方言映射和外键关系**

Run: `.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_repository_models.py tests/test_migrations.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add app/models/entities.py migrations/versions/0008_rag_ingestion.py tests/test_migrations.py tests/rag_ingestion/test_repository_models.py
git commit -m "feat: persist versioned RAG ingestion state"
```

### Task 4: Artifact Store 与 Markdown/TXT Parser

**Files:**
- Create: `app/rag_ingestion/artifacts.py`
- Create: `app/rag_ingestion/parsers/__init__.py`
- Create: `app/rag_ingestion/parsers/base.py`
- Create: `app/rag_ingestion/parsers/text.py`
- Test: `tests/rag_ingestion/test_artifacts_and_text_parser.py`

**Interfaces:**
- Consumes: Task 2 Schema 与 ID。
- Produces: `ArtifactStore.write_source/write_json/read_json/page_path`、`DocumentParser.parse_bytes`、`TextDocumentParser.parse_bytes`。

- [ ] **Step 1: 写失败测试，验证路径穿越不可达、原子写入、Markdown 标题路径和代码块保持**

```python
def test_artifact_paths_ignore_hostile_filename(tmp_path):
    store = ArtifactStore(tmp_path)
    path = store.write_source("doc_1", "a" * 64, b"payload")
    assert path.resolve().is_relative_to(tmp_path.resolve())

def test_markdown_parser_preserves_heading_path_and_code_block():
    parsed = TextDocumentParser().parse_bytes("guide.md", b"# A\n## B\n```py\nprint(1)\n```")
    assert parsed.pages[0].blocks[-1].section_path == ["A", "B"]
    assert "print(1)" in parsed.pages[0].blocks[-1].text
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_artifacts_and_text_parser.py -q`

- [ ] **Step 3: 实现仅使用系统 ID/哈希的安全路径和 `tempfile + os.replace` 原子写入**

- [ ] **Step 4: 实现 Markdown/TXT parser，不压平换行、标题、列表和代码块**

- [ ] **Step 5: 运行测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_artifacts_and_text_parser.py -q
git add app/rag_ingestion/artifacts.py app/rag_ingestion/parsers tests/rag_ingestion/test_artifacts_and_text_parser.py
git commit -m "feat: add artifact store and text parsers"
```

### Task 5: LiteParse no-OCR PDF Adapter

**Files:**
- Create: `app/rag_ingestion/parsers/liteparse.py`
- Test: `tests/rag_ingestion/test_liteparse_adapter.py`

**Interfaces:**
- Consumes: `DocumentParser`、`ParseEvidence`、`ArtifactStore`。
- Produces: `LiteParseDocumentParser.parse_bytes(filename, data, context) -> ParseEvidence`。

- [ ] **Step 1: 写 contract 测试，patch LiteParse backend 并断言 OCR 明确关闭**

```python
def test_liteparse_adapter_disables_embedded_ocr(fake_backend, artifact_store):
    parser = LiteParseDocumentParser(backend=fake_backend, artifact_store=artifact_store, dpi=150)
    parsed = parser.parse_bytes("a.pdf", PDF_BYTES, context)
    assert fake_backend.calls[0]["ocr"] is False
    assert parsed.pages[0].image_path.endswith("0001.png")
    assert parsed.pages[0].native_blocks[0].bbox_norm == [0.1, 0.1, 0.9, 0.2]
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_liteparse_adapter.py -q`

- [ ] **Step 3: 对 LiteParse 2.11.1 做窄适配层**

适配器必须把 PDF point/bbox 统一转换到页面像素归一化坐标，逐页写入 PNG 与 `native.json`，记录 `needs_ocr`、复杂原因、文本对象数、图片覆盖率、线条密度和版本；LiteParse API 差异只存在于本文件。

- [ ] **Step 4: 使用仓库中的政策 PDF 单页运行离线 smoke，不调用 OCR/Vision**

Run: `.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_liteparse_adapter.py -q -m "not online"`
Expected: 单元与真实 PDF 单页 smoke 均通过。

- [ ] **Step 5: 提交**

```powershell
git add app/rag_ingestion/parsers/liteparse.py tests/rag_ingestion/test_liteparse_adapter.py
git commit -m "feat: extract PDF evidence with LiteParse"
```

### Task 6: 二维页面 Router

**Files:**
- Create: `app/rag_ingestion/routing.py`
- Test: `tests/rag_ingestion/test_routing.py`

**Interfaces:**
- Consumes: `PageEvidence`。
- Produces: `PageRouter.route(page, access_class, cloud_vision_allowed) -> PageRoute`。

- [ ] **Step 1: 写表驱动失败测试**

```python
@pytest.mark.parametrize(("features", "text", "structure"), [
    ({"native_text_score": .92, "layout_complexity": .2}, "NATIVE", "LOCAL"),
    ({"native_text_score": .4, "layout_complexity": .2}, "PADDLE_OCR", "LOCAL"),
    ({"native_text_score": .9, "layout_complexity": .8, "multi_column": True}, "NATIVE", "VISION"),
    ({"native_text_score": .8, "image_text_risk": .8}, "HYBRID", "VISION"),
])
def test_two_axis_routes_are_independent(features, text, structure):
    route = PageRouter().route(make_page(**features), AccessClass.BUILTIN_PUBLIC, True)
    assert route.text_strategy.value == text
    assert route.structure_strategy.value == structure
```

- [ ] **Step 2: 运行测试并确认失败**

- [ ] **Step 3: 实现版本化特征与阈值**

阈值固定为原生文本 0.85/0.65、LOCAL layout 0.35、Vision layout/semantic 0.55、OCR 置信度升级 0.82；表格、漫画、信息图强信号不可被高 OCR 置信度取消。

- [ ] **Step 4: 加入禁止云发送时的 `NEEDS_REVIEW` 决策测试并运行全套**

Run: `.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_routing.py -q`

- [ ] **Step 5: 提交**

```powershell
git add app/rag_ingestion/routing.py tests/rag_ingestion/test_routing.py
git commit -m "feat: route RAG pages across text and structure"
```

### Task 7: PaddleOCR Provider

**Files:**
- Create: `app/rag_ingestion/ocr/__init__.py`
- Create: `app/rag_ingestion/ocr/base.py`
- Create: `app/rag_ingestion/ocr/paddle.py`
- Test: `tests/rag_ingestion/test_paddle_ocr_provider.py`

**Interfaces:**
- Produces: `OcrProvider.recognize(image_path) -> OcrPageEvidence`、`PaddleOcrProvider`。

- [ ] **Step 1: 写失败测试，使用 fake engine 验证模型只初始化一次、坐标归一化与置信度汇总**

```python
def test_paddle_engine_is_lazy_singleton(fake_factory, image_path):
    provider = PaddleOcrProvider(engine_factory=fake_factory, device="cpu")
    provider.recognize(image_path)
    provider.recognize(image_path)
    assert fake_factory.call_count == 1
```

- [ ] **Step 2: 运行测试并确认失败**

- [ ] **Step 3: 适配 PaddleOCR 3.7.0 输出，不在模块导入时加载 Paddle**

Provider 记录实际 pipeline/model/device/version，异常分类为 `OCR_MODEL_LOAD_FAILED`、`OCR_IMAGE_INVALID` 或可重试运行错误；结果写 `ocr.json`，不得记录整页 OCR 文本到日志。

- [ ] **Step 4: 运行 fake 测试和显式 opt-in 的真实中文图片 smoke**

Run: `.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_paddle_ocr_provider.py -q`

- [ ] **Step 5: 提交**

```powershell
git add app/rag_ingestion/ocr tests/rag_ingestion/test_paddle_ocr_provider.py
git commit -m "feat: add local PaddleOCR provider"
```

### Task 8: OpenAI-compatible Vision Provider

**Files:**
- Create: `app/rag_ingestion/vision/__init__.py`
- Create: `app/rag_ingestion/vision/base.py`
- Create: `app/rag_ingestion/vision/openai_compatible.py`
- Create: `tests/fixtures/rag/vision_valid.json`
- Create: `tests/fixtures/rag/vision_translated_keys.json`
- Test: `tests/rag_ingestion/test_vision_provider.py`

**Interfaces:**
- Produces: `VisionProvider.analyze(request: VisionAnalysisRequest) -> VisionPageResult`、`OpenAICompatibleVisionProvider`。

- [ ] **Step 1: 写失败测试，覆盖有效响应、中文翻译字段、缺字段、拒答、429/5xx 与权限策略**

```python
def test_schema_invalid_response_retries_once_then_fails(httpx_mock, provider):
    httpx_mock.add_response(json=translated_key_fixture)
    httpx_mock.add_response(json=translated_key_fixture)
    with pytest.raises(VisionSchemaInvalid):
        provider.analyze(public_request)
    assert len(httpx_mock.get_requests()) == 2

def test_private_page_is_rejected_before_http(provider, private_page):
    with pytest.raises(CloudVisionForbidden):
        provider.analyze(private_request)
    assert provider.client_request_count == 0
```

- [ ] **Step 2: 运行测试并确认失败**

- [ ] **Step 3: 实现 Chat Completions payload 与本地严格校验**

请求必须包含 `model=gpt-5.6-luna`、data URL、`detail=original`、英文不可翻译字段协议和 `response_format.json_schema`；从 content 中剥离可选代码围栏后用 `VisionPageResult.model_validate_json` 校验。第一次 Schema 失败后仅重试一次，并追加“字段名必须原样输出”；第二次失败抛 `VISION_SCHEMA_INVALID`。

- [ ] **Step 4: 实现超时、429、5xx 的指数退避分类和日志脱敏**

- [ ] **Step 5: 运行离线 fixture 测试；在线 smoke 仅在 `RAG_VISION_ONLINE_TEST=true` 时运行**

Run: `.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_vision_provider.py -q`

- [ ] **Step 6: 提交**

```powershell
git add app/rag_ingestion/vision tests/rag_ingestion/test_vision_provider.py tests/fixtures/rag/vision_valid.json tests/fixtures/rag/vision_translated_keys.json
git commit -m "feat: add validated cloud Vision provider"
```

### Task 9: Page Evidence Fusion

**Files:**
- Create: `app/rag_ingestion/fusion.py`
- Test: `tests/rag_ingestion/test_fusion.py`

**Interfaces:**
- Consumes: native blocks、`OcrPageEvidence`、`VisionPageResult`、`PageRoute`。
- Produces: `PageEvidenceFusion.fuse(...) -> CanonicalPage`。

- [ ] **Step 1: 写失败测试，覆盖原生文本优先、OCR 图像文字补充、冲突 provenance、Vision 阅读顺序和表格文本对齐**

```python
def test_vision_changes_structure_but_not_verified_policy_number():
    page = fusion.fuse(native=native_with_number("12345"), ocr=None,
                       vision=vision_with_number("54321"), route=vision_route)
    assert "12345" in page.blocks[0].text
    assert {p.provider for p in page.blocks[0].provenance} == {"native", "vision"}
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_fusion.py -q`

- [ ] **Step 3: 实现 bbox IoU/中心距离对齐、文本相似度、置信度与乱码率选择**

页眉、页脚和页码保留但标记 `searchable=false`；无页面证据的 Vision 正文丢弃；无法对齐的表格值降低置信度并写质量原因。

- [ ] **Step 4: 运行 Fusion 测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_fusion.py -q
git add app/rag_ingestion/fusion.py tests/rag_ingestion/test_fusion.py
git commit -m "feat: fuse native OCR and Vision evidence"
```

### Task 10: 结构感知父子 Chunker

**Files:**
- Create: `app/rag_ingestion/chunking.py`
- Test: `tests/rag_ingestion/test_chunking.py`

**Interfaces:**
- Consumes: `CanonicalDocument`。
- Produces: `StructureAwareChunker.chunk(document) -> list[ChunkDraft]`。

- [ ] **Step 1: 写失败测试，覆盖中文句界、父子关系、稳定 ID、跨页 section、列表/漫画不截断和长表重复表头**

```python
def test_long_table_splits_by_rows_and_repeats_header():
    chunks = StructureAwareChunker(config).chunk(table_document(rows=80))
    children = [item for item in chunks if item.chunk_kind == ChunkKind.CHILD]
    assert len(children) > 1
    assert all("姓名 | 处理方式" in item.content for item in children)
    assert all(item.parent_stable_id for item in children)
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_chunking.py -q`

- [ ] **Step 3: 实现 token 计数协议和中文结构切分**

先按标题路径、表格、figure/comic 单元形成 parent，再在段落/句末边界形成 child；只有超长普通段落可二次按分号和逗号切分，绝不从 Unicode 代理对或中文词中间机械截断。

- [ ] **Step 4: 实现 chunk 内容前缀、页码、section path、block ID 与 parent stable ID**

- [ ] **Step 5: 运行测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_chunking.py -q
git add app/rag_ingestion/chunking.py tests/rag_ingestion/test_chunking.py
git commit -m "feat: add structure-aware parent child chunking"
```

### Task 11: Repository、阶段检查点与 Pipeline

**Files:**
- Create: `app/rag_ingestion/repository.py`
- Create: `app/rag_ingestion/pipeline.py`
- Test: `tests/rag_ingestion/test_repository.py`
- Test: `tests/rag_ingestion/test_pipeline.py`

**Interfaces:**
- Produces: `KnowledgeIngestionRepository`、`IngestionPipeline.run(job_id) -> None`。
- Stage: `PENDING -> EXTRACTING -> ROUTING -> OCR_RUNNING -> VISION_RUNNING -> FUSING -> CHUNKING -> INDEXING -> ACTIVATING -> COMPLETED`。

- [ ] **Step 1: 写 repository 失败测试，验证相同 source+sha 复用版本、同版本只有一个 active job、进度单调**

- [ ] **Step 2: 写 pipeline 失败测试，使用 fake parser/OCR/Vision/index 验证各二维路线只调用所需 Provider**

```python
def test_native_local_page_never_calls_ocr_or_vision(pipeline):
    pipeline.run(job_id)
    assert pipeline.ocr.calls == []
    assert pipeline.vision.calls == []

def test_resume_uses_page_artifacts_without_rebilling_vision(pipeline_with_checkpoint):
    pipeline_with_checkpoint.run(job_id)
    assert pipeline_with_checkpoint.vision.call_count == 0
```

- [ ] **Step 3: 实现事务 repository 与唯一约束防重**

- [ ] **Step 4: 实现逐阶段幂等编排和 artifact 检查点恢复**

每页错误保存脱敏 code/message；`NeedsReviewError` 结束为 `NEEDS_REVIEW`；可重试错误交给 Celery；任何失败均不得运行 ACTIVATING。

- [ ] **Step 5: 实现索引数量/ID 集校验与单事务 active 切换**

- [ ] **Step 6: 运行测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_repository.py tests/rag_ingestion/test_pipeline.py -q
git add app/rag_ingestion/repository.py app/rag_ingestion/pipeline.py tests/rag_ingestion/test_repository.py tests/rag_ingestion/test_pipeline.py
git commit -m "feat: orchestrate resumable RAG ingestion"
```

### Task 12: Active Child 检索、Parent 展开与引用

**Files:**
- Modify: `app/services/vector_store.py`
- Modify: `app/services/knowledge.py`
- Modify: `app/agents/result.py`
- Modify: `app/agents/harness.py`
- Modify: `app/agents/event_driven_runtime.py`
- Create: `tests/rag_ingestion/test_retrieval_compatibility.py`

**Interfaces:**
- Extends: `SearchResult` 新增可选 `document_id`、`filename`、`page_start`、`page_end`、`block_ids`、`section_path`。
- Preserves: 原调用者仍可只读取 `chunk_id/source/content/score`。

- [ ] **Step 1: 写失败测试，验证 inactive/parent 不参与召回、命中 child 后返回 parent、legacy 行仍可检索**

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_retrieval_compatibility.py -q`

- [ ] **Step 3: 修改 BM25/Chroma metadata 与 DB 过滤**

Chroma ID 优先使用 `stable_id`，metadata 包含 `db_id/document_id/document_version_id/chunk_kind/active/page_start/page_end`；查询结果必须再次以数据库 active 状态过滤，避免旧向量短暂泄漏。

- [ ] **Step 4: 用 parent 替代 `source_index ± 1` 邻接扩展并携带 citation metadata**

- [ ] **Step 5: 删除新摄取对模块级 `extract_pdf` 和固定字符 `chunk_text` 的调用；旧函数只保留迁移期兼容，不再作为入口**

- [ ] **Step 6: 运行相关回归并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_retrieval_compatibility.py tests/test_async_agent_runtime.py tests/test_event_driven_multi_agent.py -q`

```powershell
git add app/services/vector_store.py app/services/knowledge.py app/agents tests/rag_ingestion/test_retrieval_compatibility.py
git commit -m "feat: retrieve active child chunks with citations"
```

### Task 13: 摄取服务、Celery 独立队列与管理 API

**Files:**
- Create: `app/rag_ingestion/service.py`
- Create: `app/workers/ingestion_tasks.py`
- Modify: `app/workers/celery_app.py`
- Create: `app/api/knowledge_routes.py`
- Modify: `app/api/routes.py`
- Modify: `app/main.py`
- Test: `tests/rag_ingestion/test_ingestion_service.py`
- Test: `tests/rag_ingestion/test_ingestion_api.py`
- Modify: `tests/test_worker_tasks.py`

**Interfaces:**
- Produces: `KnowledgeIngestionService.submit_file(...)`、Celery task `app.workers.ingestion_tasks.ingest_knowledge_document`。
- API: 设计稿列出的 files/jobs/documents/pages/retry 路由；旧 `/api/admin/knowledge/file` 返回兼容字段外加 `jobId`。

- [ ] **Step 1: 写 service/API 失败测试，验证 MIME/大小、202、私有默认禁云、相同哈希去重和状态脱敏**

```python
def test_upload_returns_202_job_identifiers(admin_client):
    response = admin_client.post("/api/admin/knowledge/files", files={"file": ("guide.pdf", PDF_BYTES, "application/pdf")})
    assert response.status_code == 202
    assert set(response.json()) >= {"documentId", "versionId", "jobId", "status"}
```

- [ ] **Step 2: 运行测试并确认失败**

- [ ] **Step 3: 实现上传落盘、版本创建、`send_task` 派发和查询 DTO**

API 不读取或加载 Paddle；只创建任务。重试端点仅允许 `FAILED` 且 error.retryable=true 的任务，或管理员处理后的 `NEEDS_REVIEW`。

- [ ] **Step 4: 配置 `mindbridge.ingestion` durable queue 与 task route**

- [ ] **Step 5: 运行 API、worker 和权限回归并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_ingestion_service.py tests/rag_ingestion/test_ingestion_api.py tests/test_worker_tasks.py tests/test_admin_api.py -q`

```powershell
git add app/rag_ingestion/service.py app/workers app/api app/main.py tests/rag_ingestion/test_ingestion_service.py tests/rag_ingestion/test_ingestion_api.py tests/test_worker_tasks.py
git commit -m "feat: expose asynchronous knowledge ingestion"
```

### Task 14: 五篇 PDF 黄金数据与评测 Runner

**Files:**
- Create: `app/rag_ingestion/evaluation/__init__.py`
- Create: `app/rag_ingestion/evaluation/dataset.py`
- Create: `app/rag_ingestion/evaluation/metrics.py`
- Create: `app/rag_ingestion/evaluation/runner.py`
- Create: `app/rag_eval/gold/routing-gold-v1.json`
- Create: `app/rag_eval/gold/deep-pages-v1.json`
- Create: `tests/rag_ingestion/test_evaluation.py`

**Interfaces:**
- Produces: `load_routing_gold`、`evaluate_routing`、`evaluate_deep_pages`、CLI `python -m app.rag_ingestion.evaluation.runner`。

- [ ] **Step 1: 写失败测试，固定数据集 Schema、页数总和 270、文件 SHA-256 和指标计算**

```python
def test_routing_gold_covers_every_pdf_page_once():
    dataset = load_routing_gold(path)
    assert sum(len(doc.pages) for doc in dataset.documents) == 270
    assert len({(doc.filename, page.page_number) for doc in dataset.documents for page in doc.pages}) == 270
```

- [ ] **Step 2: 运行测试并确认失败**

- [ ] **Step 3: 生成 270 页机器预标注，再逐页保存确定的路由标签与原因**

五份文档页数固定为 133、99、7、19、12；每页包含 page type、二维路线、原因、云权限、table/multi_column/image_text/comic/infographic/searchable 布尔值。标注文件只保存标签和哈希，不复制 PDF 正文。

- [ ] **Step 4: 建立 30 个代表页深度条目**

每条包含 reading order、关键文本、section path、block/table/figure 关系、RAG 问题和证据页；选择覆盖政策纯文本、双列页、表格页、漫画页、信息图页。

- [ ] **Step 5: 实现并测试门槛指标**

指标输出 Vision recall、simple Vision false-positive rate、Schema valid rate、key text recall、reading order、table relation、citation page hit 和原 RAG 回归差值；任一低于设计门槛 CLI 返回非零。

- [ ] **Step 6: 提交**

```powershell
git add app/rag_ingestion/evaluation app/rag_eval/gold tests/rag_ingestion/test_evaluation.py
git commit -m "test: add five-PDF RAG ingestion evaluation"
```

### Task 15: Docker、内置知识同步、README 与端到端验收

**Files:**
- Modify: `Dockerfile`
- Create: `Dockerfile.ingestion`
- Modify: `docker-compose.yml`
- Modify: `app/core/bootstrap.py`
- Modify: `README.md`
- Modify: `.env.example`
- Create: `tests/rag_ingestion/test_builtin_sync.py`
- Create: `tests/rag_ingestion/test_end_to_end.py`

**Interfaces:**
- Produces: `worker-ingestion` 服务；Markdown 与五篇 PDF 的内置同步命令；最终运维说明。

- [ ] **Step 1: 写失败测试，确保 Web 镜像不安装 Paddle、ingestion 镜像安装专用依赖、bootstrap 不再同步调用 toy ingest**

- [ ] **Step 2: 运行测试并确认失败**

- [ ] **Step 3: 创建 ingestion 镜像并配置单并发 worker**

`Dockerfile.ingestion` 基于 production 系统依赖安装 `requirements-ingestion.txt`；compose 服务命令固定 `--queues=mindbridge.ingestion --concurrency=1`，挂载 `./data:/app/data`，传递 Vision/RAG 配置但不在镜像写入密钥。

- [ ] **Step 4: 将内置 Markdown/PDF 同步改为创建幂等异步版本，不在 Web 启动时解析 PDF**

- [ ] **Step 5: 更新 README**

README 必须解释 LiteParse 的真实职责、二维路由、Canonical JSON、Paddle/Vision 安装启动、隐私边界、API、任务恢复、五 PDF 评测命令、在线 smoke 开关和回滚方法，并删除旧固定字符 chunk 作为推荐架构的描述。

- [ ] **Step 6: 运行离线完整测试、迁移、Harness 和 RAG 回归**

```powershell
.venv\Scripts\python.exe -m pytest tests/rag_ingestion tests/test_migrations.py tests/test_admin_api.py tests/test_worker_tasks.py -q
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m app.harness.runner --json
.venv\Scripts\python.exe -m app.rag_eval.runner
```

- [ ] **Step 7: 显式运行 Paddle 单页和 Vision 单页 smoke，再执行五 PDF 摄取**

```powershell
$env:RAG_OCR_ONLINE_TEST='true'
.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_paddle_ocr_provider.py -q
$env:RAG_VISION_ONLINE_TEST='true'
.venv\Scripts\python.exe -m pytest tests/rag_ingestion/test_vision_provider.py -q
.venv\Scripts\python.exe -m app.rag_ingestion.evaluation.runner --ingest app/knowledge/pdf --output target/rag-ingestion-eval.json
```

验收输出必须逐项报告设计门槛；若中转站或本机资源导致在线任务未完成，保留旧 active 知识并给出具体失败 stage、错误 code 和可恢复命令，不能把未执行描述为通过。

- [ ] **Step 8: 检查密钥与运行产物未被追踪，提交最终集成**

```powershell
git status --short
git diff --check
git grep -n "OPENAI_API_KEY=" -- ':!*.example' ':!.env.example'
git add Dockerfile Dockerfile.ingestion docker-compose.yml app/core/bootstrap.py README.md .env.example tests/rag_ingestion/test_builtin_sync.py tests/rag_ingestion/test_end_to_end.py
git commit -m "feat: ship production RAG ingestion pipeline"
```

## Completion Verification

- [ ] 新 PDF 摄取路径没有调用 `KnowledgeService.extract_pdf`、模块级 `extract_pdf` 或固定字符 `chunk_text`。
- [ ] Web 进程导入图中不存在 `paddle`/`paddleocr`，ingestion worker 只初始化一次模型。
- [ ] 五份 PDF 均生成 `document.json`、逐页证据、parent/child chunk 和 page citation。
- [ ] Vision 请求只覆盖允许云发送且路由为 VISION 的页面；所有响应本地 Schema 合法率为 100%。
- [ ] 失败版本 inactive，旧 active 文档和向量继续可检索。
- [ ] Alembic empty/legacy/MySQL 测试、Python 全套测试、Harness、原 RAG 评测和新黄金评测均有真实输出。
- [ ] `.env`、API Key、PDF 原件、Base64 图片、OCR/Vision 正文日志和 `data/knowledge-artifacts` 未进入 Git。
