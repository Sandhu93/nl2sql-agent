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

export interface QueryResponse {
  answer: string;
  sql: string;
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
      // Bypass the ngrok browser-warning interstitial page for API calls.
      // Safe to include even when not using ngrok; ignored by other servers.
      "ngrok-skip-browser-warning": "true",
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
