/**
 * The Granada agent API.
 *
 * Reuses the auth transport's axios instance, so the bearer token, the base URL and the
 * refresh-cookie handling are shared rather than reimplemented. A second client that
 * assembled its own headers is how one of them ends up unauthenticated.
 *
 * EVERY function here distinguishes three outcomes: data, empty, and **could not load**.
 *
 * That distinction is the whole reason this module is typed the way it is. The backend went
 * to considerable trouble to ensure a gauge that cannot be measured is absent rather than
 * zero - because a zero is indistinguishable from health. The same rule applies to a screen:
 * a failed fetch that renders as "0 overdue reports" tells an operator their portfolio is
 * fine when the truth is that nobody looked. `AgentResult` makes that state representable
 * so a component cannot accidentally collapse it.
 */
import { api } from './api';

/**
 * The outcome of a call, with failure kept distinct from emptiness.
 *
 * `ok: true, data: []` means "we looked and there is nothing".
 * `ok: false` means "we could not look". A UI that renders both as empty is lying.
 */
export type AgentResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string };

async function load<T>(path: string, params?: Record<string, unknown>): Promise<AgentResult<T>> {
  try {
    const response = await api.get(path, { params });
    return { ok: true, data: response.data as T };
  } catch (error: any) {
    // The reason is preserved rather than swallowed. An operator staring at an empty
    // screen needs to know whether the platform is idle or broken.
    const status = error?.response?.status;
    const detail = error?.response?.data?.detail;
    const reason =
      typeof detail === 'string'
        ? detail
        : status
          ? `the server answered ${status}`
          : error?.message || 'the request did not complete';
    return { ok: false, error: reason };
  }
}

// ---------------------------------------------------------------------------
// Shapes, mirroring the API's own responses.
// ---------------------------------------------------------------------------
export interface AgentStatus {
  organisation_id?: string;
  agent?: {
    id: string;
    display_name?: string;
    status: string;
    autonomy: string;
    version?: number;
    last_active_at?: string | null;
  } | null;
}

export interface Grant {
  id: string;
  reference: string;
  title: string;
  donor_name?: string | null;
  currency: string;
  awarded_amount?: number | null;
  requested_amount?: number | null;
  size_relative_to_request?: string | null;
  status: string;
  awarded_at?: string | null;
  starts_on?: string | null;
  ends_on?: string | null;
  source_package_id?: string | null;
  application_id?: string | null;
}

export interface Deadline {
  kind: string;
  id: string;
  title: string;
  due_on: string;
  days_remaining: number;
  overdue: boolean;
  blocks_payment: boolean;
  grant_id?: string | null;
}

export interface ComplianceSummary {
  as_of?: string;
  grants_active: number;
  blocking_conditions: Array<{
    condition_id: string;
    grant_id: string;
    title: string;
    due_on: string | null;
    overdue: boolean;
  }>;
  overdue_reports: Array<{
    obligation_id: string;
    grant_id: string;
    title: string;
    due_on: string | null;
    days_late: number | null;
  }>;
  late_disbursements: Array<{
    disbursement_id: string;
    grant_id: string;
    label: string | null;
    amount: string;
    currency: string;
    expected_on: string;
    days_late: number;
  }>;
  portfolio: {
    scheduled_total: string;
    received_total: string;
    outstanding_total: string;
  };
  counts: {
    blocking_conditions: number;
    overdue_reports: number;
    late_disbursements: number;
  };
}

export interface Notification {
  id: string;
  category: string;
  severity: 'CRITICAL' | 'WARNING' | 'INFO';
  status: string;
  title: string;
  body?: string | null;
  action_required: boolean;
  action_url?: string | null;
  repeat_count: number;
  created_at?: string | null;
  last_raised_at?: string | null;
  source_event_type?: string | null;
}

export interface NotificationFeed {
  count: number;
  summary: { unread: number; action_required: number; critical: number };
  notifications: Notification[];
}

