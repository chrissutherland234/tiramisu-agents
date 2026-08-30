export interface OperatorCredentials {
  tenantId: string;
  actorId: string;
}

export interface ProcessSummary {
  id: string;
  process_type: string;
  definition_version: string;
  status: string;
  state_version: number;
  memory_summary: string | null;
  open_commitments: string[];
  pending_reviews: number;
  updated_at: string;
}

export interface WakeCondition {
  type: "event" | "timer" | "human";
  event_type?: string;
  at?: string;
  interaction?: string;
}

export interface TimelineItem {
  id: string;
  kind: string;
  occurred_at: string;
  title: string;
  status: string | null;
  detail: Record<string, unknown>;
}

export interface ProcessDetail {
  id: string;
  process_type: string;
  definition_version: string;
  status: string;
  state_version: number;
  workflow_id: string;
  authoritative_facts: Record<string, unknown>;
  customer_claims: Record<string, unknown>;
  fact_provenance: Record<string, Record<string, unknown>>;
  memory_summary: string | null;
  memory_summary_source_event_ids: string[];
  open_commitments: string[];
  current_wake_conditions: WakeCondition[];
  created_at: string;
  updated_at: string;
  timeline: TimelineItem[];
}

export interface PendingReview {
  thread_id: string;
  process_instance_id: string;
  process_type: string;
  action_request_id: string;
  action_type: string;
  revision: number;
  parameters: Record<string, unknown>;
  rationale: string;
  payload_hash: string;
  required_role: string | null;
  expires_at: string | null;
  created_at: string;
}

export type ReviewCommandType = "approve" | "reject" | "request_revision" | "comment";

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/$/, "");

async function request<T>(
  path: string,
  credentials: OperatorCredentials,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-Tiramisu-Tenant-ID": credentials.tenantId,
      "X-Tiramisu-Actor-ID": credentials.actorId,
      ...init.headers,
    },
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(payload?.detail ?? `Request failed (${response.status})`);
  }
  return (await response.json()) as T;
}

export const operatorApi = {
  listProcesses: (credentials: OperatorCredentials) =>
    request<ProcessSummary[]>("/v1/processes", credentials),
  getProcess: (credentials: OperatorCredentials, processId: string) =>
    request<ProcessDetail>(`/v1/processes/${processId}`, credentials),
  listReviews: (credentials: OperatorCredentials) =>
    request<PendingReview[]>("/v1/reviews", credentials),
  submitReview: (
    credentials: OperatorCredentials,
    review: PendingReview,
    commandType: ReviewCommandType,
    message: string,
  ) =>
    request(`/v1/reviews/${review.thread_id}/commands`, credentials, {
      method: "POST",
      body: JSON.stringify({
        command_type: commandType,
        message: message || null,
        expected_payload_hash: commandType === "approve" ? review.payload_hash : null,
      }),
    }),
};
