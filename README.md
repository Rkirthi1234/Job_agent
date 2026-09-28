# AI Job Agent

An AI-powered job application agent, built step by step.

## Current Phase

**Phase 1 — Resume Intelligence**, **Phase 2 — Job Intelligence**, **Phase 3 —
Job Matching**, and **Phase 4 — Job Discovery** are implemented.

```
Phase 1 — Resume Intelligence     COMPLETE
Phase 2 — Job Intelligence        COMPLETE
Phase 3 — Job Matching            COMPLETE
Phase 4 — Job Discovery           COMPLETE
Phase 5 — Application Agent       NOT STARTED
Phase 6 — Application Tracking    NOT STARTED
```

```
Resume → Extract text → LLM analysis → Structured candidate profile → Store in DB
Job description → LLM analysis → Structured job profile → Store in DB
Candidate Profile + Job Profile → LLM analysis → Structured match result → Store in DB
Job search request → Job source (Mock/Jooble) → Normalized jobs
    → Job Intelligence (Phase 2, reused) → Matching (Phase 3, reused) → Matched jobs
```

Phases 5 and 6 (an application agent and application tracking) are **not
implemented yet**. The architecture is laid out so they can be added on top
without reworking any of the four existing phases.

## Architecture

```
HTTP Request
     ↓
Router               (app/api/routes/resume.py)
     ↓
Resume Service        (app/services/resume_service.py)
     ↓
Document Service      (app/services/document_service.py)
     ↓
Extracted Text
     ↓
Resume Agent          (app/agents/resume_agent.py)
     ↓
LLM Service           (app/services/llm_service.py)
     ↓
Structured Candidate Profile   (app/schemas/candidate.py)
     ↓
Database              (app/models/candidate.py, app/models/database.py)
     ↓
API Response
```

### What each layer is, in simple terms

- **Router** — the "front door". Receives the HTTP request, validates that a
  file was actually sent, calls the service, and returns its result as JSON.
  It contains no business logic — if you deleted everything except the
  router, you wouldn't be able to tell *how* a resume gets processed, only
  *that* an endpoint exists.
- **Service** (`ResumeService`) — the conductor. It knows the *order* of
  steps: validate the file → store it → extract text → ask the agent for a
  profile → save to the database. It doesn't know *how* to extract text or
  *how* to talk to an LLM — it just calls the pieces that do.
- **Agent** (`ResumeAgent`) — the "brain" for this task. It builds the prompt,
  sends resume text to the LLM Service, and validates the response against
  the Pydantic schema. This is the piece you'd swap out if you wanted a
  smarter, multi-step extraction process later.
- **LLM Service** — the only place that knows which AI provider is in use.
  Today it points at a local Ollama model through an OpenAI-compatible
  endpoint. Switching to OpenAI, Azure OpenAI, or OpenRouter later is a
  `.env` change, not a code change.
- **Schema** (Pydantic, `app/schemas/candidate.py`) — the contract for what a
  "candidate profile" looks like. The LLM's raw JSON output is validated
  against this before anything trusts it or saves it.
- **Model** (SQLAlchemy, `app/models/candidate.py`) — the database table
  definition. This is what actually gets stored, separate from the API-facing
  schema.
- **Database** (`app/models/database.py`) — the engine/session setup. SQLite
  today; changing `DATABASE_URL` in `.env` is the only step needed to move to
  PostgreSQL later.

### Phase 2 — Job Intelligence

Same shape as Phase 1, minus a document-extraction step (a job description
arrives as plain text in the request body, not an uploaded file):

```
HTTP Request
     ↓
Router               (app/api/routes/jobs.py)
     ↓
Job Service           (app/services/job_service.py)
     ↓
Job Agent             (app/agents/job_agent.py)
     ↓
LLM Service           (app/services/llm_service.py)   ← same instance as Phase 1
     ↓
Structured Job Profile   (app/schemas/job.py)
     ↓
Database              (app/models/job.py, app/models/database.py)   ← same DB as Phase 1
     ↓
API Response
```

Job Service and Job Agent follow the same responsibilities as Resume Service
and Resume Agent above. The `jobs` table lives in the same SQLite database as
`candidate_profiles` — no second database or second LLM configuration was
introduced.

### Phase 3 — AI Job Matching

