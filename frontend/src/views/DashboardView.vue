<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";

import {
  operatorApi,
  type DeadLetterSummary,
  type OperatorCredentials,
  type PendingReview,
  type ProcessSummary,
} from "@/api";

const credentials = reactive<OperatorCredentials>({ tenantId: "", actorId: "" });
const processes = ref<ProcessSummary[]>([]);
const reviews = ref<PendingReview[]>([]);
const deadLetters = ref<DeadLetterSummary[]>([]);
const connected = ref(false);
const loading = ref(false);
const error = ref("");
const lastUpdatedAt = ref<string | null>(null);

const POLL_INTERVAL_MS = 10_000;
let pollTimer: ReturnType<typeof window.setTimeout> | undefined;

const activeCount = computed(
  () => processes.value.filter((process) => ["active", "waiting"].includes(process.status)).length,
);
const needsAttention = computed(
  () =>
    processes.value
      .filter((process) => process.pending_reviews || ["review", "failed"].includes(process.status))
      .slice(0, 4),
);
const recentProcesses = computed(() =>
  [...processes.value]
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at))
    .slice(0, 6),
);

onMounted(() => {
  document.addEventListener("visibilitychange", handleVisibilityChange);
  credentials.tenantId = localStorage.getItem("tiramisu.tenantId") ?? "";
  credentials.actorId = localStorage.getItem("tiramisu.actorId") ?? "";
  if (credentials.tenantId && credentials.actorId) {
    connected.value = true;
    void refresh();
  }
});

onBeforeUnmount(() => {
  if (pollTimer !== undefined) window.clearTimeout(pollTimer);
  document.removeEventListener("visibilitychange", handleVisibilityChange);
});

async function connect() {
  error.value = "";
  if (!credentials.tenantId.trim() || !credentials.actorId.trim()) {
    error.value = "Enter both local development identity IDs.";
    return;
  }
  localStorage.setItem("tiramisu.tenantId", credentials.tenantId.trim());
  localStorage.setItem("tiramisu.actorId", credentials.actorId.trim());
  connected.value = true;
  await refresh();
}

async function refresh() {
  if (!connected.value || loading.value) return;
  loading.value = true;
  error.value = "";
  try {
    const operations = operatorApi
      .listDeadLetters(credentials)
      .catch(() => [] as DeadLetterSummary[]);
    const [nextProcesses, nextReviews, nextDeadLetters] = await Promise.all([
      operatorApi.listProcesses(credentials),
      operatorApi.listReviews(credentials),
      operations,
    ]);
    processes.value = nextProcesses;
    reviews.value = nextReviews;
    deadLetters.value = nextDeadLetters;
    lastUpdatedAt.value = new Date().toISOString();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Could not load the dashboard.";
  } finally {
    loading.value = false;
    schedulePoll();
  }
}

function schedulePoll() {
  if (pollTimer !== undefined) window.clearTimeout(pollTimer);
  if (!connected.value || document.hidden) return;
  pollTimer = window.setTimeout(() => void refresh(), POLL_INTERVAL_MS);
}

function handleVisibilityChange() {
  if (document.hidden) {
    if (pollTimer !== undefined) window.clearTimeout(pollTimer);
    pollTimer = undefined;
    return;
  }
  void refresh();
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}

function shortId(value: string) {
  return `${value.slice(0, 8)}…${value.slice(-4)}`;
}
</script>

