import React, { useCallback, useEffect, useState } from 'react';
import {
  AgentResult,
  Opportunity,
  OpportunityList,
  fetchOpportunities,
} from '../lib/agent';

/**
 * Funding: what the discovery bot found, and what the matcher decided about it.
 *
 * THREE RULES, each the consequence of a way this screen could mislead.
 *
 * 1. **A blocked match says what would unblock it.** "95 NEEDS_DATA" is a number nobody can act on.
 *    "95 are waiting on your country" points at the profile field that fixes it. The header puts the
 *    single highest-value fact first, because that is the one action that moves the most matches.
 *
 * 2. **Rejections are shown, not hidden.** The matcher keeps `REJECTED_BY_RULE` rows deliberately:
 *    an organisation that cannot see what it was ruled out of, and on what basis, cannot correct its
 *    own profile. A tidier screen that showed only good matches would be less useful.
 *
 * 3. **A failure is never a zero.** The four states are kept distinct. A summary of "0 matched"
 *    because the request failed would tell an organisation it has no funding when nobody looked.
 */

type Load =
  | { kind: 'loading' }
  | { kind: 'failed'; error: string }
  | { kind: 'ready'; value: OpportunityList };

function loadOf(result: AgentResult<OpportunityList> | null): Load {
  if (result === null) return { kind: 'loading' };
  if (!result.ok) return { kind: 'failed', error: result.error };
  return { kind: 'ready', value: result.data };
}

function money(amount?: number | null, currency?: string | null): string {
  if (amount == null) return '—';
  const formatted = amount >= 1000 ? `${Math.round(amount / 1000)}k` : `${amount}`;
  return `${currency || ''} ${formatted}`.trim();
}

function deadline(value?: string | null): { text: string; urgent: boolean } {
  if (!value) return { text: 'no deadline', urgent: false };
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return { text: 'no deadline', urgent: false };
  const days = Math.round((then - Date.now()) / 86_400_000);
  if (days < 0) return { text: `closed ${-days}d ago`, urgent: true };
  if (days <= 14) return { text: `${days}d left`, urgent: true };
  return { text: `${days}d left`, urgent: false };
}

const STATE_STYLES: Record<string, string> = {
  MATCHED: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  NEEDS_DATA: 'bg-amber-50 text-amber-800 border-amber-200',
  REJECTED_BY_RULE: 'bg-gray-100 text-gray-600 border-gray-200',
  SUPERSEDED: 'bg-gray-50 text-gray-400 border-gray-200',
};

