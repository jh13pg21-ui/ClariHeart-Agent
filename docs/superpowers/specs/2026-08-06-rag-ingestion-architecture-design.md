# MindBridge RAG 文档摄取架构设计

## 1. 背景与结论

MindBridge 当前的知识摄取由 `KnowledgeService` 同时承担文件解析、固定窗口切块、数据库写入、Embedding 和向量索引同步。PDF 仅通过 `pypdf` 提取纯文本，随后按 512 字符、64 字符重叠切块。该实现适合验证检索链路，但无法可靠处理扫描页、双栏阅读顺序、表格、漫画、信息图、标题层级、页码引用和增量更新。

本设计以新的独立摄取子系统替换这条链路。现有聊天 Agent、混合检索、BM25 降级路径和 Chroma 查询接口在第一阶段保持兼容；`KnowledgeService` 最终只保留知识查询与索引管理职责，不继续承担文档解析。

核心决策是采用二维页面路由：

- 文字证据轴决定使用 PDF 原生文本、PaddleOCR 或两者融合。
- 结构理解轴决定使用本地规则或云端 Vision Model。

“是否需要 OCR”和“是否需要视觉理解”是两个独立问题。原生文字完整的双栏页仍可能需要 Vision 修复阅读顺序；扫描但版式简单的文字页可以只使用 PaddleOCR，不必调用 Vision。

## 2. 目标

新摄取子系统必须实现以下结果：

1. 为 PDF、Markdown 和 TXT 提供可插拔解析器边界；本阶段只完整实现这三类输入。
2. 使用 LiteParse 2.11.1 进行 PDF 原生文本、坐标、页面图像和低成本特征提取，默认关闭其 Tesseract OCR。
3. 使用本地 PaddleOCR 作为唯一默认 OCR Provider。
4. 使用 OpenAI-compatible 云端接口和 `gpt-5.6-luna` 处理复杂视觉页面。
5. 生成统一、可验证、带页码与坐标溯源的 Canonical Document JSON。
6. 使用结构感知的父文档—子 Chunk 模型替代固定字符滑窗。
7. 通过独立 Celery 摄取队列异步执行解析、OCR、Vision、融合、切块、Embedding 和索引激活。
8. 提供稳定文档 ID、版本哈希、稳定 Chunk ID、幂等重试、去重和原子版本激活。
9. 对公开内置资料和未来私有上传资料实施不同的云端发送策略。
10. 使用五份 PDF 的 270 页逐页路由标注与代表页深度标注作为验收基线。

## 3. 非目标

本次实现不包括以下扩展：

- 不重写聊天 Agent runtime、风险识别或回复生成流程。
- 不在第一阶段引入 Office 文档解析、音视频知识源或网页爬取。
- 不实现本地 Vision Language Model；Vision Provider 保留接口，首个实现使用云端 API。
- 不把 PaddleOCR 的 PP-Structure 或 PaddleOCR-VL 作为新的完整文档解析框架；PaddleOCR 首先只承担文字检测和识别。
- 不在第一阶段替换 Chroma、OpenAI Embedding 或现有 BM25 算法。
- 不实现人工标注管理 UI；黄金数据集以版本化 JSON 文件维护。
- 不直接把 LiteParse Markdown、PaddleOCR Markdown 或 Vision Markdown 当作最终文档真相。

## 4. 总体架构

```text
文件上传或内置知识同步
        │
        ▼
文件校验、SHA-256、访问级别、文档版本
        │
        ▼
LiteParse --no-ocr
原生文本、bbox、页面渲染、图片和页面特征
        │
        ├───────────────┐
        ▼               ▼
文字证据路由          结构理解路由
NATIVE/OCR/HYBRID     LOCAL/VISION
        │               │
        ├───────┬───────┘
                ▼
       PaddleOCR / GPT Vision
                │
                ▼
          Page Evidence Fusion
                │
                ▼
       Canonical Document JSON
                │
                ▼
        Parent / Child Chunking
                │
                ▼
       Embedding + Chroma + BM25
                │
                ▼
           原子激活新版本
```

新代码位于独立包中：

```text
app/rag_ingestion/
├── schema.py
├── pipeline.py
├── routing.py
├── fusion.py
├── chunking.py
├── artifacts.py
├── errors.py
├── parsers/
│   ├── base.py
│   ├── liteparse.py
│   └── text.py
├── ocr/
│   ├── base.py
│   └── paddle.py
├── vision/
│   ├── base.py
│   └── openai_compatible.py
└── evaluation/
    ├── dataset.py
    ├── metrics.py
    └── runner.py
```

