export function ConfidenceBadge({ confidence }: { confidence: number }) {
  return (
    <span className="text-sm text-[var(--ink-mute)]">
      <span className="eyebrow">Confidence</span>{" "}
      <strong className="font-mono-brand text-[var(--ink)]">
        {confidence.toFixed(2)}
      </strong>
    </span>
  )
}
