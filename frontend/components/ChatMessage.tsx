"use client";

import SqlBlock from "./SqlBlock";
import InsightsCard from "./InsightsCard";
import ChartBlock from "./ChartBlock";
import { Insights } from "@/lib/api";

/**
 * ChatMessage — renders a single turn in the conversation.
 *
 * Phase 8: Renders an InsightsCard below the answer for assistant messages
 *          that have insights (key_takeaway + follow_up_chips).
 *
 * Phase 9: Renders a ChartBlock below the insights when a chart_spec is
 *          present (i.e. the user asked for a chart).
 *
 * TODO: Extend this component to support streaming responses once the
 *       backend supports SSE / streaming.
 */

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  /** Present only on assistant messages that include a generated SQL query. */
  sql?: string;
  /** Phase 8 — insight data returned by the backend insights_agent. */
  insights?: Insights | null;
  /**
   * Phase 9 — Vega-Lite v5 spec, only present when the user asked for a chart.
   * TODO: Will be sourced from MCP chart server once wired up.
   */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  chart_spec?: Record<string, any> | null;
  /**
   * Streaming — true while the answer is still arriving (sql_ready received
   * but answer_ready not yet).  Shows a "..." placeholder in the bubble.
   */
  pending?: boolean;
}

interface ChatMessageProps {
  message: Message;
  /** Called when the user clicks a follow-up chip — pre-fills the input. */
  onChipClick?: (question: string) => void;
}

export default function ChatMessage({ message, onChipClick }: ChatMessageProps) {
  const isUser = message.role === "user";

  return (
    <div
      className={`flex w-full ${isUser ? "justify-end" : "justify-start"}`}
      aria-label={isUser ? "Your message" : "Agent response"}
    >
      <div
        className={`max-w-[80%] rounded-2xl px-4 py-3 ${
          isUser
            ? "bg-indigo-600 text-white rounded-br-sm"
            : "bg-gray-800 text-gray-100 rounded-bl-sm"
        }`}
      >
        <p className="text-sm leading-relaxed whitespace-pre-wrap break-words">
          {message.content || (message.pending ? "..." : "")}
        </p>

        {/* SQL block — existing */}
        {!isUser && message.sql && <SqlBlock sql={message.sql} />}

        {/* Phase 9 — chart rendered before insights so the data leads */}
        {!isUser && message.chart_spec && (
          <ChartBlock spec={message.chart_spec} />
        )}

        {/* Phase 8 — insights + follow-up chips */}
        {!isUser && message.insights && onChipClick && (
          <InsightsCard
            insights={message.insights}
            onChipClick={onChipClick}
          />
        )}
      </div>
    </div>
  );
}