```
HTTP Request
     ↓
Router               (app/api/routes/matching.py)
     ↓
Matching Service      (app/services/matching_service.py)
     ↓
Candidate DB + Job DB   ← existing rows from Phase 1 / Phase 2, never re-parsed
     ↓
Matching Agent        (app/agents/matching_agent.py)
     ↓
LLM Service           (app/services/llm_service.py)   ← same instance as Phase 1 & 2
     ↓
Structured Match Result   (app/schemas/matching.py)
     ↓
Database              (app/models/job_match.py, app/models/database.py)   ← same DB
     ↓
API Response
```

`MatchingService` looks up the candidate and job by id, decodes their
JSON-text columns back into plain dicts, and hands both to `MatchingAgent`.
The agent never re-extracts a resume or job description — it only ever sees
the already-structured Phase 1 / Phase 2 data. The result is stored in a new
`job_matches` table with foreign keys into `candidate_profiles` and `jobs`.

## Project Structure

```
ai_job_agent/
├── app/
│   ├── main.py                  FastAPI app, health check, startup DB init
│   ├── config.py                All settings, loaded from .env
│   ├── api/routes/
│   │   ├── resume.py            POST /api/resume/upload
│   │   ├── candidates.py        GET /api/candidates/{id}
│   │   ├── jobs.py              POST /api/jobs, GET /api/jobs/{id}
│   │   ├── job_search.py        POST /api/jobs/search, POST /api/jobs/search/match
│   │   └── matching.py          POST /api/matching
│   ├── services/
│   │   ├── resume_service.py    Orchestrates the resume workflow
│   │   ├── document_service.py  PDF/DOCX text extraction + cleaning
│   │   ├── llm_service.py       OpenAI-compatible LLM client (Ollama by default)
│   │   ├── job_service.py       Orchestrates the job-description workflow
│   │   ├── matching_service.py  Orchestrates the candidate/job matching workflow
│   │   ├── job_discovery_service.py         Discover + normalize jobs (Phase 4)
│   │   └── job_discovery_matching_service.py Bridges discovery into Job Intelligence + Matching (Phase 4)
│   ├── agents/
│   │   ├── resume_agent.py      Resume prompt + schema validation
│   │   ├── job_agent.py         Job description prompt + schema validation
│   │   └── matching_agent.py    Candidate-vs-job prompt + schema validation
│   ├── integrations/job_sources/
│   │   ├── base.py              BaseJobSource interface
│   │   ├── mock.py              MockJobSource (fixed sample data)
│   │   ├── jooble.py            JoobleJobSource (official Jooble REST API)
│   │   ├── normalizer.py        Raw source fields -> NormalizedJob
│   │   ├── registry.py          Source name -> adapter class
│   │   └── exceptions.py        Shared config/unavailable/response exceptions
│   ├── models/
│   │   ├── candidate.py         SQLAlchemy table: candidate_profiles
│   │   ├── job.py               SQLAlchemy table: jobs (+ source/source_url for Phase 4)
│   │   ├── job_match.py         SQLAlchemy table: job_matches (FKs into the two above)
│   │   └── database.py          Engine, session, init_db
│   ├── schemas/
│   │   ├── candidate.py         Pydantic request/response models
│   │   ├── job.py               Pydantic request/response models
│   │   ├── job_search.py        Discovery + discovery-and-match request/response models
│   │   └── matching.py          Pydantic request/response models
│   └── utils/file_utils.py      Safe filename generation, extension checks
├── uploads/                     Stored resume files (git-ignored contents)
├── tests/
│   ├── conftest.py              Test DB, temp upload dir, dummy resume generators
│   ├── test_document_parser.py
│   ├── test_resume.py
│   ├── test_jobs.py
│   ├── test_matching.py
│   ├── test_job_discovery.py           Job source architecture: mock source, normalizer, registry
│   ├── test_jooble_source.py           Jooble adapter (HTTP fully mocked)
│   └── test_job_discovery_matching.py  Complete Phase 4: discovery -> Job Intelligence -> Matching
├── .env / .env.example
├── requirements.txt
└── README.md
```

## Installation

```bash
cd ai_job_agent
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -r requirements.txt
```

## Environment Setup

A working `.env` is already included, pointed at a local Ollama instance —
no secret needed:

```env
APP_NAME=AI Job Agent
ENVIRONMENT=development

DATABASE_URL=sqlite:///./ai_job_agent.db

UPLOAD_DIR=uploads
MAX_FILE_SIZE_MB=10

LLM_PROVIDER=ollama
LLM_MODEL=llama3.1
LLM_BASE_URL=http://localhost:11434/v1
OPENAI_API_KEY=
LLM_TIMEOUT_SECONDS=60
LLM_JSON_RESPONSE_FORMAT=true
```

