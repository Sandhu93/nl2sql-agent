"use client";

import { useState, useRef, useEffect, FormEvent, useCallback } from "react";
import ChatMessage, { Message } from "@/components/ChatMessage";
import { queryAgent } from "@/lib/api";

/**
 * Main chat page — NL2SQL Agent interface.
 *
 * Phase 8: Assistant messages now carry `insights` (key_takeaway +
 *          follow_up_chips).  Clicking a chip pre-fills the input and submits.
 *
 * Phase 9: Assistant messages carry `chart_spec` when the user asked for a
 *          chart — rendered inline by ChartBlock via ChatMessage.
 *
 * TODO: Connect the real LangGraph backend by ensuring `queryAgent` in
 *       lib/api.ts points to the correct NEXT_PUBLIC_BACKEND_URL and the
 *       backend's /api/query endpoint is fully implemented in agent.py.
 */

export default function ChatPage() {
  // A stable session ID — generated once per page load, not persisted.
  // TODO: Persist thread_id in a URL param or cookie if you want history
  //       to survive page refreshes.
  const [threadId] = useState<string>(() => crypto.randomUUID());

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Auto-scroll to the latest message
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  /**
   * Submit the current input as a question.
   * Extracted so it can be called from both the form and chip clicks.
   */
  const submitQuestion = useCallback(
    async (question: string) => {
      if (!question.trim() || loading) return;

      const userMessage: Message = {
        id: crypto.randomUUID(),
        role: "user",
        content: question.trim(),
      };

      setMessages((prev) => [...prev, userMessage]);
      setInput("");
      setError(null);
      setLoading(true);

      try {
        // TODO: Replace with streaming once the backend supports SSE.
        const response = await queryAgent({
          question: question.trim(),
          thread_id: threadId,
        });

        const assistantMessage: Message = {
          id: crypto.randomUUID(),
          role: "assistant",
          content: response.answer,
          sql: response.sql,
          // Phase 8 — insights
          insights: response.insights,
          // Phase 9 — chart spec (null when not a viz request)
          chart_spec: response.chart_spec,
        };

        setMessages((prev) => [...prev, assistantMessage]);
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "An unexpected error occurred.";
        setError(message);
      } finally {
        setLoading(false);
      }
    },
    [loading, threadId]
  );

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    await submitQuestion(input);
  }

  /**
   * Phase 8 — chip click handler.
   * Pre-fills the input textarea with the chip question and focuses it,
   * then submits immediately so the user gets an instant follow-up answer.
   */
  const handleChipClick = useCallback(
    (question: string) => {
      setInput(question);
      // Submit after state update settles
      setTimeout(() => {
        submitQuestion(question);
      }, 0);
    },
    [submitQuestion]
  );

  return (
    <main className="flex flex-col h-full max-w-4xl mx-auto px-4">
      {/* Header */}
      <header className="py-6 border-b border-gray-800 flex-shrink-0">
        <h1 className="text-2xl font-semibold tracking-tight">NL2SQL Agent</h1>
        <p className="text-sm text-gray-400 mt-1">
          Ask questions in plain English — get answers powered by SQL.
        </p>
      </header>

      {/* Message list */}
      <section
        className="flex-1 overflow-y-auto py-6 space-y-4"
        aria-live="polite"
        aria-label="Conversation"
      >
        {messages.length === 0 && !loading && (
          <p className="text-center text-gray-500 text-sm mt-12">
            Ask your first question to get started.
          </p>
        )}

        {messages.map((msg) => (
          <ChatMessage
            key={msg.id}
            message={msg}
            onChipClick={handleChipClick}
          />
        ))}

        {/* Loading indicator */}
        {loading && (
          <div className="flex justify-start" aria-label="Loading">
            <div className="bg-gray-800 rounded-2xl rounded-bl-sm px-4 py-3">
              <div className="flex gap-1.5 items-center h-5">
                {[0, 1, 2].map((i) => (
                  <span
                    key={i}
                    className="w-2 h-2 bg-gray-400 rounded-full animate-bounce"
                    style={{ animationDelay: `${i * 150}ms` }}
                  />
                ))}
              </div>
            </div>
          </div>
        )}

        {/* Error banner */}
        {error && (
          <div
            role="alert"
            className="rounded-lg border border-red-700 bg-red-950 px-4 py-3 text-sm text-red-300"
          >
            <strong className="font-medium">Error:</strong> {error}
          </div>
        )}

        <div ref={bottomRef} />
      </section>

      {/* Input form */}
      <footer className="py-4 border-t border-gray-800 flex-shrink-0">
        <form onSubmit={handleSubmit} className="flex gap-3 items-end">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
            placeholder="Ask a question about your database…"
            rows={2}
            disabled={loading}
            className="flex-1 resize-none rounded-xl bg-gray-800 border border-gray-700
                       px-4 py-3 text-sm text-gray-100 placeholder-gray-500
                       focus:outline-none focus:ring-2 focus:ring-indigo-500
                       disabled:opacity-50 disabled:cursor-not-allowed"
            aria-label="Question input"
          />
          <button
            type="submit"
            disabled={loading || !input.trim()}
            className="rounded-xl bg-indigo-600 px-5 py-3 text-sm font-medium
                       hover:bg-indigo-500 active:bg-indigo-700
                       disabled:opacity-50 disabled:cursor-not-allowed
                       transition-colors flex-shrink-0"
            aria-label="Submit question"
          >
            {loading ? "Thinking…" : "Ask"}
          </button>
        </form>
        <p className="text-xs text-gray-600 mt-2">
          Press Enter to send · Shift+Enter for new line
        </p>
      </footer>
    </main>
  );
}
