"use client";

import { Insights } from "@/lib/api";

/**
 * InsightsCard — Phase 8: Insight generation layer.
 *
 * Renders the key takeaway and follow-up question chips returned by the
 * backend's insights_agent.py.  Follow-up chips are clickable buttons that
 * call onChipClick to pre-fill the chat input with the suggested question.
 *
 * TODO: Animate the chips in with a staggered fade once the answer appears.
 */

interface InsightsCardProps {
  insights: Insights;
  onChipClick: (question: string) => void;
}

export default function InsightsCard({
  insights,
  onChipClick,
}: InsightsCardProps) {
  const { key_takeaway, follow_up_chips } = insights;

  if (!key_takeaway && follow_up_chips.length === 0) return null;

  return (
    <div className="mt-3 rounded-xl border border-indigo-800/40 bg-indigo-950/30 px-4 py-3 space-y-3">
      {/* Key takeaway */}
      {key_takeaway && (
        <p className="text-xs text-indigo-300 leading-relaxed">
          <span className="font-semibold text-indigo-200">Insight: </span>
          {key_takeaway}
        </p>
      )}

      {/* Follow-up chips */}
      {follow_up_chips.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {follow_up_chips.map((chip, i) => (
            <button
              key={i}
              onClick={() => onChipClick(chip)}
              className="rounded-full border border-indigo-700/60 bg-indigo-900/40
                         px-3 py-1 text-xs text-indigo-300
                         hover:bg-indigo-800/60 hover:text-indigo-100
                         transition-colors cursor-pointer"
            >
              {chip}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