<template>
  <main class="operator-shell dashboard-shell">
    <header class="topbar">
      <a class="brand" href="/" aria-label="Tiramisu dashboard">
        <span class="brand-mark">T</span>
        <span><strong>Tiramisu</strong><small>Operator workspace</small></span>
      </a>
      <nav class="topbar-actions" aria-label="Primary navigation">
        <a class="topbar-link active" href="/">Dashboard</a>
        <a class="topbar-link" href="/processes">Processes</a>
        <a class="topbar-link" href="/quarantine">Event quarantine</a>
        <a href="http://127.0.0.1:8233" target="_blank">Temporal ↗</a>
        <a href="http://127.0.0.1:8000/docs" target="_blank">API ↗</a>
      </nav>
    </header>

    <section class="dashboard-hero">
      <div>
        <p class="eyebrow">Operator dashboard</p>
        <h1>Process overview</h1>
        <p>Journeys, reviews, and delivery recovery.</p>
      </div>
      <a class="button button-primary" href="/processes">Open process console <span>→</span></a>
    </section>

    <form class="identity-bar dashboard-identity" aria-label="Local operator identity" @submit.prevent="connect">
      <label><span>Tenant ID</span><input v-model="credentials.tenantId" data-testid="tenant-id" placeholder="UUID" /></label>
      <label><span>Actor ID</span><input v-model="credentials.actorId" data-testid="actor-id" placeholder="UUID" /></label>
      <button class="button" data-testid="connect" :disabled="loading">{{ loading ? "Updating…" : connected ? "Refresh now" : "Connect locally" }}</button>
      <p v-if="connected && lastUpdatedAt">Last updated {{ formatDate(lastUpdatedAt) }} · updates every 10 seconds</p>
      <p v-else>Development headers only. Production identity is deliberately not implied.</p>
    </form>

    <div v-if="error" class="alert alert-error" role="alert">{{ error }}</div>

    <template v-if="connected">
      <section class="metric-grid" aria-label="Journey overview">
        <article class="metric-card metric-card-primary">
          <span>All journeys</span><strong>{{ processes.length }}</strong><small>Process records</small>
        </article>
        <article class="metric-card">
          <span>In motion</span><strong>{{ activeCount }}</strong><small>Active or waiting</small>
        </article>
        <article class="metric-card metric-card-attention">
          <span>Needs review</span><strong>{{ reviews.length }}</strong><small>Awaiting review</small>
        </article>
        <article class="metric-card" :class="{ 'metric-card-danger': deadLetters.length }">
          <span>Delivery recovery</span><strong>{{ deadLetters.length }}</strong><small>Dead-lettered messages</small>
        </article>
      </section>

      <section class="dashboard-grid">
        <section class="dashboard-panel attention-panel">
          <div class="section-heading">
            <div><p class="eyebrow">Priority queue</p><h2>Needs attention</h2></div>
            <a href="/processes">View all</a>
          </div>
          <div v-if="needsAttention.length" class="dashboard-list">
            <a
              v-for="process in needsAttention"
              :key="process.id"
              class="dashboard-row"
              :href="`/processes?process=${process.id}`"
            >
              <span class="dashboard-row-title"><strong>{{ process.process_type.replaceAll("_", " ") }}</strong><small>{{ shortId(process.id) }}</small></span>
              <span class="dashboard-row-meta"><i :class="`status-${process.status}`">{{ process.status }}</i><b v-if="process.pending_reviews">{{ process.pending_reviews }} review</b></span>
            </a>
          </div>
          <div v-else class="dashboard-empty"><strong>No action needed.</strong><span>Reviews and failed journeys appear here.</span></div>
        </section>

        <section class="dashboard-panel recovery-panel">
          <p class="eyebrow">Delivery health</p>
          <h2>{{ deadLetters.length ? "Recovery needed" : "Delivery clear" }}</h2>
          <p>{{ deadLetters.length ? "Inspect each delivery and add a reason before requeueing." : "No dead-lettered deliveries." }}</p>
          <a class="text-button" href="/processes">Open delivery operations →</a>
        </section>

        <section class="dashboard-panel recent-panel">
          <div class="section-heading">
            <div><p class="eyebrow">Latest changes</p><h2>Recent journeys</h2></div>
            <a href="/processes">Browse processes</a>
          </div>
          <div v-if="recentProcesses.length" class="dashboard-list">
            <a
              v-for="process in recentProcesses"
              :key="process.id"
              class="dashboard-row"
              :href="`/processes?process=${process.id}`"
            >
              <span class="dashboard-row-title"><strong>{{ process.process_type.replaceAll("_", " ") }}</strong><small>{{ process.memory_summary ?? "No durable summary yet." }}</small></span>
              <span class="dashboard-row-meta"><i :class="`status-${process.status}`">{{ process.status }}</i><time>{{ formatDate(process.updated_at) }}</time></span>
            </a>
          </div>
          <div v-else class="dashboard-empty"><strong>No journeys yet.</strong><span>Ingest a fictional enquiry and it will appear here.</span></div>
        </section>
      </section>
    </template>
  </main>
</template>
