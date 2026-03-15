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

---

## #18 — SQL error correction loop never triggered: `QuerySQLDataBaseTool` returns errors as strings, not exceptions

**Symptom**
SQL with a bad CTE alias (`b.batsman` where alias `b` was never defined) failed with `missing FROM-clause entry for table "b"`. The retry loop was expected to call `_fix_sql()` and generate a corrected query, but instead the raw error string flowed straight to `rephrase_answer`, which just explained the error back to the user.

Docker log showed:
```
INFO | app.agent | Query result: Error: (psycopg2.errors.UndefinedTable) missing FROM-clause entry for table "b"
INFO | app.agent | Rephrased answer: The SQL query provided contains an error...
```

No "Corrected SQL" log line ever appeared — `_fix_sql` was never called.

**Root cause**
`QuerySQLDataBaseTool.ainvoke()` **does not raise an exception on SQL failure**. It catches the psycopg2 exception internally and returns the error as a plain string starting with `"Error:"`. The retry loop used `try/except`, which only fires on raised exceptions — so it always "succeeded" (no exception raised), broke out of the loop with `result = "Error: ..."`, and passed that error string to `rephrase_answer`.

**Fix**
Added `_is_sql_error(result)` to detect the error-string pattern, and restructured the retry loop to check the result value instead of catching exceptions:

```python
def _is_sql_error(result: str) -> bool:
    """QuerySQLDataBaseTool returns errors as strings, not exceptions."""
    return result.strip().startswith("Error:")
```

```python
# Before — try/except never fires for SQL errors
try:
    result = await _run_sql(execute_query, sql_to_run)
    sql = sql_to_run
    break
except Exception as exc:
    ...  # ← never reached for SQL errors

# After — check result string
result = await _run_sql(execute_query, sql_to_run)

if not _is_sql_error(result):
    sql = sql_to_run
    break   # success

# result is an error string → retry via _fix_sql
sql_to_run = await _fix_sql(sql_to_run, question, result, table_names)
```

---

## #19 — Allrounder query: LLM counts wickets from `batsman` column instead of `bowler` column

**Status: RESOLVED**

**Symptom**
When asking *"Who are the best allrounders in IPL history?"*, the results were either wrong (everyone showing 1 wicket) or the `rephrase_answer` chain said "the data cannot be determined" instead of presenting the rows. Example bad SQL:

```sql
SELECT
    batsman AS player,
    SUM(batsman_runs) AS total_runs,
    COUNT(DISTINCT CASE WHEN player_dismissed IS NOT NULL
          AND dismissal_kind NOT IN ('run out','retired hurt','obstructing the field')
          THEN player_dismissed END) AS total_wickets
FROM deliveries
GROUP BY batsman
ORDER BY total_runs + total_wickets DESC
LIMIT 10;
```

