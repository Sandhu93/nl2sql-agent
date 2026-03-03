"use client";

/**
 * SqlBlock — renders generated SQL in a styled, copy-able code block.
 *
 * TODO: Add syntax highlighting (e.g. with `react-syntax-highlighter` or
 *       `shiki`) once the agent is producing real SQL.
 */

interface SqlBlockProps {
  sql: string;
}

export default function SqlBlock({ sql }: SqlBlockProps) {
  return (
    <div className="mt-3 rounded-lg overflow-hidden border border-gray-700">
      <div className="flex items-center justify-between px-4 py-1.5 bg-gray-800 text-xs text-gray-400 font-mono">
        <span>Generated SQL</span>
        <button
          onClick={() => navigator.clipboard.writeText(sql)}
          className="hover:text-gray-100 transition-colors"
          aria-label="Copy SQL to clipboard"
        >
          Copy
        </button>
      </div>
      <pre className="p-4 bg-gray-900 text-green-400 text-sm font-mono overflow-x-auto whitespace-pre-wrap break-words">
        <code>{sql}</code>
      </pre>
    </div>
  );
}