每个模块只依赖其公开协议：Parser 产生基础页面证据，Router 产生决策，OCR/Vision 产生补充证据，Fusion 生成 Canonical Page，Chunker 只读取 Canonical Document。

## 5. Provider 边界

### 5.1 DocumentParser

`DocumentParser` 接收不可变的文件引用和解析上下文，输出包含页面、原生文本块、bbox、页面尺寸、图片引用和可选页面渲染的 `ParseEvidence`。Parser 不执行 Chunking、Embedding 或数据库激活。

PDF 实现使用 LiteParse Python binding 2.11.1，并明确关闭内置 OCR。Markdown/TXT 实现保留标题、段落、列表和代码块结构，不再先压缩为空白归一化的单段文本。

### 5.2 OcrProvider

`OcrProvider` 接收单页或裁剪区域的标准化图片，返回：

- 原始识别文本；
- 文本行与多边形坐标；
- 每行置信度；
- OCR 模型、版本和运行参数；
- 耗时和失败分类。

首个实现使用本地 PaddleOCR，依赖固定为 `paddleocr==3.7.0` 和 `paddlepaddle==3.3.1`，使用该版本的通用 OCR pipeline，并把实际解析出的检测模型名、识别模型名和参数记录进 parser run。模型在 worker 启动时加载一次，不在每页重新初始化。CPU worker 默认并发为 1，防止多个进程同时加载模型导致内存失控。Docker 使用独立摄取 worker 和独立依赖文件，不把 PaddleOCR 强制加载到 FastAPI Web 进程。

### 5.3 VisionProvider

首个实现是 `OpenAICompatibleVisionProvider`，调用已配置中转站的 `/v1/chat/completions`。模型默认 `gpt-5.6-luna`，图片使用 `detail: original`。

实际兼容性探测结果如下：

- `/v1/models` 可访问并返回 `gpt-5.6-luna`；
- 真实 PDF 页面图片输入成功，模型能识别中文政策通知；
- 请求接受 `response_format=json_schema`，但实际返回字段没有遵循所给 Schema。

因此 Provider 不能把 HTTP 200 或 `json_schema` 参数被接受视为结构正确。所有输出必须经过本地 Pydantic 严格验证。第一次失败后使用同一图片和更精简的协议提示重试一次；提示明确规定字段名是不可翻译的协议标识。第二次失败后页面进入 `VISION_SCHEMA_INVALID`，不得将未经验证的内容写入 Canonical Document。

中转站的模型 ID 仅代表供应商声明。质量判定只依据本项目黄金评测，不依据模型名称推断。

## 6. Canonical Document JSON

Canonical JSON 使用内部 `snake_case` 字段，是解析结果的唯一真相。Markdown、纯文本预览和用于模型上下文的格式均由它派生。

### 6.1 文档对象

```json
{
  "schema_version": "1.0",
  "document_id": "doc_01J...",
  "version_id": "docver_01J...",
  "source": {
    "filename": "心理健康知识手册.pdf",
    "mime_type": "application/pdf",
    "sha256": "...",
    "size_bytes": 2507885,
    "access_class": "BUILTIN_PUBLIC",
    "cloud_vision_allowed": true
  },
  "parser_run": {
    "pipeline_version": "rag-ingestion-v1",
    "router_version": "page-router-v1",
    "chunker_version": "structure-chunker-v1",
    "liteparse_version": "2.11.1",
    "ocr_provider": "paddleocr",
    "ocr_model": "PP-OCR",
    "vision_provider": "openai_compatible",
    "vision_model": "gpt-5.6-luna"
  },
  "pages": []
}
```

文档 ID 表示逻辑知识源，版本 ID 表示某次内容哈希。相同逻辑来源上传相同 SHA-256 时直接复用已完成版本，不重复解析、OCR、Vision 或 Embedding。

### 6.2 页面对象

```json
{
  "page_number": 8,
  "geometry": {
    "width_px": 1240,
    "height_px": 1754,
    "rotation": 0
  },
  "route": {
    "text_strategy": "NATIVE",
    "structure_strategy": "VISION",
    "reasons": ["multi_column", "reading_order_risk"],
    "router_version": "page-router-v1"
  },
  "quality": {
    "native_text_score": 0.92,
    "ocr_confidence": null,
    "layout_complexity": 0.81,
    "visual_semantic_need": 0.62,
    "final_page_score": 0.90
  },
  "blocks": []
}
```

