# NL2SQL Agent — Multi Agent Chatbot Flow

```mermaid
flowchart TD
    Start([🟢 User asks a question]) --> guardrails_agent

    guardrails_agent["🛡️ Safety Check\nIs this question safe and appropriate?\nBlocks harmful inputs, injections,\nand non-cricket questions"]
    guardrails_agent --> check_scope

    check_scope{Safe to proceed?}
    check_scope -->|❌ No — blocked\nreject with error| END_guard([🔴 End — Question Rejected])
    check_scope -->|✅ Yes| rewrite_agent

    rewrite_agent["✏️ Clarify the Question\nIf this is a follow-up like 'What about 2020?',\nrewrite it into a complete standalone question\nusing conversation history"]
    rewrite_agent --> table_selector_agent & cricket_knowledge_agent

    table_selector_agent["📋 Find Relevant Tables\nAI reads table descriptions and picks\nwhich database tables are needed\nto answer this question"]
    cricket_knowledge_agent["🏏 Look Up Cricket Rules\nSearch cricket knowledge base\nfor relevant domain rules\n(e.g. how to calculate batting average)"]

    table_selector_agent --> sql_agent
    cricket_knowledge_agent --> sql_agent

    sql_agent["🤖 Generate SQL Query\nAI writes a SQL query using:\n• the question\n• relevant tables\n• cricket rules\n• similar past examples"]
    sql_agent --> validate_sql_output

    validate_sql_output{Is the SQL safe to run?}
    validate_sql_output -->|❌ No — contains\ndangerous keywords\nlike DROP or DELETE| safe_response_agent
    validate_sql_output -->|✅ Yes — safe\nread-only query| execute_sql

    safe_response_agent["🚫 Block Unsafe SQL\nReturns a polite refusal message\ninstead of running the query"]
    safe_response_agent --> END_blocked([🔴 End — SQL Blocked])

    execute_sql["⚡ Run the SQL Query\nExecute the query against\nthe IPL cricket database"]
    execute_sql --> should_retry

    should_retry{Did the query succeed?}
    should_retry -->|❌ Error — try to fix\n(up to 2 attempts)| fix_sql_agent
    should_retry -->|❌ All retries failed| error_agent
    should_retry -->|✅ Got results!| rephrase_agent

    fix_sql_agent["🔧 Fix the SQL\nAI reads the error message\nand rewrites the query\nto correct the mistake"]
    fix_sql_agent --> execute_sql

    error_agent["⚠️ Return Error\nTell the user we couldn't\nanswer their question"]
    error_agent --> END_error([🔴 End — Error])

    rephrase_agent["💬 Write the Answer\nConvert raw database results\ninto a friendly natural language response\ne.g. 'Virat Kohli scored 6,634 runs'"]
    rephrase_agent --> history_update

    history_update["💾 Save to History\nStore the question and answer\nso follow-up questions\ncan reference this conversation"]
    history_update --> END_final([🟢 End — Answer Delivered])

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

## What Each Agent Does

| Agent | What it does |
|---|---|
| 🛡️ **Safety Check** | Blocks harmful inputs, injection attacks, and oversized questions |
| ✏️ **Clarify the Question** | Rewrites vague follow-ups like "What about 2020?" into complete questions |
| 📋 **Find Relevant Tables** | AI picks which database tables are needed to answer the question |
| 🏏 **Look Up Cricket Rules** | Searches the cricket knowledge base for domain-specific rules (e.g. how to calculate a batting average) |
| 🤖 **Generate SQL Query** | AI writes a database query using the question, tables, cricket rules, and similar past examples |
| ⚡ **Run the SQL Query** | Executes the generated query against the IPL cricket database |
| 🔧 **Fix the SQL** | If the query fails, AI reads the error and rewrites it to fix the mistake |
| 🚫 **Block Unsafe SQL** | If the generated SQL tries to modify data, returns a polite refusal instead |
| ⚠️ **Return Error** | If all retry attempts fail, tells the user the question couldn't be answered |
| 💬 **Write the Answer** | Converts raw database results into a friendly plain-English response |
| 💾 **Save to History** | Remembers the conversation so follow-up questions work correctly |

## Parallel Execution

**Find Relevant Tables** and **Look Up Cricket Rules** run **at the same time** to save time.
While the AI is figuring out which tables to use, the cricket knowledge search runs in the background — so neither step adds extra waiting time.

## Decision Points

| Decision | What happens? |
|---|---|
| **Safe to proceed?** | If the question is harmful or invalid → rejected with an error. Otherwise → continues. |
| **Is the SQL safe to run?** | If the SQL tries to modify/delete data → blocked with a polite refusal. If it's a safe read-only query → executed. |
| **Did the query succeed?** | If there's a database error → AI tries to fix & re-run (up to 2 attempts). If all attempts fail → error message. If it works → answer is generated. |
