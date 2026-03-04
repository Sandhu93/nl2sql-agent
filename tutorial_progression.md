# Tutorial Progression — NL2SQL Agent

A chronological record of every iteration: what existed before, what changed, and why.

---

## Phase 0 — Project Scaffold

**Goal:** Production-ready full-stack template with no business logic yet.

### What was built

| Layer | Technology | Port |
|---|---|---|
| Backend | FastAPI + Python 3.11 | 8086 |
| Frontend | Next.js 14 + TypeScript + Tailwind CSS | 8085 |
| Database | PostgreSQL (ipl_db) | 5432 |
| Container | Docker Compose with shared `app_net` bridge | — |

### Key files created

- `backend/app/config.py` — pydantic-settings reads `.env`; exposes `database_url`, `openai_api_key`, `allowed_origins`
- `backend/app/main.py` — FastAPI app with CORS middleware and global error handler (no stack traces to client)
- `backend/app/routes/query.py` — `POST /api/query` endpoint; calls `run_agent()` stub
- `backend/app/agent.py` — stub that returned a hardcoded placeholder response
- `frontend/app/page.tsx` — chat UI; generates `thread_id` via `crypto.randomUUID()` per session
- `frontend/lib/api.ts` — fetch wrapper pointing at the backend
- `docker-compose.yml` — orchestrates both services; frontend `depends_on` backend health check
- `.env.example` — template for secrets (real `.env` never committed)

### Infrastructure bugs fixed during this phase

| # | Problem | Fix |
|---|---|---|
| 1 | `npm ci` fails — no `package-lock.json` | Changed to `npm install --legacy-peer-deps` in frontend Dockerfile |
| 2 | Docker Desktop not running | Started Docker Desktop before `docker compose up --build` |
| 3 | `version:` key obsolete in Compose v2 | Removed `version: "3.9"` from `docker-compose.yml` |
| 4 | `curl` missing in `python:3.11-slim` | Added `curl` to `apt-get install` in backend Dockerfile |
| 5 | Next.js 14.2.3 security CVE | Bumped `next` and `eslint-config-next` to `14.2.29` |
| 6 | MySQL driver on a PostgreSQL server | Replaced `pymysql` → `psycopg2-binary`; changed URL scheme to `postgresql+psycopg2://` |

### ngrok integration

Added an ngrok tunnel exposing the backend (port 8086) externally:

- `NEXT_PUBLIC_BACKEND_URL` baked into the Next.js bundle at Docker build time via `ARG`
- `ngrok-skip-browser-warning` added to CORS `allow_headers` and to every fetch call in `api.ts`
- Bug fixed: CORS preflight `OPTIONS` returned 400 because the ngrok header was not in the allowlist

---

## Step 1 — Basic NL2SQL

**Tutorial:** *Building a Basic NL2SQL Model*

**Goal:** Accept a natural-language question, generate SQL, execute it against the database, and return the raw result.

### What changed in `agent.py`

**Before:** stub returning `{"answer": "placeholder", "sql": ""}`.

**After:** a real three-step pipeline.

```
User question
    │
    ▼
create_sql_query_chain   ← LangChain turns NL into SQL using GPT-3.5-turbo
    │
    ▼
QuerySQLDataBaseTool     ← executes the SQL string against PostgreSQL
    │
    ▼
{"answer": <raw DB result as string>, "sql": <generated SQL>}
```

### Key additions

```python
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_community.tools.sql_database.tool import QuerySQLDataBaseTool
from langchain.chains import create_sql_query_chain
from langchain_openai import ChatOpenAI

_db = SQLDatabase.from_uri(settings.database_url)
llm = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)
_generate_query = create_sql_query_chain(llm, _db)
_execute_query  = QuerySQLDataBaseTool(db=_db)
```

- Lazy singleton pattern: DB and chain initialised on the first request, not at startup
- `ainvoke()` used throughout (FastAPI is async; `.invoke()` would block the event loop)

### Bugs fixed during this step

| # | Problem | Fix |
|---|---|---|
| 8 | Pylance could not resolve `langchain_core` | Added `langchain-core==0.2.5` explicitly to `requirements.txt` |
| 9 | Unused `itemgetter` import | Removed the leftover import from `agent.py` |
| 10 | `ORDER BY total_runs` → `UndefinedColumn` in PostgreSQL | Upgraded LLM from `gpt-3.5-turbo` to `gpt-4o`; GPT-4o repeats the full expression instead of referencing the alias |

---

## Step 2 — Rephrasing Answers for Enhanced Clarity

**Tutorial:** *Rephrasing Answers for Enhanced Clarity*

**Goal:** Instead of returning the raw DB result string to the user, have the LLM convert it into a natural-language sentence.

### What changed in `agent.py`

**Before:** raw DB result returned directly as `answer`.