### 6.3 Block 对象

每个 Block 至少包含：

- `block_id`：版本内稳定 ID；
- `type`：`title`、`heading`、`paragraph`、`list`、`list_item`、`table`、`table_cell`、`figure`、`caption`、`comic_panel`、`equation`、`header`、`footer`、`page_number`；
- `reading_order`：页面内从 0 开始的确定顺序；
- `bbox_norm`：`[x0, y0, x1, y1]`，坐标归一化到 `0.0–1.0`；
- `text`：最终可检索精确文本；
- `section_path`：从文档标题到当前章节的标题路径；
- `parent_block_id` 和 `child_block_ids`；
- `confidence`；
- `provenance`：每条原生、OCR、Vision 证据及对应坐标；
- `table`：表格行列数、单元格、rowspan、colspan 和单元格 bbox；
- `figure`：资源引用、图注、OCR 文本和视觉说明。

所有 bbox 都关联页面像素宽高，禁止混用 PDF point、OCR 像素和 Vision 坐标。Provider Adapter 在边界处完成坐标转换。

## 7. 页面路由

### 7.1 文字证据轴

路由器计算原生字符数、字符密度、乱码比例、可见内容覆盖率、文本对象数量、图片覆盖率和 LiteParse complexity reasons。

- `NATIVE`：原生文本充足、乱码低且与可见内容覆盖一致。
- `PADDLE_OCR`：无文本、文本稀疏、乱码高或页面主要由栅格图组成。
- `HYBRID`：原生文本基本可靠，但图片区域包含可能影响语义的文字。

LiteParse `needs_ocr` 只作为一个输入特征。它不能直接决定路由，因为现有样本已出现纯文本政策页误报和双栏阅读顺序漏报。

### 7.2 结构理解轴

路由器计算列聚类、文本框交叉、阅读顺序异常、矢量横纵线密度、表格候选、图片占比、图注候选、漫画面板候选和信息图特征。

- `LOCAL`：单栏、连续段落、简单标题与列表，可以由本地规则稳定恢复。
- `VISION`：双栏或多栏、复杂表格、图表、信息图、漫画、强图文关系、阅读顺序不确定。

如果页面需要 Vision 但来源不允许云端发送，页面状态变为 `NEEDS_REVIEW`，而不是自动上传或伪装成本地解析成功。

### 7.3 初始质量门

初始阈值作为版本化配置进入 `page-router-v1`，由黄金评测调整：

- 原生文本评分不低于 0.85 且布局复杂度低于 0.35，可走 `NATIVE + LOCAL`。
- 原生文本评分低于 0.65，至少运行 PaddleOCR。
- 布局复杂度或视觉语义需求不低于 0.55，路由到 Vision。
- PaddleOCR 平均置信度低于 0.82，或 OCR 后页面覆盖仍明显不足，在允许云端时升级到 Vision。
- 任何表格、漫画、信息图强信号不因 OCR 置信度高而取消 Vision。

阈值不是业务常量，必须与 `router_version` 一起记录，便于回放和比较。

## 8. 证据融合

融合遵循以下优先级和约束：

1. 可信原生 PDF 文本优先作为精确字符来源。
2. PaddleOCR 补充栅格区域或损坏文字层，不无条件覆盖原生文本。
3. Vision 决定复杂页的 Block 类型、阅读顺序、表格关系、图文关系和视觉说明。
4. Vision 不得在缺少页面证据时改写或补造政策条款、数字、热线、疾病名称和处置步骤。
5. 原生文本和 OCR 冲突时，两种证据都保留在 provenance 中，由区域覆盖、OCR 置信度、乱码率和字符一致率选择最终文本。
6. Vision 表格结构中的单元格文本优先与原生/OCR 证据对齐；无法对齐的值降低置信度并进入质量报告。
7. 每个最终 Block 必须能追溯到页码和 bbox；无来源的正文不得进入检索 Chunk。
8. 页眉、页脚和页码保留在 Canonical JSON，但默认不进入检索正文。

## 9. 结构感知 Chunk

Chunker 只读取 Canonical Document，不读取 Parser、OCR 或 Vision 的原始输出。

### 9.1 父子模型

- `parent` 表示完整章节、连续主题区域、完整表格或图文单元。
- `child` 表示用于 Embedding、BM25 和 rerank 的检索单元。
- 只索引 active 版本的 child Chunk。
- 命中 child 后返回其 parent 内容，并根据上下文预算附加相邻 child，而不是按 `source_index ± 1` 盲目扩展。