Before running for real:

1. Install [Ollama](https://ollama.com) and pull a model:
   ```bash
   ollama pull llama3.1
   ```
2. Make sure Ollama is running (it starts a server on `localhost:11434`).
3. If `LLM_MODEL` in `.env` doesn't match the model you pulled, update it.
4. If your model/Ollama version doesn't support structured JSON output, set
   `LLM_JSON_RESPONSE_FORMAT=false` — the prompt already asks for JSON-only
   output as a fallback.

To switch to OpenAI or Azure OpenAI later, only `.env` changes:
```env
LLM_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
```

## Running the Application

```bash
uvicorn app.main:app --reload
```

- Swagger UI: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health → `{"status": "healthy"}`

## API Endpoint

```
POST /api/resume/upload
Content-Type: multipart/form-data
Field: file  (a .pdf or .docx)
```

### Testing via Swagger

1. Go to `/docs`.
2. Expand `POST /api/resume/upload`.
3. Click **Try it out**, choose a `.pdf` or `.docx` file, click **Execute**.

### Testing via curl

```bash
curl -X POST "http://127.0.0.1:8000/api/resume/upload" \
  -F "file=@/path/to/resume.pdf"
```

### Example Response

```json
{
  "message": "Resume processed successfully",
  "candidate_id": 1,
  "candidate": {
    "name": "John Doe",
    "email": "john.doe@example.com",
    "skills": ["Python", "FastAPI", "Azure", "SQL"],
    "target_roles": ["AI Engineer", "Backend Engineer"]
  }
}
```

Unsupported file types return `415`, empty/oversized/corrupted/unreadable
files return `400`, and LLM failures return `502` — with clean, non-leaky
error messages in every case.

## Phase 2 — Job Intelligence Endpoint

```
POST /api/jobs
Content-Type: application/json
```

### Example Request

```json
{
  "job_description": "We are looking for an AI Engineer with 3+ years of experience in Python, FastAPI, Azure, LLMs, RAG and Docker."
}
```

### Example Response

```json
{
  "message": "Job processed successfully",
  "job": {
    "id": 1,
    "job_title": "AI Engineer",
    "company": null,
    "location": null,
    "experience_required": "3+ years",
    "required_skills": ["Python", "FastAPI", "Azure", "LLMs", "RAG", "Docker"]
  }
}
```

### How Job Intelligence works

A job description is sent as plain text (no file upload). `JobService` hands
it to `JobAgent`, which prompts the same LLM Service used by Phase 1 to
extract a structured profile — job title, company, location, employment
type, experience required, and skills split into the same categories as the
candidate profile (`programming_languages`, `frameworks`,
`cloud_technologies`, `ai_ml_skills`, `databases`, `tools`), plus
responsibilities, certifications, and education required. The LLM is
instructed not to invent anything not present in the text — missing fields
come back as `null` or an empty list. The validated profile is stored in a
new `jobs` table in the same SQLite database used by Phase 1.

Empty/whitespace-only descriptions return `400`; LLM failures return `502`.

### Testing via curl

```bash
curl -X POST "http://127.0.0.1:8000/api/jobs" \
  -H "Content-Type: application/json" \
  -d "{\"job_description\": \"We are looking for an AI Engineer with 3+ years of experience in Python, FastAPI, Azure, LLMs, RAG and Docker.\"}"
```

## Phase 3 — AI Job Matching Endpoint

```
POST /api/matching
Content-Type: application/json
```

### Example Request

```json
{
  "candidate_id": 1,
  "job_id": 1
}
```

### Example Response

```json
{
  "message": "Job matching completed successfully",
  "match": {
    "id": 1,
    "candidate_id": 1,
    "job_id": 1,
    "match_score": 92,
    "recommendation": "Apply",
    "matched_skills": ["Python", "FastAPI", "Azure", "LLMs", "RAG", "Docker", "PostgreSQL"],
    "missing_skills": [],
    "experience_match": true,
    "role_match": true,
    "summary": "The candidate strongly matches the AI Engineer position."
  }
}
```

### How Job Matching works

`candidate_id` and `job_id` point at existing rows already created by Phase 1
and Phase 2 — no resume or job description is re-parsed here. `MatchingService`
fetches both rows, decodes their JSON-text columns, and hands the two plain
profiles to `MatchingAgent`, which prompts the same LLM Service used by
Phases 1 and 2. The LLM is instructed to use only what's explicitly present in
the two profiles — it must not invent candidate skills or job requirements —
and to weigh required/preferred skills, programming languages, frameworks,
cloud technologies, AI/ML skills, databases, tools, required experience, and
the candidate's job titles/target roles/work experience.

### Match score

An integer 0–100 based on semantic relevance (not simple keyword counting),
mapped to a recommendation:

| Score | Recommendation |
|-------|-----------------|
| 75–100 | Apply |
| 60–74  | Review |
| 0–59   | Skip |

The result (score, recommendation, matched/missing skills, experience/role
match, summary) is stored in a new `job_matches` table with foreign keys into
`candidate_profiles` and `jobs`.

Candidate/job not found return `404`; LLM failures or a malformed LLM
response return `502`.

### Testing via curl

```bash
curl -X POST "http://127.0.0.1:8000/api/matching" \
  -H "Content-Type: application/json" \
  -d "{\"candidate_id\": 1, \"job_id\": 1}"
```

### Retrieving a stored match

```
GET /api/matching/{match_id}
```

Returns the same match fields as above (score, recommendation, matched/missing
skills, experience/role match, summary) for a previously computed result.
Unknown ids return `404`.

```bash
curl "http://127.0.0.1:8000/api/matching/1"
```

## Running Tests

The LLM is always mocked in tests — no real Ollama/OpenAI calls happen.
Dummy PDF/DOCX resumes are generated on the fly by the test fixtures, so no
sample files need to be checked into the repo.

```bash
pytest -v
```

Expected: all tests pass, covering PDF/DOCX extraction, unsupported
extensions, empty documents, successful uploads, oversized files, documents
with no extractable text, job description intake, job lookup, and candidate/job
matching (including not-found and LLM-failure cases).

## Phase 4 -- Job Discovery

Phase 4 discovers real jobs from external sources, understands them with the
same Job Intelligence Agent used in Phase 2, and matches them against a
candidate with the same Matching Agent used in Phase 3. It was built in two
internal steps -- a source-agnostic discovery architecture, then Jooble as
the first real source -- but both are just implementation history now; there
is one Phase 4, and it is complete.

### Job source abstraction

```
Job Search Request
        v
Job Discovery Service   (app/services/job_discovery_service.py)
        v
Job Source Adapter      (app/integrations/job_sources/)
        v
Raw Job Data
        v
Job Normalizer          (app/integrations/job_sources/normalizer.py)
        v
Normalized Job          (app/schemas/job_search.py)
```

- **BaseJobSource** -- the interface every source implements: one method,
  `search_jobs(request)`, returning raw source-specific dicts. Nothing above
  it (the discovery service, the router) ever branches on which concrete
  source it's talking to -- there is no `if source == "jooble"` anywhere in
  this codebase.
- **MockJobSource** -- a fixed in-memory sample of five jobs (AI Engineer,
  Generative AI Engineer, Machine Learning Engineer, Backend Engineer, Data
  Scientist), filtered by keywords/location/remote. Not a scraper, not a
  real integration -- exists purely to exercise the pipeline in tests and
  demos without any external dependency.
- **JoobleJobSource** (`app/integrations/job_sources/jooble.py`) -- the first
  real integration, calling the official Jooble REST API
  (`POST {JOOBLE_BASE_URL}/{JOOBLE_API_KEY}`). Never scrapes, never uses an
  unofficial/private API. All network/HTTP/parsing failures are translated
  into one of three shared exceptions (`app/integrations/job_sources/exceptions.py`)
  so the router never needs to know which adapter raised them:

  | Situation | Exception | HTTP status |
  |---|---|---|
  | Missing `JOOBLE_API_KEY` | `JobSourceConfigError` | 500 |
  | Network failure / timeout / 403 / 404 / other non-200 | `JobSourceUnavailableError` | 503 |
  | Invalid JSON / missing or malformed `jobs` field | `JobSourceResponseError` | 502 |
  | Unrecognized `source` value | `UnknownJobSourceError` | 400 |

  No error path ever includes the API key in a log line or HTTP response.
- **JobNormalizer** -- converts a source's raw field names (`title` vs
  `jobTitle` vs `job_title`; `description` vs `snippet`, ...) into one common
  `NormalizedJob` shape. Plain field mapping, no LLM involved.
