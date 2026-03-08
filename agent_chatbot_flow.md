# NL2SQL Agent — Multi Agent Chatbot Flow

```mermaid
flowchart TD
    Start([Start]) --> guardrails_agent

    guardrails_agent["guardrails_agent\ninput_validator.py · validate_question()"]
    guardrails_agent --> check_scope

    check_scope{check_scope}
    check_scope -->|out_of_scope\nHTTP 400| END_guard([END])
    check_scope -->|in_scope| rewrite_agent

    rewrite_agent["rewrite_agent\nagent.py · Step 0 — _rewrite_query\nrewrites follow-up into standalone question\nskipped on first turn"]
    rewrite_agent --> table_selector_agent & cricket_knowledge_agent

    table_selector_agent["table_selector_agent\ntable_selector.py · Step 1\nLLM picks relevant tables from CSV\nfallback: all tables"]
    cricket_knowledge_agent["cricket_knowledge_agent\ncricket_knowledge.py · Step 1b\nChromaDB similarity search\nk=3 cricket domain sections"]

    table_selector_agent --> sql_agent
    cricket_knowledge_agent --> sql_agent

    sql_agent["sql_agent\nprompts.py · Step 2 — _generate_query\nNL → SQL · k=3 dynamic few-shot examples\n{cricket_context} injected into system prompt"]
    sql_agent --> validate_sql_output

    validate_sql_output{validate_sql_output\nLayer 3}
    validate_sql_output -->|non-SELECT / forbidden keyword\nHTTP 200 + safe answer| safe_response_agent
    validate_sql_output -->|valid SELECT or WITH| execute_sql

    safe_response_agent["safe_response_agent\nagent.py · returns safe answer string\nconversation history still updated"]
    safe_response_agent --> END_blocked([END])

    execute_sql["execute_sql\nsql_helpers.py · Step 4 — _run_sql\nQuerySQLDataBaseTool\nreturns errors as strings not exceptions"]
    execute_sql --> should_retry

    should_retry{should_retry\nmax 2 retries}
    should_retry -->|error string detected\n_is_sql_error| fix_sql_agent
    should_retry -->|retries exhausted| error_agent
    should_retry -->|success| rephrase_agent

    fix_sql_agent["fix_sql_agent\nagent.py · _fix_sql\nLLM corrects SQL using error + schema"]
    fix_sql_agent --> execute_sql

    error_agent["error_agent\nreturns error message to user"]
    error_agent --> END_error([END])

    rephrase_agent["rephrase_agent\nagent.py · Step 5 — _rephrase_answer\npresents data in natural language\nguard: empty result → friendly no-data message"]
    rephrase_agent --> history_update

    history_update["history_update\nstores original question + answer\nin-memory ChatMessageHistory keyed by thread_id"]
    history_update --> END_final([END])

    %% Styling
    classDef agent        fill:#AED6F1,stroke:#2E86C1,color:#000
    classDef decision     fill:#FAD7A0,stroke:#E67E22,color:#000
    classDef execution    fill:#A9DFBF,stroke:#27AE60,color:#000
    classDef safe         fill:#D7BDE2,stroke:#8E44AD,color:#000
    classDef terminal     fill:#F1948A,stroke:#C0392B,color:#000
    classDef storage      fill:#F9E79F,stroke:#F39C12,color:#000

    class guardrails_agent,rewrite_agent,table_selector_agent,cricket_knowledge_agent,sql_agent,rephrase_agent agent
    class check_scope,validate_sql_output,should_retry decision
    class execute_sql,fix_sql_agent execution
    class safe_response_agent,error_agent safe
    class END_guard,END_blocked,END_error,END_final terminal
    class history_update storage
```

---

## Agent Responsibilities

| Agent | File | Role |
|---|---|---|
| `guardrails_agent` | `input_validator.py` | Blocks injections, DDL keywords, oversized input |
| `rewrite_agent` | `agent.py` | Rewrites follow-ups into standalone questions |
| `table_selector_agent` | `table_selector.py` | Picks relevant tables from CSV descriptions |
| `cricket_knowledge_agent` | `cricket_knowledge.py` | RAG retrieval of cricket domain rules (ChromaDB) |
| `sql_agent` | `prompts.py` + `agent.py` | NL → SQL with few-shot examples + domain context |
| `execute_sql` | `sql_helpers.py` | Runs SQL against PostgreSQL via LangChain tool |
| `fix_sql_agent` | `agent.py` | LLM corrects SQL using DB error message + schema |
| `safe_response_agent` | `agent.py` | Returns safe answer when SQL is blocked (Layer 3) |
| `error_agent` | `agent.py` | Returns final error when retries exhausted |
| `rephrase_agent` | `agent.py` | Converts raw SQL result to natural language answer |
| `history_update` | `agent.py` | Persists turn to in-memory `ChatMessageHistory` |

## Parallel Execution

`table_selector_agent` and `cricket_knowledge_agent` run **simultaneously** via `asyncio.gather`.
This hides the embedding API call latency behind the table-selection LLM call — net wall-clock cost: zero.

## Decision Points

| Decision | Outcomes |
|---|---|
| `check_scope` | `out_of_scope` (HTTP 400) · `in_scope` |
| `validate_sql_output` | `blocked` (HTTP 200 + safe answer) · `valid` |
| `should_retry` | `error` → `fix_sql_agent` → `execute_sql` loop (max 2) · `exhausted` → `error_agent` · `success` → `rephrase_agent` |
```
