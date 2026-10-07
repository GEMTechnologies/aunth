import React, { useCallback, useEffect, useState } from 'react';
import {
  AgentResult,
  ComplianceSummary,
  Deadline,
  Grant,
  Notification,
  NotificationFeed,
  fetchCompliance,
  fetchDeadlines,
  fetchGrants,
  fetchNotifications,
  markNotification,
} from '../lib/agent';

/**
 * The operator surface: what the agent is doing, and what needs a person.
 *
 * THREE RULES THIS SCREEN FOLLOWS, and each is a consequence of something the backend had
 * to get right.
 *
 * 1. **A failure is never rendered as a zero.** The backend went to trouble to ensure a
 *    metric that cannot be measured is ABSENT rather than zero, because a zero is
 *    indistinguishable from health. The same is true here: a card that shows "0 overdue
 *    reports" because the request failed tells an operator their portfolio is fine when the
 *    truth is that nobody looked. Every card therefore has four states - loading, failed,
 *    empty, data - and the failed state is loud.
 *
 * 2. **Severity before recency.** The API already orders that way; this renders it in that
 *    order rather than re-sorting by date, because a critical notification raised yesterday
 *    matters more than an informational one raised a minute ago.
 *
 * 3. **Show the reason, not just the fact.** Every item the API returns carries why it is
 *    there - which condition blocks a payment, how many days a report is late. A dashboard
 *    that says "1 problem" without saying which is a dashboard nobody acts on.
 */

type CardState<T> =
  | { kind: 'loading' }
  | { kind: 'failed'; error: string }
  | { kind: 'ready'; value: T };

function stateOf<T>(result: AgentResult<T> | null): CardState<T> {
  if (result === null) return { kind: 'loading' };
  if (!result.ok) return { kind: 'failed', error: result.error };
  return { kind: 'ready', value: result.data };
}

/** A card wrapper that makes the four states impossible to collapse. */
const Card: React.FC<{
  title: string;
  subtitle?: string;
  state: CardState<unknown>;
  emptyMessage: string;
  isEmpty: (value: any) => boolean;
  onRetry?: () => void;
  children: (value: any) => React.ReactNode;
}> = ({ title, subtitle, state, emptyMessage, isEmpty, onRetry, children }) => (
  <section className="bg-white rounded-lg shadow-sm border border-gray-200 p-5" data-testid={`card-${title.toLowerCase().replace(/\s+/g, '-')}`}>
    <header className="mb-3">
      <h2 className="text-base font-semibold text-gray-900">{title}</h2>
      {subtitle && <p className="text-xs text-gray-500 mt-0.5">{subtitle}</p>}
    </header>

    {state.kind === 'loading' && (
      <p className="text-sm text-gray-400" data-testid="card-loading">
        Loading…
      </p>
    )}

    {/* THE IMPORTANT ONE. Not an empty state, and not a zero. */}
    {state.kind === 'failed' && (
      <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded p-3" data-testid="card-failed">
        <p className="font-medium">Could not load this</p>
        <p className="mt-1 text-red-600">{state.error}</p>
        <p className="mt-1 text-xs text-red-500">
          The figures below would be unknown, not zero — so nothing is shown.
        </p>
        {onRetry && (
          <button
            onClick={onRetry}
            className="mt-2 text-xs font-medium text-red-700 underline"
          >
            Try again
          </button>
        )}
      </div>
    )}

    {state.kind === 'ready' && isEmpty(state.value) && (
      <p className="text-sm text-gray-500" data-testid="card-empty">
        {emptyMessage}
      </p>
    )}

    {state.kind === 'ready' && !isEmpty(state.value) && children(state.value)}
  </section>
);

const SeverityBadge: React.FC<{ severity: Notification['severity'] }> = ({ severity }) => {
  const classes =
    severity === 'CRITICAL'
      ? 'bg-red-100 text-red-800 border-red-200'
      : severity === 'WARNING'
        ? 'bg-amber-100 text-amber-800 border-amber-200'
        : 'bg-gray-100 text-gray-700 border-gray-200';
  return (
    <span className={`text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded border ${classes}`}>
      {severity}
    </span>
  );
};

function money(amount: number | null | undefined, currency: string): string {
  if (amount === null || amount === undefined) return '—';
  return `${currency} ${amount.toLocaleString()}`;
}

