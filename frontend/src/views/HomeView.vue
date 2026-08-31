<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";

import {
  operatorApi,
  type DeadLetterSummary,
  type OperatorCredentials,
  type PendingReview,
  type ProcessControlType,
  type ProcessDetail,
  type ProcessIntervention,
  type ProcessSummary,
  type RecoveryCommandSummary,
  type ReviewCommandType,
  type WakeCondition,
} from "@/api";

const credentials = reactive<OperatorCredentials>({ tenantId: "", actorId: "" });
const processes = ref<ProcessSummary[]>([]);
const reviews = ref<PendingReview[]>([]);
const deadLetters = ref<DeadLetterSummary[]>([]);
const recoveryCommands = ref<RecoveryCommandSummary[]>([]);
const selected = ref<ProcessDetail | null>(null);
const reviewNotes = reactive<Record<string, string>>({});
const interventionReasons = reactive<Record<string, string>>({});
const requeueReasons = reactive<Record<string, string>>({});
const processControlReason = ref("");
const connected = ref(false);
const loading = ref(false);
const error = ref("");
const notice = ref("");
const operationsError = ref("");

const selectedReviews = computed(() =>
  reviews.value.filter((review) => review.process_instance_id === selected.value?.id),
);

onMounted(() => {
  credentials.tenantId = localStorage.getItem("tiramisu.tenantId") ?? "";
  credentials.actorId = localStorage.getItem("tiramisu.actorId") ?? "";
  if (credentials.tenantId && credentials.actorId) void connect();
});

async function connect() {
  error.value = "";
  notice.value = "";
  if (!credentials.tenantId.trim() || !credentials.actorId.trim()) {
    error.value = "Enter both local development identity IDs.";
    return;
  }
  localStorage.setItem("tiramisu.tenantId", credentials.tenantId.trim());
  localStorage.setItem("tiramisu.actorId", credentials.actorId.trim());
  connected.value = true;
  await refresh();
}

async function refresh(preferredProcessId?: string) {
  loading.value = true;
  error.value = "";
  try {
    const operationsRequest = Promise.all([
      operatorApi.listDeadLetters(credentials),
      operatorApi.listRecoveryCommands(credentials),
    ])
      .then(([letters, commands]) => ({ letters, commands, error: "" }))
      .catch((cause: unknown) => ({
        letters: [] as DeadLetterSummary[],
        commands: [] as RecoveryCommandSummary[],
        error: cause instanceof Error ? cause.message : "Delivery operations are unavailable.",
      }));
    const [nextProcesses, nextReviews, operations] = await Promise.all([
      operatorApi.listProcesses(credentials),
      operatorApi.listReviews(credentials),
      operationsRequest,
    ]);
    processes.value = nextProcesses;
    reviews.value = nextReviews;
    deadLetters.value = operations.letters;
    recoveryCommands.value = operations.commands;
    operationsError.value = operations.error;
    const processId =
      preferredProcessId ??
      selected.value?.id ??
      nextReviews[0]?.process_instance_id ??
      nextProcesses[0]?.id;
    selected.value = processId ? await operatorApi.getProcess(credentials, processId) : null;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Could not load the operator view.";
  } finally {
    loading.value = false;
  }
}

async function requeueDeadLetter(message: DeadLetterSummary) {
  const reason = (requeueReasons[message.id] ?? "").trim();
  if (!reason) {
    error.value = "Add a reason before requeueing a dead-lettered delivery.";
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    await operatorApi.requeueDeadLetter(credentials, message.id, reason);
    notice.value = "Delivery requeued. The recovery decision is retained in audit history.";
    requeueReasons[message.id] = "";
    await refresh(message.process_instance_id ?? undefined);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Could not requeue the delivery.";
  } finally {
    loading.value = false;
  }
}

async function selectProcess(processId: string) {
  loading.value = true;
  error.value = "";
  try {
    selected.value = await operatorApi.getProcess(credentials, processId);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Could not load the process.";
  } finally {
    loading.value = false;
  }
}

async function submitReview(review: PendingReview, commandType: ReviewCommandType) {
  const message = (reviewNotes[review.thread_id] ?? "").trim();
  if (["reject", "request_revision", "comment"].includes(commandType) && !message) {
    error.value = "Add a note before sending feedback or requesting a change.";
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    await operatorApi.submitReview(credentials, review, commandType, message);
    notice.value =
      commandType === "approve"
        ? "Exact proposal approved and queued for the process."
        : commandType === "request_revision"
          ? "Feedback sent; the agent will propose a new revision."
          : commandType === "comment"
            ? "Comment added to the agent review thread."
            : "Proposal rejected.";
    reviewNotes[review.thread_id] = "";
    await refresh(review.process_instance_id);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Could not submit the review.";
  } finally {
    loading.value = false;
  }
}