export interface ApplicationPackage {
  package_id: string;
  opportunity_title?: string | null;
  opportunity_url?: string | null;
  deadline?: string | null;
  deadline_is_exact?: boolean | null;
  status: string;
  //: The readiness verdict, evaluated by the server at read time.
  readiness: 'READY' | 'BLOCKED' | 'ASSEMBLING' | 'FAILED';
  //: The operator sentence. Names the blocker; never says "failed" for a missing upload.
  message: string;
  satisfied: number;
  required: number;
  documents: string[];
  missing: string[];
  //: What the ORGANISATION must supply. Distinct from anything the platform still owes, because
  //: asking an NGO for a document the system generates is the failure this field prevents.
  needs_organisation: string[];
  version?: number | null;
  submission_mode: string;
  //: True only once an external receipt exists.
  submitted: boolean;
  //: AUTHORISATION IS SEPARATE FROM READINESS. A package can be READY and still unauthorised - a
  //: person has not approved the fingerprint that would be sent - and the product could not express
  //: that difference, which is why there was no authorise button to build.
  authorised_at?: string | null;
  authorised_by?: string | null;
  fingerprint?: string | null;
  awaiting_authorisation?: boolean;
  created_at?: string | null;
}

export interface PackageSummary {
  packages: ApplicationPackage[];
  total: number;
  ready: number;
  blocked: number;
  failed: number;
  submitted: number;
}

// ---------------------------------------------------------------------------
// Calls
// ---------------------------------------------------------------------------
export const fetchAgentStatus = () => load<AgentStatus>('/agent');

export const fetchPackages = () => load<PackageSummary>('/agent/packages');

export const fetchGrants = () => load<{ count: number; grants: Grant[] }>('/agent/grants');

export const fetchCompliance = () => load<ComplianceSummary>('/agent/compliance');

export const fetchDeadlines = (withinDays = 30) =>
  load<{ count: number; deadlines: Deadline[] }>('/agent/deadlines', {
    within_days: withinDays,
  });

export const fetchNotifications = (unreadOnly = true) =>
  load<NotificationFeed>('/agent/notifications', { unread_only: unreadOnly });

/**
 * Mark a notification read, actioned or dismissed.
 *
 * A separate function because it WRITES, and the read helper above is a GET. Merging them
 * was the first version's mistake: it issued a GET and then a POST for one user action,
 * which is two requests where one is meant and a read that can fail independently of the
 * write it was supposed to perform.
 *
 * There is no delete. A notification that was raised is evidence the platform knew, and the
 * API has no route that erases one.
 */
export async function markNotification(
  id: string,
  status: 'READ' | 'ACTIONED' | 'DISMISSED',
): Promise<AgentResult<{ id: string; status: string }>> {
  try {
    const response = await api.post(`/agent/notifications/${id}`, { status });
    return { ok: true, data: response.data };
  } catch (error: any) {
    const detail = error?.response?.data?.detail;
    return {
      ok: false,
      error: typeof detail === 'string' ? detail : 'the status could not be changed',
    };
  }
}

// ---------------------------------------------------------------------------
// Mail
//
// An organisation holds ONE MAILBOX PER PORTAL ACCOUNT, so this is a list of mailboxes rather than
// a single inbox. Keeping them distinguishable is the whole reason the mailbox is recorded on every
// message: a code for the UN portal and a notification from a different portal belong to different
// histories, and merging them is what makes a verification impossible to attribute.
// ---------------------------------------------------------------------------

export interface Mailbox {
  id: string;
  address: string;
  //: Already translated by the server into a sentence, e.g. "Registration mail for www.example.org".
  purpose: string;
  is_primary: boolean;
  status: string;
  identity_type: string;
  message_count: number;
  last_message_at?: string | null;
}

export interface MailboxList {
  mailboxes: Mailbox[];
  total: number;
  //: Messages that arrived before a mailbox was recorded. Counted separately rather than hidden,
  //: because "no mailbox" and "no mail" are different facts.
  unassigned_messages: number;
}

