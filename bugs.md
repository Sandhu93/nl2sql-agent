# Bug Log — NL2SQL Agent

Chronological record of every bug, error, and issue encountered during development, along with the root cause and the fix applied.

---

## #1 — `npm ci` fails: no `package-lock.json`

**Symptom**
```
npm error code EUSAGE
npm error The `npm ci` command can only install with an existing package-lock.json
```

**Root cause**
The frontend `Dockerfile` used `npm ci`, which requires a pre-existing `package-lock.json`. The file had never been generated because `npm install` was never run locally.

**Fix**
Changed `npm ci` → `npm install` in `frontend/Dockerfile` so the lockfile is generated inside the image on first build.

```dockerfile
# Before
RUN npm ci --legacy-peer-deps

# After
RUN npm install --legacy-peer-deps
```

---

## #2 — Docker Desktop not running

**Symptom**
```
open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.
```

**Root cause**
Docker Desktop was installed but not started. The Linux engine named pipe did not exist yet.

**Fix**
Started Docker Desktop and waited for the tray icon to become stable before re-running `docker compose up --build`.

---

## #3 — `version` key in `docker-compose.yml` is obsolete

**Symptom**
```
the attribute `version` is obsolete, it will be ignored
```

**Root cause**
Docker Compose v2 no longer requires or uses the top-level `version:` key.

**Fix**
Removed the `version: "3.9"` line from `docker-compose.yml`.

---

## #4 — Health check fails: `curl` not found in container

**Symptom**
```
dependency failed to start: container nl2sql_backend is unhealthy
```

**Root cause**
The health check used `curl -f http://localhost:8086/health`, but `python:3.11-slim` does not include `curl`.

**Fix**
Added `curl` to the `apt-get install` block in `backend/Dockerfile`.

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*
```

---

## #5 — Next.js security vulnerability (CVE in 14.2.3)

**Symptom**
```
npm warn deprecated next@14.2.3: This version has a security vulnerability.
```

**Root cause**
`package.json` pinned `next` at `14.2.3`, which contained a known CVE.

**Fix**
Bumped `next` and `eslint-config-next` to `14.2.29` (patched release) in `frontend/package.json`.

---

## #6 — Wrong database driver (MySQL instead of PostgreSQL)

**Symptom**
Backend failed to connect — `pymysql` driver used a `mysql+pymysql://` URL against a PostgreSQL server.

**Root cause**
Initial scaffold was written for MySQL; project uses PostgreSQL.

**Fix**
- Replaced `pymysql` with `psycopg2-binary` in `requirements.txt`
- Changed connection URL scheme to `postgresql+psycopg2://` in `config.py`
- Added `db_port: int = 5432` field to `Settings`
- Replaced `default-libmysqlclient-dev` with `libpq-dev` in `backend/Dockerfile`
- Updated `.env.example` and `docker-compose.yml`

---

## #7 — CORS preflight blocked (400 on OPTIONS)

**Symptom**
```
Access to fetch at 'https://<ngrok>.ngrok-free.dev/api/query' from origin
'http://localhost:8085' has been blocked by CORS policy: Response to preflight
request doesn't pass access control check: It does not have HTTP ok status.
```
Docker logs showed `OPTIONS /api/query HTTP/1.1" 400 Bad Request`.

**Root cause**
The browser's CORS preflight `OPTIONS` request included `ngrok-skip-browser-warning` in `Access-Control-Request-Headers`. FastAPI's `CORSMiddleware` checked each requested header against the `allow_headers` list. Since `ngrok-skip-browser-warning` was not listed, it returned a non-2xx response and the browser blocked the subsequent `POST`.

**Fix**
Added `"ngrok-skip-browser-warning"` to `allow_headers` in `backend/app/main.py`.

```python
# Before
allow_headers=["Content-Type", "Authorization"],

# After
allow_headers=["Content-Type", "Authorization", "ngrok-skip-browser-warning"],
```

---

## #8 — `langchain_core` unresolved by Pylance / VS Code

