"use client";

import SqlBlock from "./SqlBlock";

/**
 * ChatMessage — renders a single turn in the conversation.
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
}

interface ChatMessageProps {
  message: Message;
}

export default function ChatMessage({ message }: ChatMessageProps) {
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
          {message.content}
        </p>
        {!isUser && message.sql && <SqlBlock sql={message.sql} />}
      </div>
    </div>
  );
}
