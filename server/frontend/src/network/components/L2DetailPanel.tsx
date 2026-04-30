import type { TopologyL2 } from "../types";
import { timeAgo } from "../../utils";

interface Props {
  l2: TopologyL2 | null;
  onClose: () => void;
}

export function L2DetailPanel({ l2, onClose }: Props) {
  if (!l2) return null;
  return (
    <aside
      data-testid="l2-detail-panel"
      className="absolute right-0 top-0 h-full w-80 overflow-y-auto border-l border-gray-200 bg-white p-5 shadow-lg"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-base font-semibold text-gray-900">
            {l2.l2_id}
          </h3>
          <p className="text-xs uppercase tracking-wide text-gray-500">
            {l2.group}
          </p>
        </div>
        <button
          onClick={onClose}
          aria-label="Close detail panel"
          className="rounded text-gray-400 hover:text-gray-700"
        >
          <svg className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor">
            <path
              fillRule="evenodd"
              d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z"
              clipRule="evenodd"
            />
          </svg>
        </button>
      </div>

      <div className="mt-4 space-y-1 text-xs">
        <div className="text-gray-500">Endpoint</div>
        <code className="block truncate rounded bg-gray-100 px-2 py-1 font-mono text-[11px] text-gray-700">
          {l2.endpoint_url}
        </code>
      </div>

      <dl className="mt-4 grid grid-cols-3 gap-2 text-center">
        <div className="rounded border border-gray-200 p-2">
          <dt className="text-[10px] uppercase text-gray-400">KUs</dt>
          <dd className="text-lg font-semibold text-gray-900">{l2.ku_count}</dd>
        </div>
        <div className="rounded border border-gray-200 p-2">
          <dt className="text-[10px] uppercase text-gray-400">Domains</dt>
          <dd className="text-lg font-semibold text-gray-900">
            {l2.domain_count}
          </dd>
        </div>
        <div className="rounded border border-gray-200 p-2">
          <dt className="text-[10px] uppercase text-gray-400">Peers</dt>
          <dd className="text-lg font-semibold text-gray-900">
            {l2.peer_count}
          </dd>
        </div>
      </dl>

      <section className="mt-5">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">
          Peers
        </h4>
        {l2.peers.length === 0 ? (
          <p className="mt-1 text-sm text-gray-400">No peers</p>
        ) : (
          <ul className="mt-2 space-y-1">
            {l2.peers.map((p) => (
              <li
                key={p.l2_id}
                className="flex items-center justify-between rounded px-2 py-1 text-sm hover:bg-gray-50"
              >
                <span className="truncate font-mono text-xs text-gray-700">
                  {p.l2_id}
                </span>
                <span className="text-[11px] text-gray-400">
                  {p.last_signature_at ? timeAgo(p.last_signature_at) : "—"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="mt-5">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">
          Active personas
        </h4>
        {l2.active_personas.length === 0 ? (
          <p className="mt-1 text-sm text-gray-400">None active</p>
        ) : (
          <ul className="mt-2 space-y-2">
            {l2.active_personas.map((p) => (
              <li key={p.persona} className="rounded border border-gray-100 p-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate font-mono text-xs text-gray-800">
                    {p.persona}
                  </span>
                  <span className="text-[10px] text-gray-400">
                    {timeAgo(p.last_seen_at)}
                  </span>
                </div>
                {p.working_dir_hint && (
                  <code className="mt-1 block truncate text-[11px] text-gray-500">
                    {p.working_dir_hint}
                  </code>
                )}
                {p.expertise_domains.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {p.expertise_domains.map((d) => (
                      <span
                        key={d}
                        className="rounded-full bg-indigo-50 px-1.5 py-0.5 text-[10px] text-indigo-700"
                      >
                        {d}
                      </span>
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </aside>
  );
}