- **Source registry** (`registry.py`) -- `{"mock": MockJobSource, "jooble": JoobleJobSource}`
  today; adding a real source later is one new adapter class plus one new
  registry line, with no changes to `JobDiscoveryService`.
- **JobDiscoveryService** -- looks up the requested source, calls it, and
  normalizes the result. No platform-specific code, no AI logic, no
  database writes -- it only produces `NormalizedJob` objects.

### Job Intelligence integration (reuses Phase 2, unchanged)

Each discovered job's `description` is sent through the **exact same**
`JobAgent` (`app/agents/job_agent.py`) that Phase 2's `POST /api/jobs`
endpoint uses -- no second Job Intelligence Agent was created. The resulting
structured profile is saved into the **exact same** `jobs` table Phase 2
writes to, tagged with two new nullable columns so a discovered job can be
traced back to where it came from and never duplicated:

```python
# app/models/job.py -- additive, nullable, existing Phase 2 rows unaffected
source: Mapped[str | None]       # e.g. "jooble", "mock"
source_url: Mapped[str | None]   # the posting's original URL; used to de-duplicate
```

Before calling the agent, Phase 4 checks whether a `Job` row already exists
for that `source_url`. If it does, that row is reused directly -- **the LLM
is not called again** for a job you've already discovered and analyzed in an
earlier search. This is the only place Phase 4 avoids repeat LLM calls;
matching (below) intentionally still runs every time, since it's the
candidate's fit being evaluated, not the job's content.

