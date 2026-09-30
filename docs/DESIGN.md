# ColdTrace

**AI-Powered Cold-Chain Logistics Copilot — Complete Design Document**
Forward Deployed Engineer (FDE) Portfolio Project

| | |
|---|---|
| Prepared by | Prathamesh Mishra |
| Role | Data Analyst → Analytics Engineering |
| Date | October 2026 |
| Status | Active Build |

---

## 1. The Story — What Problem Are We Solving?

Imagine you drive a truck. Inside that truck is medicine. Not just any medicine — insulin that keeps diabetics alive. Vaccines that protect babies. The moment your truck's fridge breaks and gets too warm, that medicine starts dying. You might not even know it's happening.

Now imagine you are the dispatcher. You sit in an office and you are responsible for 50 trucks, all driving across the country, right now. Every truck has a sensor telling you the temperature inside it. Every truck has a GPS telling you where it is. You have a company rulebook — called a Standard Operating Procedure, or SOP — telling you exactly what to do if things go wrong.

**The problem?** Nobody can watch 50 temperature sensors, 50 GPS locations, check the weather on 50 routes, and remember all the rules in a thick rulebook — all at the same time, at 2am, under pressure.

People make mistakes. Medicine gets ruined. The company loses money. Patients don't get what they need.

### 1.1 The Old Way (What Humans Do Now)

| Step | What the human does | Why this is slow / risky | Time wasted |
|---|---|---|---|
| 1 | Driver notices temperature is rising. Radios the dispatcher. | Driver might not notice. Radio might be busy. | 0–30 min |
| 2 | Dispatcher opens a spreadsheet or dashboard to check sensor data. | Spreadsheets update slowly. Dashboards show average, not individual truck detail. | 5–15 min |
| 3 | Dispatcher manually looks up the route, checks Google Maps for nearby depots. | Human error. Might pick wrong depot. Might forget to check weather. | 10–20 min |
| 4 | Dispatcher opens the SOP PDF to find the right rule for this cargo type. | SOP is 40 pages. Wrong rule = wrong action = damaged cargo. | 5–10 min |
| 5 | Dispatcher makes a decision. Phones driver. Logs it somewhere. | If the log is a notepad or email, nobody can review decisions later. | 5 min |
| **TOTAL** | | Best case: 25 minutes of delay while medicine spoils. | **25–75 min** |

### 1.2 The New Way (What ColdTrace Does)

A dispatcher types one sentence: *"Find any trucks near Los Angeles with temperature problems and tell me what to do."*

ColdTrace does everything else:

- → Checks the database for trucks in that area and their temperatures
- → Checks the live weather on those routes
- → Reads the company's SOP and finds the exact rule that applies
- → Tells the dispatcher exactly what to do, citing which SOP rule it used
- → Logs the entire decision permanently so it can be reviewed later

**Total time: 8–15 seconds.**

---

## 2. The Reference Project — What Was Already Built (And What Is Broken)

Before writing a single line of code, I studied the existing FDE reference project (`nimowhyca/cold-chain-logistics-FDE-Project`) in full — every Python file, every SQL script, their own Technical Design Document, and their business presentation slides. This section documents what they built, what works, and — crucially — where the code contradicts their own documentation.

### 2.1 What the Reference Project Gets Right

Credit where it is due. The reference project makes several good decisions:

- **A. The security view idea is sound.** Instead of letting the AI write SQL directly against the raw table, they expose only a clean view (`VW_ACTIVE_FLEET`) with renamed columns. The AI can never drop tables or modify data.
- **B. Three-tool architecture is logical.** SQL telemetry + live weather API + SOP vector search is exactly the right set of tools for this problem.
- **C. The UI structure is sensible.** Two modes — Dispatch Console and Audit Logs — is the right split for an operational tool.
- **D. AuditLog intent is correct.** The idea of logging every agent action to a SQL table is exactly what a real enterprise would need.

### 2.2 What the Reference Project Gets Wrong — Verified Line by Line

The following gaps were found by reading every file in the repository, including comparing their own Technical Design Document (TDD PDF) against the actual Python and SQL code they shipped. These are not opinions — each one is traceable to a specific file and line number.

| # | File + Line | What the TDD/Doc claims | What the code actually does | Why this matters |
|---|---|---|---|---|
| 1 | `setup_security_and_view.sql`; `ui.py` line 47 | TDD §3.3 says the system has 'immutable auditing.' TDD §3.2 says the agent role 'lacks permissions to write.' | The SQL setup script never grants INSERT on `AgentAuditLog`. The `ui.py` code tries to INSERT using the read-only `USR_FDE_RO` account. This INSERT will always fail. | 'Immutable auditing' is the headline governance feature. It silently does not work as shipped. |
| 2 | `agent_tools.py` line 104 | TDD §3 says security 'mitigates prompt injection risks.' | The only check is: `if not sql_query.strip().upper().startswith('SELECT')`. A batch like `SELECT 1; DROP TABLE users;` passes this check. | SQL injection is the most common attack on AI-connected databases. Their mitigation does not stop the most basic variant. |
| 3 | `orchestrator.py` (full file) | TDD §2.2 says: 'Self-Correction: If SQL returns an error, the agent attempts a simplified query iteration.' | The LangGraph graph is: START → reasoner → tools → reasoner → END. There is no retry node, no error-handling branch, no simplified-query logic anywhere in 118 lines. | A claimed reliability feature does not exist. The agent either succeeds or returns a raw error. |
| 4 | `system_prompt.txt` lines 1–8 | TDD §2.1 calls the system 'a LangGraph ReAct agent' that does 'intent deconstruction' to select 'the optimal tool.' | The system prompt hard-codes: '1. ALWAYS use query_telemetry_db first. 2. Use fetch_corridor_conditions. 3. Use search_compliance_sop.' Every single query runs all three tools in this fixed order. | It is a scripted pipeline, not an agent. It cannot skip a tool, change the order, or reason about what is actually needed for a given question. |
| 5 | `agent_tools.py` lines 136–139 | The tool is called `fetch_corridor_conditions` and its output header says 'LIVE CORRIDOR TELEMETRY' suggesting sophisticated route analysis. | The entire corridor risk model is: `congestion_index = 8.5 if wind > 10.0 else 2.5`. That is one if/else on wind speed. No route. No forecast. No congestion data. | The marketing language ('LIVE CORRIDOR TELEMETRY') is disproportionate to a two-branch heuristic. A recruiter who reads the code will notice this immediately. |
| 6 | `ingest_sop_pinecone.py`; `Cold_Chain_Incident_SOP_v2.md` | The system presents SOP retrieval as a compliance-grade feature. | The SOP document is 16 lines covering exactly 3 rules. The retriever uses k=2 with no date/version filtering. If two versions of a rule existed, the agent could retrieve the outdated one. | In a real regulated industry (pharma, food safety), retrieving a superseded rule and acting on it is a compliance failure, not an inconvenience. |
| 7 | `ui.py` line 56 (audit table) | TDD §3.3 lists structured log fields: 'Timestamp, User_Prompt, Tool_Invoked, Tool_Raw_Output, LLM_Decision.' | The actual table has a single `Content` column of type `NVARCHAR(MAX)` — a free-text blob. All tool outputs and LLM responses go into one unstructured text field. | You cannot query 'show me every decision where the agent called the weather tool' without parsing raw text. The audit log is write-only in practice. |

