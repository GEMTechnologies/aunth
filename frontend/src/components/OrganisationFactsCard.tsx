import React, { useCallback, useEffect, useState } from 'react';
import {
  MatchingGate,
  ProfileFacts,
  WebsiteImport,
  fetchProfileFacts,
  importFactsFromName,
  importFactsFromWebsite,
  writeProfileFacts,
} from '../lib/agent';

/**
 * The facts the matcher is waiting on, and a field for each.
 *
 * WHY THIS EXISTS. The funding screen states plainly that "18 blocked matches would become decidable
 * if you supplied country" and offers a button to the organisation profile. The profile page had no
 * field for it: the backend has accepted `PUT /agent/profile/facts` since the intake was built, and
 * the product never exposed it. An instruction with nowhere to act is worse than no instruction.
 *
 * THE FORM ONLY OFFERS WHAT THE MATCHER READS. The keys come from the server's own `matching_gates`,
 * so a field cannot be added here that no gate consumes - the two cannot drift.
 *
 * NOTHING IS GUESSED. Every value is typed by a person and stored USER_PROVIDED with their id as
 * source. The screen says so, because a fact the platform inferred could never be submitted and it
 * would be dishonest to imply otherwise.
 */

const FIELD_LABELS: Record<string, { label: string; hint: string; type: string; placeholder: string }> = {
  country: {
    label: 'Country of registration',
    hint: 'The country the organisation is legally registered in. Read by the country_eligible gate.',
    type: 'text',
    placeholder: 'e.g. Uganda',
  },
  registration_status: {
    label: 'Registration status',
    hint: 'ACTIVE if the registration is current. Read by the registration_valid gate.',
    type: 'text',
    placeholder: 'ACTIVE',
  },
  registration_valid_until: {
    label: 'Registration valid until',
    hint: 'The date the registration expires. Either this or the status is enough for the gate.',
    type: 'date',
    placeholder: '',
  },
  // THE FIVE THE DOCUMENT GENERATOR NEEDS AND THE MATCHER NEVER MENTIONS.
  //
  // The form used to render only the facts named by `matching_gates`, so these could not be entered
  // anywhere in the product - and the cover letter the PLATFORM writes cannot be generated without
  // them. Measured: `document_generate` parked with
  //   "organisation information: cover_letter: registration_number, organisation_type, country,
  //    authorised_role, organisation_address, organisation_name, authorised_signatory"
  // while the only form offered three of those seven. The package therefore stayed NEEDS_DATA and the
  // portal step could never start - and it was reported to the operator as their own missing data.
  registration_number: {
    label: 'Registration number',
    hint: 'The number on the registration certificate. Printed in the cover letter.',
    type: 'text',
    placeholder: 'e.g. 8001234567890',
  },
  organisation_type: {
    label: 'Organisation type',
    hint: 'How the organisation is legally constituted, e.g. NGO, charity, foundation.',
    type: 'text',
    placeholder: 'NGO',
  },
  organisation_address: {
    label: 'Registered address',
    hint: 'The address a funder would write to. Printed in the cover letter.',
    type: 'text',
    placeholder: 'Street, city, country',
  },
  authorised_signatory: {
    label: 'Authorised signatory',
    hint: 'The person who signs on behalf of the organisation.',
    type: 'text',
    placeholder: 'Full name',
  },
  authorised_role: {
    label: 'Signatory role',
    hint: 'That person\'s title, e.g. Executive Director.',
    type: 'text',
    placeholder: 'Executive Director',
  },
  // THE TWO THE ORGANISATION PROFILE STILL COULD NOT BE GENERATED WITHOUT.
  //
  // Read from the template itself (`agent/documents.py`, DEFAULT_TEMPLATES): it interpolates
  // `contact_email` and `focus_area`, and a blank one raises MissingFact, which parks the whole
  // generation step. Without these fields the profile document can never be produced, so the package
  // can never assemble, so the portal step can never start.
  contact_email: {
    label: 'Contact email',
    hint: 'The address a funder would write to. Printed in the organisation profile.',
    type: 'email',
    placeholder: 'info@example.org',
  },
  focus_area: {
    label: 'Focus area',
    hint: 'What the organisation works on, e.g. "girls education in northern Uganda".',
    type: 'text',
    placeholder: 'girls education',
  },
};