**Snippet limitation:** Jooble only ever supplies a short excerpt (`snippet`),
never a guaranteed-complete job description. Phase 4 does not pretend
otherwise -- every discovered job in an API response carries an
`is_partial_description` flag (`true` for Jooble, `false` for Mock, which
returns full sample text), and the Job Intelligence Agent's own prompt
already refuses to invent fields it can't see in the text, so a sparse
snippet yields `null`/empty fields rather than fabricated ones. If a
discovered job has **no** description at all, Phase 4 skips Job Intelligence
and Matching entirely for that job and says so explicitly via `match_note`,
rather than inventing anything.

### Matching integration (reuses Phase 3, unchanged)

Once a discovered job has a `Job` row (existing or newly created), Phase 4
calls `MatchingService.match(candidate_id, job_id)` -- the **exact same**
Phase 3 service `POST /api/matching` calls, saving into the **exact same**
`job_matches` table. No matching logic was duplicated or rewritten. The
response's `match` field reuses the existing `MatchAnalysis` schema from
`app/schemas/matching.py` unchanged -- no separate match structure exists
for discovered jobs.

If Job Intelligence or Matching fails for one specific discovered job (LLM
timeout, malformed LLM output, etc.), that job comes back with `match: null`
and an explanatory `match_note` -- the rest of the request still succeeds
rather than failing outright over one bad job.

### API usage

Two endpoints now exist side by side; the first is unchanged from before
Phase 4 was completed, the second is the new complete pipeline:

**`POST /api/jobs/search`** -- discover jobs only, no candidate needed (works exactly as it always has):
```json
{"keywords": "AI Engineer", "location": "Bangalore", "limit": 10, "source": "jooble"}
```
```json
{
  "message": "Jobs discovered successfully",
  "source": "jooble",
  "count": 1,
  "jobs": [
    {"title": "AI Engineer", "company": "Example Technologies", "location": "Bangalore",
     "description": "Build and deploy LLM-powered applications...", "url": "https://in.jooble.org/jdp/12345", "source": "jooble"}
  ]
}
```

**`POST /api/jobs/search/match`** -- discover, analyze, and match against a candidate:
```json
{"candidate_id": 1, "keywords": "AI Engineer", "location": "Bangalore", "limit": 10, "source": "jooble"}
```
```json
{
  "message": "Jobs discovered and matched successfully",
  "candidate_id": 1,
  "source": "jooble",
  "count": 1,
  "jobs": [
    {
      "title": "AI Engineer",
      "company": "Example Technologies",
      "location": "Bangalore",
      "description": "Build and deploy LLM-powered applications...",
      "url": "https://in.jooble.org/jdp/12345",
      "source": "jooble",
      "job_id": 7,
      "is_partial_description": true,
      "match_note": null,
      "match": {
        "match_score": 95,
        "recommendation": "Apply",
        "matched_skills": ["Python", "FastAPI", "Azure"],
        "missing_skills": [],
        "experience_match": true,
        "role_match": true,
        "summary": "..."
      }
    }
  ]
}
```

