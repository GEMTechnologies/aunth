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
