# NL2SQL Agent

A production-ready full-stack template for building a natural-language-to-SQL
agent with a **FastAPI** backend and a **Next.js 14** frontend, containerized
with **Docker Compose**.

```
nl2sql_agent/
├── backend/
│   ├── app/
│   │   ├── main.py          ← FastAPI app, CORS, global error handler
│   │   ├── config.py        ← pydantic-settings (reads from .env)
│   │   ├── agent.py         ← ⭐ LangGraph agent placeholder (start here)
│   │   └── routes/
│   │       └── query.py     ← POST /api/query endpoint
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   ├── app/
│   │   ├── layout.tsx
│   │   └── page.tsx         ← chat UI
│   ├── components/
│   │   ├── ChatMessage.tsx
│   │   └── SqlBlock.tsx
│   ├── lib/
│   │   └── api.ts           ← fetch wrapper for /api/query
│   ├── Dockerfile
│   └── .env.local.example
├── docker-compose.yml
├── .env.example
└── README.md                ← you are here
```

---

## Prerequisites

| Tool | Version |
|------|---------|
| Docker | 24+ |
| Docker Compose | v2 (bundled with Docker Desktop) |
| Node.js (local dev only) | 20+ |
| Python (local dev only) | 3.11+ |

---

## Setup

### 1. Clone & configure

```bash
git clone <your-repo-url> nl2sql_agent
cd nl2sql_agent
cp .env.example .env
```

Edit `.env` and fill in your values:

```
OPENAI_API_KEY=sk-...
DB_USER=postgres
DB_PASSWORD=1234
DB_HOST=host.docker.internal   # host machine DB; use service name for a Compose-managed DB
DB_PORT=5432
DB_NAME=nl2sql
```

Create the database before starting the stack:

```bash
psql -U postgres -c "CREATE DATABASE nl2sql;"
```

### 2. Run with Docker Compose

```bash
docker compose up --build
```

| Service | URL |
|---------|-----|
| Frontend (Next.js) | http://localhost:8085 |
| Backend (FastAPI) | http://localhost:8086 |
| API docs (Swagger) | http://localhost:8086/docs |
| Health check | http://localhost:8086/health |

Stop everything:

```bash
docker compose down
```

---

## Local development (without Docker)

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp ../.env.example .env            # or symlink to the root .env
uvicorn app.main:app --reload --port 8086
```

### Frontend

```bash
cd frontend
npm install
cp .env.local.example .env.local
npm run dev                        # starts on port 8085
```

---

## API reference

### `GET /health`

Returns `{"status": "ok"}` when the backend is running.

### `POST /api/query`

**Request body**

```json
{
  "question": "How many orders were placed last month?",
  "thread_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Response**

```json
{
  "answer": "There were 1,234 orders placed last month.",
  "sql": "SELECT COUNT(*) FROM orders WHERE ..."
}
```

---

## Next steps — adding the LangGraph agent

All agent logic lives in **`backend/app/agent.py`**. The file contains a
detailed integration checklist in its module docstring. In summary:

### Step 1 — Install the LangGraph agent

Open `backend/app/agent.py` and follow the `TODO` comments:

1. Import `ChatOpenAI`, `SQLDatabase`, `SQLDatabaseToolkit`, and
   `create_react_agent`.
2. Connect to your PostgreSQL database via `settings.database_url`.
3. Build the toolkit and create the LangGraph agent with a `MemorySaver`
   checkpointer for per-thread conversation history.
4. Replace the `run_agent` stub to invoke the real agent and extract the
   SQL query and natural-language answer from the output messages.

### Step 2 — (Optional) Add ChromaDB few-shot examples

The `requirements.txt` already includes `chromadb`. Use it to store example
`(question, sql)` pairs and retrieve them as few-shot prompts at inference
time. Wire the vector store into `run_agent` in `agent.py`.

### Step 3 — Stream responses to the frontend

Replace the single `fetch` call in `frontend/lib/api.ts` with a
streaming consumer (SSE or NDJSON) and update `frontend/app/page.tsx` to
render tokens as they arrive.

### Step 4 — Add authentication

Add an API key or JWT middleware to `backend/app/main.py` and pass the
corresponding `Authorization` header from `frontend/lib/api.ts`.

### Step 5 — Production hardening

- Set `--reload` → `False` in the backend `Dockerfile` `CMD`.
- Enable Next.js [output: "standalone"](https://nextjs.org/docs/app/api-reference/next-config-js/output)
  (already configured in the multi-stage `Dockerfile`).
- Point `ALLOWED_ORIGINS` and `NEXT_PUBLIC_BACKEND_URL` at your real domain.
- Add a reverse proxy (nginx / Caddy) in front of both services.