export interface MailSummary {
  id: string;
  mailbox_id?: string | null;
  mailbox_address?: string | null;
  direction: 'INBOUND' | 'OUTBOUND';
  sender?: string | null;
  sender_name?: string | null;
  subject?: string | null;
  //: ALREADY REDACTED. A verification code never reaches this field; the server masks it. `null`
  //: here does not mean "empty" - it means the message had no preview stored.
  preview?: string | null;
  received_at?: string | null;
  created_at?: string | null;
  processing_status: string;
  has_body: boolean;
  //: True when a code was masked on the way out. The UI says "a code arrived, value withheld"
  //: rather than pretending the message had no code.
  code_redacted: boolean;
}

export interface MessagePage {
  messages: MailSummary[];
  count: number;
  has_more: boolean;
  next_before?: string | null;
}

export const fetchMailboxes = () => load<MailboxList>('/agent/mail/mailboxes');
export const fetchMailMessages = (params?: { mailbox_id?: string; direction?: string; limit?: number; before?: string }) =>
  load<MessagePage>('/agent/mail/messages', params);

// ---------------------------------------------------------------------------
// Documents: the vault, and the approval that unblocks assembly
//
// `DocumentVault` could generate, approve and reject; NO route exposed any of it, so a generated cover
// letter was invisible and unapprovable. Assembly then refused to freeze a package built on drafts -
// correctly - and the chain stopped one step from the portal with nothing in the product to unblock it.
// ---------------------------------------------------------------------------

export interface VaultDocument {
  id: string;
  title: string;
  doc_type: string;
  approval_status: string;
  version: number;
  is_current: boolean;
  scope_ref?: string | null;
  created_at?: string | null;
  approved_at?: string | null;
  size_bytes?: number | null;
}

export interface VaultDocumentList {
  documents: VaultDocument[];
  total: number;
  pending_approval: number;
  note: string;
}

export const fetchDocuments = () => load<VaultDocumentList>('/agent/documents');

/** Approve one document in the name of the person clicking. A WRITE, so it reports its own failure. */
export async function approveDocument(id: string): Promise<AgentResult<{ approval_status: string }>> {
  try {
    const response = await api.post(`/agent/documents/${id}/approve`);
    return { ok: true, data: response.data };
  } catch (error: any) {
    const detail = error?.response?.data?.detail;
    return { ok: false, error: typeof detail === 'string' ? detail : 'this document could not be approved' };
  }
}

export async function rejectDocument(
  id: string,
  reason?: string,
): Promise<AgentResult<{ approval_status: string; rejection_reason?: string | null }>> {
  try {
    // The reason is sent when given: a rejection naming only the person leaves the next one guessing
    // what to change, and an audit trail that answers "who" but not "why" cannot explain itself.
    const response = await api.post(`/agent/documents/${id}/reject`, null, {
      params: reason ? { reason } : undefined,
    });
    return { ok: true, data: response.data };
  } catch (error: any) {
    const detail = error?.response?.data?.detail;
    return { ok: false, error: typeof detail === 'string' ? detail : 'this document could not be rejected' };
  }
}

// ---------------------------------------------------------------------------
// Asking the agent to register on the portal
//
// `POST /agent/portal/registration` has existed since the browser workflow was made reachable, and no
// surface called it - so the mission's FIRST action, "go and register", had no button anywhere in the
// product. A person could prepare and authorise everything and still have no way to say go.
// ---------------------------------------------------------------------------

export interface RegistrationStart {
  workflow_id: string;
  state: string;
  opportunity_id: string;
  application_id: string;
  documents_workflow_id: string;
  note: string;
}

/**
 * Ask the agent to open the portal for this organisation.
 *
 * It never submits on this call: it queues the work. The browser stops before sending anything unless a
 * package has been authorised by a person, which is a separate deliberate act.
 */
export async function startPortalRegistration(): Promise<AgentResult<RegistrationStart>> {
  try {
    const response = await api.post('/agent/portal/registration');
    return { ok: true, data: response.data };
  } catch (error: any) {
    const detail = error?.response?.data?.detail;
    return {
      ok: false,
      error:
        typeof detail === 'string'
          ? detail
          : 'the registration task could not be queued',
    };
  }
}

