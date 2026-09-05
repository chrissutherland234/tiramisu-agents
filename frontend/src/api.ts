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
  current_wake_conditions: WakeCondition[];
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
  agent_turn_id?: string | null;
  action_request_id?: string | null;
  detail: Record<string, unknown>;
}

export interface ProcessIntervention {
  id: string;
  agent_turn_id: string;
  kind: string;
  status: "open" | "resolved";
  error_type: string;
  error: string;
  source_event_ids: string[];
  source_review_command_ids: string[];
  source_action_attempt_ids: string[];
  source_timer_ids: string[];
  resolved_by_command_id: string | null;
  resolved_at: string | null;
  created_at: string;
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
  communication_safety?: CommunicationSafety | null;
  interventions: ProcessIntervention[];
  timeline: TimelineItem[];
}

export interface CommunicationSafetyBlock {
  code: string;
  message: string;
  next_allowed_at: string | null;
}

export interface CommunicationSafety {
  evaluated_at: string;
  outbound_action_types: string[];
  outbound_allowed_now: boolean;
  blocks: CommunicationSafetyBlock[];
  outbound_messages_total: number;
  max_outbound_messages_per_process: number;
  outbound_messages_in_window: number;
  max_outbound_messages_per_window: number;
  outbound_message_window_hours: number;
  follow_ups_since_reply: number;
  max_follow_ups_without_reply: number;
  minimum_follow_up_interval_hours: number;
  last_human_reply_at: string | null;
  latest_automated_response_at: string | null;
  opted_out_at: string | null;
  process_expires_at: string;
  quiet_hours_timezone: string | null;
  quiet_hours_start_local: string | null;
  quiet_hours_end_local: string | null;
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

export interface DeadLetterSummary {
  id: string;
  process_instance_id: string | null;
  message_type: string;
  destination: string;
  attempt_count: number;
  last_error: string | null;
  dead_lettered_at: string;
  created_at: string;
}

export interface RecoveryCommandSummary {
  id: string;
  outbox_message_id: string;
  actor_id: string;
  command_type: string;
  reason: string;
  previous_attempt_count: number;
  previous_error: string | null;
  previous_dead_lettered_at: string;
  created_at: string;
}

export type ReviewCommandType = "approve" | "reject" | "request_revision" | "comment";
export type ProcessControlType = "retry" | "wake" | "takeover" | "resume";

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
  listDeadLetters: (credentials: OperatorCredentials) =>
    request<DeadLetterSummary[]>("/v1/outbox/dead-letters", credentials),
  listRecoveryCommands: (credentials: OperatorCredentials) =>
    request<RecoveryCommandSummary[]>("/v1/outbox/recovery-commands", credentials),
  requeueDeadLetter: (
    credentials: OperatorCredentials,
    outboxMessageId: string,
    reason: string,
  ) =>
    request(`/v1/outbox/dead-letters/${outboxMessageId}/requeue`, credentials, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
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
  submitProcessControl: (
    credentials: OperatorCredentials,
    processId: string,
    commandType: ProcessControlType,
    reason: string,
    interventionId?: string,
  ) =>
    request(`/v1/processes/${processId}/controls`, credentials, {
      method: "POST",
      body: JSON.stringify({
        command_type: commandType,
        reason,
        intervention_id: interventionId ?? null,
      }),
    }),
};