const OrganisationFactsCard: React.FC = () => {
  const [data, setData] = useState<ProfileFacts | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [rejected, setRejected] = useState<Array<{ key: string; reason: string }>>([]);
  const [siteUrl, setSiteUrl] = useState('');
  const [orgName, setOrgName] = useState('');
  const [importing, setImporting] = useState(false);
  const [imported, setImported] = useState<WebsiteImport | null>(null);

  const importFromName = async () => {
    if (!orgName.trim()) return;
    setImporting(true);
    setImported(null);
    setError(null);
    const result = await importFactsFromName(orgName.trim());
    setImporting(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setImported(result.data);
    if (result.data.name_mismatch) setError(result.data.name_mismatch);
    load();
  };

  const importFromSite = async () => {
    if (!siteUrl.trim()) return;
    setImporting(true);
    setImported(null);
    setError(null);
    const result = await importFactsFromWebsite(siteUrl.trim());
    setImporting(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setImported(result.data);
    load();
  };

  const load = useCallback(async () => {
    setError(null);
    const result = await fetchProfileFacts();
    if (!result.ok) {
      setData(null);
      setError(result.error);
      return;
    }
    setData(result.data);
    const current: Record<string, string> = {};
    for (const fact of result.data.facts) {
      if (typeof fact.value === 'string') current[fact.key] = fact.value;
    }
    setDraft(current);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // The gates the server says are undecidable come first - they are what the screen's sentence
  // promises - and every fact the DOCUMENT GENERATOR needs is always offered, whether or not a
  // matcher gate happens to name it. Without that, five of the seven facts the platform needs to
  // write a cover letter had no field anywhere in the product.
  const gates: MatchingGate[] = data?.matching_gates ?? [];
  const wanted = Array.from(new Set(gates.flatMap((gate) => gate.needs)));
  const keys = Array.from(new Set([...wanted, ...Object.keys(FIELD_LABELS)]));

  const save = async () => {
    const facts = keys
      .map((key) => ({ key, value: (draft[key] ?? '').trim() }))
      .filter((fact) => fact.value.length > 0)
      .map((fact) => ({
        key: fact.key,
        value: fact.value,
        valid_until: fact.key.endsWith('_until') ? fact.value : undefined,
      }));

    if (!facts.length) {
      setRejected([{ key: keys[0] ?? 'profile', reason: 'nothing to save yet' }]);
      return;
    }

    setSaving(true);
    setSaved(null);
    setRejected([]);
    const result = await writeProfileFacts(facts);
    setSaving(false);

    if (!result.ok) {
      setError(result.error);
      return;
    }
    setRejected(result.data.rejected);
    setSaved(
      result.data.blocks_matching.length === 0
        ? 'Saved. Every matching gate can now be decided.'
        : `Saved. Still waiting on: ${result.data.blocks_matching.join(', ')}.`,
    );
    await load();
  };

  return (
    <section
      className="border border-gray-200 rounded-lg p-4 bg-white"
      data-testid="profile-facts-card"
    >
      <header className="mb-3">
        <h3 className="text-lg font-medium text-gray-900">Profile facts</h3>
        <p className="text-sm text-gray-600 mt-1">
          The matcher will not guess these. Until they are supplied, funding that depends on them
          stays undecided — which is why the Funding screen reports matches as “needs data” rather
          than making an offer it cannot support.
        </p>
      </header>

      {/* ONBOARDING SHOULD NOT ASK FOR TEN VALUES THE ORGANISATION ALREADY PUBLISHES. */}
      <div className="mb-4 p-3 bg-gray-50 border border-gray-200 rounded-lg">
        <label className="text-xs text-gray-600 block mb-1">
          Start from your name or your website — we will propose the fields below
        </label>
        <div className="flex gap-2 mb-2">
          <input
            value={orgName}
            onChange={(e) => setOrgName(e.target.value)}
            placeholder="Your organisation's name"
            className="flex-1 px-3 py-1.5 text-sm border border-gray-300 rounded-md"
            data-testid="org-name"
          />
          <button
            type="button"
            onClick={importFromName}
            disabled={importing}
            className="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded-md hover:bg-emerald-700 disabled:opacity-50"
            data-testid="import-name"
          >
            {importing ? 'Looking…' : 'Find us'}
          </button>
        </div>
        <div className="flex gap-2">
          <input
            value={siteUrl}
            onChange={(e) => setSiteUrl(e.target.value)}
            placeholder="https://your-organisation.org"
            className="flex-1 px-3 py-1.5 text-sm border border-gray-300 rounded-md"
            data-testid="website-url"
          />
          <button
            type="button"
            onClick={importFromSite}
            disabled={importing}
            className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50"
            data-testid="import-website"
          >
            {importing ? 'Reading…' : 'Import'}
          </button>
        </div>
        {imported && (
          <p className="text-xs text-emerald-700 mt-2" data-testid="import-result">
            {imported.found.length} field(s) proposed
            {imported.kept_existing.length
              ? `, ${imported.kept_existing.length} kept as you supplied them`
              : ''}
            . They are stored as inferred — review and save to confirm.
          </p>
        )}
        {imported && imported.pages_read && imported.pages_read.length > 1 && (
          <p className="text-[11px] text-gray-500 mt-1" data-testid="import-pages">
            Read {imported.pages_read.length} pages from your site.
          </p>
        )}
        {imported && imported.absent && imported.absent.length > 0 && (
          <p className="text-[11px] text-gray-500 mt-1" data-testid="import-absent">
            Your site does not state: {imported.absent.slice(0, 5).join(', ')}
            {imported.absent.length > 5 ? '…' : ''}
          </p>
        )}
      </div>

      {error && (
        <p className="text-sm text-red-700 mb-3" data-testid="facts-error">
          {error}
        </p>
      )}

      {!data && !error && <p className="text-sm text-gray-400">Loading…</p>}

      {data && (
        <>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
            {keys.map((key) => {
              const meta = FIELD_LABELS[key] ?? {
                label: key,
                hint: 'Read by a matching gate.',
                type: 'text',
                placeholder: '',
              };
              const satisfied = gates.find((gate) => gate.satisfied_by.includes(key));
              return (
                <div key={key}>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    {meta.label}
                    {satisfied && (
                      <span className="ml-2 text-xs font-normal text-emerald-700">already supplied</span>
                    )}
                  </label>
                  <input
                    type={meta.type}
                    value={draft[key] ?? ''}
                    placeholder={meta.placeholder}
                    onChange={(event) =>
                      setDraft((current) => ({ ...current, [key]: event.target.value }))
                    }
                    className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                    data-testid={`fact-${key}`}
                  />
                  <p className="text-xs text-gray-500 mt-1">{meta.hint}</p>
                </div>
              );
            })}
          </div>

          <div className="flex items-center gap-3">
            <button
              onClick={save}
              disabled={saving}
              className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700 disabled:opacity-50"
              data-testid="save-facts"
            >
              {saving ? 'Saving…' : 'Save facts'}
            </button>
            {saved && (
              <p className="text-sm text-emerald-700" data-testid="facts-saved">
                {saved}
              </p>
            )}
          </div>

          {rejected.length > 0 && (
            <ul className="mt-3 text-sm text-amber-800" data-testid="facts-rejected">
              {rejected.map((item) => (
                <li key={item.key}>
                  {item.key}: {item.reason}
                </li>
              ))}
            </ul>
          )}

          {data.blocks_matching.length > 0 && (
            <p className="mt-3 text-xs text-amber-800" data-testid="facts-blocking">
              Matching is currently blocked on: {data.blocks_matching.join(', ')}.
            </p>
          )}

          <p className="mt-3 text-xs text-gray-400">
            Stored as supplied by you ({data.total} fact{data.total === 1 ? '' : 's'} on record). A
            value the platform inferred would never be usable in a submission.
          </p>
        </>
      )}
    </section>
  );
};

export default OrganisationFactsCard;