### 9.2 初始切分参数

- child 目标 400 tokens，最小 120，最大 650；
- 连续段落间重叠不超过 60 tokens；
- parent 目标 1200 tokens，最大 1800；
- 标题、列表项、漫画面板、图注和短表格不从中间切断；
- 长表格按完整数据行分块，每块重复表头并保存原表 Block ID；
- 中文优先在句末标点、分号、段落和标题边界切分，不从词语中间截断；
- 图片或漫画 child 同时包含视觉说明、图中文字、图注和章节路径，但不注入无证据推断。

稳定 Chunk ID 由逻辑文档 ID、版本化结构位置、Block ID 范围、Chunk 类型和标准化内容摘要生成。相同版本重复执行必须产生相同 ID。

## 10. 持久化模型

新增以下表：

### 10.1 `knowledge_documents`

保存逻辑文档 ID、来源键、显示名称、MIME、访问级别、云 Vision 权限、当前 active 版本 ID 和审计时间。

### 10.2 `knowledge_document_versions`

保存版本 ID、文档 ID、SHA-256、文件大小、页数、状态、pipeline fingerprint、Canonical Artifact 路径和前一版本 ID。`document_id + sha256` 唯一。

### 10.3 `knowledge_ingestion_jobs`

保存公开任务 ID、文档版本、当前 stage、状态、进度页数、尝试次数、错误代码、脱敏错误信息、开始/结束时间和触发者。相同版本同一时间最多有一个活动任务。

### 10.4 `knowledge_pages`

保存版本 ID、页码、几何信息、二维路由结果、质量评分、页面状态、页面图像 Artifact 路径和页面 Canonical JSON。JSON 字段使用 MySQL `LONGTEXT`，避免普通 `TEXT` 的容量限制。

### 10.5 扩展 `knowledge_chunks`

保留现有主键以兼容检索调用，新增：

- `stable_id`；
- `document_id` 和 `document_version_id`；
- `parent_chunk_id`；
- `chunk_kind`；
- `page_start`、`page_end`；
- `section_path_json`；
- `block_ids_json`；
- `content_hash`；
- `embedding_model`、`embedding_dimension`；
- `active`。

迁移后旧数据作为 `LEGACY_TEXT` 文档版本保留，直到新摄取版本成功激活。禁止在数据库迁移期间删除现有 Chunk。

## 11. Artifact 存储

本地开发与当前 Docker 部署使用：

```text
data/knowledge-artifacts/{document_id}/{version_sha256}/
├── source.bin
├── document.json
└── pages/
    ├── 0001.png
    ├── 0001.native.json
    ├── 0001.ocr.json
    └── 0001.vision.json
```

路径只使用系统生成 ID 和哈希，不直接使用上传文件名，防止路径穿越和编码问题。写入使用临时文件加原子重命名。失败任务的临时目录按配置保留短期诊断窗口后清理；active 版本所需 Artifact 不自动删除。

## 12. 异步摄取与原子激活

新增独立 Celery 队列 `mindbridge.ingestion` 和 `worker-ingestion`。FastAPI 上传只负责验证、落盘、创建文档版本与任务，并返回 HTTP 202。

任务阶段如下：

```text
PENDING
→ EXTRACTING
→ ROUTING
→ OCR_RUNNING
→ VISION_RUNNING
→ FUSING
→ CHUNKING
→ INDEXING
→ ACTIVATING
→ COMPLETED
```

终止状态为 `FAILED`、`NEEDS_REVIEW` 或 `CANCELLED`。

每个阶段以文档版本和 pipeline fingerprint 为幂等键。重试时读取已完成页面 Artifact，不重复调用已成功的 OCR 或 Vision。网络超时、429 和 5xx 可重试；Schema 不合法只允许一次协议重试；权限禁止、文件损坏和内容超限不可重试。

Chroma 与 MySQL 不能组成单一事务，因此采用“先构建、后激活”：

1. 新 Chunk 以 `active=false` 写入数据库；
2. 生成 Embedding 并写入 Chroma，metadata 包含文档版本和 stable ID；
3. 校验数据库 Chunk 数、Embedding 数和 Chroma ID 集一致；
4. 在 MySQL 事务内将新版本和新 Chunk 激活，并停用旧版本；
5. 检索层只返回数据库中 active 的 Chunk；
6. 激活成功后异步清理旧 Chroma 向量和过期 Artifact。

任一前置步骤失败时，旧 active 版本继续服务。

## 13. API 与兼容层

新增管理接口：