> **Summary:** The reference project is a good learning skeleton. Its architecture is conceptually sound. But when read line by line, the shipped code has multiple gaps between documentation and reality. ColdTrace is built to close those gaps — not to replace the concept, but to build a version that actually does what it says.

---

## 3. ColdTrace — What I Am Building and Why

### 3.1 One-Line Description

An AI copilot for cold-chain logistics dispatchers that checks its own data quality before reasoning, separates fact-collection from recommendations so both are visible, logs every decision in a structured and queryable audit trail, and uses real parameterised queries instead of agent-authored SQL.

### 3.2 The Five Core Improvements Over the Reference Project

Each improvement below maps directly to a verified gap in Section 2.2. Each one is explained so simply that someone who has never written code can understand why it matters. Together they are delivered through six parameterised tool functions (4 SQL · 1 weather · 1 SOP) — the agent never writes its own queries.

#### Improvement A — Parameterised Queries Instead of Agent-Written SQL (Fixes Gap #2)

**Plain English:** Right now the AI writes its own SQL sentences and sends them to the database. That is like letting a stranger into your office and saying 'type whatever you want on my computer.' ColdTrace does not do that. Instead, the AI picks from a list of safe pre-written queries, like a menu. It can pick 'Show me all trucks above temperature threshold' but it cannot write its own query from scratch.

**Technical detail:** We replace the `query_telemetry_db(sql_query: str)` tool — which accepts raw SQL text from the LLM — with a set of parameterised functions:

- `get_fleet_status(risk_level: str = None)`
- `get_truck_telemetry(lat: float, lon: float, radius_km: float)`
- `get_temperature_history(shipment_id: str, hours: int = 6)`
- `get_high_risk_shipments(delay_prob_threshold: float = 0.65)`

Alongside these four SQL functions, the agent has `fetch_route_conditions()` (weather) and `search_sop()` (SOP retrieval) — six tool functions in total.

The LLM chooses which function to call and provides the parameter values. The SQL itself is pre-written and safe. SQL injection via agent-authored queries becomes structurally impossible. Prompt injection — where malicious text in tool outputs or user input manipulates the model's reasoning — remains a residual risk mitigated by the system prompt's explicit instructions to treat tool output as data, not instructions. An interviewer will push on this distinction: be ready to name both.

#### Improvement B — Data Quality Gate Before the Agent Sees Any Data (New Capability)

**Plain English:** Imagine a truck sensor that has been stuck showing exactly 3.2°C for six hours. The fridge might actually be fine, or the sensor might be broken. The reference project sends that 3.2°C reading straight to the AI, which then confidently says 'temperature is within threshold.' ColdTrace checks whether sensor data is trustworthy before showing it to the AI at all.

**Technical detail:** Every result from the database passes through a Python validation function before reaching the LangGraph agent. The checks are:

- **STALE_SENSOR:** The same temperature value in 8 or more consecutive readings
- **OUT_OF_RANGE:** Temperature below -30°C or above 60°C — a sensor fault, not real cargo data
- **GPS_FROZEN:** GPS coordinates have not moved (beyond 25 m of jitter) for 2 hours while `trip_status` is `in_transit`
- **DATA_AGE:** If the most recent reading is more than 15 minutes old, or a reading follows a gap of more than 15 minutes in the feed, flag as STALE_FEED

A reading can raise several flags at once (e.g. a stuck sensor right after a feed gap). Each result row gets `data_quality_flags` — every flag raised, most severe first (OUT_OF_RANGE > STALE_SENSOR > GPS_FROZEN > STALE_FEED) — plus a single headline `data_quality_flag` (the most severe, or CLEAN). The full list goes to the audit log; the headline is what the UI shows.

Because three of the four checks look at patterns over time, the gate needs history: every telemetry tool fetches at least a 135-minute lookback window per truck, runs the gate over it, and only then narrows to what the agent asked for. The system prompt instructs the agent: if any reading is flagged, include the flag explicitly in the response and do not make a definitive recommendation without noting the data quality concern.

**The gate never drops readings, it annotates them.** A reading of 85 °C is stored exactly as received and flagged OUT_OF_RANGE at query time. Dropping it at ingest would mean the gate could never see the fault pattern, and an auditor could never reconstruct what the truck actually reported. The schema enforces this: `temperature_c` has no range constraint, and a test (`test_faulty_temperature_still_lands`) proves an 85 °C reading can be stored.

This is the same mindset as dbt testing — validating data quality at the layer closest to the source — applied to a live telemetry context instead of a batch warehouse.

#### Improvement C — Two-Stage Agent: Gather First, Recommend Second (Fixes Gap #4)

**Plain English:** A good doctor does not say 'take this medicine' the moment you walk in. They check your symptoms, run tests, and then give you a verdict. The reference project's agent skips the tests and goes straight to the verdict — it collects facts and makes recommendations in one jumbled step, in a hard-coded order. ColdTrace uses two separate steps: first collect the evidence, then reason over it.

