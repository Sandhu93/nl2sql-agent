/**
 * API wrapper for the NL2SQL backend.
 *
 * TODO: Replace BACKEND_URL with an environment variable in production:
 *   process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8086"
 */

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8086";

// Phase 16 — API key auth. Set NEXT_PUBLIC_API_KEY in .env to enable.
// When not set the header is omitted and the backend allows the request through
// (auth is only enforced when API_KEY is configured on the backend).
const API_KEY = process.env.NEXT_PUBLIC_API_KEY ?? "";

export interface QueryRequest {
  question: string;
  thread_id: string;
}

/** Phase 8 — key takeaway + follow-up question chips */
export interface Insights {
  key_takeaway: string;
  follow_up_chips: string[];
}

export interface QueryResponse {
  answer: string;
  sql: string;
  /** Phase 8 — present on every successful query */
  insights: Insights | null;
  /**
   * Phase 9 — Vega-Lite v5 spec, only present when the user asked for a chart.
   * TODO: Will be sourced from the MCP chart server once wired up.
   */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  chart_spec: Record<string, any> | null;
}

/**
 * Streaming event types — one per pipeline milestone.
 *
 * The streaming endpoint always returns HTTP 200; errors mid-stream are
 * delivered as { type: "error" } events and thrown by queryAgentStream.
 */
export type StreamEvent =
  | { type: "sql_ready"; sql: string }
  | { type: "answer_ready"; answer: string }
  | { type: "insights_ready"; insights: Insights }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  | { type: "chart_ready"; chart_spec: Record<string, any> }
  | { type: "error"; error: string };

/**
 * POST /api/query/stream — streaming NDJSON version of queryAgent.
 *
 * Yields one StreamEvent per pipeline milestone in completion order:
 *   sql_ready → answer_ready → insights_ready → chart_ready
 *
 * Error events (type === "error") are converted to thrown Errors so the
 * caller's catch block fires rather than needing to check each event type.
 */
export async function* queryAgentStream(
  payload: QueryRequest
): AsyncGenerator<StreamEvent> {
  const res = await fetch(`${BACKEND_URL}/api/query/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(API_KEY ? { "X-API-Key": API_KEY } : {}),
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    let message = `Request failed: ${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) message = body.detail;
    } catch {
      // ignore parse error; use default message
    }
    throw new Error(message);
  }

  if (!res.body) {
    throw new Error("Response body is null");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed) {
          const event = JSON.parse(trimmed) as StreamEvent;
          if (event.type === "error") {
            throw new Error(event.error);
          }
          yield event;
        }
      }
    }
    // Flush any remaining buffered line (stream ended without trailing newline).
    if (buffer.trim()) {
      const event = JSON.parse(buffer.trim()) as StreamEvent;
      if (event.type === "error") {
        throw new Error(event.error);
      }
      yield event;
    }
  } finally {
    reader.releaseLock();
  }
}

/**
 * POST /api/query — send a natural-language question to the backend agent.
 */
export async function queryAgent(payload: QueryRequest): Promise<QueryResponse> {
  const res = await fetch(`${BACKEND_URL}/api/query`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(API_KEY ? { "X-API-Key": API_KEY } : {}),
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    let message = `Request failed: ${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) message = body.detail;
    } catch {
      // ignore parse error; use default message
    }
    throw new Error(message);
  }

  return res.json() as Promise<QueryResponse>;
}