This counts distinct values of `player_dismissed` **grouped by `batsman`** — which yields 1 wicket for every player (a batsman's row only records themselves being dismissed, so `DISTINCT player_dismissed` = 1 at most). The fundamental error is computing bowling wickets from a batsman-grouped query.

**Root cause**
The LLM does not inherently know that:
- Batting stats require `GROUP BY batsman`
- Bowling wickets require `GROUP BY bowler`

Without a teaching example, it conflates them into a single GROUP BY on `batsman`, producing valid SQL that runs without error but returns semantically incorrect results. Because the query succeeds (no DB error), `_fix_sql` is never triggered — the bad result flows straight to `rephrase_answer`.

**Secondary symptom: `rephrase_answer` critiquing results**
Even when the corrected CTE SQL ran successfully, `rephrase_answer` sometimes said "this cannot be determined" or audited the SQL instead of presenting the data. The LLM was treating the result rows as input for analysis rather than as the answer.

**Fix — multi-pronged approach**

### Fix 1 — Tightened `rephrase_answer` prompt with explicit RULES (in `agent.py`)

The prompt was rewritten to prevent the LLM from critiquing or auditing the SQL result. This fixed the secondary symptom.

### Fix 2 — Iteratively improved allrounder few-shot example (in `prompts.py`)

Multiple iterations culminating in a full ICC-style match-points formula using `inning_ctx` CTE for context, `BOOL_AND(player_dismissed IS DISTINCT FROM batsman)` for not-out detection, separate `m_bat` (GROUP BY batsman) and `m_bowl` (GROUP BY bowler) CTEs, and `AllRounderIndex = batting_rating * bowling_rating / 1000`.

The final formula:
- Batting: `AVG(LEAST(1000, GREATEST(0, 300 + 8 * (12*LN(1+runs) + 8*not_out + 10*SR_adj + result_bonus))))`
- Bowling: `AVG(LEAST(1000, GREATEST(0, 300 + 8 * (22*wkts + 6*LN(1+wkts) + 18*econ_adj + 4*workload + result_bonus))))`
- Index: `bat_rating * bowl_rating / 1000`

### Fix 3 — Cricket Domain Knowledge RAG (cricket_knowledge.py + cricket_rules.md)

Added `backend/app/cricket_rules.md` — a comprehensive specification (~1300 lines) covering metric formulas, dismissal attribution, phase definitions, SQL generation rules, eligibility, ranking logic (including the ICC-style formula in §16.7), and SQL examples.

Added `backend/app/cricket_knowledge.py` — loads `cricket_rules.md`, splits it into chunks at `## ` heading boundaries, embeds each chunk via OpenAI embeddings, stores in ChromaDB. `retrieve_cricket_rules(question, k=3)` retrieves the 3 most relevant sections for each query and injects them into the SQL-generation system prompt as `{cricket_context}`.

### Fix 4 — System prompt `KEY SCHEMA RULES` block (in `prompts.py`)

Added explicit schema rules to the system prompt covering: `GROUP BY batsman` for batting, `GROUP BY bowler` for bowling, `wicket_fielders` table for fielding, `year` vs `season` column distinction.

**Result**: Allrounder queries now correctly use separate CTEs with proper GROUP BY, dismissal kind positive IN list, and ICC-style balanced ranking. Verified against IPL 2025 — SP Narine, HH Pandya, RA Jadeja appear in top 5 as expected.

---

## #20 — Ducks / Innings-Level Stats Computed at Ball Level (Grain Mismatch)

**Symptom**
"Which batsmen have the most ducks?" → LLM generates:
```sql
SELECT batsman, COUNT(*) AS ducks
FROM deliveries
WHERE batsman_runs = 0 AND dismissal_kind IS NOT NULL
GROUP BY batsman
ORDER BY ducks DESC LIMIT 5;
```
Returns RG Sharma = 239, V Kohli = 224 — obviously impossible (real answer: GJ Maxwell = 19, RG Sharma = 18).

**Root cause**
The LLM counts at **ball level** (each delivery where runs=0 AND a dismissal occurred) instead of at **innings level** (total innings runs = 0 AND batter was dismissed). This is fundamentally wrong because:
1. A batter can score 20 in an innings and get out on a ball with `batsman_runs = 0` — that's not a duck
2. On run-outs the `batsman` column may differ from `player_dismissed` (non-striker dismissed)
3. `COUNT(*)` on deliveries counts balls, not innings

The same grain mismatch bug applies to: half-centuries, centuries, golden ducks, batting average, and any other stat that is an **innings outcome**, not a ball event.

**Fix**
Three-layer approach:

### Fix 1 — Innings-level rules in cricket_rules.md (§21)
Added Section 21 "Innings-Level Aggregation Rules" covering:
- 21.1: Aggregation grain principle (always GROUP BY match_id, inning, batsman first)
- 21.2: Duck definition + canonical SQL pattern (CTE-based)
- 21.3–21.7: Golden duck, half-century, century, batting average, not-out detection
- 21.8: Common mistakes to avoid (explicit "NEVER DO THIS" examples)

Also added to §19 Mandatory Rules:
- "Ducks, half-centuries, centuries, and batting average are innings-level outcomes — never compute them at ball level"
- "Per-innings stats must GROUP BY match_id, inning, batsman before counting"

### Fix 2 — Few-shot examples in prompts.py
Added two new examples to `IPL_EXAMPLES`:
- **Ducks pattern**: Full CTE-based query with `batting_innings` + `dismissals` CTEs joined to count innings where runs=0 AND dismissed
- **Half-century pattern**: CTE aggregating per-innings runs, filtering `BETWEEN 50 AND 99`

### Fix 3 — System prompt rule in prompts.py
Added to KEY SCHEMA RULES:
- "INNINGS-LEVEL STATS (ducks, half-centuries, centuries, batting average): ALWAYS aggregate to per-innings level first (GROUP BY match_id, inning, batsman), then count/filter at the innings level. NEVER count these at ball level."

---

## #21 — Batting Average Outs Miscounted (player_dismissed vs batsman)

**Symptom**
"Who has the highest batting average in IPL?" → LLM generates:
```sql
COUNT(*) FILTER (WHERE player_dismissed IS NOT NULL AND dismissal_kind <> 'retired hurt') AS outs
... GROUP BY batsman
```
Returns Iqbal Abdulla = 88.00 (should be 44.00). Official IPL: Vivrant Sharma 69.00, MN van Wyk 55.66, B Sai Sudharsan ~49.8.

**Root cause**
The `outs` column counts ALL dismissals that occurred while the player was striker (`batsman`), NOT dismissals where the player themselves was out (`player_dismissed`). On run-outs, the non-striker can be dismissed while a different player is at the striker’s end. This inflates or deflates the outs count.

Iqbal Abdulla has 88 runs and 2 career dismissals (average 44.00), but the wrong query counted only 1 dismissal event while he was striker, giving 88.00.

**Fix**
Three-layer approach:

### Fix 1 — cricket_rules.md §8.3 + §21.6
Rewrote the `outs` formula in §8.3 to require `player_dismissed = batsman` and added a "NEVER DO THIS" warning against `player_dismissed IS NOT NULL` inside `GROUP BY batsman`. Added canonical two-CTE SQL pattern for batting average (runs CTE grouped by `batsman`, outs CTE grouped by `player_dismissed`, joined on player name). Expanded §21.6 with the same pattern and explicit warning.

Added to §19 Mandatory Rules:
- "Batting outs must be counted by player_dismissed, never by counting player_dismissed IS NOT NULL inside GROUP BY batsman."

### Fix 2 — Few-shot example in prompts.py
Added batting average example (#15) using the two-CTE pattern: `batting_runs` CTE (GROUP BY batsman) + `batting_outs` CTE (GROUP BY player_dismissed), joined on player name.

### Fix 3 — System prompt rule in prompts.py
Added to KEY SCHEMA RULES:
- "BATTING AVERAGE: outs must be counted by player_dismissed (who got out), NOT by counting player_dismissed IS NOT NULL inside GROUP BY batsman. Use separate CTEs: runs GROUP BY batsman, outs GROUP BY player_dismissed, then JOIN on player name."

---

## #22 — Gemini Fallback Infinite Retry Loop + Missing Rate Limit Handling

**Symptom**
Load testing with 10 concurrent users caused requests to hang for 310+ seconds (5+ minutes) before eventually returning HTTP 500. Docker logs showed continuous Gemini 404 retries with exponential backoff (2s → 4s → 8s → 16s → 32s → 60s → 60s...) that never resolved.

**Root cause**
Two compounding issues:

1. **Wrong Gemini model name**: `gemini-1.5-pro` was configured as the fallback but has been removed from Google's API (`v1beta`). Every call returns a 404 `NotFound`. LangChain's `ChatGoogleGenerativeAI` treats this as a retryable error and uses aggressive exponential backoff with no max-retry cap — a single request retried for 5+ minutes on a permanently dead endpoint.

2. **No rate limit differentiation**: When OpenAI returned 429 (TPM limit: 30,000 tokens/min), the `openai.RateLimitError` was caught by the generic `except Exception` handler and returned as HTTP 500 — giving the client no signal to back off. The fallback chain also exacerbated the problem by piling Gemini retry loops on top of OpenAI retries.

3. **No request timeout**: There was no upper bound on how long a single `/api/query` request could run. The Gemini retry loop could block a request indefinitely.

**Fix**
### Fix 1 — Updated Gemini model name in `agent.py`
Changed `gemini-1.5-pro` → `gemini-2.0-flash` and added `max_retries=2` to prevent infinite retry loops on permanent errors.

### Fix 2 — Request timeout in `routes/query.py`
Wrapped `run_agent()` in `asyncio.wait_for(timeout=60)`. Requests that exceed 60 seconds now return HTTP 504 with a clear timeout message instead of hanging.

### Fix 3 — Rate limit error handling in `routes/query.py`
Added explicit `except RateLimitError` handler that returns HTTP 429 (not 500) with a "please wait" message, giving clients proper back-pressure signals.

### Fix 4 — Updated Locust test
Updated `locustfile.py` to track 429 and 504 responses separately in reporting.

---

## #23 — Rewrite chain hallucinating answers from conversation history

**Symptom**
After 3+ turns in a session, the query rewrite chain occasionally outputs a full answer (e.g. `"The top 5 run scorers in IPL history are:\n\n- V Kohli: 7263 runs\n..."`) instead of a reformulated question. The safety guard catches it (output doesn't end with `?`), falls back to the original question, and the pipeline continues correctly — but an LLM call is wasted, and the numbers in the hallucinated answer are wrong (7263 vs actual 8671 for Kohli).

Docker log:
```
WARNING | app.agent | Query rewrite produced a non-question — falling back to original.
rewrite='The top 5 run scorers in IPL history are:\n\n- V Kohli: 7263 runs\n- S Dhawan: 6617 runs...'
```

**Root cause**
The full `history.messages` list was passed to the rewrite chain. After several turns, AI messages in the history contain entire cricket stat tables. GPT-4o reads those stats and generates an answer rather than reformulating the question — even though the system prompt says "only rewrite, never answer". With growing history (6+ turns, 12 messages), the accumulated data overwhelms the instruction.

**Fix**
Two changes:
1. Cap the history sent to the rewrite chain at the last 4 turns (8 messages) so recent context is preserved without feeding the full transcript.
2. Stop passing full conversation history into SQL generation (`messages: []`), relying on the rewritten standalone question instead.

```python
# Before
standalone_question = await rewrite_query.ainvoke({
    "history": history.messages,
    "question": question,
})

# After — cap at last 4 turns (8 messages)
rewrite_history = history.messages[-8:]
standalone_question = await rewrite_query.ainvoke({
    "history": rewrite_history,
    "question": question,
})
```

---

## #24 — Player name mismatch: full names fail in playing_xi / deliveries queries

**Symptom**
"In which teams did he [Rohit Sharma] play in IPL?" → rewrite correctly resolves to "In which teams did Rohit Sharma play in the IPL?" but the generated SQL uses `WHERE player_name = 'Rohit Sharma'` on `playing_xi`, which returns empty results.

Docker log:
```
INFO  | app.agent | Query result:
WARNING | app.agent | Empty query result | sql=SELECT DISTINCT team FROM playing_xi WHERE player_name = 'Rohit Sharma' LIMIT 5;
```

**Root cause**
Player names in `deliveries` (batsman/bowler), `playing_xi`, and `wicket_fielders` are stored in abbreviated form (`'RG Sharma'`, `'V Kohli'`). The `players` table holds both `player_name` (abbreviated) and `player_full_name` (full). When the rewrite chain resolves a pronoun to a full name like "Rohit Sharma", the LLM uses that full name directly in the query without joining `players` — the mismatch silently returns zero rows with no SQL error.

**Fix**
Primary fix: added deterministic entity resolution before SQL generation.

### Fix 1 — New `entity_resolver.py`
- Loads player index from `players (player_full_name, player_name)` once.
- Resolves full-name mentions to canonical short names used in fact tables.
- Example mapping: `Sanju Samson` → `SV Samson`.
- `run_agent()` now applies this right after query rewrite, before table selection and SQL generation.

### Fix 2 — Prompt reinforcement in `prompts.py`
- Added explicit rule that ball-by-ball and playing_xi queries should use canonical short player names.
- Added examples to steer the model toward canonical-name usage.

---

## #25 — Semantic SQL bug: innings total filtered at ball level (`batsman_runs = 119`)

**Symptom**
Follow-up questions like "What was his strike rate in that 119?" sometimes produced SQL using `WHERE batsman_runs = 119`, which is impossible at ball level and returns empty/wrong results.

**Root cause**
`batsman_runs` is a per-ball field (0–6). The model mixed grains by applying an innings-level milestone (119) directly to a ball-level column.

**Fix**
Two-layer safeguard:

### Fix 1 — Semantic SQL validator in `sql_helpers.py`
Added `detect_semantic_sql_issue(sql)` to flag high-confidence logical errors, currently including impossible `batsman_runs` comparisons (>6).

### Fix 2 — Auto-repair loop in `agent.py`
After `validate_sql()`, semantic issues trigger `_fix_sql()` with explicit feedback:
- "batsman_runs is per-ball (0-6); use GROUP BY ... HAVING SUM(batsman_runs)=N for innings milestones."
Retries up to `_MAX_SQL_RETRIES`, then safely refuses execution if still invalid.

### Prompt/Few-shot reinforcement
- Added rules in `prompts.py` forbidding innings milestones in `WHERE batsman_runs = N`.
- Added a few-shot example for strike-rate-in-119 pattern using innings identification via `HAVING SUM(...)` then join-back.

---

## #26 — NL2SQL always returns LIMIT 5 regardless of question intent

**Symptom**
All single-answer questions (e.g. "Who scored the most runs in IPL 2025?") returned 5 rows instead of 1.
Evaluation showed 17 of 50 test cases failing with "column count mismatch" or extra rows — all traceable to LIMIT 5 appended unconditionally.

**Root cause**
`create_sql_query_chain` has a `top_k` parameter defaulting to 5. When the few-shot prompt template contains `{top_k}`, the model sees `LIMIT 5` in the instruction and blindly applies it to every query, including ones where only a single result is appropriate ("who has the most…", "what is the highest…").

**Fix**
Added an explicit `LIMIT` rule to the system prompt in `prompts.py`:

```
LIMIT rules: Use LIMIT 1 when the question asks for a single result
('who has the most', 'which team/player', 'what is the highest/best/lowest').
Only use LIMIT {top_k} when the user explicitly requests multiple results
('top 5', 'top 10', 'list', 'give me N', 'give the top N').
```

Also added a system prompt rule to suppress extra debug columns:
```
Return only the columns needed to answer the question. Do not add
intermediate, debug, or context columns (e.g. do not include player name
alongside a single computed stat when only the stat was asked for).
```

**Impact**: Resolved ~17 of 50 eval failures in a single pass.

---

## #27 — Semantic SQL direction errors: "runs against" and "wickets against" use wrong team column

**Symptom** (three distinct but related errors)

1. **"Runs against team"**: "Which player has scored the most runs against Chennai Super Kings?" → SQL used `batting_team = 'Chennai Super Kings'`, returning runs scored BY CSK batsmen, not AGAINST them. Correct column: `bowling_team`.

2. **"Wickets against team"**: "Which bowler has the most wickets against Mumbai Indians?" → SQL used `bowling_team = 'Mumbai Indians'`, counting MI's own bowlers' wickets. Correct: `batting_team = 'Mumbai Indians'` (MI is batting; count dismissals of their batsmen).

3. **Dot ball definition**: "Who bowled the most dot balls?" → SQL used `extras = 0` to identify dot balls, which incorrectly excludes deliveries with byes/leg-byes (still dot balls for the bowler). Correct: `batsman_runs = 0 AND NOT is_wide AND NOT is_no_ball`.

**Root cause**
The model has no teaching signal for these domain-specific directional semantics. Without few-shot examples, it makes the natural but wrong assumption:
- "against CSK" → CSK is in the query somehow → uses `batting_team` (CSK batting) or `bowling_team` (CSK bowling) at random
- "dot ball" → no runs scored → checks `extras = 0` (a reasonable-sounding but wrong proxy)

**Fix**
Three-layer fix:

### System prompt rules in `prompts.py`
```
- RUNS SCORED AGAINST A TEAM: to find how many runs a batsman has scored against an
  opposing team, filter bowling_team = '[opponent]' (the team that is BOWLING).
  NEVER use batting_team for this — batting_team is the batsman's OWN team.
- 'Wickets against [team]' means dismissals OF that team's batsmen:
  filter batting_team = '[team]'. NEVER use bowling_team = '[team]' for this.
- DOT BALL definition: a legal delivery where the batsman scores 0 runs:
  batsman_runs = 0 AND NOT is_wide AND NOT is_no_ball.
  Do NOT use extras = 0 — a delivery with byes/leg-byes is still a dot ball to the bowler.
```

### New few-shot examples in `IPL_EXAMPLES`
- "Against which team has Rohit Sharma scored the most IPL runs?" → `WHERE batsman = 'RG Sharma' GROUP BY bowling_team`
- "Which bowler has taken the most wickets against Chennai Super Kings?" → `WHERE batting_team = 'Chennai Super Kings' AND dismissal_kind IN (...)`
- "How many wickets did Rajasthan Royals lose in IPL 2023?" → `WHERE batting_team = 'Rajasthan Royals' AND dismissal_kind IS NOT NULL`

---

## #28 — `winner_runs` and `winner_wickets` misinterpreted as innings scores

**Symptom**
"Which team chased the highest total?" and "Which team defended the lowest total?" returned wrong teams and scores. The model was reading `winner_runs` as the winning team's innings score (e.g. 200) instead of the winning margin in runs.

**Root cause**
`winner_runs` in the `matches` table stores the **winning margin** in runs (e.g. won by 47 runs), NOT the batting team's innings total. Similarly, `winner_wickets` stores wickets REMAINING for the winner (e.g. won by 3 wickets), NOT wickets lost. Queries that used `MAX(winner_runs)` to find "highest chase" got the biggest winning margin, not the highest total chased.

**Fix**

### System prompt rules in `prompts.py`
```
- winner_runs in matches is the WINNING MARGIN in runs (not the score).
  winner_wickets is wickets REMAINING for the winner (not wickets lost).
  To find innings scores: SUM(batsman_runs + extras) FROM deliveries GROUP BY match_id, inning, batting_team.
- Highest successfully chased total: aggregate inning=2 from deliveries JOIN matches
  WHERE winner_wickets IS NOT NULL AND batting_team = winner, ORDER DESC LIMIT 1.
- Lowest successfully defended total: aggregate inning=1 from deliveries JOIN matches
  WHERE winner_runs IS NOT NULL AND batting_team = winner, ORDER ASC LIMIT 1.
```

### New few-shot example in `IPL_EXAMPLES`
- "Which team defended the lowest total in IPL history?" — full CTE using `first_innings` aggregation from deliveries, joined to matches with `winner_runs IS NOT NULL AND batting_team = winner`.

### Expected SQL updated in `eval_testcases.json`
Both Q34 (highest chase) and Q35 (lowest defended) expected SQL rewrote from `winner_runs`-based logic to `SUM(batsman_runs + extras)` from deliveries.

---

## #29 — Death overs range wrong in system prompt (BETWEEN 15 AND 19 instead of 16 AND 19)

**Symptom**
"Which bowler took the most wickets in death overs in IPL 2025?" returned M Prasidh Krishna (15 wickets, using overs 16–20) instead of Arshdeep Singh (11 wickets, using overs 17–20 = 0-indexed overs 16–19). A regression introduced while adding the over-indexing rule to the system prompt.

**Root cause**
The system prompt rule was written as `over BETWEEN 15 AND 19` for death overs, which includes `over=15` (the 16th over, a middle over, not a death over). The correct 0-indexed death overs are `BETWEEN 16 AND 19` (overs 17–20). The model faithfully followed the prompt rule, producing an incorrect over range.

**Fix**
Corrected the system prompt rule and added an explicit `NEVER` guard:
```
Death overs = over BETWEEN 16 AND 19 (overs 17-20, the last 4 overs).
NEVER use BETWEEN 1 AND 6 for powerplay — that skips over=0 and includes over=6.
NEVER use BETWEEN 15 AND 19 for death overs — that adds over=15 (the 16th over).
```

**Lesson**: System prompt rules teaching over ranges must include explicit negative examples, not just the correct range. The model can follow a wrong rule as faithfully as a right one.

---

## #30 — Chart silently skipped: `Decimal` values in SQL result break `ast.literal_eval`

**Symptom**
Any query whose result contains a computed decimal (e.g. batting average, economy rate, ROUND()) produces no chart even when the user explicitly asks for one. Docker log:
```
INFO  | app.viz_agent | Chart intent | chart_type=line | x=season | y=batting_average
WARNING | app.viz_agent | Chart skipped — no parseable rows in SQL result
```
Example trigger: *"Can you plot Virat Kohli's batting average per IPL season?"*

SQL result string:
```
[('2007/08', 165, 11, Decimal('15.00')), ('2009', 246, 11, Decimal('22.36')), ...]
```

**Root cause**
`_parse_result_to_rows()` in `viz_agent.py` called `ast.literal_eval(result)` directly on the raw string returned by `QuerySQLDataBaseTool`. psycopg2 serialises `NUMERIC`/`DECIMAL` PostgreSQL columns as Python `Decimal('15.00')` constructor calls inside the repr string. `ast.literal_eval` only handles Python literals (`str`, `int`, `float`, `list`, `tuple`, etc.) — constructor calls like `Decimal('15.00')` are not literals and raise a `ValueError`. The `except Exception: return []` guard silently swallowed the error, returning an empty list, which caused the chart to be skipped with no visible error to the user.

**Fix**
Strip `Decimal('...')` with a regex substitution before parsing:

```python
# Before — fails silently on Decimal values
rows = ast.literal_eval(result)

# After — sanitise Decimal constructor calls first
sanitized = re.sub(r"Decimal\('([^']+)'\)", r"\1", result)
rows = ast.literal_eval(sanitized)
```

`re` was already imported in `viz_agent.py`. No new dependency required.

**Files changed**: `backend/app/viz_agent.py`

---

## #31 — Query rewrite length guard discards valid rewrites for short follow-up questions

**Symptom**
Short follow-up messages like `"you forgot to plot"` or `"plot"` caused the query rewrite chain to produce a correct standalone question which was then discarded by the safety guard. The pipeline fell back to the raw 4-word original, passed it to the SQL generator, and received a completely wrong query (ducks query instead of Kohli batting average).

Docker log:
```
WARNING | app.agent | Query rewrite produced a non-question — falling back to original.
rewrite="Can you plot Virat Kohli's batting average in every IPL season?"
INFO    | app.agent | Query rewrite | original='you forgot to plot' | standalone='you forgot to plot'
```
The rewrite was correct and ended with `?`. It was discarded because it was 3.5× the original length, exceeding the `3×` threshold.

**Root cause**
The safety guard used a **length ratio** as its second condition:
```python
_looks_like_answer = (
    not standalone_question.strip().endswith("?")
    or len(standalone_question) > 3 * len(question)
)
```
A length ratio is the wrong tool for short inputs. For a 4-word original ("you forgot to plot", 18 chars), any meaningful rewrite ("Can you plot Virat Kohli's batting average in every IPL season?", 63 chars) exceeds a 3× multiplier. The threshold is arbitrary and fails proportionally worse as the original question gets shorter.

**Why changing 3× to 5× is not a general fix**
Increasing the multiplier only shifts the breakpoint — it does not eliminate the problem:
- `"plot"` (4 chars) × 5 = 20 chars — even `"Can you plot the batting average?"` (34 chars) exceeds the threshold
- `"why?"` (4 chars) × 5 = 20 chars — same failure mode
- Any follow-up shorter than ~20 chars will still fail under a 5× rule

The multiplier needs to grow as the original shrinks, but the right fix is to remove the ratio entirely.

**Fix**
The only reliable signal that the rewriter hallucinated an *answer* (vs. a valid question) is that answers are statements — they do not end with `?`. The `?` check already handles this correctly. The length ratio was redundant and harmful. It was replaced with a generous absolute ceiling (300 chars) that rejects multi-sentence paragraph-length outputs regardless of the original question length:

```python
# Before — ratio breaks for short originals
_looks_like_answer = (
    not standalone_question.strip().endswith("?")
    or len(standalone_question) > 3 * len(question)
)

# After — absolute ceiling; ratio removed
_looks_like_answer = (
    not standalone_question.strip().endswith("?")
    or len(standalone_question) > 300
)
```

A 300-char ceiling is well above any legitimate standalone question rewrite (~60–120 chars) and well below a hallucinated multi-fact answer (~400+ chars). It is input-length-agnostic.

**Files changed**: `backend/app/agent.py`

---

## #32 — MCP chart server Docker build takes 5+ minutes due to pip backtracking

**Symptom**
`docker compose up --build` appeared to hang for 5+ minutes on the `mcp_chart_server` pip install step. No error — it eventually succeeded, but silently wasted build time on every `--build`.

**Root cause**
`backend/requirements.txt` used loose lower-bound constraints: `pydantic-settings>=2.5.2` and `mcp>=1.4.0`. When pip resolves a `>=` constraint it downloads metadata for every available version from PyPI to find the best match. `mcp` had 10+ published versions and a deep transitive dependency tree. pip's backtracking resolver explored many combinations, causing the delay.

**Fix**
Pinned all direct dependencies to exact versions in `backend/requirements.txt`:
```
# Before
pydantic-settings>=2.5.2
mcp>=1.4.0

# After
pydantic-settings==2.7.0
mcp==1.6.0
```

**Lesson**: Use `==` for all direct dependencies in Docker images. Loose lower bounds (`>=`) are appropriate for library `pyproject.toml` files; in a containerised service the image should be fully reproducible with pinned versions.

**Files changed**: `backend/requirements.txt`

---

## #33 — `mcp==1.6.0` requires `pydantic>=2.7.2`; `backend/requirements.txt` had `pydantic==2.7.1`

**Symptom**
Docker build error after pinning `mcp==1.6.0`:
```
ERROR: Cannot install mcp==1.6.0 because these package versions have conflicting dependencies.
mcp 1.6.0 depends on pydantic<3.0.0 and >=2.7.2
```

**Root cause**
`backend/requirements.txt` had `pydantic==2.7.1`. `mcp==1.6.0` requires `pydantic>=2.7.2` (a one-patch bump). pip cannot satisfy both constraints simultaneously and aborts the build.

**Fix**
Bumped `pydantic==2.7.1` → `pydantic==2.7.2`. `pydantic-settings==2.7.0` requires only `pydantic>=2.7.0`, so it is satisfied by `2.7.2`.

**Files changed**: `backend/requirements.txt`

---

## #34 — `FastMCP.run()` TypeError: `host` and `port` moved to constructor in `mcp==1.6.0`

**Symptom**
`mcp_chart_server` container crashed on startup immediately after a successful Docker build:
```
TypeError: FastMCP.run() got an unexpected keyword argument 'host'
```
Container entered a restart loop.

**Root cause**
`server.py` was written against an older `mcp` API where `host` and `port` were kwargs of `mcp.run()`:
```python
# Old API (pre-1.6.0)
mcp = FastMCP("chart-server")
mcp.run(transport="sse", host="0.0.0.0", port=8087)  # ← TypeError in 1.6.0
```
In `mcp==1.6.0` the `FastMCP` class signature changed: `host` and `port` are constructor arguments only and are no longer accepted by `run()`.

**Fix**
Moved `host` and `port` to the `FastMCP()` constructor. `run()` now takes only `transport`. `port` was promoted to module level so it is available at construction time:
```python
# After (mcp==1.6.0 API)
port = int(os.getenv("PORT", "8087"))
mcp = FastMCP("chart-server", host="0.0.0.0", port=port)
...
mcp.run(transport="sse")
```

**Files changed**: `mcp_chart_server/server.py`

---

## Phase 10 — Redis Persistent History: No Bugs

The Redis implementation (2026-03-16) was a clean feature addition with no bugs encountered during development.

**Changes made:**
- `docker-compose.yml` — added `redis:7-alpine` service with `--save 60 1` persistence and health check; `backend` `depends_on` redis
- `backend/requirements.txt` — added `redis==5.0.4`
- `backend/app/config.py` — added `redis_url` + `redis_ttl_seconds` settings
- `backend/app/agent.py` — replaced `_conversation_histories` dict with `RedisChatMessageHistory`; replaced `_recent_follow_up_chips` dict with Redis JSON keys; added `_init_redis()`, `_get_history()`, `_get_recent_chips()`, `_set_recent_chips()` helpers; graceful in-memory fallback if Redis is unreachable

**Design decisions that prevented bugs:**
- 2-second `socket_connect_timeout` on `_init_redis()` so a missing Redis instance fails fast and triggers the fallback, not a hang
- `_redis_available` flag set once at startup — no per-request connection checks that could race
- `RedisChatMessageHistory` creates the Redis key lazily on first `add_*` write, so new threads with no history just start with an empty list — no explicit "create if not exists" logic needed

## Per-IP Rate Limiting: No Bugs

The slowapi rate limiting implementation (2026-03-16) was a clean feature addition with no bugs encountered.

**Changes made:**
- `backend/requirements.txt` — added `slowapi==0.1.9`
- `backend/app/config.py` — added `rate_limit_per_minute: int = 20`
- `backend/app/limiter.py` (new) — singleton `Limiter` with Redis backend + in-memory fallback
- `backend/app/main.py` — `app.state.limiter = limiter`, `SlowAPIMiddleware`, custom `RateLimitExceeded` handler returning `{"detail": "..."}` consistent with our other 429 responses
- `backend/app/routes/query.py` — added `request: Request` param (required by slowapi), `@limiter.limit(f"{settings.rate_limit_per_minute}/minute")` decorator

**Design decisions:**
- Limiter singleton in its own `limiter.py` module (not `main.py`) avoids circular imports — both `main.py` and `routes/query.py` import from it
- Custom `RateLimitExceeded` handler (not slowapi's built-in `_rate_limit_exceeded_handler`) keeps all 429 responses in `{"detail": "..."}` format — the frontend only needs to handle one error shape
- `RATE_LIMIT_PER_MINUTE` is a config setting, not a hardcoded constant — easy to raise/lower per environment without a code change