- `POST /api/admin/knowledge/files`：上传并返回文档 ID、版本 ID、任务 ID 和状态；
- `GET /api/admin/knowledge/jobs/{job_id}`：查询阶段、进度和脱敏错误；
- `GET /api/admin/knowledge/documents`：列出逻辑文档及 active 版本；
- `GET /api/admin/knowledge/documents/{document_id}`：查看版本、页面路由和质量摘要；
- `POST /api/admin/knowledge/jobs/{job_id}/retry`：只重试允许重试的失败任务；
- `GET /api/admin/knowledge/documents/{document_id}/pages/{page_number}`：返回页面 Canonical JSON 和受控预览引用。

现有 `POST /api/admin/knowledge/file` 在过渡期调用新服务并返回兼容响应，同时增加任务 ID；前端由“正在切分入库”改为异步任务进度。文本 `POST /api/admin/knowledge` 继续支持内置或管理员直接输入内容，但也通过新的 Text Parser 和 Chunker。

`SearchResult` 增加可选 citation metadata，包括文档 ID、文件名、页码范围、Block ID 和 section path。现有调用只读取 `content`、`source` 和 `score` 时保持兼容。

## 14. 配置

新增配置采用以下默认值：

```text
RAG_INGESTION_ENABLED=true
RAG_INGESTION_QUEUE=mindbridge.ingestion
RAG_ARTIFACT_DIR=data/knowledge-artifacts
RAG_ARTIFACT_TEMP_RETENTION_HOURS=24
RAG_PARSER_PROVIDER=liteparse
RAG_LITEPARSE_OCR_ENABLED=false
RAG_PAGE_RENDER_DPI=150
RAG_OCR_ENABLED=true
RAG_OCR_PROVIDER=paddleocr
RAG_OCR_DEVICE=cpu
RAG_OCR_WORKER_CONCURRENCY=1
RAG_VISION_ENABLED=true
RAG_VISION_PROVIDER=openai_compatible
RAG_VISION_MODEL=gpt-5.6-luna
RAG_VISION_DETAIL=original
RAG_VISION_TIMEOUT_SECONDS=90
RAG_VISION_MAX_ATTEMPTS=2
RAG_PRIVATE_CLOUD_VISION_DEFAULT=false
RAG_CHILD_TARGET_TOKENS=400
RAG_CHILD_MIN_TOKENS=120
RAG_CHILD_MAX_TOKENS=650
RAG_CHILD_OVERLAP_TOKENS=60
RAG_PARENT_TARGET_TOKENS=1200
RAG_PARENT_MAX_TOKENS=1800
```

Vision Base URL 和 API Key 可以单独配置；未设置时读取现有 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。密钥值不写入文档、状态接口、任务参数、日志或数据库。

## 15. 隐私与安全

访问级别至少包含：

- `BUILTIN_PUBLIC`：仓库内置且经审核的公开资料，允许云 Vision；
- `ADMIN_PRIVATE`：管理员上传资料，默认禁止云 Vision；
- `RESTRICTED`：禁止云 Vision，并限制 Artifact 和引用查看权限。

学生聊天、心理报告、长期记忆和用户上传的个人材料不得通过知识摄取 Vision Provider 发送。私有文档只有管理员显式设置 `cloud_vision_allowed=true` 才能发送；该动作写入安全审计。

第三方中转站不能自动继承 OpenAI 官方数据保留、ZDR、区域和合规承诺，因此系统不依赖 `store=false` 作为隐私边界。真正的边界是来源分类、显式授权和调用前策略检查。

日志只记录任务 ID、文档版本、页码、Provider、模型、耗时、状态码、重试次数和脱敏错误分类。禁止记录 API Key、Authorization Header、图片 Base64、完整 Vision prompt、OCR 全文或 Canonical 正文。

## 16. 可观测性

每个任务和页面记录：

- 各阶段耗时；
- LiteParse、PaddleOCR 和 Vision 版本；
- 二维路由决策及原因；
- OCR 平均与最低置信度；
- Vision 请求次数、响应状态和本地 Schema 校验结果；
- 原生/OCR/Vision 文本一致率；
- Block 数、Chunk 数、Embedding 数；
- API usage 字段；中转站不返回 usage 时至少记录请求次数和图片像素；
- 降级、重试、人工复核和旧版本继续服务原因。

管理状态页显示聚合指标，不暴露文档正文或敏感错误。

## 17. 五份 PDF 黄金评测

评测语料为 `app/knowledge/pdf` 中五份 PDF，共 270 页：

