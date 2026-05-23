# TriagePilot

AI submission triage agent for commercial P&C underwriters. Accepts a broker submission package (ACORD forms, loss runs, photos, etc.), runs it through a multi-stage AI pipeline, and returns a structured risk assessment with appetite score, broker questions, and a 1-page risk brief — in seconds.

---

## What It Does

An underwriter uploads submission documents (PDFs, images) and optionally some structured form data. TriagePilot:

1. Parses every document into structured data
2. Computes underwriting metrics deterministically (no LLM math)
3. Runs those metrics through a rule-based appetite engine
4. Searches its appetite guide knowledge base for relevant policy language
5. Has an LLM write narratives and broker questions grounded in the computed facts
6. Produces a 1-page risk brief and a referral note if needed

The key design principle: **LLMs write text, Python does the math.** All loss ratios, claim counts, and dollar figures are calculated in pure Python before any LLM sees them.

---

## Architecture

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                           CLIENT / FRONTEND                                  ║
║               POST /api/submissions  (files + form_data)                     ║
╚═══════════════════════════╦════════════════════════════════════════════════╝
                            ║ PDFs · Images · Excel + Company/Incident JSON
                            ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║                     FASTAPI  —  Auth + Validation                            ║
║        JWT (Supabase) · filename dedup · size check · format check           ║
╚═══════════════════════════╦════════════════════════════════════════════════╝
                            ║
               ┌────────────┴────────────┐
               ▼                         ▼
   ┌───────────────────┐    ╔═══════════════════════════════╗
   │  Supabase Storage │    ║     LANGGRAPH PIPELINE         ║
   │  (file archive)   │    ╚══════════════╦════════════════╝
   └───────────────────┘                   ║
                                           ▼
                            ╔══════════════════════════════╗
                            ║  STAGE 1 — CLASSIFY           ║
                            ║  (skipped by default)         ║
                            ║  LlamaParse preview           ║
                            ║  + LLM vision                 ║  ◄── Claude / GPT-4o
                            ║  → DocType + confidence       ║
                            ║  (acord · loss_run · legal…)  ║
                            ╚══════════════╦═══════════════╝
                                           ║
                                           ▼
                            ╔══════════════════════════════╗
                            ║  STAGE 2 — EXTRACT            ║
                            ║  ┌──────────────────────┐    ║
                            ║  │ PDF → LlamaParse      │    ║  ◄── LlamaCloud API
                            ║  │   ACORD: agentic mode │    ║
                            ║  │   Other: fast mode    │    ║
                            ║  │   → Markdown          │    ║
                            ║  ├──────────────────────┤    ║
                            ║  │ Image → Base64        │    ║  ◄── Qwen (OpenRouter)
                            ║  │   → Structured JSON   │    ║
                            ║  ├──────────────────────┤    ║
                            ║  │ Excel → openpyxl      │    ║
                            ║  │   → Markdown table    │    ║
                            ║  └──────────────────────┘    ║
                            ║  Classify + extract per doc   ║  ◄── Claude / GPT-4o
                            ║  All docs run in parallel     ║
                            ║  3-tier loss history dedup    ║
                            ║  (claim# · year+amt · fuzzy)  ║
                            ║  Merge → ExtractionResult:    ║
                            ║    company · locations        ║
                            ║    loss_history · coverages   ║
                            ║    prior_insurance · legal    ║
                            ║    stated_total_premium       ║
                            ║    stated_total_incurred      ║
                            ║    data_conflicts             ║
                            ╚══════════════╦═══════════════╝
                                           ║
                                           ▼
                            ╔══════════════════════════════╗
                            ║  STAGE 3 — ANALYZE  (No LLM) ║
                            ║                              ║
                            ║  analytics_service.py:       ║
                            ║  · Deduplicate loss events   ║
                            ║  · Resolve total_incurred    ║
                            ║    (double-count detection)  ║
                            ║  · Resolve total_premium     ║
                            ║    (loss run → prior ins →   ║
                            ║     text scan fallback)      ║
                            ║  · Compute loss_ratio        ║
                            ║  · Freq / severity trends    ║
                            ║  · Causation per event       ║
                            ║  · Subrogation potential     ║
                            ║  · Carrier non-renewal       ║
                            ║  · Property analytics (TIV)  ║
                            ║                              ║
                            ║  rules_engine.py:            ║
                            ║  · Decline triggers          ║
                            ║  · Referral triggers         ║
                            ║  · Overrides (mitigations)   ║
                            ║  · Positive signals          ║
                            ║  · Mitigation-weighted score ║
                            ║  → appetite_score (1–5)      ║
                            ║  → appetite_status           ║
                            ║  → winnability / priority    ║
                            ║  → _uw_facts  (canonical)    ║
                            ╚══════════════╦═══════════════╝
                                           ║
                               ┌───────────┴───────────┐
                               ▼                        ▼
                ╔═════════════════════╗   ╔════════════════════════╗
                ║  STAGE 4 — RETRIEVE ║   ║  Pinecone Vector DB     ║
                ║  Build queries from ║   ║  (appetite guides ·     ║
                ║  extraction results ║◄──║   policy wordings ·     ║
                ║  5 targeted queries ║   ║   UW guides)            ║
                ║  Hybrid dense+sparse║──►║  alpha=0.7 dense        ║
                ║  → RetrievedChunk[] ║   ║  alpha=0.3 sparse       ║
                ╚══════════╦══════════╝   ╚════════════════════════╝
                           ║
                           ▼
                ╔═════════════════════╗
                ║  STAGE 5 — EVALUATE ║
                ║  Input: _uw_facts   ║  ◄── Claude / GPT-4o
                ║  + rule signals     ║      (tool-calling loop
                ║  + retrieved chunks ║       up to 5 rounds)
                ║  Tool calls:        ║
                ║  · Search Pinecone  ║──► Pinecone
                ║  · Validate sources ║
                ║  → Narrative per    ║
                ║    signal           ║
                ║  → Broker questions ║
                ║  → Recommended queue║
                ╚══════════╦══════════╝
                           ║
                           ▼
                ╔═════════════════════╗
                ║  STAGE 6 — BRIEF    ║
                ║  Input: _uw_facts   ║  ◄── Claude / GPT-4o
                ║  + _analytics       ║
                ║  + retrieved chunks ║
                ║  → risk_brief.md    ║
                ║  → referral_note    ║
                ║  → citations[]      ║
                ╚══════════╦══════════╝
                           ║
                           ▼
                ╔═════════════════════╗
                ║  STAGE 7 — COMPLETE ║
                ║  Bundle all results ║
                ║  → SubmissionOutput ║
                ╚══════════╦══════════╝
                           ║
               ┌───────────┴───────────┐
               ▼                       ▼
   ╔═══════════════════════╗   ╔═══════════════════════════════╗
   ║  Supabase PostgreSQL   ║   ║  API Response to Client       ║
   ║  · submissions table   ║   ║                               ║
   ║  · audit_log table     ║   ║  SubmissionOutput:            ║
   ║    (every stage logged) ║   ║  · appetite_score (1–5)      ║
   ╚═══════════════════════╝   ║  · appetite_status            ║
                               ║  · rule_results[]             ║
                               ║  · risk_brief_markdown        ║
                               ║  · referral_note              ║
                               ║  · citations[]                ║
                               ║  · broker_questions[]         ║
                               ║  · winnability_score          ║
                               ║  · priority_score             ║
                               ║  · missing_information[]      ║
                               ╚═══════════════════════════════╝
```

---

## Pipeline Stages

### 1. Classify (`ClassifierAgent`)
Skipped by default (`SKIP_CLASSIFICATION=true`). When enabled, sends each document to the LLM to identify its type: ACORD form, loss run, broker submission, legal docs, incident photo, etc.

PDFs → LlamaParse → markdown preview → LLM  
Images → Qwen 2.5 VL (via OpenRouter) → doc type + confidence

### 2. Extract (`ExtractorAgent`)
The most complex stage. Extracts all structured data from every document in parallel.

**Parse step:**
- ACORD forms (detected by filename or first-page text) → LlamaParse agentic/premium mode (handles complex form layouts)
- All other PDFs → LlamaParse fast mode
- Excel files → openpyxl → markdown table (no API call)
- PyMuPDF is the fallback if LlamaParse fails

**Extract step (per document):**
- Quick classify from first 1,500 chars of markdown
- Focused extraction prompt based on doc type (loss run prompt ≠ ACORD prompt ≠ legal prompt)
- All PDFs and images run in parallel via `asyncio.gather`

**Merge step:**
Combines extractions from all documents into one `ExtractionResult`:
- Company fields: first non-null value wins
- Loss history: **3-tier deduplication** — exact claim number → same year + exact amount → same year + amount within 10% + same LOB. Loss run records always override broker/ACORD records on any match.
- Data conflicts: broker-reported claims that don't match the loss run are flagged and turned into mandatory broker questions.
- Loss run summary figures (total premium, total incurred) are used directly — trusted as carrier-computed.
- Form data submitted via the API is merged in after document extraction.

### 3. Analyze (pure Python — no LLM)
`analytics_service.py` and `rules_engine.py` run entirely in Python. Nothing is estimated by an LLM here.

**`compute_analytics` calculates:**
- Loss events (deduplicates claims by date — same-day claims = one event)
- Total incurred (detects and corrects double-count pattern where summary row was also extracted as a claim)
- Total premium (uses loss run stated value directly; falls back to prior insurance records, then text scan)
- Loss ratio and loss ratio excluding largest claim
- Frequency trend (declining / stable / increasing) and severity trend
- Clean years (calendar years with zero claims)
- Subrogation potential (keyword scan on claim descriptions)
- Fire suppression effectiveness
- Causation pattern per event (electrical, kitchen fire, premises liability, theft, etc.) → systemic if same cause repeats 3+ times or 2+ times and dominates >60%
- Carrier non-renewal (from `cancelled_by_carrier` flags + broker note keywords)
- Property analytics: TIV, oldest building, renovation years, vacancy, coinsurance, fire station distance

**`evaluate_rules` applies UW rules:**

| Category | Examples |
|---|---|
| **Decline** | Loss ratio > 200%, 5+ claims with 2+ open, business < 1 year, non-renewed by 2+ carriers |
| **Refer** | Loss ratio 50–200%, prior carrier non-renewal, open reserves > $100K, 3+ claims, revenue > $50M, requested limit > $5M, property > 40 years with no updates, ACV valuation on large building, vacancy > 30%, historical landmark |
| **Override** | Loss ratio ex-largest < 30%, subrogation potential, suppression worked, 3+ clean years |
| **Positive** | Business 3+ years, multi-line opportunity, all locations sprinklered, declining claim trend, formal safety program |

Mitigation-weighted decisioning: decline triggers carry a numeric weight; overrides reduce that weight; the adjusted weight determines the final status (decline / refer / refer_with_conditions).

A canonical `_uw_facts` object is built here — **this is the single source of truth** for all downstream LLM stages. No LLM is allowed to recalculate or invent any figure.

### 4. Retrieve (`RetrieverAgent`)
Searches the Pinecone appetite guide knowledge base using hybrid search (70% dense semantic + 30% sparse keyword by default).

Builds 5 targeted queries from the extraction: industry/class, coverage limits, loss history thresholds, property/construction type, and state-specific requirements. Returns top-20 unique chunks ranked by score.

### 5. Evaluate (`EvaluatorAgent`)
LLM with **tool-calling**. Supports Anthropic and Azure OpenAI tool-calling loops (up to 5 rounds).

Receives:
- The pre-computed `_uw_facts` object with exact numbers
- All rule signals (triggers, overrides, positives) with their `rule_id`s
- Retrieved appetite guide chunks

Writes:
- A 2–3 sentence **narrative** for every signal, naming the company and citing specific figures
- 3–5 **broker questions** grounded in actual findings (not generic)
- A **recommended queue** (preferred-commercial / standard-commercial / specialty / large-account / referral-senior-uw / decline-review)

Source citations are validated — only filenames that actually exist in the submission or Pinecone results are accepted. Hallucinated filenames are rejected.

### 6. Brief (`BriefWriterAgent`)
LLM writes the final 1-page risk brief in markdown and (if referral required) a concise referral note for a senior underwriter.

Both are constrained to the `_uw_facts` object — the grounded facts block is injected at the very top of the prompt so the LLM sees the authoritative numbers first.

---

## Data Flow: Submission

```
POST /api/submissions
  files=[acord.pdf, loss_run.pdf, photo.jpg]
  form_data={company, business_description, incidents}

1. Auth check (Supabase JWT)
2. File validation (extension + size)
3. Upload to Supabase Storage
4. run_pipeline(documents, files, form_data)
   a. LlamaParse → markdown (parallel)
   b. LLM extract per doc (parallel)
   c. Merge + dedup → ExtractionResult
   d. compute_analytics() → pure Python metrics
   e. evaluate_rules() → triggers/overrides/positives/score
   f. Pinecone hybrid search → retrieved chunks
   g. LLM evaluator + tool calls → narratives, queue, questions
   h. LLM brief writer → risk_brief_markdown, referral_note
5. Persist to Supabase submissions table
6. Return SubmissionOutput JSON
```

## Data Flow: Appetite Guide Ingestion

```
POST /api/appetite/ingest
  files=[uw_guide.pdf, policy_wording.pdf]

1. LlamaParse → markdown
2. Structure-aware chunking (ChunkingService)
3. Embed each chunk (Azure OpenAI text-embedding-3-small)
4. Sparse encode (term-frequency hash)
5. Upsert to Pinecone (dense + sparse + metadata)
```

---

## External Services

| Service | Purpose |
|---|---|
| **Supabase** | Auth (JWT), file storage, submissions DB, audit log |
| **Pinecone** | Hybrid vector search over appetite guides |
| **LlamaParse** | PDF → structured markdown (ACORD agentic, others fast) |
| **Anthropic / Azure OpenAI** | Reasoning, extraction, brief generation |
| **OpenRouter (Qwen 2.5 VL)** | Image extraction (incident photos, property photos) |
| **Azure OpenAI** | Embeddings (text-embedding-3-small) |

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/submissions` | Upload files + form data, run full pipeline |
| `GET` | `/api/submissions` | List user's submissions |
| `GET` | `/api/submissions/{id}` | Get a submission result |
| `POST` | `/api/appetite/ingest` | Ingest reference docs into Pinecone |
| `GET` | `/health` | Health check |

### `POST /api/submissions`

**Form fields:**
- `files` — PDF, PNG, JPG, JPEG, TIFF (max 50 MB each)
- `form_data` — JSON string (optional):

```json
{
  "company": {
    "name": "Acme Corp",
    "naics_code": "722511",
    "annual_revenue": 3000000,
    "headcount": 45,
    "year_established": 2015
  },
  "business_description": "Full-service restaurant in downtown Austin",
  "property_description": "2-story brick building, built 1990, fully sprinklered",
  "incidents": [
    {"date": "2023-06-15", "location": "Kitchen", "description": "Grease fire, suppressed by hood system"}
  ]
}
```

**Response:** `SubmissionOutput` with appetite score (1–5), status (accept / refer / decline), risk brief markdown, broker questions, and all extracted facts.

---

## Project Structure

```
app/
├── main.py                  # FastAPI app, CORS, router
├── api/
│   └── routes.py            # Endpoints: submissions, appetite ingest
├── agents/
│   ├── pipeline.py          # LangGraph graph: classify → extract → analyze → retrieve → evaluate → brief → complete
│   ├── classifier.py        # Document type classifier
│   ├── extractor.py         # Parallel extraction + 3-tier loss dedup + merge
│   ├── retriever.py         # Pinecone RAG queries
│   ├── evaluator.py         # LLM evaluator with tool-calling (Anthropic + Azure)
│   ├── brief_writer.py      # Risk brief + referral note generation
│   ├── tools.py             # Tool definitions for evaluator tool-calling
│   ├── tool_executor.py     # Tool execution (Pinecone search)
│   └── prompts.py           # All LLM prompt templates
├── services/
│   ├── analytics_service.py # Pure Python UW metrics (loss ratio, trend, causation, etc.)
│   ├── rules_engine.py      # Deterministic appetite rules (triggers, overrides, positives)
│   ├── parse_service.py     # LlamaParse + openpyxl + PyMuPDF fallback
│   ├── chunking_service.py  # Structure-aware markdown chunking for ingestion
│   ├── vector_service.py    # Pinecone hybrid search (dense + sparse)
│   ├── llm_service.py       # LLM abstraction (Anthropic / Azure / OpenRouter)
│   └── supabase_service.py  # Storage + DB client
├── core/
│   ├── config.py            # Pydantic settings (env vars)
│   └── auth.py              # Supabase JWT verification
├── models/
│   └── schemas.py           # All Pydantic models (ExtractionResult, SubmissionOutput, etc.)
└── utils/
    └── document_processor.py  # PDF page splitting, image base64
```

---

## Setup

### Environment Variables

Copy `.env.example` to `.env` and fill in:

```bash
# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-anon-key
SUPABASE_SERVICE_KEY=your-service-role-key

# Pinecone
PINECONE_API_KEY=your-pinecone-key
PINECONE_INDEX=triagepilot-appetite

# LLM Providers
ANTHROPIC_API_KEY=your-anthropic-key       # for reasoning + brief
OPENROUTER_API_KEY=your-openrouter-key     # for image extraction (Qwen)
AZURE_OPENAI_ENDPOINT=...                  # for embeddings
AZURE_OPENAI_API_KEY=...

# Document parsing
LLAMA_CLOUD_API_KEY=your-llamaparse-key

# Models (defaults)
REASONING_MODEL=claude-sonnet-4-20250514
EXTRACTION_MODEL=qwen/qwen2.5-vl-72b-instruct
EMBEDDING_DEPLOYMENT=text-embedding-3-small
```

### Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Docker

```bash
docker build -t triagepilot .
docker run -p 8000:8000 --env-file .env triagepilot
```

---

## Key Design Decisions

**Deterministic first.** Loss ratios, claim counts, trend analysis — all computed in Python before the LLM sees the data. This prevents hallucinated numbers and ensures auditability.

**Single source of truth.** The `_uw_facts` object built in `analyze_node` is injected into every LLM prompt. The LLM is explicitly told not to recalculate any figure.

**Loss run is authoritative.** Claims from carrier loss runs override broker/ACORD-reported claims. Mismatches become mandatory broker questions.

**Tool-calling evaluator.** The evaluator LLM can call Pinecone search tools mid-response to look up specific policy language for a signal it's narrating — rather than relying only on pre-retrieved chunks.

**Source validation.** Every source citation from the LLM is checked against the real list of uploaded filenames and retrieved chunk source names. Hallucinated filenames are silently dropped.