// ---------------------------------------------------------------------------
// Browser evidence
//
// The screenshots the browser captured while working a package. They were written to the host and
// their paths recorded, but nothing showed them: for a system whose product is partly a defensible
// record of what it did, a file nobody can open is not evidence.
// ---------------------------------------------------------------------------

export interface EvidenceItem {
  name: string;
  url: string;
}

export interface EvidenceList {
  package_id: string;
  count: number;
  evidence: EvidenceItem[];
  note: string;
}

export const fetchEvidence = (packageId: string) =>
  load<EvidenceList>(`/agent/evidence/${packageId}`);

/** The absolute URL for one screenshot, for an <img src>. */
export function evidenceUrl(item: EvidenceItem): string {
  const base = (api.defaults.baseURL || '/api/v1').replace(/\/$/, '');
  return `${base}${item.url}`;
}

// ---------------------------------------------------------------------------
// Authorising a package
//
// Authorisation approves a FINGERPRINT over the exact bytes that will be sent, and it is what permits
// the browser to submit. The endpoint has existed and been permission-checked since the submission
// service was written; what did not exist was any way for a person to reach it, so the chain stopped
// one click from the portal.
//
// This reuses the existing etchPackages and ApplicationPackage rather than declaring a second,
// quietly-diverging copy of the same shape.
// ---------------------------------------------------------------------------

/**
 * Authorise one package. A WRITE, and the most consequential one in the product.
 *
 * A 409 means the package changed after it was assembled; re-reading and retrying is the intended
 * remedy, which is why the message is surfaced rather than swallowed.
 */
export async function authorisePackage(
  id: string,
  note?: string,
): Promise<AgentResult<{ status: string; fingerprint?: string }>> {
  try {
    const response = await api.post(`/agent/packages/${id}/authorise`, null, {
      params: note ? { note } : undefined,
    });
    return { ok: true, data: response.data };
  } catch (error: any) {
    const detail = error?.response?.data?.detail;
    return { ok: false, error: typeof detail === 'string' ? detail : 'this package could not be authorised' };
  }
}

// ---------------------------------------------------------------------------
// Connecting the organisation's OWN mailbox
//
// The product promised organisations could connect their existing Gmail rather than take a
// Granada-managed address. It matters beyond convenience: reading their own inbox is the one route to
// a verification code that does not depend on Cloudflare Email Routing.
// ---------------------------------------------------------------------------

export interface ConnectMailboxInput {
  address: string;
  imap_host: string;
  imap_port: number;
  imap_user: string;
  imap_password: string;
  use_ssl: boolean;
  display_name?: string;
}

export interface ConnectedMailbox {
  id: string;
  address: string;
  provider: string;
  connection_type: string;
  status: string;
  note: string;
}

/**
 * Register an external mailbox. A WRITE, so it is separate from the read helpers above.
 *
 * The password is sent once and never returned: the server encrypts it and keeps only a reference.
 * Nothing in this module reads it back.
 */
export async function connectMailbox(
  input: ConnectMailboxInput,
): Promise<AgentResult<ConnectedMailbox>> {
  try {
    const response = await api.post('/agent/mail/accounts', input);
    return { ok: true, data: response.data };
  } catch (error: any) {
    const detail = error?.response?.data?.detail;
    return {
      ok: false,
      error:
        typeof detail === 'string'
          ? detail
          : Array.isArray(detail)
            ? 'some fields were not accepted'
            : 'the mailbox could not be connected',
    };
  }
}

// ---------------------------------------------------------------------------
// Funding: what the discovery bot found, and what the matcher decided
//
// The catalogue is SHARED and belongs to nobody; only the `match` is this organisation's. That is
// why `match` is nullable: an opportunity the matcher has not reached yet is a real state, not a
// missing value.
// ---------------------------------------------------------------------------

export interface BlockingRequirement {
  gate: string;
  //: The profile facts that would make this gate decidable, e.g. ["country"].
  needs_facts: string[];
  action: string;
}

