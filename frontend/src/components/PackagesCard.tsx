import React, { useCallback, useEffect, useState } from 'react';
import {
  ApplicationPackage,
  PackageSummary,
  authorisePackage,
  fetchPackages,
} from '../lib/agent';

/**
 * The frozen packages, and the authorisation that lets the browser act on one.
 *
 * WHY THIS EXISTS. `SubmissionService.authorise` has existed and been permission-checked since the
 * submission service was written, and no surface exposed it. A package assembled to DRAFT and the
 * browser task then refused with `browser.package_not_ready` - correctly, because opening a portal on
 * a package no person has authorised is exactly what the gate is for - and there was nothing in the
 * product with which to authorise one. The chain stopped one click from the portal.
 *
 * WHAT AUTHORISING MEANS, stated on the screen because it is the most consequential click here: it
 * approves a FINGERPRINT over the exact bytes that will be sent, and it is what permits submission.
 * Readiness is shown separately, because a package can be READY and still unauthorised - a person has
 * not approved what would go out.
 */

const READINESS_STYLES: Record<string, string> = {
  READY: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  BLOCKED: 'bg-amber-50 text-amber-700 border-amber-200',
  FAILED: 'bg-red-50 text-red-700 border-red-200',
};

const PackagesCard: React.FC = () => {
  const [data, setData] = useState<PackageSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [authorised, setAuthorised] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    const result = await fetchPackages();
    if (!result.ok) {
      setData(null);
      setError(result.error);
      return;
    }
    setData(result.data);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const authorise = async (item: ApplicationPackage) => {
    setBusy(item.package_id);
    setActionError(null);
    setAuthorised(null);
    const result = await authorisePackage(item.package_id);
    setBusy(null);
    if (!result.ok) {
      setActionError(result.error);
      return;
    }
    setAuthorised(item.package_id);
    load();
  };

  const shell = (children: React.ReactNode) => (
    <div className="bg-white rounded-xl border border-gray-200 p-5" data-testid="packages-card">
      <div className="flex items-baseline justify-between mb-2">
        <h2 className="text-sm font-semibold text-gray-900">Submission packages</h2>
        {data && (
          <span className="text-xs text-gray-500" data-testid="packages-total">
            {data.packages.length}
          </span>
        )}
      </div>
      <p className="text-xs text-gray-600 mb-3">
        A package is frozen at a fingerprint. Authorising it approves <strong>that</strong> fingerprint
        and is what permits the browser to submit — nothing is sent without it.
      </p>
      {children}
    </div>
  );

  if (error) {
    return shell(
      <p className="text-sm text-red-700" data-testid="packages-error">{error}</p>,
    );
  }
  if (!data) {
    return shell(<p className="text-sm text-gray-500">Loading packages…</p>);
  }
  if (data.packages.length === 0) {
    return shell(
      <p className="text-sm text-gray-500" data-testid="packages-empty">
        Nothing assembled yet. A package is built once the documents are approved.
      </p>,
    );
  }

  return shell(
    <>
      {actionError && (
        <p className="text-xs text-red-700 mb-2" data-testid="packages-action-error">{actionError}</p>
      )}

      <ul className="space-y-2">
        {data.packages.map((item) => (
          <li
            key={item.package_id}
            className="border border-gray-100 rounded-lg p-3"
            data-testid="package-row"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm text-gray-900 truncate">
                  {item.opportunity_title || 'Package'}
                </p>
                <p className="text-xs text-gray-500">
                  {item.status}
                  {item.documents.length > 0 && ` · ${item.documents.length} document(s)`}
                  {item.fingerprint && ` · ${item.fingerprint.slice(0, 12)}`}
                </p>
                <div className="flex items-center gap-2 mt-1">
                  <span
                    className={`text-[11px] px-1.5 py-0.5 rounded border ${
                      READINESS_STYLES[item.readiness] || 'bg-gray-50 text-gray-600 border-gray-200'
                    }`}
                    data-testid="package-readiness"
                  >
                    {item.readiness}
                  </span>
                  {item.authorised_at ? (
                    <span className="text-[11px] text-emerald-700" data-testid="package-authorised">
                      authorised
                    </span>
                  ) : (
                    <span className="text-[11px] text-amber-700" data-testid="package-unauthorised">
                      not authorised
                    </span>
                  )}
                </div>
                <p className="text-xs text-gray-500 mt-1">{item.message}</p>
              </div>

              {item.awaiting_authorisation && (
                <button
                  onClick={() => authorise(item)}
                  disabled={busy === item.package_id}
                  className="px-2.5 py-1 text-xs bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 shrink-0"
                  data-testid="authorise-package"
                >
                  {busy === item.package_id ? '…' : 'Authorise'}
                </button>
              )}
            </div>

            {authorised === item.package_id && (
              <p className="text-xs text-emerald-700 mt-2" data-testid="package-authorised-now">
                Authorised. The browser may now act on this package.
              </p>
            )}
          </li>
        ))}
      </ul>
    </>,
  );
};

export default PackagesCard;
