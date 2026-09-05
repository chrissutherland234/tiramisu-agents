<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import {
  quarantineApi,
  type OperatorCredentials,
  type QuarantineDetail,
  type QuarantinePage,
  type ResolveQuarantineRequest,
} from "@/api";

const credentials = reactive<OperatorCredentials>({ tenantId: "", actorId: "" });
const activeCredentials = ref<OperatorCredentials | null>(null);
const page = ref<QuarantinePage | null>(null);
const selected = ref<QuarantineDetail | null>(null);
const state = ref<"unresolved" | "resolved">("unresolved");
const offset = ref(0);
const loading = ref(false);
const detailLoading = ref(false);
const saving = ref(false);
const error = ref("");
const notice = ref("");
const destination = ref("");
const reason = ref("");
const boundIndices = ref<number[]>([]);
let generation = 0;
let selectionGeneration = 0;
let pendingCommand: { eventId: string; fingerprint: string; body: ResolveQuarantineRequest } | null = null;

const target = computed(() => selected.value?.candidates.find((item) => item.id === destination.value.trim()));
const terminalTarget = computed(() => target.value && ["completed", "cancelled", "failed"].includes(target.value.status));
const canSubmit = computed(() => !!destination.value.trim() && !!reason.value.trim() && !saving.value);

onMounted(() => {
  credentials.tenantId = localStorage.getItem("tiramisu.tenantId") ?? "";
  credentials.actorId = localStorage.getItem("tiramisu.actorId") ?? "";
  if (credentials.tenantId && credentials.actorId) void connect();
});

watch(destination, () => {
  boundIndices.value = boundIndices.value.filter((index) => {
    const owner = selected.value?.references[index]?.process_instance_id;
    return !owner || owner === destination.value.trim();
  });
});

async function connect() {
  if (!credentials.tenantId.trim() || !credentials.actorId.trim()) {
    error.value = "Enter both local development identity IDs.";
    return;
  }
  generation++;
  selectionGeneration++;
  activeCredentials.value = { tenantId: credentials.tenantId.trim(), actorId: credentials.actorId.trim() };
  localStorage.setItem("tiramisu.tenantId", activeCredentials.value.tenantId);
  localStorage.setItem("tiramisu.actorId", activeCredentials.value.actorId);
  page.value = null;
  selected.value = null;
  detailLoading.value = false;
  notice.value = "";
  pendingCommand = null;
  offset.value = 0;
  await refresh();
}

async function refresh() {
  if (!activeCredentials.value) return;
  const current = ++generation;
  loading.value = true;
  error.value = "";
  try {
    const next = await quarantineApi.list(activeCredentials.value, state.value, offset.value);
    if (current !== generation) return;
    if (offset.value > 0 && offset.value >= next.total) {
      offset.value = Math.max(0, Math.floor((next.total - 1) / 25) * 25);
      await refresh();
      return;
    }
    page.value = next;
  } catch (cause) {
    if (current === generation) error.value = message(cause);
  } finally {
    if (current === generation) loading.value = false;
  }
}

async function changePage(nextOffset = 0) {
  offset.value = nextOffset;
  selected.value = null;
  selectionGeneration++;
  detailLoading.value = false;
  notice.value = "";
  await refresh();
}

async function inspect(eventId: string) {
  if (!activeCredentials.value || saving.value) return;
  const current = ++selectionGeneration;
  detailLoading.value = true;
  error.value = "";
  notice.value = "";
  selected.value = null;
  try {
    const detail = await quarantineApi.get(activeCredentials.value, eventId);
    if (current !== selectionGeneration) return;
    selected.value = detail;
    destination.value = "";
    reason.value = "";
    boundIndices.value = [];
    pendingCommand = null;
  } catch (cause) {
    if (current === selectionGeneration) error.value = message(cause);
  } finally {
    if (current === selectionGeneration) detailLoading.value = false;
  }
}