Unrecognized `source` returns `400`; unknown `candidate_id` returns `404`;
Jooble misconfiguration/outage returns `500`/`503`/`502` exactly as in the
table above. Empty results are a normal `200` with `count: 0, jobs: []`, not
an error.

```bash
curl -X POST "http://127.0.0.1:8000/api/jobs/search/match" \
  -H "Content-Type: application/json" \
  -d "{\"candidate_id\": 1, \"keywords\": \"AI Engineer\", \"location\": \"Bangalore\", \"limit\": 5, \"source\": \"jooble\"}"
```

### Environment variables

```env
JOOBLE_API_KEY=
JOOBLE_BASE_URL=https://jooble.org/api
```

Register for a free key at https://jooble.org/api/about. **Each Jooble
country domain issues its own key** -- confirm you have the right regional
key for the jobs you actually need before relying on this in production.
If `JOOBLE_API_KEY` is empty, `JoobleJobSource` raises a clear configuration
error instead of silently failing or using a placeholder. **Never commit a
real key** -- keep it in your local `.env` only.

### Database note (important, one-time)

Adding `source`/`source_url` to the `Job` model means the on-disk
`ai_job_agent.db` SQLite file needs those two columns. `init_db()` only
creates *missing tables*, not missing *columns* on a table that already
exists, so if you already had a `jobs` table before this change, either:

- delete the local dev database file and let it get recreated on next
  startup (fine for a dev SQLite file with no data you need to keep), or
- add the columns manually: `ALTER TABLE jobs ADD COLUMN source VARCHAR(50);`
  and `ALTER TABLE jobs ADD COLUMN source_url VARCHAR(500);`

Fresh test runs are unaffected -- `tests/conftest.py` always creates a brand
new temporary SQLite file per test.

### Testing

```bash
pytest tests/test_job_discovery.py tests/test_jooble_source.py tests/test_job_discovery_matching.py -v
```

All HTTP calls (Jooble) and all LLM calls (Job Intelligence, Matching) are
mocked -- nothing in the test suite makes a real network or LLM call,
including in CI. Run the full suite the same way as always:

```bash
pytest -v
```

## Phase 5 -- Application Agent (source-tracking fix)

The `applications` table originally stored a single `source` column meaning
"which adapter processed this attempt" (e.g. `"mock"`), which silently
discarded the job's own discovery source (e.g. `"jooble"`). This has been
split into two columns so both are preserved:

```python
# app/models/application.py
job_source: Mapped[str | None]     # WHERE the job came from -- copied from Job.source
submission_adapter: Mapped[str]    # HOW the application was processed -- e.g. "mock"
```

`job_source` always comes from the stored `Job` row, never from the request
body and never from the adapter name. `POST /api/applications` also accepts
an optional `source` field used only as a client-side sanity check -- if
supplied, it must match the job's stored `source` or the request is rejected
with `400`; it is never used to overwrite `job_source`.

### Database note (important, one-time)

Same situation as the Phase 4 note above: `init_db()` only creates *missing
tables*, not missing/renamed *columns* on a table that already exists. If
your local `ai_job_agent.db` already has an `applications` table from before
this change, either:

- delete the local dev database file and let it get recreated on next
  startup (fine for a dev SQLite file with no data you need to keep), or
- run the one-off migration script: `python migrate_phase5_source_split.py`
  (adds `job_source`/`submission_adapter`, backfills them from the old
  `source` column and the `jobs` table, then drops the old `source`
  column -- it's `NOT NULL` and the ORM no longer writes to it, so
  leaving it in place breaks every new application with a `NOT NULL
  constraint failed` error).

Fresh test runs are unaffected -- `tests/conftest.py` always creates a brand
new temporary SQLite file per test.

## Phase 5C -- Real Application Workflow

Phase 5 originally only had `MockApplicationSource`, which always reports
`status="submitted"` without ever contacting a real site. Phase 5C adds a
second, real adapter -- `RealApplicationSource` -- and a human-approval step,
without removing or changing the mock adapter (still used by default and by
every automated test).

### Why there is no single "submit to Jooble" button