async function submitProcessControl(
  commandType: ProcessControlType,
  intervention?: ProcessIntervention,
) {
  if (!selected.value) return;
  const reason = (
    intervention ? interventionReasons[intervention.id] : processControlReason.value
  )?.trim();
  if (!reason) {
    error.value = "Add a reason before issuing a process control.";
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    await operatorApi.submitProcessControl(
      credentials,
      selected.value.id,
      commandType,
      reason,
      intervention?.id,
    );
    notice.value =
      commandType === "retry"
        ? "Failed turn queued for retry with its original sources."
        : commandType === "takeover"
          ? "Agent paused for operator takeover."
          : commandType === "resume"
            ? "Agent resumed and queued to wake."
            : "Manual wake queued for the process.";
    if (intervention) interventionReasons[intervention.id] = "";
    else processControlReason.value = "";
    await refresh(selected.value.id);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Could not control the process.";
  } finally {
    loading.value = false;
  }
}

function wakeLabel(wake: WakeCondition) {
  if (wake.type === "event") return `Event · ${wake.event_type}`;
  if (wake.type === "timer") return `Timer · ${formatDate(wake.at)}`;
  return `Human · ${wake.interaction}`;
}

function formatDate(value?: string | null) {
  if (!value) return "Not set";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function displayValue(value: unknown) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function shortId(value: string) {
  return `${value.slice(0, 8)}…${value.slice(-4)}`;
}

function factSource(kind: "authoritative" | "customer_claim", key: string) {
  const source = selected.value?.fact_provenance[`${kind}:${key}`];
  if (!source) return "";
  return `${String(source.source_type).replaceAll("_", " ")} · ${shortId(String(source.source_id))}`;
}
</script>

<template>
  <main class="operator-shell">
    <header class="topbar">
      <a class="brand" href="/" aria-label="Tiramisu operator home">
        <span class="brand-mark">T</span>
        <span><strong>Tiramisu</strong><small>Operator console</small></span>
      </a>
      <div class="topbar-actions">
        <span v-if="connected" class="connection-dot">Local identity</span>
        <a href="http://127.0.0.1:8233" target="_blank">Temporal ↗</a>
        <a href="http://127.0.0.1:8000/docs" target="_blank">API ↗</a>
      </div>
    </header>

    <section class="identity-bar" aria-label="Local operator identity">
      <label><span>Tenant ID</span><input v-model="credentials.tenantId" data-testid="tenant-id" placeholder="UUID" /></label>
      <label><span>Actor ID</span><input v-model="credentials.actorId" data-testid="actor-id" placeholder="UUID" /></label>
      <button class="button button-primary" data-testid="connect" :disabled="loading" @click="connect">
        {{ connected ? "Refresh" : "Connect locally" }}
      </button>
      <p>Development headers only. Production identity is deliberately not implied.</p>
    </section>

    <div v-if="error" class="alert alert-error" role="alert">{{ error }}</div>
    <div v-if="notice" class="alert alert-success" role="status">{{ notice }}</div>

    <section v-if="connected" class="delivery-operations" data-testid="delivery-operations">
      <div class="section-heading">
        <div><p class="eyebrow">Delivery operations</p><h2>Dead-letter queue</h2></div>
        <span :class="{ 'danger-count': deadLetters.length }">{{ deadLetters.length }} waiting</span>
      </div>
      <div v-if="deadLetters.length" class="dead-letter-list">
        <article v-for="message in deadLetters" :key="message.id" class="dead-letter-card">
          <div class="dead-letter-copy">
            <span>{{ message.message_type.replaceAll('_', ' ') }} · {{ formatDate(message.dead_lettered_at) }}</span>
            <h3>{{ message.last_error ?? "Delivery exhausted its retry policy." }}</h3>
            <p>{{ message.attempt_count }} attempts · {{ message.destination }}</p>
            <button
              v-if="message.process_instance_id"
              class="text-button"
              @click="selectProcess(message.process_instance_id)"
            >Open process {{ shortId(message.process_instance_id) }}</button>
            <small v-else>No process is attached to this delivery.</small>
          </div>
          <div class="requeue-control">
            <label>
              <span>Why is retry safe now?</span>
              <textarea
                v-model="requeueReasons[message.id]"
                rows="3"
                placeholder="Provider restored, configuration corrected, or other evidence…"
              />
            </label>
            <button class="button button-primary" :disabled="loading" @click="requeueDeadLetter(message)">Requeue delivery</button>
          </div>
        </article>
      </div>
      <div v-else-if="operationsError" class="operations-empty operations-unavailable"><strong>Delivery operations unavailable.</strong><span>{{ operationsError }}</span></div>
      <div v-else class="operations-empty"><strong>No dead-lettered deliveries.</strong><span>Exhausted delivery failures will appear here for attributed recovery.</span></div>

      <details v-if="recoveryCommands.length" class="recovery-history">
        <summary>{{ recoveryCommands.length }} recent recovery decisions</summary>
        <ol>
          <li v-for="command in recoveryCommands" :key="command.id">
            <div><strong>{{ command.command_type }}</strong><time>{{ formatDate(command.created_at) }}</time></div>
            <p>{{ command.reason }}</p>
            <small>{{ command.previous_attempt_count }} previous attempts · message {{ shortId(command.outbox_message_id) }} · actor {{ shortId(command.actor_id) }}</small>
          </li>
        </ol>
      </details>
    </section>

    <section v-if="connected" class="workspace" :class="{ 'is-loading': loading }">
      <aside class="process-rail">
        <div class="rail-heading">
          <div><p class="eyebrow">Journeys</p><h1>{{ processes.length }} processes</h1></div>
          <span class="review-count">{{ reviews.length }} review</span>
        </div>
        <div v-if="processes.length" class="process-list" data-testid="process-list">
          <button
            v-for="process in processes"
            :key="process.id"
            class="process-item"
            :class="{ active: selected?.id === process.id }"
            @click="selectProcess(process.id)"
          >
            <span class="process-item-top">
              <strong>{{ process.process_type.replaceAll("_", " ") }}</strong>
              <i :class="`status-${process.status}`">{{ process.status }}</i>
            </span>
            <span>{{ shortId(process.id) }}</span>
            <span class="process-item-foot">
              <time>{{ formatDate(process.updated_at) }}</time>
              <b v-if="process.pending_reviews">{{ process.pending_reviews }} needs review</b>
            </span>
          </button>
        </div>
        <div v-else class="empty-state"><strong>No process journeys yet.</strong><span>Ingest a fictional enquiry to start one.</span></div>
      </aside>

      <article v-if="selected" class="process-detail" data-testid="process-detail">
        <header class="detail-header">
          <div><p class="eyebrow">{{ shortId(selected.id) }} · state {{ selected.state_version }}</p><h2>{{ selected.process_type.replaceAll("_", " ") }}</h2></div>
          <span class="large-status" :class="`status-${selected.status}`">{{ selected.status }}</span>
        </header>

        <section class="wake-panel" data-testid="wake-panel">
          <div><p class="eyebrow">Sleeping until</p><strong v-if="selected.current_wake_conditions.length">Wake plan is durable</strong><strong v-else>No wake condition</strong></div>
          <div class="wake-list">
            <span v-for="(wake, index) in selected.current_wake_conditions" :key="index">{{ wakeLabel(wake) }}</span>
            <span v-if="!selected.current_wake_conditions.length" class="muted">Terminal or active</span>
          </div>
        </section>

        <section
          v-if="selected.interventions.length || !['completed', 'cancelled', 'failed'].includes(selected.status)"
          class="intervention-section"
          data-testid="intervention-controls"
        >
          <div class="section-heading">
            <div><p class="eyebrow">Operator control</p><h3>Interventions and manual control</h3></div>
            <span>{{ selected.interventions.filter((item) => item.status === 'open').length }} open</span>
          </div>
          <article
            v-for="intervention in selected.interventions"
            :key="intervention.id"
            class="intervention-card"
            :class="{ resolved: intervention.status === 'resolved' }"
          >
            <div>
              <span>{{ intervention.kind.replaceAll('_', ' ') }} · {{ formatDate(intervention.created_at) }}</span>
              <h4>{{ intervention.error_type }}</h4>
              <p>{{ intervention.error }}</p>
              <small>{{ intervention.status }} · turn {{ shortId(intervention.agent_turn_id) }}</small>
            </div>
            <div v-if="intervention.status === 'open'" class="intervention-action">
              <label>
                <span>Why retry?</span>
                <textarea
                  v-model="interventionReasons[intervention.id]"
                  rows="3"
                  placeholder="Prompt corrected, provider restored, or other reason…"
                />
              </label>
              <button
                class="button button-primary"
                :disabled="loading"
                @click="submitProcessControl('retry', intervention)"
              >Retry failed turn</button>
            </div>
          </article>
          <div
            v-if="!['completed', 'cancelled', 'failed'].includes(selected.status)"
            class="manual-control"
          >
            <label>
              <span>Control reason</span>
              <textarea
                v-model="processControlReason"
                rows="2"
                placeholder="Required audit reason…"
              />
            </label>
            <div class="review-buttons">
              <button
                v-if="selected.status !== 'paused'"
                class="button"
                :disabled="loading"
                @click="submitProcessControl('wake')"
              >Wake now</button>
              <button
                v-if="selected.status !== 'paused'"
                class="button button-danger"
                :disabled="loading"
                @click="submitProcessControl('takeover')"
              >Pause and take over</button>
              <button
                v-else
                class="button button-primary"
                :disabled="loading"
                @click="submitProcessControl('resume')"
              >Resume agent</button>
            </div>
          </div>
        </section>

        <section v-if="selectedReviews.length" class="review-section" data-testid="review-queue">
          <div class="section-heading">
            <div><p class="eyebrow">Human checkpoint</p><h3>Review the exact proposal</h3></div>
            <span>{{ selectedReviews.length }} waiting</span>
          </div>
          <article v-for="review in selectedReviews" :key="review.thread_id" class="review-card">
            <div class="proposal-copy">
              <span>Revision {{ review.revision }} · {{ review.action_type.replaceAll("_", " ") }}</span>
              <h4>{{ review.rationale }}</h4>
              <dl>
                <template v-for="(value, key) in review.parameters" :key="key">
                  <dt>{{ key.replaceAll("_", " ") }}</dt><dd>{{ displayValue(value) }}</dd>
                </template>
              </dl>
              <small>Payload {{ shortId(review.payload_hash) }}</small>
            </div>
            <div class="review-controls">
              <label><span>Suggestion or decision note</span><textarea v-model="reviewNotes[review.thread_id]" rows="4" placeholder="Maybe something more like this…" /></label>
              <div class="review-buttons">
                <button class="button button-primary" @click="submitReview(review, 'approve')">Approve exact proposal</button>
                <button class="button" @click="submitReview(review, 'request_revision')">Suggest &amp; try again</button>
                <button class="button" @click="submitReview(review, 'comment')">Comment</button>
                <button class="button button-danger" @click="submitReview(review, 'reject')">Reject</button>
              </div>
            </div>
          </article>
        </section>

        <div class="state-grid">
          <section class="state-card memory-card">
            <p class="eyebrow">Working memory</p><p class="memory-copy">{{ selected.memory_summary ?? "No durable summary yet." }}</p>
            <ul v-if="selected.open_commitments.length"><li v-for="commitment in selected.open_commitments" :key="commitment">{{ commitment }}</li></ul>
          </section>
          <section class="state-card">
            <p class="eyebrow">Authoritative facts</p>
            <dl v-if="Object.keys(selected.authoritative_facts).length" class="fact-list">
              <template v-for="(value, key) in selected.authoritative_facts" :key="key"><dt>{{ key }}</dt><dd>{{ displayValue(value) }}<small v-if="factSource('authoritative', key)">{{ factSource("authoritative", key) }}</small></dd></template>
            </dl><p v-else class="muted">No provider facts recorded.</p>
          </section>
          <section class="state-card claims-card">
            <p class="eyebrow">Customer claims</p>
            <dl v-if="Object.keys(selected.customer_claims).length" class="fact-list">
              <template v-for="(value, key) in selected.customer_claims" :key="key"><dt>{{ key }}</dt><dd>{{ displayValue(value) }}<small v-if="factSource('customer_claim', key)">{{ factSource("customer_claim", key) }}</small></dd></template>
            </dl><p v-else class="muted">No customer claims recorded.</p>
          </section>
        </div>

        <section class="timeline-section" data-testid="timeline">
          <div class="section-heading"><div><p class="eyebrow">Durable history</p><h3>Process timeline</h3></div><span>{{ selected.timeline.length }} records</span></div>
          <ol class="timeline">
            <li v-for="item in selected.timeline" :key="`${item.kind}-${item.id}`">
              <span class="timeline-marker" :class="`kind-${item.kind}`"></span>
              <div class="timeline-content">
                <div><span>{{ item.kind }}</span><time>{{ formatDate(item.occurred_at) }}</time></div>
                <h4>{{ item.title }}</h4><i v-if="item.status" :class="`status-${item.status}`">{{ item.status }}</i>
                <details v-if="Object.keys(item.detail).length"><summary>Record detail</summary><pre>{{ JSON.stringify(item.detail, null, 2) }}</pre></details>
              </div>
            </li>
          </ol>
        </section>
      </article>

      <div v-else class="empty-detail"><span class="brand-mark">T</span><h2>Nothing is waiting yet.</h2><p>Start a journey and its durable context will appear here.</p></div>
    </section>

    <section v-else class="welcome">
      <p class="eyebrow">Long-running business agents</p><h1>See what is waiting.<br />Know why it wakes.</h1>
      <p>Connect with local development identity IDs to inspect process memory, business facts, wake plans, action history, and exact-payload human reviews.</p>
    </section>
  </main>
</template>