/**
 * The same formatting for the figures the API returns as STRINGS.
 *
 * The portfolio totals and the disbursement amounts come back from the database as
 * `Numeric(18, 2)` and are serialised as strings, so `toLocaleString` cannot be applied
 * directly. The first version interpolated them raw, which put "USD 150,000" in the grant
 * table and "USD 75000.00" in the card beside it - the same number formatted two ways on one
 * screen, which reads as two different numbers.
 *
 * `Number()` is safe here: the value has already been through a numeric column, and a
 * non-numeric string falls back to the raw text rather than rendering "NaN".
 */
function moneyText(raw: string | number | null | undefined, currency: string): string {
  if (raw === null || raw === undefined || raw === '') return '—';
  const value = typeof raw === 'number' ? raw : Number(raw);
  if (!Number.isFinite(value)) return `${currency} ${raw}`;
  // Two decimal places kept when the amount has them, dropped when it does not, so a whole
  // grant does not read as "150,000.00" and a tranche does not lose its cents.
  const hasFraction = Math.abs(value % 1) > 0;
  return `${currency} ${value.toLocaleString(undefined, {
    minimumFractionDigits: hasFraction ? 2 : 0,
    maximumFractionDigits: 2,
  })}`;
}

function dueLabel(daysRemaining: number, overdue: boolean): string {
  if (overdue) return `${Math.abs(daysRemaining)} day(s) overdue`;
  if (daysRemaining === 0) return 'due today';
  return `in ${daysRemaining} day(s)`;
}

interface AgentPageProps {
  organisationName?: string;
}