**After:** a fourth step added — a `rephrase_answer` chain turns the raw result into a readable sentence. Also added `sample_rows_in_table_info=3` so the LLM sees real sample values from each table.

```
User question
    │
    ▼
create_sql_query_chain       ← NL → raw LLM output (gpt-4o)
    │
    ▼
_clean_sql()                 ← NEW: strips markdown, prefixes, prose
    │
    ▼
_run_sql()                   ← NEW: executes each statement separately
    │
    ▼
rephrase_answer chain        ← NEW: (question + SQL + result) → sentence
    │
    ▼
{"answer": <natural language sentence>, "sql": <clean SQL>}
```

### Key additions

```python
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser

answer_prompt = PromptTemplate.from_template("""
Given the following user question, corresponding SQL query, and SQL result,
answer the user question.

Question: {question}
SQL Query: {query}
SQL Result: {result}
Answer: """)

_rephrase_answer = answer_prompt | llm | StrOutputParser()
```

### SQL cleaning bugs fixed during this step

The LLM output was not always a clean SQL string — it included markdown fences, label prefixes, and explanatory prose. Three bugs were discovered and fixed:

| # | Problem | Fix |
|---|---|---|
| 11 | Only the first ```` ```sql ``` ```` block extracted | Changed `re.search` → `re.findall`; all blocks collected and joined |
| 12 | `SQLQuery:` prefix surviving *inside* a code block | Extracted `_strip_prefix_and_prose()` helper; applied per block, not just on the outer string |
| 13 | psycopg2 rejects multiple statements in one `execute()` call | Added `_run_sql()`: strips `-- comments`, splits on `;`, executes each statement individually |

### Final `_clean_sql` logic

```
Raw LLM output
    │
    ├── contains ```sql ... ``` ?
    │       └── re.findall → get ALL blocks
    │               └── _strip_prefix_and_prose() on each block
    │                       └── join non-empty blocks with "\n\n"
    │
    └── no code fences
            └── _strip_prefix_and_prose() on full text
                    └── jump to first SELECT / WITH / ...
```

---

## Step 3 — Enhancing NL2SQL with Few-Shot Examples

**Tutorial:** *Enhancing NL2SQL Models with Few-Shot Examples*

**Goal:** Steer the LLM toward correct column names, JOIN patterns, and PostgreSQL idioms by showing it worked IPL examples before it writes any SQL.

### What changed in `agent.py`

**Before:** `create_sql_query_chain(llm, _db)` — used LangChain's generic default prompt.

**After:** `create_sql_query_chain(llm, _db, prompt=_build_few_shot_prompt())` — uses a custom `ChatPromptTemplate` that injects 8 IPL-specific examples into every request.

### Prompt structure

```
[system]  Role + {table_info} + {top_k} instruction
[human]   Example question 1 \n SQLQuery:
[ai]      SELECT SUM(batsman_runs) …
[human]   Example question 2 \n SQLQuery:
[ai]      SELECT bowler, COUNT(*) …
  … (8 examples total)
[human]   Actual user question \n SQLQuery:     ← model responds here
```

### IPL examples added (`IPL_EXAMPLES`)

| Question pattern | Columns / constructs demonstrated |
|---|---|
| Total runs for a player | `SUM(batsman_runs)`, `WHERE batsman =` |
| Top run-scorers | `GROUP BY batsman`, `ORDER BY … DESC`, `LIMIT` |
| Top wicket-takers | `dismissal_kind NOT IN (…)`, `player_dismissed IS NOT NULL` |
| Most IPL titles | `WHERE match_type = 'Final'`, `winner` |
| Player of the Match awards | `player_of_match`, `COUNT(*)` |
| Sixes in a season | `JOIN matches ON match_id`, `season`, `batsman_runs = 6` |
| Highest individual score in a match | `GROUP BY batsman, match_id` |
| Most-used venue | `venue`, `matches_hosted` |

### Key new imports

```python
from langchain_core.prompts import (
    ChatPromptTemplate,
    FewShotChatMessagePromptTemplate,
    PromptTemplate,
)
```

### Why this matters

Without examples the LLM guessed column names (causing `UndefinedColumn` errors) and used MySQL idioms. With domain-specific examples it:
- Uses the correct column names from the first attempt
- Applies the right `dismissal_kind` filter for wicket queries
- Joins `deliveries` and `matches` correctly via `match_id`
- Stays within PostgreSQL syntax (no backtick quoting, no `ISNULL`)

---

## What's next — Step 4

Replace the single-chain pipeline with a **LangGraph `create_react_agent`** + `MemorySaver` so the agent can:
- Hold multi-turn conversation history per `thread_id`
- Use the `SQLDatabaseToolkit` as a proper tool set (schema inspection + query execution)
- Return the final answer from the agent's last message rather than a separate rephrase chain