async function resolve() {
  if (!activeCredentials.value || !selected.value || !canSubmit.value) return;
  const processId = destination.value.trim();
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(processId)) {
    error.value = "Enter a valid destination process UUID.";
    return;
  }
  const eventId = selected.value.id;
  const content = {
    process_instance_id: processId,
    reason: reason.value.trim(),
    bind_references: [...boundIndices.value].sort((a, b) => a - b).map((index) => selected.value!.references[index]!.reference),
  };
  const fingerprint = JSON.stringify(content);
  if (!pendingCommand || pendingCommand.eventId !== eventId || pendingCommand.fingerprint !== fingerprint) {
    pendingCommand = { eventId, fingerprint, body: { command_id: crypto.randomUUID(), ...content } };
  }
  saving.value = true;
  error.value = "";
  notice.value = "";
  try {
    const result = await quarantineApi.resolve(activeCredentials.value, eventId, pendingCommand.body);
    // Keep the successful decision visible even if the subsequent refresh fails.
    selected.value = { ...selected.value, resolution: result, correlation_status: "matched", process_instance_id: result.process_instance_id };
    notice.value = result.delivery_scheduled
      ? "Resolved. The original event is queued for delivery."
      : "Resolved and recorded only. The destination process is terminal; no delivery was scheduled.";
    pendingCommand = null;
    await refresh();
  } catch (cause) {
    error.value = message(cause);
  } finally {
    saving.value = false;
  }
}

function message(cause: unknown) {
  return cause instanceof Error ? cause.message : "Could not load or resolve the quarantined event.";
}
function label(value: string | null) { return value?.replaceAll("_", " ") ?? "No reason recorded"; }
function formatDate(value: string) { return new Date(value).toLocaleString(); }
</script>