- 压力缓解漫画：133 页；
- 心理健康知识手册：99 页；
- 危机干预实施方案：7 页；
- 双列预防资料：19 页；
- 一图读懂：12 页。

黄金数据分两层：

### 17.1 全量逐页路由标注

全部 270 页标注：

- 页面类型；
- `text_strategy`；
- `structure_strategy`；
- 路由原因；
- 是否允许云端；
- 是否存在表格、双栏、图片文字、漫画或信息图；
- 页面是否应该进入检索正文。

### 17.2 代表页深度标注

选择约 30 页覆盖所有页面类型，标注：

- 阅读顺序；
- 关键文本；
- 标题路径；
- Block 类型和 bbox；
- 表格单元格与行列关系；
- 图片、漫画面板、气泡文字和图注关系；
- 可回答的 RAG 问题与证据页。

深度页数量固定为 30；如果新增页面类型，用替换方式保持首版规模，后续通过数据集版本扩展。

### 17.3 验收门槛

- Vision 必需页召回率不低于 98%；
- 简单原生页误送 Vision 比例不高于 15%；
- Canonical JSON Schema 合法率 100%；
- 深度页关键文字召回率不低于 95%；
- 双栏阅读顺序正确率不低于 95%；
- 表格关键单元格关系正确率不低于 90%；
- 引用页码命中率 100%；
- 原有 RAG Recall@K、MRR、NDCG 和 HitRate 不低于当前基线；
- 任一页面失败不得导致半成品版本进入 active 索引。

同时对 `gpt-5.6-luna` 记录页面级质量、延迟、请求数和中转站账单。模型质量不达门槛时，通过 Vision Provider 配置替换模型，不修改 Parser、Fusion 或 Chunker 协议。

## 18. 测试策略

### 18.1 单元测试

- Canonical Pydantic Schema 的合法与非法样例；
- bbox 坐标转换；
- 原生文字质量评分；
- 二维路由决策；
- OCR 与原生文字去重；
- Vision 字段名翻译、缺字段、拒答和无效 JSON；
- 表格融合；
- 中文句界切分；
- 父子 Chunk 关系和稳定 ID；
- 云端权限策略；
- 文件路径和哈希校验。

### 18.2 集成测试

- LiteParse 真实 PDF 页面解析；
- PaddleOCR 真实扫描页；
- Vision Provider 使用录制响应和显式 opt-in 的在线 smoke test；
- Celery 任务幂等重试；
- MySQL 迁移和旧 Chunk 兼容；
- Chroma 新版本预写、激活和旧版本清理；
- FastAPI 上传、状态查询和重试。

### 18.3 回归测试

- 270 页路由评测；
- 30 页深度解析评测；
- 现有 RAG 评测集；
- 现有聊天、风险、安全门、权限和异步任务测试。

在线 Vision 测试默认不在普通单元测试中运行，必须显式设置开关，防止 CI 意外计费。录制 fixture 不包含 Authorization Header 或原始敏感文档。

## 19. 发布与回滚

发布采用双轨切换：

1. 新表、模块和 worker 上线，但检索仍使用旧 active Chunk；
2. 对五份 PDF 执行离线摄取和评测；
3. 达到验收门槛后按文档逐个激活新版本；
4. 观察 RAG 回归、Vision 成本和失败率；
5. 全部稳定后停止 `pypdf + 固定字符滑窗` 新摄取入口；
6. 旧 Chunk 在保留窗口后删除。

回滚只需把文档的 active 版本指回上一版本并恢复其 Chunk active 状态。Canonical Artifact 和上一版向量在回滚窗口内保留，回滚不需要重新 OCR 或调用 Vision。

## 20. 完成标准

实现完成必须同时满足：

1. 新摄取不再调用 `KnowledgeService.extract_pdf` 或固定字符 `chunk_text`。
2. 五份 PDF 可以通过异步任务完整进入 Canonical JSON、父子 Chunk 和索引。
3. 每个召回 Chunk 能返回文档、页码、章节和 Block 引用。
4. PaddleOCR 在本地摄取 worker 中运行，Web 进程不加载 OCR 模型。
5. `gpt-5.6-luna` 只接收策略允许的公开页面，返回结果经过本地严格校验。
6. 任一中途失败不影响现有 active 知识版本。
7. 迁移、单元测试、集成测试、现有 Harness 和 RAG 评测全部通过。
8. README 的旧版 RAG 后续演进清单更新为实际架构、配置、运行方式、隐私边界和评测命令。
