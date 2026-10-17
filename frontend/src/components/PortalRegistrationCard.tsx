import React, { useState } from 'react';
import { RegistrationStart, startPortalRegistration } from '../lib/agent';

/**
 * The button that starts the mission.
 *
 * WHY THIS EXISTS. `POST /agent/portal/registration` — the endpoint that schedules the browser work on
 * the UN Partner Portal — has existed since the browser workflow was made reachable, and nothing in the
 * product called it. A person could supply facts, approve documents, authorise a package, and still have
 * no way to say "go". The first action of the entire mission had no button.
 *
 * WHAT IT DOES NOT DO, stated here because the distinction matters: it QUEUES the work. It does not
 * submit anything, and the browser will stop before sending unless a package has been authorised — a
 * separate, deliberate act on this same page.
 */
const PortalRegistrationCard: React.FC = () => {
  const [busy, setBusy] = useState(false);
  const [started, setStarted] = useState<RegistrationStart | null>(null);
  const [error, setError] = useState<string | null>(null);

  const start = async () => {
    setBusy(true);
    setError(null);
    setStarted(null);
    const result = await startPortalRegistration();
    setBusy(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setStarted(result.data);
  };

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5" data-testid="registration-card">
      <h2 className="text-sm font-semibold text-gray-900 mb-2">Portal registration</h2>
      <p className="text-xs text-gray-600 mb-3">
        Ask the agent to open the UN Partner Portal and work through registration. It prepares and
        navigates; it will not send anything until a package is authorised below.
      </p>

      <button
        onClick={start}
        disabled={busy}
        className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50"
        data-testid="start-registration"
      >
        {busy ? 'Starting…' : 'Start UN portal registration'}
      </button>

      {error && (
        <p className="text-xs text-red-700 mt-2" data-testid="registration-error">
          {error}
        </p>
      )}

      {started && (
        <div className="mt-3 text-xs text-emerald-700" data-testid="registration-started">
          <p>Queued — the agent will begin shortly.</p>
          <p className="text-gray-500 mt-1">
            workflow {started.workflow_id.slice(0, 8)} · documents {started.documents_workflow_id.slice(0, 8)} · state {started.state}
          </p>
          <p className="text-gray-500">{started.note}</p>
        </div>
      )}
    </div>
  );
};

export default PortalRegistrationCard;