<template>
  <main class="operator-shell quarantine-shell">
    <header class="topbar">
      <a class="brand" href="/"><span class="brand-mark">T</span><span><strong>Tiramisu</strong><small>Operator workspace</small></span></a>
      <nav class="topbar-actions" aria-label="Primary navigation">
        <a class="topbar-link" href="/">Dashboard</a><a class="topbar-link" href="/processes">Processes</a><a class="topbar-link active" href="/quarantine">Event quarantine</a>
      </nav>
    </header>
    <section class="dashboard-hero"><div><p class="eyebrow">Event recovery</p><h1>Event quarantine</h1><p>Inspect unmatched events, choose their destination, and replay the original event.</p></div></section>
    <form class="identity-bar" aria-label="Local operator identity" @submit.prevent="connect">
      <label><span>Tenant ID</span><input v-model="credentials.tenantId" data-testid="tenant-id" :disabled="saving" /></label>
      <label><span>Actor ID</span><input v-model="credentials.actorId" data-testid="actor-id" :disabled="saving" /></label>
      <button class="button" data-testid="connect" :disabled="saving || loading">Connect locally</button>
      <p>Local development identity headers.</p>
    </form>
    <div v-if="error" role="alert" class="alert alert-error">{{ error }}</div>
    <div v-if="notice" role="status" class="alert">{{ notice }}</div>
    <template v-if="activeCredentials">
      <div class="section-heading quarantine-toolbar">
        <label>Show <select v-model="state" data-testid="queue-state" :disabled="saving" @change="changePage()"><option value="unresolved">Unresolved events</option><option value="resolved">Resolution history</option></select></label>
        <button class="button" :disabled="saving || loading" @click="refresh">{{ loading ? "Loading…" : "Refresh" }}</button>
      </div>
      <div class="quarantine-grid">
        <section class="dashboard-panel" aria-label="Quarantine queue">
          <h2>{{ state === 'unresolved' ? 'Unresolved events' : 'Resolution history' }} <small v-if="page">({{ page.total }})</small></h2>
          <p v-if="page && !page.items.length">{{ state === 'unresolved' ? 'No unresolved events on this page.' : 'No resolutions on this page.' }}</p>
          <div class="dashboard-list">
            <button v-for="event in page?.items" :key="event.id" class="dashboard-row quarantine-event" :class="{ selected: selected?.id === event.id }" :disabled="saving" @click="inspect(event.id)">
              <span><strong>{{ event.event_type }}</strong><small>{{ event.source }} · {{ event.source_event_id }}</small><small>{{ label(event.resolution?.previous_reason ?? event.correlation_reason) }}</small><small>{{ formatDate(event.received_at) }}</small></span>
            </button>
          </div>
          <div v-if="page" class="quarantine-pagination">
            <button class="button" :disabled="saving || loading || offset === 0" @click="changePage(Math.max(0, offset - 25))">Previous</button>
            <span>{{ page.total ? offset + 1 : 0 }}–{{ Math.min(offset + page.items.length, page.total) }} of {{ page.total }}</span>
            <button class="button" :disabled="saving || loading || offset + 25 >= page.total" @click="changePage(offset + 25)">Next</button>
          </div>
        </section>
        <section class="dashboard-panel quarantine-detail" aria-label="Event inspection">
          <p v-if="detailLoading" role="status">Loading event…</p>
          <p v-else-if="!selected">Select an event to inspect its original content and correlation references.</p>
          <template v-if="selected">
            <h2>{{ selected.event_type.replaceAll('_', ' ').replaceAll('.', ' · ') }}</h2><p>{{ selected.source }} · {{ selected.source_event_id }}</p>
            <p><strong>Quarantine reason:</strong> {{ label(selected.resolution?.previous_reason ?? selected.correlation_reason) }}</p>
            <p>Occurred {{ formatDate(selected.event.occurred_at) }} · received {{ formatDate(selected.received_at) }}</p>
            <p v-if="selected.event.process_instance_id">Original explicit process: {{ selected.event.process_instance_id }}</p>
            <details><summary>Original event content · {{ selected.event.sensitivity }}</summary><pre>{{ JSON.stringify(selected.event, null, 2) }}</pre></details>
            <h3>Current reference matches</h3>
            <p v-if="!selected.references.length">This event has no external references. Resolution applies only to this event.</p>
            <ul class="quarantine-references">
              <li v-for="(item, index) in selected.references" :key="index">
                <strong>{{ item.reference.provider }} / {{ item.reference.resource_type }} / {{ item.reference.external_id }}</strong>
                <a v-if="item.process_instance_id" :href="`/processes?process=${item.process_instance_id}`">Assigned to {{ item.process_instance_id }}</a><span v-else>Unassigned</span>
              </li>
            </ul>
            <div v-if="selected.resolution" data-testid="resolution-audit" class="state-card">
              <h3>Resolution recorded</h3><p>{{ selected.resolution.reason }}</p>
              <p>By {{ selected.resolution.actor_id }} · {{ formatDate(selected.resolution.created_at) }}</p>
              <a :href="`/processes?process=${selected.resolution.process_instance_id}`">Open destination process</a>
              <p>{{ selected.resolution.delivery_scheduled ? 'Original event queued for delivery. Check delivery operations for any delivery failure.' : 'Recorded only: the destination was terminal. No delivery scheduled.' }}</p>
              <p>References selected for future routing: {{ selected.resolution.bound_references.length }}</p>
              <ul><li v-for="(reference, index) in selected.resolution.bound_references" :key="index">{{ reference.provider }} / {{ reference.resource_type }} / {{ reference.external_id }}</li></ul>
            </div>
            <form v-else-if="selected.can_resolve" data-testid="resolve-form" @submit.prevent="resolve">
              <h3>Resolve destination</h3>
              <p>Choose the correct process. Existing reference ownership cannot be changed here.</p>
              <label v-if="selected.candidates.length">Matching processes<select data-testid="candidate" :disabled="saving" :value="destination" @change="destination = ($event.target as HTMLSelectElement).value"><option value="">Select a process or enter its ID below</option><option v-for="candidate in selected.candidates" :key="candidate.id" :value="candidate.id">{{ candidate.process_type }} · {{ candidate.status }} · {{ candidate.id }}</option></select></label>
              <label>Destination process ID<input v-model="destination" data-testid="destination" :disabled="saving" required /></label>
              <p v-if="terminalTarget">This process is terminal. The event will be recorded without waking it.</p>
              <fieldset v-if="selected.references.length" :disabled="saving"><legend>Use these references for future events</legend>
                <p>Leave all unchecked to resolve only this event. References owned by another process remain unchanged.</p>
                <label v-for="(item, index) in selected.references" :key="index" class="quarantine-checkbox"><input v-model="boundIndices" type="checkbox" :value="index" :disabled="!!item.process_instance_id && item.process_instance_id !== destination.trim()" />{{ item.reference.provider }} / {{ item.reference.resource_type }} / {{ item.reference.external_id }}</label>
              </fieldset>
              <label>Reason<textarea v-model="reason" data-testid="resolution-reason" :disabled="saving" maxlength="10000" required placeholder="Explain why this event belongs to the selected process." /></label>
              <p>Terminal destinations retain the event without delivery. Other destinations receive the original event once through normal delivery.</p>
              <button class="button button-primary" data-testid="resolve" :disabled="!canSubmit">{{ saving ? 'Resolving…' : terminalTarget ? 'Resolve and record' : 'Resolve and replay' }}</button>
            </form>
            <p v-else data-testid="read-only">Your credential can inspect quarantine but cannot resolve events.</p>
          </template>
        </section>
      </div>
    </template>
  </main>
</template>