**Symptom**
IDE underlined the imports with "Import could not be resolved" warnings.

```python
from langchain_core.output_parsers import StrOutputParser  # ← red squiggle
from langchain_core.prompts import PromptTemplate          # ← red squiggle
```

**Root cause**
`langchain-core` is a transitive dependency of `langchain` and IS installed in the Docker container, but it was not listed explicitly in `requirements.txt`. Pylance could not locate it without an explicit entry.

**Fix**
Added `langchain-core==0.2.5` explicitly to `backend/requirements.txt`.

---

## #9 — Unused `itemgetter` import

**Symptom**
IDE hint: `"itemgetter" is not accessed`.

**Root cause**
The tutorial combined all steps into a single LangChain LCEL chain using `itemgetter`. Our implementation uses three separate `ainvoke()` calls instead, so `itemgetter` was imported but never used.

**Fix**
Removed `from operator import itemgetter` from `agent.py`.

---

## #10 — SQL query not executed: `ORDER BY` alias error in PostgreSQL

**Symptom**
```
Error: (psycopg2.errors.UndefinedColumn) column "total_runs" does not exist
LINE 4: ORDER BY total_runs + total_wickets DESC
```

**Root cause**
`gpt-3.5-turbo` generated `ORDER BY total_runs + total_wickets` using SELECT-clause aliases. PostgreSQL does not resolve aliases in the `ORDER BY` when they are part of an expression (`alias + alias`); it requires the full expressions to be repeated.

**Fix**
Upgraded the LLM from `gpt-3.5-turbo` to `gpt-4o` in `_get_chain()`. GPT-4o correctly emits `ORDER BY SUM(...) + COUNT(...)` instead of referencing aliases.

---

## #11 — Only first code block extracted from multi-query LLM output

**Symptom**
For questions requiring two separate queries (e.g. "most runs AND most wickets"), the LLM returned two ```` ```sql ``` ```` blocks. Only the first was executed; the second was silently dropped.

**Root cause**
`_clean_sql` used `re.search(...)` which returns only the **first** regex match.

**Fix**
Replaced `re.search` with `re.findall` to collect **all** code blocks, then joined them so `_run_sql` could execute each statement.

```python
# Before — only first block
code_block = re.search(r"```(?:sql)?\s*([\s\S]*?)```", text, re.IGNORECASE)
if code_block:
    return code_block.group(1).strip()

# After — all blocks
blocks = re.findall(r"```(?:sql)?\s*([\s\S]*?)```", text, re.IGNORECASE)
if blocks:
    cleaned = [_strip_prefix_and_prose(b) for b in blocks]
    return "\n\n".join(b for b in cleaned if b)
```

---

## #12 — `SQLQuery:` prefix surviving inside a code block

**Symptom**
```
Error: (psycopg2.errors.SyntaxError) syntax error at or near "SQLQuery"
LINE 1: SQLQuery:
        ^
```

**Root cause**
The LLM sometimes placed `SQLQuery:` **inside** the code fence:
```
```sql
SQLQuery:
SELECT "bowler" ...
```
```
The old `_clean_sql` only stripped `SQLQuery:` when it appeared at the very start of the **raw** string. Once the code block was extracted, the prefix was still present and was passed directly to the database.

**Fix**
Split the prefix-stripping logic into a separate `_strip_prefix_and_prose()` helper and called it on **each extracted code block** individually, not just on the raw outer string.

```python
def _strip_prefix_and_prose(text: str) -> str:
    # strips "SQLQuery:" / "SQL:" prefix
    # then jumps to the first SELECT/WITH/... keyword
    ...

def _clean_sql(raw: str) -> str:
    blocks = re.findall(...)
    if blocks:
        cleaned = [_strip_prefix_and_prose(b) for b in blocks]  # ← applied per block
        return "\n\n".join(b for b in cleaned if b)
    return _strip_prefix_and_prose(raw)
```

---

## #13 — Multiple SQL statements rejected by psycopg2