**Technical detail:** The LangGraph graph has two named nodes instead of one:

- **GATHER NODE:** Routes to whichever tools are actually needed for this specific question. If the dispatcher asks only about temperature, it does not also call the weather API unnecessarily. Returns structured evidence: raw tool outputs with data quality flags attached.
- **RECOMMEND NODE:** Receives only the structured evidence from the Gather node. Has no access to raw tool calls. Its only job is to reason over the facts and produce a recommendation with SOP citation.

Routing is enforced, not suggested: the router's intent decides which tools the gather node is even offered (a pure SOP question is offered only `search_sop`). Invalid arguments and tool errors are returned to the model as tool messages so it can correct itself — the self-correction the reference TDD claimed but its code never had (gap #3).

The recommend node's output then passes deterministic guards, because a model's own claims cannot be trusted: a cited SOP clause must be one the SOP tool actually returned (otherwise it is removed and confidence drops); every truck named must appear in the evidence (otherwise a caveat is added and confidence drops to low); and evidence with data quality flags always carries a caveat and never high confidence. Before the model reasons, code also hands it a *key facts* list — which truck has which breach, delay or flag — so it does not have to pair numbers with trucks itself.

The Streamlit UI shows both panels side by side — the Evidence Panel (what was found) and the Verdict Panel (what is recommended). A dispatcher can verify the evidence before trusting the recommendation, the same way a doctor shows you the test result before the diagnosis.

#### Improvement D — Structured, Queryable Audit Log with Hash Chaining (Fixes Gaps #1 and #7)

**Plain English:** The reference project's audit table is like a notebook where every action is written as one long sentence in a single column. You cannot flip back through it and ask 'which decisions involved the weather tool this week?' ColdTrace writes every piece of information into its own column. And every row is cryptographically linked to the row before it — like a chain — so if anyone tries to delete or change a record, the chain breaks and the tampering is detectable.

**Technical detail:** The audit table schema (one row per dispatcher question):

```sql
log_id               SERIAL PRIMARY KEY
amends_log_id        INT NULL REFERENCES audit_log(log_id)  -- NULL for agent rows; set on human-decision rows
session_id           UUID
created_at           TIMESTAMPTZ           -- set in Python before insert
dispatcher_question  TEXT NOT NULL
tool_calls           JSONB                 -- [{tool_name, input, output, quality_flags}, ...]
data_quality_flags   JSONB
final_recommendation TEXT
sop_clause_cited     VARCHAR(100)
human_decision       VARCHAR(20)           -- accepted / overridden / pending
override_reason      TEXT
token_count_in       INTEGER
token_count_out      INTEGER
previous_row_hash    CHAR(64)
row_hash             CHAR(64)
```

The `row_hash` is SHA-256 of every field in the current row plus the `previous_row_hash`. If any past row is modified, every subsequent hash becomes invalid — tamper-evidence without a specialised WORM database.

The INSERT permission bug from the reference project is fixed: the migration grants INSERT explicitly to the agent role on this table only.

#### Improvement E — SOP Version Awareness (Fixes Gap #6)

**Plain English:** Imagine the company updates its rules in March. The old version says 'escalate after 60 minutes.' The new version says 'escalate after 30 minutes.' If the AI retrieves the old version by accident, it gives advice that could get someone fired or cause a compliance failure. ColdTrace tags every SOP chunk with an effective date and filters to current rules only.

**Technical detail:** Each chunk stored in Qdrant includes metadata:

```json
{ "effective_date": "2024-01-01", "superseded_by": null, "version": "3.1", "section": "4.2", "cargo_type": "fresh_perishables" }
```

The retrieval function returns only chunks in force on the query date (default today): `effective_date <= as_of AND (valid_until IS NULL OR valid_until > as_of)` — see §4.3. It also returns the SOP version number and page reference in the output so the dispatcher can look up the source document themselves.

---

## 4. Database Design — Every Table, Every Column, Explained