export interface OpportunityMatch {
  state: 'MATCHED' | 'NEEDS_DATA' | 'REJECTED_BY_RULE' | 'SUPERSEDED';
  hard_gate_passed: boolean;
  final_score?: number | null;
  rank?: number | null;
  computed_at?: string | null;
  //: The verdict was computed while the listing was still live. A stale answer is different from a
  //: wrong one, and the screen must say which it is.
  stale?: boolean;
  failed_gates: string[];
  unknown_gates: string[];
  blocking: BlockingRequirement[];
  gate_reasons: Array<{ gate: string; outcome: string; reason: string }>;
}

export interface Opportunity {
  id: string;
  title: string;
  source_name: string;
  source_url: string;
  country: string;
  deadline?: string | null;
  amount_min?: number | null;
  amount_max?: number | null;
  currency?: string | null;
  sector?: string | null;
  is_active: boolean;
  match?: OpportunityMatch | null;
}

export interface OpportunityList {
  opportunities: Opportunity[];
  total: number;
  limit: number;
  offset: number;
  include_inactive?: boolean;
  summary: { matched: number; needs_data: number; rejected: number; not_evaluated: number };
  catalogue_size: number;
  //: How many blocked matches each missing fact would unblock. The single most useful number on the
  //: screen: it turns "95 NEEDS_DATA" into "95 are waiting on your country".
  blocked_by_fact: Record<string, number>;
  next_offset?: number | null;
}

export const fetchOpportunities = (params?: {
  state?: string;
  limit?: number;
  offset?: number;
  include_inactive?: boolean;
}) => load<OpportunityList>('/agent/opportunities', params);

// ---------------------------------------------------------------------------
// The organisation profile - the facts the matcher is waiting on
//
// The funding screen says "18 blocked matches would become decidable if you supplied country".
// Without this there was nowhere to supply it: the backend has had PUT /agent/profile/facts since
// the profile intake was built, and the product offered no form, so the instruction pointed at a
// page with no field on it.
// ---------------------------------------------------------------------------

export interface ProfileFact {
  key: string;
  value: unknown;
  state: string;
  source: string;
  version: number;
  valid_until?: string | null;
  //: Whether this fact could actually be used in a submission (state is VERIFIED/USER_PROVIDED).
  submission_usable: boolean;
}

export interface MatchingGate {
  gate: string;
  decidable: boolean;
  needs: string[];
  satisfied_by: string[];
}

export interface ProfileFacts {
  facts: ProfileFact[];
  total: number;
  usable_fact_keys: string[];
  matching_gates: MatchingGate[];
  blocks_matching: string[];
  suggested_keys: Record<string, string>;
  //: The facts the document TEMPLATES interpolate, read from the templates by the server. The
  //: product used to hardcode this list, which meant a changed template could leave the panel saying
  //: "all facts supplied" while generation parked on a placeholder it needed.
  required_fact_keys?: string[];
}

export interface ProfileFactsWriteResult {
  recorded: Array<{ key: string; version: number; state: string }>;
  rejected: Array<{ key: string; reason: string }>;
  usable_fact_keys: string[];
  matching_gates: MatchingGate[];
  blocks_matching: string[];
}

export const fetchProfileFacts = () => load<ProfileFacts>('/agent/profile/facts');

/**
 * Record facts as USER_PROVIDED.
 *
 * A WRITE, so it is separate from the read helper above for the same reason `markNotification` is:
 * merging them issues a GET and then a POST for one user action, and the read can fail
 * independently of the write it was supposed to perform.
 *
 * The state is not sent - the server fixes it to USER_PROVIDED with the acting user as source. A
 * body that could claim VERIFIED, or AI_INFERRED, would defeat the one guarantee the fact table
 * exists for.
 */
export async function writeProfileFacts(
  facts: Array<{ key: string; value: unknown; valid_until?: string | null }>,
): Promise<AgentResult<ProfileFactsWriteResult>> {
  try {
    const response = await api.put('/agent/profile/facts', { facts });
    return { ok: true, data: response.data };
  } catch (error: any) {
    const detail = error?.response?.data?.detail;
    return {
      ok: false,
      error: typeof detail === 'string' ? detail : 'the profile could not be saved',
    };
  }
}