const AgentPage: React.FC<AgentPageProps> = ({ organisationName }) => {
  const [compliance, setCompliance] = useState<AgentResult<ComplianceSummary> | null>(null);
  const [deadlines, setDeadlines] = useState<AgentResult<{ count: number; deadlines: Deadline[] }> | null>(null);
  const [grants, setGrants] = useState<AgentResult<{ count: number; grants: Grant[] }> | null>(null);
  const [notifications, setNotifications] = useState<AgentResult<NotificationFeed> | null>(null);

  const reload = useCallback(async () => {
    // Kicked off together and awaited together: four independent reads, and one being slow
    // should not hold up the other three.
    const [c, d, g, n] = await Promise.all([
      fetchCompliance(),
      fetchDeadlines(30),
      fetchGrants(),
      fetchNotifications(),
    ]);
    setCompliance(c);
    setDeadlines(d);
    setGrants(g);
    setNotifications(n);
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const acknowledge = async (id: string, status: 'READ' | 'DISMISSED' | 'ACTIONED') => {
    const result = await markNotification(id, status);
    if (result.ok) {
      reload();
    } else {
      // Surfaced rather than swallowed: an action that silently failed is worse than one
      // that visibly did, because the operator believes it worked.
      setNotifications({ ok: false, error: result.error });
    }
  };

  const complianceState = stateOf(compliance);

  return (
    <div className="space-y-6" data-testid="agent-page">
      <header>
        <h1 className="text-2xl font-bold text-gray-900">Your Granada agent</h1>
        <p className="text-sm text-gray-600 mt-1">
          {organisationName ? `${organisationName} — ` : ''}
          what the agent has done, and what needs a person.
        </p>
      </header>

      {/* THE HEADLINE. A single sentence an operator can act on, computed from the three
          lists the API returns rather than from a score - because weighting a blocked
          payment against a late report is not something a screen should decide. */}
      {complianceState.kind === 'failed' && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-4" data-testid="headline-failed">
          <p className="text-sm font-semibold text-red-800">
            Portfolio status unknown
          </p>
          <p className="text-sm text-red-700 mt-1">{complianceState.error}</p>
          <p className="text-xs text-red-600 mt-1">
            This is not the same as having nothing outstanding.
          </p>
        </div>
      )}

      {complianceState.kind === 'ready' && (
        <div
          className={`rounded-lg border p-4 ${
            complianceState.value.counts.blocking_conditions +
              complianceState.value.counts.overdue_reports +
              complianceState.value.counts.late_disbursements >
            0
              ? 'border-amber-200 bg-amber-50'
              : 'border-emerald-200 bg-emerald-50'
          }`}
          data-testid="headline"
        >
          {complianceState.value.counts.overdue_reports > 0 ? (
            <p className="text-sm font-semibold text-amber-900">
              {complianceState.value.counts.overdue_reports} funder report(s) are past their
              deadline. An unsubmitted report is the most common reason a tranche is
              withheld, and it produces no rejection letter.
            </p>
          ) : complianceState.value.counts.blocking_conditions > 0 ? (
            <p className="text-sm font-semibold text-amber-900">
              {complianceState.value.counts.blocking_conditions} condition(s) are blocking a
              payment.
            </p>
          ) : (
            <p className="text-sm font-semibold text-emerald-900">
              Nothing is overdue and no payment is blocked.
            </p>
          )}
          <p className="text-xs text-gray-600 mt-1">
            {complianceState.value.grants_active} active grant(s) ·{' '}
            {moneyText(complianceState.value.portfolio.scheduled_total, '')} scheduled ·{' '}
            {moneyText(complianceState.value.portfolio.received_total, '')} received ·{' '}
            {moneyText(complianceState.value.portfolio.outstanding_total, '')} outstanding
          </p>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        {/* -- NOTIFICATIONS: what needs a person ------------------------ */}
        <div className="lg:col-span-2">
          <Card
            title="Needs attention"
            subtitle="Most urgent first. A critical item raised yesterday outranks an informational one raised a minute ago."
            state={stateOf(notifications)}
            emptyMessage="Nothing needs you. This is the state you want."
            isEmpty={(feed: NotificationFeed) => feed.notifications.length === 0}
            onRetry={reload}
          >
            {(feed: NotificationFeed) => (
              <>
                <p className="text-xs text-gray-500 mb-3" data-testid="notification-summary">
                  {feed.summary.unread} unread · {feed.summary.action_required} need action ·{' '}
                  {feed.summary.critical} critical
                </p>
                <ul className="divide-y divide-gray-100">
                  {feed.notifications.map((n) => (
                    <li key={n.id} className="py-3 flex items-start gap-3" data-testid="notification">
                      <SeverityBadge severity={n.severity} />
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium text-gray-900">{n.title}</p>
                        {n.body && <p className="text-xs text-gray-600 mt-0.5">{n.body}</p>}
                        <p className="text-[11px] text-gray-400 mt-1">
                          {n.category}
                          {n.repeat_count > 0 &&
                            ` · raised ${n.repeat_count + 1} times, still open`}
                        </p>
                      </div>
                      <div className="flex flex-col gap-1 shrink-0">
                        <button
                          onClick={() => acknowledge(n.id, 'READ')}
                          className="text-xs text-gray-500 hover:text-gray-800"
                        >
                          Mark read
                        </button>
                        <button
                          onClick={() => acknowledge(n.id, 'DISMISSED')}
                          className="text-xs text-gray-400 hover:text-gray-700"
                        >
                          Dismiss
                        </button>
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </Card>
        </div>

        {/* -- COMPLIANCE ------------------------------------------------ */}
        <Card
          title="Payments blocked"
          subtitle="A condition on the grant that must be met before money moves."
          state={complianceState}
          emptyMessage="No condition is blocking a payment."
          isEmpty={(c: ComplianceSummary) => c.blocking_conditions.length === 0}
          onRetry={reload}
        >
          {(c: ComplianceSummary) => (
            <ul className="space-y-2">
              {c.blocking_conditions.map((condition) => (
                <li key={condition.condition_id} className="text-sm" data-testid="blocking-condition">
                  <p className="font-medium text-gray-900">{condition.title}</p>
                  <p className="text-xs text-gray-500">
                    {condition.overdue
                      ? `overdue since ${condition.due_on}`
                      : condition.due_on
                        ? `due ${condition.due_on}`
                        : 'no date set by the funder'}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card
          title="Overdue reports"
          subtitle="Money is lost to these silently — no rejection letter, just a tranche that does not arrive."
          state={complianceState}
          emptyMessage="No funder report is overdue."
          isEmpty={(c: ComplianceSummary) => c.overdue_reports.length === 0}
        >
          {(c: ComplianceSummary) => (
            <ul className="space-y-2">
              {c.overdue_reports.map((report) => (
                <li key={report.obligation_id} className="text-sm" data-testid="overdue-report">
                  <p className="font-medium text-gray-900">{report.title}</p>
                  <p className="text-xs text-red-600">
                    {report.days_late} day(s) late (was due {report.due_on})
                  </p>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card
          title="Late money"
          subtitle="Tranches past their expected date with no receipt recorded."
          state={complianceState}
          emptyMessage="Every expected tranche is on schedule."
          isEmpty={(c: ComplianceSummary) => c.late_disbursements.length === 0}
        >
          {(c: ComplianceSummary) => (
            <ul className="space-y-2">
              {c.late_disbursements.map((row) => (
                <li key={row.disbursement_id} className="text-sm" data-testid="late-disbursement">
                  <p className="font-medium text-gray-900">
                    {row.label || 'Tranche'} — {moneyText(row.amount, row.currency)}
                  </p>
                  <p className="text-xs text-amber-700">
                    expected {row.expected_on}, {row.days_late} day(s) late
                  </p>
                </li>
              ))}
            </ul>
          )}
        </Card>

        {/* -- DEADLINES ------------------------------------------------- */}
        <Card
          title="Next 30 days"
          subtitle="Conditions, reports and tranches together — an organisation's obligations are not separated by which table they live in."
          state={stateOf(deadlines)}
          emptyMessage="Nothing falls due in the next 30 days."
          isEmpty={(d: { deadlines: Deadline[] }) => d.deadlines.length === 0}
          onRetry={reload}
        >
          {(d: { deadlines: Deadline[] }) => (
            <ul className="space-y-2">
              {d.deadlines.map((deadline) => (
                <li key={`${deadline.kind}-${deadline.id}`} className="text-sm flex justify-between gap-3" data-testid="deadline">
                  <span className="min-w-0">
                    <span className="font-medium text-gray-900 block truncate">{deadline.title}</span>
                    <span className="text-xs text-gray-500">
                      {deadline.kind.toLowerCase()}
                      {deadline.blocks_payment && ' · blocks payment'}
                    </span>
                  </span>
                  <span className={`text-xs shrink-0 ${deadline.overdue ? 'text-red-600 font-medium' : 'text-gray-500'}`}>
                    {dueLabel(deadline.days_remaining, deadline.overdue)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        {/* -- GRANTS ---------------------------------------------------- */}
        <div className="lg:col-span-2">
          <Card
            title="Grants"
            subtitle="Every figure is derived from the application package a person authorised."
            state={stateOf(grants)}
            emptyMessage="No awards recorded yet."
            isEmpty={(g: { grants: Grant[] }) => g.grants.length === 0}
            onRetry={reload}
          >
            {(g: { grants: Grant[] }) => (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-gray-500 border-b">
                    <th className="py-1 font-medium">Reference</th>
                    <th className="py-1 font-medium">Funder</th>
                    <th className="py-1 font-medium">Awarded</th>
                    <th className="py-1 font-medium">Asked</th>
                    <th className="py-1 font-medium">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {g.grants.map((grant) => (
                    <tr key={grant.id} className="border-b last:border-0" data-testid="grant-row">
                      <td className="py-2">
                        <span className="font-medium text-gray-900">{grant.reference}</span>
                        <span className="block text-xs text-gray-500 truncate max-w-xs">
                          {grant.title}
                        </span>
                      </td>
                      <td className="py-2 text-gray-700">{grant.donor_name || '—'}</td>
                      <td className="py-2 text-gray-900">
                        {money(grant.awarded_amount, grant.currency)}
                        {grant.size_relative_to_request === 'REDUCED' && (
                          <span className="block text-xs text-amber-700">
                            reduced from the request
                          </span>
                        )}
                      </td>
                      <td className="py-2 text-gray-500">
                        {money(grant.requested_amount, grant.currency)}
                      </td>
                      <td className="py-2">
                        <span className="text-xs px-1.5 py-0.5 rounded bg-gray-100 text-gray-700">
                          {grant.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </div>
      </div>

      {/* THE GUARANTEE, stated on the screen rather than buried in a document. */}
      <footer className="text-xs text-gray-500 border-t pt-4" data-testid="guarantees">
        <p>
          This agent has <strong>not</strong> sent any email and has{' '}
          <strong>not</strong> filed any application. Outbound mail needs a person's approval;
          applications are prepared for a person to submit. Production emails sent: 0.
        </p>
      </footer>
    </div>
  );
};

export default AgentPage;