**Symptom**
When two separate `SELECT` statements were joined with a newline and passed to `QuerySQLDataBaseTool`, psycopg2 raised a syntax error because it does not support multiple statements in a single `execute()` call.

**Root cause**
PostgreSQL's psycopg2 driver only executes one statement per call. Passing `"SELECT ...; SELECT ...;"` causes a protocol error.

**Fix**
Added `_run_sql()` which strips `-- comments`, splits on `;`, and executes each non-empty statement individually, collecting and joining the results.

```python
async def _run_sql(execute_query, sql: str) -> str:
    cleaned = re.sub(r"--[^\n]*", "", sql)
    statements = [s.strip() for s in cleaned.split(";") if s.strip()]
    if len(statements) == 1:
        return await execute_query.ainvoke(statements[0])
    results = []
    for stmt in statements:
        results.append(await execute_query.ainvoke(stmt))
    return "\n".join(results)
```

---

## #14 — LLM uses wrong CTE alias in SELECT clause

**Symptom**
```
Error: (psycopg2.errors.UndefinedColumn) column bp.total_runs does not exist
LINE 8: SELECT b.player, bp.total_runs, bp.total_wickets, ...
```

For the question *"who is the best allrounder in IPL?"*, the LLM generated a query with two CTEs aliased as `b` (batting_performance) and `bp` (bowling_performance). In the final `SELECT` it correctly used `b.player` but then referenced `bp.total_runs` — a column that lives in the `b` CTE, not `bp`.

**Root cause**
GPT-4o occasionally confuses CTE aliases when both are short and similar (`b` vs `bp`). The model wrote `bp.total_runs` instead of `b.total_runs` in the `SELECT` and `ORDER BY` clauses. This is a hallucination at the alias-resolution level that no amount of SQL cleaning can catch — it requires re-generation with the error context.

**Fix**
Added `_fix_sql()` async coroutine and a retry loop in `run_agent()`. When `_run_sql()` raises, the failing SQL, the psycopg2 error message, and the relevant table schema are fed back to the LLM, which returns a corrected query. The loop retries up to `_MAX_SQL_RETRIES = 2` times before propagating the exception.

```python
# In run_agent() — replaces the single _run_sql() call:
sql_to_run = sql
for attempt in range(1 + _MAX_SQL_RETRIES):
    try:
        result = await _run_sql(execute_query, sql_to_run)
        sql = sql_to_run   # keep corrected SQL for the response
        break
    except Exception as exc:
        if attempt == _MAX_SQL_RETRIES:
            raise
        sql_to_run = await _fix_sql(sql_to_run, question, str(exc), table_names)
```

```python
async def _fix_sql(bad_sql, question, error, table_names) -> str:
    # Builds a prompt with the failing SQL + error + schema,
    # calls _llm, and returns _clean_sql(raw_correction).
    ...
```

**Why this is better than post-processing**
The fix is generic — it handles any SQL error the LLM produces (wrong alias, non-existent column, bad function name, etc.), not just this specific CTE alias mistake.

---

## #15 — `create_extraction_chain_pydantic` deprecated: table selector returns `[]`

**Symptom**
```
2026-03-04T20:34:56 | INFO | app.agent | Tables selected: []
```
With no tables selected, `generate_query` received `table_names_to_use=[]`, so no schema was injected into the prompt. The LLM generated prose or schema-free SQL instead of a valid query.

**Root cause**
`create_extraction_chain_pydantic` from `langchain.chains.openai_tools` is deprecated in LangChain 0.2.x. When called against GPT-4o (which uses the newer tool-calling API), it silently returned an empty list instead of raising an error. The deprecation warning in the logs confirmed this:
```
LangChainDeprecationWarning: LangChain has introduced a method called
`with_structured_output` ... with_structured_output does not currently
support a list of pydantic schemas.
```

**Fix**
Replaced the entire extraction chain with `llm.with_structured_output()` using a single wrapper model `_TablesResponse` that holds a `List[str]` field, avoiding the list-of-schemas limitation:

```python
# Before (deprecated)
from operator import itemgetter
from langchain.chains.openai_tools import create_extraction_chain_pydantic
from langchain_core.pydantic_v1 import BaseModel, Field

class Table(BaseModel):
    name: str = Field(description="Name of table in SQL database.")

_select_table = (
    {"input": itemgetter("question")}
    | create_extraction_chain_pydantic(Table, llm, system_message=table_details_prompt)
    | get_tables
)

# After
from pydantic import BaseModel, Field

class _TablesResponse(BaseModel):
    names: List[str] = Field(description="Names of ALL SQL tables that might be relevant.")

_select_table = (
    ChatPromptTemplate.from_messages([
        ("system", table_details_prompt),
        ("human", "Question: {question}\n\nWhich tables are needed?"),
    ])
    | llm.with_structured_output(_TablesResponse)
    | (lambda r: r.names)
)
```

Also removed the now-unused imports: `itemgetter`, `create_extraction_chain_pydantic`, `langchain_core.pydantic_v1`.

---

## #16 — SQL generation fails silently when table selector returns empty list

**Symptom**
When bug #15 caused `table_names = []`, `generate_query.ainvoke({"table_names_to_use": []})` passed an empty list to the DB schema lookup. The LLM received no schema at all and either returned prose or generated SQL with unresolvable ORDER BY aliases.

**Root cause**
No defensive check existed between the table selector output and the SQL generation step. An empty list is a valid Python value but semantically wrong — it means "show no table schemas", leaving the LLM to guess.

**Fix**
Added a fallback in `run_agent()`: if `select_table` returns an empty list, immediately fall back to all tables from `_db.get_usable_table_names()` and log a warning:

```python
table_names: List[str] = await select_table.ainvoke({"question": question})
if not table_names:
    table_names = list(_db.get_usable_table_names())
    logger.warning("Table selector returned empty list; falling back to all tables: %s", table_names)
```

---

## #17 — `with_structured_output` incompatible with `langchain_openai==0.1.8`

**Symptom**
```
File "/app/app/agent.py", line 528, in run_agent
    table_names: List[str] = await select_table.ainvoke({"question": question})
  File ".../langchain_core/runnables/base.py", line 3981, in ainvoke
    ...
```
The `lambda r: r.names` step in the `_select_table` chain crashed because `with_structured_output` did not return a `_TablesResponse` instance — it returned something without a `.names` attribute.

**Root cause**
`llm.with_structured_output(_TablesResponse)` behaves differently across LangChain versions. In `langchain_openai==0.1.8` + `langchain_core==0.2.5`, the return type depends on the underlying method used internally (function calling vs JSON mode). With a pydantic v2 model from the standard `pydantic` package (not `langchain_core.pydantic_v1`), the version combination returned an unexpected type rather than a model instance, causing `.names` to raise `AttributeError`.

**Fix**
Replaced the `with_structured_output` approach entirely with a plain `StrOutputParser` + string split — no pydantic, no version compatibility risk:

```python
# Before (brittle)
from pydantic import BaseModel, Field

class _TablesResponse(BaseModel):
    names: List[str] = Field(...)

_select_table = (
    ChatPromptTemplate.from_messages([...])
    | llm.with_structured_output(_TablesResponse)
    | (lambda r: r.names)
)

# After (robust)
_select_table = (
    ChatPromptTemplate.from_messages([
        ("system", table_details_prompt),
        ("human",
         "Question: {question}\n\n"
         "Reply with ONLY a comma-separated list of table names. "
         "Example: deliveries,matches"),
    ])
    | llm
    | StrOutputParser()
    | (lambda raw: [t.strip() for t in raw.split(",") if t.strip()])
)
```

Also added name validation in `run_agent()` to discard hallucinated table names:
```python
available_tables = set(_db.get_usable_table_names())
raw_selection = await select_table.ainvoke({"question": question})
table_names = [t for t in raw_selection if t in available_tables]
```
