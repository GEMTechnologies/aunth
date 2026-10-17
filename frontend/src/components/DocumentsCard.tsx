import React, { useCallback, useEffect, useState } from 'react';
import {
  AgentResult,
  VaultDocument,
  VaultDocumentList,
  approveDocument,
  fetchDocuments,
  rejectDocument,
} from '../lib/agent';

/**
 * The documents the platform wrote on the organisation's behalf, and the approval that unblocks the
 * rest of the chain.
 *
 * WHY THIS EXISTS. Every generated document was invisible. The generator produced a cover letter and an
 * organisation profile, assembly correctly refused to freeze a package built on drafts, and the
 * organisation had no way to say "yes, that is us" - so the portal step could never start, and the
 * product reported nothing. This is the last human gate before the browser opens the UN portal.
 *
 * A document is a DRAFT until a named person approves it, and the screen says so, because the platform
 * must not imply it has stood behind a document it merely produced.
 */

const STATUS_STYLES: Record<string, string> = {
  APPROVED: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  PENDING: 'bg-amber-50 text-amber-700 border-amber-200',
  REJECTED: 'bg-red-50 text-red-700 border-red-200',
};

const DocumentsCard: React.FC = () => {
  const [data, setData] = useState<VaultDocumentList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    const result = await fetchDocuments();
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

  const act = async (document: VaultDocument, action: 'approve' | 'reject') => {
    setBusy(document.id);
    setActionError(null);
    // Ask WHY on rejection. Cancelling the prompt is not a rejection with no reason - it is a
    // decision not to reject yet, so nothing is sent.
    let why: string | undefined;
    if (action === 'reject') {
      const answer = window.prompt('Why is this document being rejected?', '');
      if (answer === null) {
        setBusy(null);
        return;
      }
      why = answer.trim() || undefined;
    }

    const result: AgentResult<{ approval_status: string }> =
      action === 'approve'
        ? await approveDocument(document.id)
        : await rejectDocument(document.id, why);
    setBusy(null);
    if (!result.ok) {
      setActionError(result.error);
      return;
    }
    load();
  };

  if (error) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 p-5" data-testid="documents-card">
        <h2 className="text-sm font-semibold text-gray-900 mb-2">Documents</h2>
        <p className="text-sm text-red-700" data-testid="documents-error">
          {error}
        </p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 p-5" data-testid="documents-card">
        <p className="text-sm text-gray-500">Loading documents…</p>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5" data-testid="documents-card">
      <div className="flex items-baseline justify-between mb-2">
        <h2 className="text-sm font-semibold text-gray-900">Documents</h2>
        <span className="text-xs text-gray-500" data-testid="documents-total">
          {data.total} in the vault
        </span>
      </div>

      {data.total === 0 ? (
        <p className="text-sm text-gray-500" data-testid="documents-empty">
          Nothing here yet. Documents are produced for an application once your organisation facts are
          supplied.
        </p>
      ) : (
        <>
          <p className="text-xs text-gray-600 mb-3" data-testid="documents-note">
            {data.note}
          </p>

          {actionError && (
            <p className="text-xs text-red-700 mb-2" data-testid="documents-action-error">
              {actionError}
            </p>
          )}

          <ul className="space-y-2">
            {data.documents.map((document) => (
              <li
                key={document.id}
                className="flex items-start justify-between gap-3 border border-gray-100 rounded-lg p-3"
                data-testid={`document-${document.doc_type}`}
              >
                <div className="min-w-0">
                  <p className="text-sm text-gray-900 truncate">{document.title}</p>
                  <p className="text-xs text-gray-500">
                    {document.doc_type} · v{document.version}
                    {!document.is_current && ' · superseded'}
                  </p>
                  <span
                    className={`inline-block mt-1 text-[11px] px-1.5 py-0.5 rounded border ${
                      STATUS_STYLES[document.approval_status] || 'bg-gray-50 text-gray-600 border-gray-200'
                    }`}
                    data-testid={`document-status-${document.doc_type}`}
                  >
                    {document.approval_status}
                  </span>
                </div>

                {document.is_current && document.approval_status === 'PENDING' && (
                  <div className="flex gap-2 shrink-0">
                    <button
                      onClick={() => act(document, 'approve')}
                      disabled={busy === document.id}
                      className="px-2.5 py-1 text-xs bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50"
                      data-testid={`approve-${document.doc_type}`}
                    >
                      {busy === document.id ? '…' : 'Approve'}
                    </button>
                    <button
                      onClick={() => act(document, 'reject')}
                      disabled={busy === document.id}
                      className="px-2.5 py-1 text-xs border border-gray-300 rounded-md text-gray-700 disabled:opacity-50"
                      data-testid={`reject-${document.doc_type}`}
                    >
                      Reject
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
};

export default DocumentsCard;