Jooble is a job aggregator. A Jooble job's URL either leads to a
Jooble-hosted application, or redirects to the employer's own careers page or
an ATS -- which one varies job to job, and Jooble's search API (used in
Phase 4 for discovery) has no candidate-submission endpoint at all. So a real
submission can only happen once the *actual* destination is known **and**
that destination has a specific, legitimate, documented submission mechanism
configured for it. `RealApplicationSource` never scrapes, never automates a
browser, and never bypasses CAPTCHA/MFA/login walls/anti-bot controls --
when no such legitimate mechanism is configured for a destination (true for
most real Jooble redirects today), the result is an honest `"unsupported"`
status with the real URL for the candidate to apply manually, never a faked
success.

### Flow

```
Candidate + Matched Job
        v
ApplicationAgent.prepare()        -- unchanged, still never invents data
        v
Adapter selected: "real" only if REAL_APPLICATION_ENABLED=true, else "mock"
        v
["mock"]  submit immediately, exactly as before  ->  status="submitted"

["real"]  RealApplicationSource.resolve_destination(application_url)
              follows redirects (e.g. a Jooble posting -> the employer's own
              careers page), WITHOUT submitting anything
        v
          not supported?  -> status="unsupported", stop.
                              application_destination + reason saved; the
                              candidate applies manually at that URL.
        v
          supported?      -> status="awaiting_approval", stop.
                              NOTHING is submitted yet.
        v
  Candidate reviews (via GET, or the response body): employer, job title,
  resolved application_destination, resume_used, answers
        v
  POST /api/applications/{id}/approve   -- explicit candidate approval
        v
  RealApplicationSource.submit_application(payload, destination_url)
        v
          submitted -> status="submitted", submitted_at set
          failed    -> status="failed"
```

No real submission ever happens as a side effect of `POST /api/applications`
itself -- that call only ever prepares, resolves, or (for the mock adapter
only) simulates. A real submission only happens from the separate `/approve`
call, and only for an application already sitting at `"awaiting_approval"`.

### Statuses

| Status | Meaning |
|---|---|
| `prepared` | Reserved for future use; not currently reachable. |
| `awaiting_approval` | Domain-map real adapter only (destination is not Greenhouse/Lever). Destination resolved and supported; nothing submitted yet -- waiting on `/approve`. |
| `submitted` | An actual submission occurred and a genuine confirmation was verified (`confirmed=true`). Mock: simulated. Domain-map real: an HTTP submission went through. Greenhouse/Lever: the destination's own "thank you"/confirmation signal was detected after clicking Submit. |
| `manual_review` | Greenhouse/Lever only. The adapter deliberately stopped rather than guess or bypass a control -- see `blocker` (`captcha`, `login`, `unanswered_required_question`, `submit_button_not_found`). |
| `unknown` | Greenhouse/Lever only. Submit was clicked but no confirmation could be verified afterward. Never silently promoted to `submitted`. |
| `skipped` | An Application row already exists for this candidate/job pair -- no second attempt was made. |
| `failed` | A submission was actually attempted and did not succeed, or an adapter raised an infrastructure error. |
| `unsupported` | Domain-map real adapter only. The resolved destination is not Greenhouse/Lever and has no configured, legitimate submission mechanism -- apply manually at `application_destination`. |

`submitted_at` is only ever set when `status == "submitted"`. `confirmed` is only ever `true` together with `status == "submitted"`.

### Phase 5D: Greenhouse / Lever ATS adapters

When the resolved `application_destination` is recognized by
`app/integrations/application_sources/ats_detector.py` as a Greenhouse
(`*.greenhouse.io`) or Lever (`jobs.lever.co`) posting, `ApplicationService`
hands the payload straight to `GreenhouseApplicationSource` /
`LeverApplicationSource` (`app/integrations/application_sources/greenhouse.py`
/ `lever.py`) instead of the domain-map "real" adapter -- no separate
`/approve` call, since the adapter itself is the approval substitute: it
stops at `manual_review` for anything it can't safely automate.

Each adapter, via Playwright:
1. Opens the real, already-resolved destination (never Jooble or any
   aggregator directly).
2. Detects a CAPTCHA or login/account-creation wall and stops
   (`manual_review`) rather than solving or bypassing either.
3. Fills first/last name, email, and phone from the candidate's stored
   profile, and looks for LinkedIn/GitHub/portfolio question fields by
   label text -- these currently always come back `skipped_no_data`
   (`CandidateApplicationInfo` has no such fields yet; nothing is ever
   invented).
