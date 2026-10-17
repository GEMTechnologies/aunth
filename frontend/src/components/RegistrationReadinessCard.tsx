import React, { useCallback, useEffect, useState } from 'react';
import {
  VaultDocumentList,
  PackageSummary,
  fetchDocuments,
  fetchMailboxes,
  fetchPackages,
  fetchProfileFacts,
} from '../lib/agent';

/**
 * What is left before this organisation can be registered.
 *
 * WHY THIS EXISTS. Every gate now has a control: facts, document approval, package authorisation, and
 * the button that starts the agent. But they live in four separate cards, and nothing told a person
 * which of them was still outstanding — so the answer to "why has nothing happened?" required reading
 * all four and knowing which mattered.
 *
 * The four rows below are the actual chain, in order. Each says what is done, what is outstanding, and
 * where to act. Nothing here guesses: an unreachable endpoint makes its row UNKNOWN rather than
 * reported as complete, because "we could not check" and "it is done" are different answers.
 */

type State = 'DONE' | 'OUTSTANDING' | 'UNKNOWN';

interface Step {
  key: string;
  label: string;
  state: State;
  detail: string;
}

const FACT_KEYS = [
  'organisation_name',
  'registration_number',
  'registration_valid_until',
  'country',
  'organisation_type',
  'organisation_address',
  'authorised_signatory',
  'authorised_role',
  'contact_email',
  'focus_area',
];

const STYLES: Record<State, string> = {
  DONE: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  OUTSTANDING: 'bg-amber-50 text-amber-700 border-amber-200',
  UNKNOWN: 'bg-gray-50 text-gray-600 border-gray-200',
};

const RegistrationReadinessCard: React.FC = () => {
  const [steps, setSteps] = useState<Step[] | null>(null);

  const load = useCallback(async () => {
    const [facts, documents, packages, mailboxes] = await Promise.all([
      fetchProfileFacts(),
      fetchDocuments(),
      fetchPackages(),
      fetchMailboxes(),
    ]);

    const out: Step[] = [];

    // 1. Facts. The generator interpolates these; a missing one parks generation.
    if (!facts.ok) {
      out.push({ key: 'facts', label: 'Organisation facts', state: 'UNKNOWN', detail: facts.error });
    } else {
      const known = new Set(
        (facts.data.facts || [])
          .filter((f) => typeof f.value === 'string' && f.value.trim())
          .map((f) => f.key),
      );
      const missing = FACT_KEYS.filter((k) => !known.has(k));
      out.push({
        key: 'facts',
        label: 'Organisation facts',
        state: missing.length ? 'OUTSTANDING' : 'DONE',
        detail: missing.length
          ? `${missing.length} of ${FACT_KEYS.length} still needed: ${missing.slice(0, 4).join(', ')}${missing.length > 4 ? '…' : ''}`
          : `all ${FACT_KEYS.length} supplied`,
      });
    }

    // 2. Document approval.
    if (!documents.ok) {
      out.push({ key: 'documents', label: 'Documents approved', state: 'UNKNOWN', detail: documents.error });
    } else {
      const pending = documents.data.pending_approval || 0;
      out.push({
        key: 'documents',
        label: 'Documents approved',
        state: pending ? 'OUTSTANDING' : documents.data.total ? 'DONE' : 'OUTSTANDING',
        detail: documents.data.total
          ? pending
            ? `${pending} waiting for approval`
            : `${documents.data.total} approved`
          : 'no documents prepared yet — they are written once the facts exist',
      });
    }

    // 3. Package authorisation — the act that permits sending.
    if (!packages.ok) {
      out.push({ key: 'package', label: 'Package authorised', state: 'UNKNOWN', detail: packages.error });
    } else {
      const awaiting = (packages.data.packages || []).filter((p) => p.awaiting_authorisation).length;
      const authorised = (packages.data.packages || []).filter((p) => p.authorised_at).length;
      out.push({
        key: 'package',
        label: 'Package authorised',
        state: awaiting ? 'OUTSTANDING' : authorised ? 'DONE' : 'OUTSTANDING',
        detail: authorised
          ? `${authorised} authorised`
          : awaiting
            ? `${awaiting} assembled and waiting for authorisation`
            : 'nothing assembled yet',
      });
    }

    // 4. A way to receive the verification code.
    if (!mailboxes.ok) {
      out.push({ key: 'mail', label: 'Verification mail', state: 'UNKNOWN', detail: mailboxes.error });
    } else {
      const boxes = mailboxes.data.mailboxes || [];
      const owned = boxes.filter((b) => (b.identity_type || '') !== 'MANAGED').length;
      out.push({
        key: 'mail',
        label: 'Verification mail',
        state: boxes.length || owned ? 'DONE' : 'OUTSTANDING',
        detail: boxes.length
          ? `${boxes.length} mailbox(es) reachable`
          : 'no mailbox connected — connect one on the Mail screen, or enable the Cloudflare catch-all',
      });
    }

    setSteps(out);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (!steps) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 p-5" data-testid="readiness-card">
        <p className="text-sm text-gray-500">Checking what is left…</p>
      </div>
    );
  }

  const outstanding = steps.filter((s) => s.state === 'OUTSTANDING').length;
  const unknown = steps.filter((s) => s.state === 'UNKNOWN').length;

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5" data-testid="readiness-card">
      <div className="flex items-baseline justify-between mb-2">
        <h2 className="text-sm font-semibold text-gray-900">Ready to register?</h2>
        <span className="text-xs text-gray-500" data-testid="readiness-summary">
          {outstanding === 0 && unknown === 0
            ? 'everything in place'
            : `${outstanding} outstanding${unknown ? `, ${unknown} unknown` : ''}`}
        </span>
      </div>

      <ul className="space-y-2">
        {steps.map((step) => (
          <li
            key={step.key}
            className="flex items-start gap-3 border border-gray-100 rounded-lg p-2.5"
            data-testid={`readiness-${step.key}`}
          >
            <span
              className={`text-[11px] px-1.5 py-0.5 rounded border shrink-0 ${STYLES[step.state]}`}
              data-testid={`readiness-state-${step.key}`}
            >
              {step.state}
            </span>
            <div className="min-w-0">
              <p className="text-sm text-gray-900">{step.label}</p>
              <p className="text-xs text-gray-500">{step.detail}</p>
            </div>
          </li>
        ))}
      </ul>

      <p className="text-xs text-gray-500 mt-3">
        Registration still needs a person to authorise the package and press start. Nothing is sent
        automatically.
      </p>
    </div>
  );
};

export default RegistrationReadinessCard;