const OpportunitiesPage: React.FC<{ onOpenProfile?: () => void }> = ({ onOpenProfile }) => {
  const [data, setData] = useState<Load | null>(null);
  const [stateFilter, setStateFilter] = useState<string>('');

  const load = useCallback(async () => {
    setData(null);
    setData(loadOf(await fetchOpportunities({ state: stateFilter || undefined, limit: 50 })));
  }, [stateFilter]);

  useEffect(() => {
    load();
  }, [load]);

  const summary = data?.kind === 'ready' ? data.value.summary : null;
  const blockers = data?.kind === 'ready' ? data.value.blocked_by_fact : {};
  const topBlocker = Object.entries(blockers).sort((a, b) => b[1] - a[1])[0];

  return (
    <div className="space-y-6" data-testid="opportunities-page">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold text-gray-900">Funding</h1>
          <p className="text-sm text-gray-600 mt-1">
            Opportunities the discovery bot has found, and whether this organisation can apply.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={stateFilter}
            onChange={(event) => setStateFilter(event.target.value)}
            className="px-3 py-1.5 text-sm border border-gray-300 rounded-md"
            data-testid="state-filter"
          >
            <option value="">All</option>
            <option value="MATCHED">Matched</option>
            <option value="NEEDS_DATA">Needs data</option>
            <option value="REJECTED_BY_RULE">Ruled out</option>
          </select>
          <button
            onClick={load}
            className="px-3 py-1.5 text-sm font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50"
            data-testid="refresh"
          >
            Refresh
          </button>
        </div>
      </header>

      {/* ---------------------------------------------------------------- */}
      {/* Summary                                                          */}
      {/* ---------------------------------------------------------------- */}
      {data?.kind === 'loading' && (
        <p className="text-sm text-gray-400" data-testid="loading">
          Loading…
        </p>
      )}

      {data?.kind === 'failed' && (
        <div className="bg-white border border-red-200 rounded-lg p-5 text-sm" data-testid="failed">
          <p className="text-red-700 font-medium">Funding could not be loaded.</p>
          <p className="text-gray-600 mt-1">{data.error}</p>
          <p className="text-gray-500 mt-1">
            This is not an empty catalogue — the request itself did not complete.
          </p>
          <button onClick={load} className="mt-2 text-blue-700 hover:underline" data-testid="retry">
            Try again
          </button>
        </div>
      )}

      {data?.kind === 'ready' && summary && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3" data-testid="summary">
            {[
              ['Matched', summary.matched, 'emerald'],
              ['Needs data', summary.needs_data, 'amber'],
              ['Ruled out', summary.rejected, 'gray'],
              ['Not judged yet', summary.not_evaluated, 'blue'],
              ['In catalogue', data.value.catalogue_size, 'gray'],
            ].map(([label, value]) => (
              <div
                key={String(label)}
                className="bg-white border border-gray-200 rounded-lg p-4"
                data-testid={`summary-${String(label).toLowerCase().replace(/\s+/g, '-')}`}
              >
                <p className="text-xs text-gray-500">{label}</p>
                <p className="text-2xl font-semibold text-gray-900 mt-1">{value as number}</p>
              </div>
            ))}
          </div>

          {/* THE ACTIONABLE SENTENCE. Not "95 NEEDS_DATA" - which fact unblocks the most. */}
          {topBlocker && (
            <div
              className="bg-amber-50 border border-amber-200 rounded-lg p-4 text-sm"
              data-testid="top-blocker"
            >
              <p className="text-amber-900 font-medium">
                {topBlocker[1]} blocked match{topBlocker[1] === 1 ? '' : 'es'} would become decidable
                if you supplied <span className="font-mono">{topBlocker[0]}</span>.
              </p>
              <p className="text-amber-800 mt-1">
                The matcher will not guess it — a value it inferred could never be submitted.
              </p>
              {onOpenProfile && (
                <button
                  onClick={onOpenProfile}
                  className="mt-2 text-blue-700 hover:underline"
                  data-testid="open-profile"
                >
                  Open organisation profile
                </button>
              )}
            </div>
          )}

          {data.value.opportunities.length === 0 && (
            <p className="text-sm text-gray-500" data-testid="empty">
              Nothing in the catalogue matches this filter yet. The discovery bot sweeps funding
              sources hourly, so new calls appear without anybody doing anything.
            </p>
          )}

          <ul className="space-y-3" data-testid="list">
            {data.value.opportunities.map((opp: Opportunity) => {
              const when = deadline(opp.deadline);
              const state = opp.match?.state;
              return (
                <li
                  key={opp.id}
                  className="bg-white border border-gray-200 rounded-lg p-4"
                  data-testid={`opportunity-${opp.id}`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <a
                        href={opp.source_url}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="text-sm font-medium text-gray-900 hover:text-blue-700"
                      >
                        {opp.title}
                      </a>
                      <p className="text-xs text-gray-500 mt-0.5">
                        {opp.source_name} · {opp.country}
                        {opp.sector ? ` · ${opp.sector}` : ''}
                      </p>
                    </div>
                    <div className="text-right shrink-0">
                      {state ? (
                        <span
                          className={`inline-block text-[11px] px-2 py-0.5 rounded-full border ${
                            STATE_STYLES[state] || STATE_STYLES.SUPERSEDED
                          }`}
                          data-testid={`state-${state}`}
                        >
                          {state.replace(/_/g, ' ').toLowerCase()}
                        </span>
                      ) : (
                        <span className="inline-block text-[11px] px-2 py-0.5 rounded-full border border-blue-200 bg-blue-50 text-blue-700">
                          not judged yet
                        </span>
                      )}
                      {opp.match?.final_score != null && (
                        <p className="text-xs text-gray-500 mt-1">
                          score {opp.match.final_score.toFixed(2)}
                          {opp.match.rank != null ? ` · rank ${opp.match.rank}` : ''}
                        </p>
                      )}
                    </div>
                  </div>

                  <div className="flex items-center gap-4 mt-2 text-xs text-gray-600">
                    <span className={when.urgent ? 'text-red-700 font-medium' : ''}>{when.text}</span>
                    <span>
                      up to {money(opp.amount_max, opp.currency)}
                      {opp.amount_min ? ` · from ${money(opp.amount_min, opp.currency)}` : ''}
                    </span>
                    {!opp.is_active && <span className="text-gray-400">no longer active</span>}
                    {opp.match?.stale && (
                      <span className="text-amber-700" data-testid="stale">
                        verdict from before the listing closed
                      </span>
                    )}
                  </div>

                  {/* WHY, in the organisation's own terms. */}
                  {opp.match && opp.match.blocking.length > 0 && (
                    <div className="mt-2 text-xs text-amber-800" data-testid="blocking">
                      <span className="font-medium">Waiting on: </span>
                      {opp.match.blocking.map((b) => b.needs_facts.join(', ') || b.gate).join(' · ')}
                    </div>
                  )}

                  {opp.match && opp.match.failed_gates.length > 0 && (
                    <div className="mt-2 text-xs text-gray-600" data-testid="failed-gates">
                      <span className="font-medium">Ruled out by: </span>
                      {opp.match.failed_gates.join(', ')}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>

          {data.value.next_offset != null && (
            <p className="text-xs text-gray-400" data-testid="more">
              Showing {data.value.opportunities.length} of {data.value.total}. More are available.
            </p>
          )}
        </>
      )}
    </div>
  );
};

export default OpportunitiesPage;
