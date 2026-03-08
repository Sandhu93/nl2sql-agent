/**
 * API wrapper for the NL2SQL backend.
 *
 * TODO: Replace BACKEND_URL with an environment variable in production:
 *   process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8086"
 */

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8086";

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
 * POST /api/query — send a natural-language question to the backend agent.
 *
 * TODO: Add auth headers here once authentication is implemented.
 */
export async function queryAgent(payload: QueryRequest): Promise<QueryResponse> {
  const res = await fetch(`${BACKEND_URL}/api/query`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
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
