// Lane F wires up the actual orchestration. For Lane E we render the buttons
// disabled with an explanatory tooltip so the layout/affordance lands first.

const BUTTONS = [
  { label: "Run cross-Group query", id: "run-cross-group" },
  { label: "Try cross-Enterprise (no consent)", id: "try-cross-enterprise" },
  { label: "Sign cross-Enterprise consent", id: "sign-consent" },
];

export function DemoControls() {
  return (
    <div
      data-testid="demo-controls"
      className="flex h-10 items-center justify-center gap-2 border-t border-gray-200 bg-white px-4"
    >
      {BUTTONS.map((b) => (
        <button
          key={b.id}
          type="button"
          disabled
          title="Wired in Lane F"
          className="cursor-not-allowed rounded-md border border-gray-200 bg-gray-50 px-3 py-1 text-xs font-medium text-gray-400"
        >
          {b.label}
        </button>
      ))}
    </div>
  );
}
