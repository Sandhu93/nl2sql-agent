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