4. Uploads the stored resume file.
5. Checks for required-but-unanswered questions and stops
   (`manual_review`) rather than guessing an answer.
6. Clicks Submit and looks for a genuine confirmation signal
   (`submitted`+`confirmed=true`) or reports `unknown` if none is found.
7. Saves a pre-submit and post-submit screenshot under
   `APPLICATION_ARTIFACTS_DIR`, and a per-field audit
   (`field_fill_audit`: `filled` / `skipped_no_data` / `skipped_not_found`).

Setup:
```bash
pip install playwright
playwright install chromium
```
```env
PLAYWRIGHT_HEADLESS=true
APPLICATION_ARTIFACTS_DIR=application_artifacts
```

A destination that isn't Greenhouse or Lever still falls back to the
original domain-map "real" adapter (`awaiting_approval` / `unsupported`)
described above, unchanged.

### `job_source` vs `submission_adapter` vs `application_destination`

Three distinct fields, none of which overwrite each other:

| Field | Meaning | Example |
|---|---|---|
| `job_source` | Where the *job* came from (Phase 4) | `"jooble"` |
| `submission_adapter` | Which adapter processed the *application* | `"mock"` or `"real"` |
| `application_url` | The original URL stored on the Job (e.g. the Jooble posting) | `https://in.jooble.org/jdp/1241627362658169157` |
| `application_destination` | The *actual* resolved destination after following redirects (real adapter only) | `https://company.example.com/careers/job/12345` |

### Configuration

```env
# Disabled by default -- ApplicationService always selects "mock" until this is true.
REAL_APPLICATION_ENABLED=false
REAL_APPLICATION_TIMEOUT_SECONDS=15.0

# One entry per specific, inspected destination domain that has a real,
# documented submission endpoint. Empty by default -- no destination is
# pre-approved out of the box. Format: domain=submit_url, comma-separated.
REAL_APPLICATION_SUPPORTED_DESTINATIONS=
# example: REAL_APPLICATION_SUPPORTED_DESTINATIONS=boards.greenhouse.io=https://boards-api.greenhouse.io/v1/boards/example/jobs
```

No credentials are hardcoded anywhere; if a configured destination needs an
API key, add it as its own `.env` variable and read it through
`app/config.py`, the same way `JOOBLE_API_KEY` is handled.

### API

```
POST /api/applications                    -- unchanged request shape (candidate_id, job_id, source?)
POST /api/applications/{id}/approve        -- new; submits a real application that is "awaiting_approval"
```

### Limitations (by design, not gaps to fix silently)

- Real browser automation exists ONLY for Greenhouse and Lever (Phase 5D) --
  every other destination still has no generic browser automation and no
  configured endpoint by default, so it stays `"unsupported"` unless you add
  it to `REAL_APPLICATION_SUPPORTED_DESTINATIONS`.
- Even for Greenhouse/Lever, CAPTCHA and login/account-creation walls are only
  ever detected, never solved or bypassed -- both stop at `"manual_review"`.
- LinkedIn/GitHub/portfolio questions always come back `skipped_no_data` --
  there's no stored source for them yet (`CandidateApplicationInfo` has no
  such fields). Extend that schema/ApplicationAgent first if you want these
  filled.
- `find_required_unanswered` (see `playwright_support.py`) only catches
  fields with an actual HTML `required` attribute -- a board that only marks
  a question required visually (e.g. an asterisk with no `required` attr) or
  uses a radio/checkbox group isn't caught by this check; if that field is
  genuinely required, the destination site's own rejection still prevents a
  false `"submitted"` (it surfaces as `"unknown"` or `"failed"` instead).
- A real submission requires the candidate to have a stored name, email, and
  resume file; if any is missing, the request is rejected with `400` rather
  than submitting with placeholder/invented data.

### Testing

`MockApplicationSource` is unchanged and still used by every existing Phase 5
test. Tests for the domain-map real adapter mock all HTTP calls (via
`unittest.mock.patch` on `httpx.get`/`httpx.post`); tests for the Greenhouse/
Lever adapters (`tests/test_ats_adapters.py`) fake out `playwright.sync_api`
entirely via `sys.modules`, so **no test in this project ever launches a
real browser, makes a real network call, or submits a real job
application**.

```bash
pytest tests/test_applications.py tests/test_ats_adapters.py -v
pytest -v   # full suite
```