ColdTrace uses PostgreSQL (hosted on Render's free tier or a local Docker container). The reason for switching from Microsoft SQL Server: MSSQL requires Docker and ODBC drivers, making it hard to deploy for free and share publicly as a portfolio piece. PostgreSQL works with SQLAlchemy directly, deploys to Render in one click, and removes the biggest setup barrier for anyone reviewing the project.

### 4.1 `trucks` — Master Register of All Vehicles

Every truck the company operates. Created once and rarely updated.

| Column | Type | What it means |
|---|---|---|
| truck_id | VARCHAR(20) PK | A unique identifier like 'TRK-001'. Every truck has one. |
| plate_number | VARCHAR(20) | The licence plate. Useful for cross-referencing physical records. |
| cargo_type | VARCHAR(50) | What it carries: 'fresh_perishables', 'frozen', 'vaccines', 'insulin'. This determines which SOP thresholds apply. |
| min_temp_c | FLOAT | The minimum safe temperature for this cargo. E.g. 0.0 for fresh produce. |
| max_temp_c | FLOAT | The maximum safe temperature. E.g. 4.0 for fresh produce. Comes from the SOP. |
| home_depot | VARCHAR(100) | Which depot this truck is based at. Used to calculate return-route costs. |
| created_at | TIMESTAMPTZ | When this truck was added to the system. Audit trail. |

### 4.2 `telemetry` — Every Sensor Reading From Every Truck

This is the biggest table. A new row is inserted roughly every 10 minutes per truck. Data quality is **not** stored here — it is computed at query time (see below), which is the key differentiator from the reference project.

| Column | Type | What it means |
|---|---|---|
| reading_id | BIGSERIAL PK | Auto-incrementing ID for each sensor reading. |
| truck_id | FK → trucks | Which truck sent this reading. |
| shipment_id | VARCHAR(20) | The load this reading belongs to, e.g. 'SHP-014-03'. A truck carries a new shipment on each leg. Used by `get_temperature_history()`. |
| recorded_at | TIMESTAMPTZ | Exactly when this reading was taken on the truck's onboard system. |
| ingested_at | TIMESTAMPTZ | When our system received the reading. The gap between recorded_at and ingested_at reveals connectivity lag. |
| lat | FLOAT | GPS latitude. Combined with lon, tells us exactly where the truck is. |
| lon | FLOAT | GPS longitude. |
| trip_status | ENUM `trip_status` | One of `in_transit`, `at_depot`, `completed` (PostgreSQL enum type, so the column can never hold freeform strings). A truck parked at a depot is expected to have static GPS; one 'in_transit' is not — this is what the GPS_FROZEN check keys on. |
| temperature_c | FLOAT | The fridge temperature reading in Celsius. |
| cargo_condition_code | VARCHAR(10) | A code from the truck system: OK, WARN, CRIT. |
| delay_probability | FLOAT | Model score 0.0–1.0 of how likely this shipment is to be delayed. |
| route_risk_index | FLOAT | Overall route risk score from the logistics platform. |

#### `vw_fleet_with_quality` + quality gate — computed `data_quality_flag`

| Column | Type | What it means |
|---|---|---|
| data_quality_flag | VARCHAR(30) (computed) | CLEAN, STALE_SENSOR, OUT_OF_RANGE, GPS_FROZEN, or STALE_FEED. Not stored in the telemetry table. Computed at query time by the `apply_quality_checks()` function in `src/data_quality.py`, which wraps every database result before it reaches the agent. Storing it would mean flags go stale between the time of insert and the time of query. The view `vw_fleet_with_quality` joins each reading to its truck's cargo thresholds and is the only telemetry object the agent role can read; the tool functions fetch from it and `apply_quality_checks()` adds the flag. The checks live in Python, not SQL, so there is one tested implementation. THIS DOES NOT EXIST IN THE REFERENCE PROJECT. |

### 4.3 `sop_chunks` — The Rulebook, Chunked for AI Retrieval

The company's SOP document is split into small chunks (about 200 words each) and stored here, along with the vector embedding that makes semantic search possible. Stored in Qdrant vector database — not PostgreSQL — but the metadata fields are described here for clarity.

| Field | Type | What it means |
|---|---|---|
| chunk_id | UUID | Unique ID for this chunk. |
| content | TEXT | The actual SOP text, e.g. 'If temperature exceeds 4.0°C, dispatcher must contact driver within 5 minutes.' |
| section | VARCHAR(20) | Which section of the SOP: '1.1', '3.2', etc. Cited in the agent response. |
| version | VARCHAR(10) | Document version: '2.4', '3.0', etc. |
| effective_date | DATE | When this rule came into force. Used to filter out old versions. |
| superseded_by | VARCHAR(10) NULLABLE | Version that replaced this chunk's document (e.g. '3.0'); NULL for the current version. THIS DOES NOT EXIST IN THE REFERENCE PROJECT. |
| valid_until | DATE NULLABLE | Effective date of the superseding version. Retrieval returns only chunks in force on the query date: `effective_date <= as_of AND (valid_until IS NULL OR valid_until > as_of)`. With `as_of` = today this equals `superseded_by IS NULL`, except that a newer version loaded before its effective date does not hide the rule still in force — and an auditor can ask which rule applied on a past date. |
| cargo_type_scope | VARCHAR(50) | Which cargo type this rule applies to. Narrows retrieval to relevant rules only. |
| embedding | VECTOR(384) | The numerical fingerprint of this chunk's meaning, generated by BAAI/bge-small-en-v1.5 (384-dimensional). Locked choice: fits Render free tier's 512 MB RAM, where bge-m3 (1024-dim) does not. |

### 4.4 `audit_log` — Every Decision, Forever

Every single agent reasoning cycle writes one row here — one row per dispatcher question, not one row per tool call. Each dispatcher accept/override appends one further row that amends it. Rows are never updated or deleted. This table is the most enterprise-credible part of the project.

| Column | Type | What it means |
|---|---|---|
| log_id | SERIAL PK | Auto-incrementing row number. |
| amends_log_id | INT NULLABLE (FK → audit_log.log_id) | NULL for agent-generated rows. For human-decision rows, the `log_id` of the original agent row being accepted or overridden — links the two without modifying the original. |
| session_id | UUID | Which dispatcher session this came from. One session = one browser tab. |
| created_at | TIMESTAMPTZ | Exact timestamp, set in Python before the insert — no database default. `row_hash` is computed in Python *before* the INSERT, so it must know `created_at` at that moment. If the database filled it in, the stored value would differ from the hashed one, and reconciling them would need an UPDATE, which the agent role does not have. Cannot be modified after insert. |
| dispatcher_question | TEXT | The exact question the dispatcher typed. Verbatim. |
| tool_calls | JSONB | Array of all tool calls made during this reasoning cycle, each as `{tool_name, input, output, quality_flags}`. One row per dispatcher question, not one row per tool call. Makes the sample query in §10.2 unambiguous: `SELECT * FROM audit_log WHERE tool_calls @> '[{"tool_name":"get_truck_telemetry"}]'`. |
| data_quality_flags | JSONB | Any quality flags raised during this reasoning cycle. |
| final_recommendation | TEXT | The agent's full text recommendation. |
| sop_clause_cited | VARCHAR(100) | e.g. 'SOP v2.4 §1 — Cold-Chain Breach Protocol.' Extracted from the recommendation. |
| human_decision | VARCHAR(20) | accepted / overridden / pending. Set by the dispatcher via a UI button. |
| override_reason | TEXT | If overridden, why. Blank if accepted. |
| token_count_in | INTEGER | How many tokens the question and context consumed. |
| token_count_out | INTEGER | How many tokens the response used. |
| previous_row_hash | CHAR(64) | SHA-256 hash of the previous row. Breaks if any prior row is altered. |
| row_hash | CHAR(64) | SHA-256 of all fields in this row plus previous_row_hash. Tamper detection. |

**Concurrency caveat:** if two agent runs insert simultaneously, both could read the same previous row and fork the chain. Mitigation: the insert function runs inside a single transaction that first takes a transaction-scoped advisory lock (`SELECT pg_advisory_xact_lock(<audit_chain_key>)`), then reads the last row's hash (`SELECT row_hash FROM audit_log ORDER BY log_id DESC LIMIT 1`), then inserts. Only one writer can hold the lock, so inserts are serialised and the chain cannot fork. (`SELECT MAX(log_id) ... FOR UPDATE` does not work: PostgreSQL rejects `FOR UPDATE` on aggregate queries, and a row lock alone does not stop a second writer reading the same "last" row.) The timestamp is set in Python before the insert, not by the database default, so the hash includes a value we control. A `verify_chain()` utility function checks the full chain on demand and reports the first broken link.

**Hash scope:** the audit log is append-only — no row is ever updated after insert. Agent rows are written with `human_decision = 'pending'`. When the dispatcher accepts or overrides (step 9 of §5.2), a *new* hashed row is appended with `amends_log_id` set to the original row's `log_id` and `human_decision` / `override_reason` filled in. The original row and its hash stay untouched, so dispatcher decisions never break the chain. The current decision for a question is the latest row that amends it.

---

## 5. System Architecture — Every Component and Why

### 5.1 Technology Choices and Reasons

| Layer | Technology | Why this, not something else | Connection to your existing skills |
|---|---|---|---|
| Database | PostgreSQL | Free on Render. No ODBC driver. Works with SQLAlchemy natively. Eliminates the biggest setup barrier the reference project has. | You use BigQuery daily. PostgreSQL is the same SQL dialect, different engine. Trivial transfer. |
| Vector Store | Qdrant | You already run it in your Agentic Data Analyst project. No new account, no new SDK, no new mental model. | Direct reuse. Say this explicitly in interviews. |
| Orchestration | LangGraph | Reference project uses it. You have Cadence orchestration experience. The two-node pattern (Gather/Recommend) is a LangGraph-native concept. | Cadence project — same mental model. |
| LLM | DeepSeek via API (api.deepseek.com) | Dramatically cheaper than GPT-4o. Comparable reasoning quality for structured tool-use tasks. Avoids the Ollama local-only limitation of the reference project. Model ID is read from `.env` as `DEEPSEEK_MODEL`. Set this to whatever `GET api.deepseek.com/v1/models` returns as the current production model at build time. | New choice. Justify it as a cost-aware engineering decision — exactly what an FDE should do. |
| Embeddings | BAAI/bge-small-en-v1.5 (384-dim) via fastembed | The deploy target is Render free tier (512 MB RAM). bge-m3 (the reference project's choice, ~2.2 GB in fp32) does not fit; bge-small does, and the SOP corpus is short English text where the quality gap is small. Served through fastembed (ONNX runtime, no PyTorch): sentence-transformers would pull in PyTorch, which alone would eat most of the 512 MB. | Same model family as the reference project; fastembed is Qdrant's own embedding library. |

**Reference project LLM note:** the reference `orchestrator.py` defaults to Ollama (`qwen2.5:7b`) when the `Agent_llm` env var is not set, and has a `DEEPSEEK` branch that uses the model string `deepseek-v4-flash`. Verify this ID against `api.deepseek.com/v1/models` before using it — DeepSeek has historically used versioned strings that may point to a current model or may be retired. Local development without a DeepSeek key uses Ollama; the provider and model are both read from `.env`, so nothing changes structurally.
| Web Framework | FastAPI + Streamlit | FastAPI separates the agent logic from the UI, making the architecture testable. Streamlit serves the UI. Reference project mixes them. | FastAPI is already in your stack. |
| Deployment | Render (free tier) | Same platform as your Agentic Data Analyst app. You already know the deploy flow. | One click. Known platform. |
| Containerisation | Docker Compose | Matches the reference project approach. Lets you run Postgres + Qdrant + the app together locally. | You already use Docker. |

### 5.2 Request Flow — What Happens When a Dispatcher Types a Question

Traced through every layer, step by step:

1. **User input.** Dispatcher types in the Streamlit chat box. Streamlit sends the text to FastAPI via HTTP POST.
2. **Session management.** FastAPI assigns a session UUID if one doesn't exist. This UUID ties all audit log rows for this conversation together.
3. **LangGraph ROUTER.** The router node reads the question and decides its intent: fleet status query, temperature alarm, route query, or SOP question. This is real intent routing, unlike the reference project's fixed three-step order.
4. **GATHER NODE.** Based on the intent, the gather node calls only the tools it needs. For a pure SOP question, it only calls `search_sop()`. For a temperature alarm, it calls `get_truck_telemetry()` and `fetch_route_conditions()`. It does not call all three tools on every question.
5. **Data Quality Gate.** Every database result passes through the validation function. Flags are added to the tool output. The gather node returns: raw facts + quality flags. No recommendations yet.
6. **RECOMMEND NODE.** Receives only the structured output from the gather node. Reasons over it. Produces: a recommendation, the SOP clause cited, and a confidence level. Outputs token counts.
7. **Audit Log Write.** FastAPI writes one row to the `audit_log` table: question, the `tool_calls` array (inputs, outputs, flags per call), aggregated quality flags, recommendation, SOP citation, token counts, and the hash chain fields.
8. **Response to UI.** Streamlit receives the structured response and renders it in two panels: Evidence Panel (what the gather node found) and Verdict Panel (what the recommend node concluded).
9. **Human decision.** The dispatcher clicks 'Accept' or 'Override' in the UI. FastAPI appends a new hashed audit row with `amends_log_id` pointing at the original row and `human_decision` set. If overridden, the dispatcher types a reason. The original row is never modified.

---

## 6. Repository Structure — Every File and What It Does

The structure follows the separation of concerns principle: data, logic, API, and UI are in separate folders. Nothing is mixed together the way the reference project mixes agent logic and UI logic in a single file.

| File / Folder | What it does |
|---|---|
| `data/raw/` | The original Kaggle dataset CSV. Never modified after initial load. |
| `data/synthetic/` | The synthetic data generator output. Trucks table, telemetry with injected faults. |
| `data/policy/` | The SOP markdown files. Each version is a separate file. Supersession is tracked in the filename metadata. |
| `scripts/generate_data.py` | Generates the synthetic telemetry dataset with realistic fault injection: stuck sensors, GPS dropouts, temperature spikes. Run once. |
| `scripts/setup_db.sql` | Creates all PostgreSQL tables and `vw_fleet_with_quality`. Grants the agent role exactly SELECT + INSERT on audit_log, USAGE on its sequence, and SELECT on the view — nothing else. The reference project's permission bug is fixed here. |
| `scripts/ingest_telemetry.py` | Loads synthetic data into PostgreSQL. Creates the curated views. |
| `scripts/ingest_sop_qdrant.py` | Chunks SOP documents (one chunk per section), embeds them with BAAI/bge-small-en-v1.5 via fastembed, loads into Qdrant with version metadata. |
| `src/data_quality.py` | THE KEY FILE. Validation functions: `detect_stale_sensor()`, `detect_out_of_range()`, `detect_gps_frozen()`, `detect_stale_feed()`, wrapped by `apply_quality_checks()`. Returns a `data_quality_flag` for each reading. |
| `src/tools/telemetry.py` | The four parameterised query functions. No raw SQL accepted from the LLM. Each is fetch-then-validate, not a plain `SELECT WHERE` wrapper: fetches the lookback window per truck, runs `apply_quality_checks()` over it, returns flagged rows for the requested scope. |
| `src/tools/weather.py` | Route-interpolated weather: fetches conditions at multiple points along the truck's route, not just current GPS position. |
| `src/tools/sop.py` | SOP retrieval with version filtering. Returns chunk content + section reference + version number. |
| `src/audit.py` | Audit log writer. SHA-256 hash chain with advisory-lock serialisation. Structured JSONB writes. Human decision updater. `verify_chain()`. |
| `src/orchestrator.py` | LangGraph graph definition. Three nodes: router, gather, recommend. No fixed tool order: the intent decides which tools gather is offered. Tool errors are fed back for self-correction; verdict guards check citations, truck IDs and quality flags. `run_query()` writes the audit row. |
| `src/tools/registry.py` | The six tools as the LLM sees them: argument schemas (the only thing the model controls), runners, and a compact view of each result for the model's context alongside the full result for the audit log. |
| `scripts/ask.py` | CLI: asks one question, prints Evidence and Verdict separately, then the audit row and chain status. The Phase 3 checkpoint. |
| `src/prompts/` | System prompt files. Gather node and recommend node have separate prompts. The gather node is instructed to return only facts; the recommend node is instructed to never invent data. |
| `api/main.py` | FastAPI application. Separates HTTP logic from agent logic: each route validates input, calls one function in `src/`, maps errors to status codes. `POST /query` (question → evidence, verdict, `log_id`), `POST /decision` (accept/override by `log_id`; appends a row, session taken from the amended row), `GET /audit` (filter by session, tool, current decision; keyset pagination; tool inputs and flags, not full outputs), `GET /audit/{log_id}` (full row plus its decision history), `GET /audit/verify` (walks the hash chain), `GET /health`. |
| `ui/app.py` | Streamlit frontend; talks only to the API over HTTP, never to the database. Dispatch console: each answer shows the Evidence panel (every tool call, its input, a table of readings, the temperature chart for history calls, SOP text, route hazards) beside the Verdict panel (recommendation, SOP citation, confidence, data quality flags as icon + label, caveats, Accept / Override with a required reason). Footer per answer: intent, clock, tokens, cost per query, audit row and hash. Audit tab: filters (session, tool, decision) in one row, keyset paging, row inspector, hash-chain check. |
| `ui/charts.py` | Temperature-vs-safe-range chart (Altair): one line, labelled safe band, warning-status markers on flagged readings, crosshair tooltip; colors validated for Streamlit's light and dark surfaces; the readings table is its accessible twin. |
| `ui/client.py`, `ui/view.py` | HTTP client for the API; pure formatting helpers (evidence summaries, readings table, cost per query). |
| `docker-compose.yml` | Runs PostgreSQL + Qdrant + the app together. Single command to run everything locally. |
| `tests/` | Unit tests for the data quality functions, the parameterised query functions, and the audit hash chain. Run in CI. |
| `.github/workflows/ci.yml` | GitHub Actions: lint, type-check, run tests on every push. |

---

## 7. Build Plan — Six Phases, Always Shippable

The phases are ordered so something demoable exists after every single phase. If job search suddenly needs full attention, you stop after any phase and still have something to show. Never leave a half-built thing.

| Phase | What you build | What you can demo after | Estimated time | Key files created |
|---|---|---|---|---|
| 0 | Project scaffold. Folder structure. Docker Compose with Postgres + Qdrant. CI workflow skeleton. | `docker-compose up` works. CI pipeline passes on empty tests. | 2–3 hours | `docker-compose.yml`, `.github/workflows/ci.yml` |
| 1 | Data generation and ingestion. Synthetic trucks + telemetry with fault injection. PostgreSQL tables and views created. `setup_db.sql` with fixed permissions. | Can run SELECT queries against the curated view. Data quality flags visible in results. | 1 day | `scripts/generate_data.py`, `scripts/setup_db.sql`, `src/data_quality.py` |
| 2 | SOP ingestion to Qdrant. Version metadata. Supersession filtering. Retrieval function returns section reference and version number. | Python script that asks a test question and returns the correct SOP clause with citation. | 3–4 hours | `scripts/ingest_sop_qdrant.py`, `src/tools/sop.py` |
| 3 | LangGraph orchestrator. Router + Gather + Recommend nodes. Parameterised query tools. Audit log writer with hash chain. | CLI script that takes a question, prints Evidence and Verdict separately, and shows an audit log row in the database. | 1–2 days | `src/orchestrator.py`, `src/tools/telemetry.py`, `src/tools/weather.py`, `src/audit.py` |
| 4 | FastAPI layer. `POST /query`, `POST /decision`, `GET /audit` routes. Separates HTTP from agent logic. | curl command that sends a question and gets back structured JSON with evidence + verdict. | 4–6 hours | `api/main.py` |
| 5 | Streamlit UI. Chat console, evidence panel, verdict panel, audit tab with queryable filters, cost-per-query footer. | Full end-to-end demo. Screenrecordable. Portfolio-ready. | 1 day | `ui/app.py` |
| 6 | Deploy to Render. README with architecture diagram. Loom walkthrough. Portfolio page update. | Public URL. Listed on portfolio site. | 4–6 hours | `README.md`, Render deploy config |

---

## 8. Known Limitations — What This Project Does Not Do

Being upfront about limitations is what distinguishes an engineer from someone who built a demo. Every point below is stated plainly, without apology, because scoping is a professional skill. This section appears verbatim on the portfolio page.

| Limitation | What it means in plain English | What a production version would need |
|---|---|---|
| Synthetic data | The truck telemetry is generated by a Python script, not a real IoT fleet. A real fleet would require a live MQTT or Kafka feed. | A real IoT ingestion pipeline. Out of scope for a portfolio project — no company shares live fleet data publicly. |
| Hash chain tamper evidence only | The audit log's SHA-256 chain detects tampering but does not physically prevent it — someone with direct database access could delete rows. A real WORM store (Amazon S3 Object Lock, AWS CloudTrail) prevents deletion. | A certified immutable log service. The portfolio version demonstrates the pattern, not a compliance-grade implementation. |
| Single tenant | No user authentication, no roles, no multi-organisation separation. One set of credentials, one audit log. | Auth0 or similar identity provider, row-level security in PostgreSQL, RBAC per organisation. |
| Route weather is still point-in-time | The weather tool fetches conditions along a route by interpolating several GPS waypoints. It is better than the reference project's single-point lookup, but it is not a certified route forecast. | A paid route weather API (Tomorrow.io, ClimaCell) with true segment-by-segment forecasting. |
| SOP is a minimal mock document | The SOP used is a short mock document with three rules. A real cold-chain SOP for a regulated carrier is hundreds of pages covering dozens of cargo types. | A real SOP document and a more sophisticated chunking strategy (semantic chunking, not fixed-size). |
| Local LLM verdict quality | Development runs on a local 7B model (qwen2.5:7b via Ollama). The pipeline — routing, tool use, evidence, citation, audit — works end to end, but the 7B model's reasoning is unreliable: in testing it named the right truck but gave the wrong reason and cited the temperature rule for what was a delay problem. The guards catch invented citations and invented trucks, not a wrong reason for a real truck. | A stronger hosted model (DeepSeek via `LLM_PROVIDER=deepseek`), plus an evaluation set of dispatcher questions with expected verdicts, run on every prompt or model change. |
| No feedback loop learning | When a dispatcher overrides the agent's recommendation, that override is logged but does not retrain the model. The system does not get smarter over time automatically. | Fine-tuning pipeline or RLHF system using the override logs as preference data. |

---

## 9. Portfolio Page Design — How This Looks on Your Website

Your CTO's feedback from our earlier session: 'too goody, marketing vibe, looks like a Claude dump.' The following spec applies the decisions already locked in for your portfolio site to this specific project page. Nothing is being reopened — just applied.

### 9.1 Standing Design Rules (Already Decided, Carried Forward)

| Element | Decision |
|---|---|
| Font: Headings | Space Grotesk — technical, not 'friendly AI' |
| Font: Body | Inter — clean and neutral |
| Font: Code / Numbers | JetBrains Mono — makes statistics read like terminal output, not marketing stats |
| Background | #0F0F0F near-black (never pure #000000) |
| Accent | Green / Lime — amber is retired |
| Decorative element | Dot-grid / particle cursor background retained, recoloured green |
| Forbidden | No rounded gradient cards. No stock icons in circles with soft shadows. No carousel. No 'revolutionise,' 'seamless,' 'cutting-edge,' or 'game-changing.' |

### 9.2 Project Page Structure, Block by Block

**Block 1 — Hero (Above the Fold)**
No image. No gradient banner. Just typography on the dark background with the dot-grid behind it.

- Heading (Space Grotesk, large): **ColdTrace**
- Subheading (Inter, muted): "An AI copilot for cold-chain dispatchers that shows its evidence before it recommends anything."
- Two CTA buttons: `[Live Demo ↗]` `[GitHub ↗]`
- Footer line (JetBrains Mono, tiny, muted): `4 tables · 6 tools (4 SQL · 1 weather · 1 SOP) · 3 LangGraph nodes (router · gather · recommend) · cost/query: logged · audit log: hash-chained`

Fill in the real cost/query number once a test call has been made and the token log checked.

**Block 2 — The Problem (Grade 5 Language, No Jargon)**
Three short paragraphs. The problem. The old way. The new way. This is the only block written for a non-technical reader. Everything after it is for technical reviewers who scroll further. No bullet points. No bold highlights. Plain prose that reads in 30 seconds.

**Block 3 — Architecture Diagram**
Clean SVG rendered inline. Monochrome lines for all inherited components (the SQL view, the weather API, the vector store). Green accent colour for the five additions you made (data quality gate, parameterised tools, two-node split, structured audit log, SOP versioning). The visual communicates 'I extended this' without a caption saying it.

**Block 4 — What I Changed and Why (The Credibility Table)**
The seven gaps from Section 2.2 of this document, formatted as a compact table. File name and line number included. This is the heaviest credibility-lifting section — keep it prominent. Do not bury it below the fold.
Column headers: `Gap | Reference code | What ColdTrace does instead`

**Block 5 — Loom Walkthrough (90 seconds)**
Embedded video. Covers: one real dispatcher question typed into the chat, evidence panel populating (gather node), verdict panel with SOP citation (recommend node), jump to audit tab showing the logged row with all structured fields visible, footer showing cost-per-query. No talking head. Screen only. Narrate in real time. No post-production required.

**Block 6 — Known Limitations**
The table from Section 8 of this document, pasted verbatim. Stated plainly, not apologetically. This section — present and confident on a portfolio page — is itself a signal of seniority that most candidates miss.

**What this page must not do:**

- → No auto-playing carousels of 'features'
- → No 'our solution' language — first person only
- → No rounded gradient stat cards
- → No ChatGPT-style purple/blue gradients
- → No stock photo of a smiling woman with a headset looking at a holographic dashboard

---

## 10. How to Talk About This in an Interview

Memorise the structure below. Practise saying it out loud three times before any interview. The goal is to sound like you built this with intent — because you did.

### 10.1 The 90-Second Version

| Beat | What you say |
|---|---|
| The context (10 sec) | 'I studied a well-known FDE project pattern — cold-chain logistics AI copilot, SQL plus RAG plus LangGraph agent. Instead of cloning it, I read every file line by line and compared the code against their own Technical Design Document.' |
| The finding (20 sec) | 'I found seven gaps between what they documented and what they shipped. The biggest one: their TDD claims immutable auditing, but their SQL setup script never grants the INSERT permission their code needs. The audit logging silently does nothing. Their SQL security is a single startsWith check on LLM-generated text. Their agent does not actually reason about tool selection — the system prompt hard-codes a fixed three-step order.' |
| What I built differently (30 sec) | 'I rebuilt the pattern with five specific improvements: parameterised queries instead of agent-authored SQL, which makes SQL injection through the agent structurally impossible, not just filtered — prompt injection is still a residual risk I mitigate separately. A data quality gate that flags stale or out-of-range sensor readings before the agent reasons over them. A two-node LangGraph graph that separates evidence collection from recommendation, so the dispatcher sees both. A structured, hash-chained audit log with the permission bug fixed. And SOP version filtering so the agent only retrieves current rules.' |
| The honest scope (20 sec) | 'I also say directly on the project page what it does not do. It runs on synthetic data. The hash chain is a pattern demonstration, not a certified WORM store. It is single-tenant. I scope my own work rather than oversell it — that is the difference between a demo and an engineered system.' |
| The skills transfer (10 sec) | 'The data quality gate reuses the same mindset as dbt testing — validating at the source layer. The orchestration reuses the LangGraph experience from my Cadence project. The Qdrant RAG layer is the same infrastructure I already run in my Agentic Data Analyst app. I chose tools I knew so the differentiation comes from the engineering decisions, not from learning new frameworks.' |

### 10.2 Questions You Will Be Asked and How to Answer

| Question | Answer |
|---|---|
| 'How is this different from the tutorial project?' | Name two specific files and what you changed. Reference gap #2 (`agent_tools.py` line 104) and gap #1 (`setup_security_and_view.sql` missing the INSERT grant). Do not say 'I added my own twist' — say the file name and the line. |
| 'What would it cost to run this at our scale?' | 'Every query logs its token count. At 50,000 queries a month on DeepSeek, the LLM cost is approximately $X. Postgres on Render free tier handles this volume. Qdrant free tier handles up to 1M vectors. I have the numbers because I built cost tracking in from the start.' (Fill $X from the token log using the model configured in `DEEPSEEK_MODEL` and its current pricing at api.deepseek.com.) |
| 'Can I see the audit trail for a past decision?' | Yes. The audit_log table has one row per question, with a structured `tool_calls` JSONB array and quality flags. You can query: `SELECT * FROM audit_log WHERE tool_calls @> '[{"tool_name":"get_truck_telemetry"}]' AND human_decision = 'overridden'`. That query is possible because I used JSONB, not a text blob. |
| 'What would you add next?' | 'The most valuable next step is closing the feedback loop — using dispatcher override logs as preference data for fine-tuning the recommend node. Right now overrides are logged but not used. That is the difference between a static system and one that improves.' |
| 'Why PostgreSQL instead of SQL Server?' | 'Three reasons: no ODBC driver dependency, native SQLAlchemy support, and free deployment on Render. The reference project requires Docker plus a Microsoft-specific ODBC driver, which is a significant setup barrier. My choice removes that barrier without sacrificing any architectural principle.' |

---

## 11. Summary — What Is New, What Is Reused, What Is Honest

### 11.1 What ColdTrace adds that the reference project does not have

| Addition | Where the reference project has nothing equivalent |
|---|---|
| Parameterised query functions — no agent-authored SQL | `agent_tools.py` line 104: raw SQL string from LLM accepted after a single startswith check |
| Data quality gate with four validation checks | Zero validation between database and agent anywhere in the codebase |
| Two-node graph: Gather / Recommend separated | `orchestrator.py`: single reasoner node, fixed three-tool order in system prompt |
| Structured JSONB audit log (15 columns, append-only, queryable) | Single `NVARCHAR(MAX)` Content blob, not queryable without text parsing |
| SHA-256 hash chain on audit rows | No tamper detection whatsoever |
| INSERT permission correctly granted in SQL setup script | INSERT grant missing from `setup_security_and_view.sql` — audit log silently fails |
| SOP version and supersession filtering | k=2 retrieval with no date or version filter |
| Cost-per-query tracking in UI footer | No token tracking anywhere |
| Human decision capture (accept / override + reason) | No human feedback loop |

### 11.2 What is reused and why that is the right call

LangGraph (Cadence experience), Qdrant (Agentic Data Analyst infrastructure), FastAPI (existing stack), BGE embeddings (same model family as the reference project; downsized from bge-m3 to bge-small-en-v1.5 and served via fastembed instead of sentence-transformers to fit the free-tier memory limit), Docker Compose (reference project approach). Reusing known tools means the differentiation comes from engineering decisions, not from learning new frameworks under time pressure.

### 11.3 What is honestly out of scope

Synthetic data. Hash chain as pattern demonstration only (not WORM-certified). Single tenant. Mock SOP document. Route weather as multi-point interpolation (not certified forecast). No fine-tuning feedback loop.

---

## 12. Development Rules

### 12.1 Git attribution (hard rules, no exceptions)

- All commits must be authored and committed as: `Prathamesh Mishra <mprathamesh2023@gmail.com>`
- `Co-authored-by` trailers are forbidden in all commit messages.
- No AI tool name (Claude, Copilot, ChatGPT, or any other) may appear in commit author, committer, message body, or PR description.
- AI-assisted tooling must have automatic attribution disabled. For Claude Code this is set in the committed `.claude/settings.json` (`"includeCoAuthoredBy": false`).
- Enforcement, not memory: `.githooks/commit-msg` rejects any commit whose message contains a `Co-authored-by` trailer or an AI tool name, or whose author/committer is not the address above. Enable it once per clone with `git config core.hooksPath .githooks`.
- Sole-author check before every push — every line must be the author above:

  ```bash
  git log --format="%an <%ae> | %cn <%ce>" | sort -u
  ```

---

*This document is the source of truth for the build. Every section is actionable and traceable to a specific file, line, or decision. Build in phase order. Never skip a phase checkpoint. Deploy and document before moving to the next.*
