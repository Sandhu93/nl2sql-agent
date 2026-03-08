# NL2SQL Agent — Multi Agent Chatbot Flow

```mermaid
flowchart TD
    Start([User asks a question]) --> guardrails_agent

    guardrails_agent["Safety Check\nBlocks harmful inputs, injection attacks,\noversized questions, and DDL keywords"]
    guardrails_agent --> check_scope

    check_scope{Safe to proceed?}
    check_scope -->|No — blocked| END_guard([End — Question Rejected])
    check_scope -->|Yes| rewrite_agent

    rewrite_agent["Clarify the Question\nRewrites follow-ups into standalone questions\nusing conversation history\nSkipped on first turn"]
    rewrite_agent --> table_selector_agent & cricket_knowledge_agent

    table_selector_agent["Find Relevant Tables\nLLM reads table descriptions\nand picks which tables are needed\nFallback: all tables"]
    cricket_knowledge_agent["Look Up Cricket Rules\nChromaDB similarity search over\ncricket_rules.md — returns k=3\nmost relevant domain sections"]

    table_selector_agent --> sql_agent
    cricket_knowledge_agent --> sql_agent

    sql_agent["Generate SQL Query\nLLM writes SQL using the question,\nrelevant tables, cricket domain rules,\nand k=3 dynamic few-shot examples"]
    sql_agent --> validate_sql_output

    validate_sql_output{Is the SQL safe to run?}
    validate_sql_output -->|No — non-SELECT or\nforbidden keyword detected| safe_response_agent
    validate_sql_output -->|Yes — safe read-only query| execute_sql

    safe_response_agent["Block Unsafe SQL\nReturns a safe refusal message\nConversation history still updated"]
    safe_response_agent --> END_blocked([End — SQL Blocked])

    execute_sql["Run the SQL Query\nExecutes query against PostgreSQL\nvia QuerySQLDataBaseTool\nErrors returned as strings not exceptions"]
    execute_sql --> should_retry

    should_retry{Did the query succeed?}
    should_retry -->|Error detected — retry| fix_sql_agent
    should_retry -->|All retries exhausted| error_agent
    should_retry -->|Success| rephrase_agent

    fix_sql_agent["Fix the SQL\nLLM rewrites the query\nusing the error message and schema\nMax 2 retry attempts"]
    fix_sql_agent --> execute_sql

    error_agent["Return Error\nReturns an error message\nto the user"]
    error_agent --> END_error([End — Error])

    rephrase_agent["Write the Answer\nConverts raw SQL results into\na natural language response\nEmpty result gets a friendly no-data message"]
    rephrase_agent --> history_update

    history_update["Save to History\nStores original question and answer\nin ChatMessageHistory keyed by thread_id\nEnables follow-up questions"]
    history_update --> END_final([End — Answer Delivered])

    %% Styling
    classDef agent      fill:#AED6F1,stroke:#2E86C1,color:#000
    classDef decision   fill:#FAD7A0,stroke:#E67E22,color:#000
    classDef execution  fill:#A9DFBF,stroke:#27AE60,color:#000
    classDef safe       fill:#D7BDE2,stroke:#8E44AD,color:#000
    classDef terminal   fill:#F1948A,stroke:#C0392B,color:#000
    classDef storage    fill:#F9E79F,stroke:#F39C12,color:#000

    class guardrails_agent,rewrite_agent,table_selector_agent,cricket_knowledge_agent,sql_agent,rephrase_agent agent
    class check_scope,validate_sql_output,should_retry decision
    class execute_sql,fix_sql_agent execution
    class safe_response_agent,error_agent safe
    class END_guard,END_blocked,END_error,END_final terminal
    class history_update storage
```

---

## What Each Agent Does

| Agent | What it does |
|---|---|
| **Safety Check** | Blocks harmful inputs, injection attacks, oversized questions, and DDL keywords |
| **Clarify the Question** | Rewrites vague follow-ups like "What about 2020?" into complete standalone questions |
| **Find Relevant Tables** | LLM picks which database tables are needed to answer the question |
| **Look Up Cricket Rules** | Searches cricket_rules.md via ChromaDB for domain-specific rules (e.g. batting average formula) |
| **Generate SQL Query** | LLM writes SQL using the question, relevant tables, cricket rules, and similar past examples |
| **Run the SQL Query** | Executes the generated query against the IPL PostgreSQL database |
| **Fix the SQL** | If the query fails, LLM reads the error message and rewrites the query to fix the mistake |
| **Block Unsafe SQL** | If the generated SQL contains forbidden keywords, returns a safe refusal instead of executing |
| **Return Error** | If all retry attempts fail, returns an error message to the user |
| **Write the Answer** | Converts raw database results into a natural language response |
| **Save to History** | Persists the question and answer so follow-up questions work correctly |

## Parallel Execution

**Find Relevant Tables** and **Look Up Cricket Rules** run simultaneously via `asyncio.gather`.
The cricket knowledge retrieval runs in the background while the table-selection LLM call is in flight — net wall-clock cost: zero.

## Decision Points

| Decision | Outcomes |
|---|---|
| **Safe to proceed?** | Blocked (HTTP 400) if the question contains injection patterns or DDL keywords. Otherwise continues. |
| **Is the SQL safe to run?** | Blocked (HTTP 200 + safe answer) if SQL is non-SELECT or contains forbidden keywords. Continues if valid read-only. |
| **Did the query succeed?** | Error detected: LLM fixes and re-runs, up to 2 attempts. All retries exhausted: error message. Success: answer is generated. |
