import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import HomeView from "./HomeView.vue";

const processId = "11111111-1111-4111-8111-111111111111";
const threadId = "22222222-2222-4222-8222-222222222222";
const agentTurnId = "55555555-5555-4555-8555-555555555555";
const interventionId = "77777777-7777-4777-8777-777777777777";
const deadLetterId = "99999999-9999-4999-8999-999999999999";
const now = "2026-08-30T10:00:00Z";

const summary = {
  id: processId,
  process_type: "enquiry_to_booking",
  definition_version: "1",
  status: "review",
  state_version: 2,
  memory_summary: "The customer is waiting for a response.",
  open_commitments: ["Send the approved response"],
  pending_reviews: 1,
  updated_at: now,
};

const review = {
  thread_id: threadId,
  process_instance_id: processId,
  process_type: "enquiry_to_booking",
  action_request_id: "33333333-3333-4333-8333-333333333333",
  action_type: "send_message",
  revision: 1,
  parameters: { body: "Hello there" },
  rationale: "Reply to the enquiry.",
  payload_hash: "a".repeat(64),
  required_role: null,
  expires_at: null,
  created_at: now,
};

const detail = {
  ...summary,
  workflow_id: `process:${processId}`,
  authoritative_facts: { "customer.identifier": "customer-1" },
  customer_claims: { "customer.initial_request": "Tuesday please" },
  fact_provenance: {
    "authoritative:customer.identifier": {
      source_type: "event",
      source_id: "66666666-6666-4666-8666-666666666666",
    },
  },
  memory_summary_source_event_ids: [],
  current_wake_conditions: [{ type: "human", interaction: "approval" }],
  created_at: now,
  interventions: [
    {
      id: interventionId,
      agent_turn_id: "88888888-8888-4888-8888-888888888888",
      kind: "turn_failure",
      status: "open",
      error_type: "DecisionRejected",
      error: "The model produced no valid progress path.",
      source_event_ids: [],
      source_review_command_ids: [],
      source_action_attempt_ids: [],
      source_timer_ids: [],
      resolved_by_command_id: null,
      resolved_at: null,
      created_at: now,
    },
  ],
  timeline: [
    {
      id: "22222222-2222-4222-8222-222222222223",
      kind: "event",
      occurred_at: "2026-08-30T08:00:00Z",
      title: "mail.received",
      status: "processed",
      detail: {},
    },
    {
      id: "33333333-3333-4333-8333-333333333334",
      kind: "decision",
      occurred_at: "2026-08-30T09:00:00Z",
      title: "Agent decision · version 1",
      status: "active",
      agent_turn_id: agentTurnId,
      detail: { memory_summary: "The customer asked to book on Tuesday." },
    },
    {
      id: "33333333-3333-4333-8333-333333333335",
      kind: "action",
      occurred_at: "2026-08-30T09:00:00Z",
      title: "find_available_slots",
      status: "succeeded",
      agent_turn_id: agentTurnId,
      action_request_id: "33333333-3333-4333-8333-333333333333",
      detail: {},
    },
    {
      id: "44444444-4444-4444-8444-444444444444",
      kind: "decision",
      occurred_at: now,
      title: "Agent decision · version 2",
      status: "review",
      agent_turn_id: "55555555-5555-4555-8555-555555555556",
      detail: {
        memory_summary: "The customer is waiting for a response.",
        wake_conditions: [{ type: "human", interaction: "approval" }],
      },
    },
  ],
};

const deadLetter = {
  id: deadLetterId,
  process_instance_id: processId,
  message_type: "temporal.process_event",
  destination: `process:${processId}`,
  attempt_count: 8,
  last_error: "Temporal unavailable",
  dead_lettered_at: now,
  created_at: now,
};

const recoveryCommand = {
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  outbox_message_id: deadLetterId,
  actor_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  command_type: "requeue",
  reason: "Temporal was restored.",
  previous_attempt_count: 8,
  previous_error: "Temporal unavailable",
  previous_dead_lettered_at: now,
  created_at: now,
};

describe("operator console", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    const values = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
      clear: () => values.clear(),
      key: (index: number) => [...values.keys()][index] ?? null,
      get length() {
        return values.size;
      },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
  });

  it("loads durable state and submits an exact-payload approval", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const payload = url.endsWith("/v1/processes")
        ? [summary]
        : url.endsWith("/v1/reviews")
          ? [review]
          : url.endsWith("/v1/outbox/dead-letters")
            ? [deadLetter]
            : url.endsWith("/v1/outbox/recovery-commands")
              ? [recoveryCommand]
              : url.endsWith(`/v1/outbox/dead-letters/${deadLetterId}/requeue`)
                ? { command_id: recoveryCommand.id, outbox_message_id: deadLetterId, status: "pending" }
          : url.includes("/commands")
            ? {
                command_id: "55555555-5555-4555-8555-555555555555",
                thread_status: "approved",
                approval_status: "approved",
                action_status: "approved",
              }
            : detail;
      return new Response(JSON.stringify(payload), {
        status: init?.method === "POST" ? 202 : 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(HomeView);

    await wrapper.get('[data-testid="tenant-id"]').setValue("tenant-id");
    await wrapper.get('[data-testid="actor-id"]').setValue("actor-id");
    await wrapper.get('[data-testid="connect"]').trigger("click");
    await flushPromises();

    expect(wrapper.get('[data-testid="process-detail"]').text()).toContain(
      "enquiry to booking",
    );
    expect(wrapper.get('[data-testid="wake-panel"]').text()).toContain("Human · approval");
    expect(wrapper.get('[data-testid="review-queue"]').text()).toContain("Hello there");
    expect(wrapper.get('[data-testid="timeline"]').text()).toContain("Agent decision");
    const turnGroups = wrapper.get('[data-testid="timeline"]').findAll(".turn-group");
    expect(turnGroups).toHaveLength(2);
    expect(turnGroups[0].attributes("open")).toBeUndefined();
    expect(turnGroups[0].text()).toContain("find_available_slots");
    expect(wrapper.get('[data-testid="timeline"]').text()).toContain(
      "Agent decision → find available slots",
    );
    expect(wrapper.get('[data-testid="timeline"]').text()).toContain("Wakes on");
    expect(wrapper.get('[data-testid="timeline"]').text()).toContain("Human · approval");
    expect(wrapper.get('[data-testid="timeline"]').findAll(".timeline-row")).toHaveLength(1);
    const memoryHistory = wrapper.get('[data-testid="memory-history"]');
    expect(memoryHistory.text()).toContain("The customer asked to book on Tuesday.");
    expect(memoryHistory.text()).not.toContain("The customer is waiting for a response.");
    expect(wrapper.get('[data-testid="intervention-controls"]').text()).toContain(
      "DecisionRejected",
    );
    expect(wrapper.get('[data-testid="delivery-operations"]').text()).toContain(
      "Temporal unavailable",
    );
    expect(wrapper.get('[data-testid="delivery-operations"]').text()).toContain(
      "Temporal was restored.",
    );
    expect(wrapper.text()).toContain("Tuesday please");
    expect(wrapper.text()).toContain("event · 66666666…6666");
    const processListCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith("/v1/processes"),
    );
    expect(processListCall?.[1]?.headers).toMatchObject({
      "X-Tiramisu-Tenant-ID": "tenant-id",
      "X-Tiramisu-Actor-ID": "actor-id",
    });

    const approve = wrapper
      .findAll("button")
      .find((button) => button.text() === "Approve exact proposal");
    expect(approve).toBeDefined();
    await approve?.trigger("click");
    await flushPromises();

    const postCall = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(postCall).toBeDefined();
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({
      command_type: "approve",
      message: null,
      expected_payload_hash: "a".repeat(64),
    });
    expect(wrapper.text()).toContain("Exact proposal approved");

    await wrapper
      .get('[data-testid="intervention-controls"] textarea')
      .setValue("The prompt and process policy have been corrected.");
    const retry = wrapper
      .findAll("button")
      .find((button) => button.text() === "Retry failed turn");
    await retry?.trigger("click");
    await flushPromises();

    const controlCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith(`/v1/processes/${processId}/controls`),
    );
    expect(controlCall).toBeDefined();
    expect(JSON.parse(String(controlCall?.[1]?.body))).toEqual({
      command_type: "retry",
      reason: "The prompt and process policy have been corrected.",
      intervention_id: interventionId,
    });
    expect(wrapper.text()).toContain("Failed turn queued for retry");

    const deliveryOperations = wrapper.get('[data-testid="delivery-operations"]');
    await deliveryOperations
      .get('textarea[placeholder^="Provider restored"]')
      .setValue("Temporal cluster connectivity has been restored.");
    await deliveryOperations.get("button.button-primary").trigger("click");
    await flushPromises();

    const requeueCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith(`/v1/outbox/dead-letters/${deadLetterId}/requeue`),
    );
    expect(requeueCall).toBeDefined();
    expect(JSON.parse(String(requeueCall?.[1]?.body))).toEqual({
      reason: "Temporal cluster connectivity has been restored.",
    });
    expect(wrapper.text()).toContain("Delivery requeued");
    wrapper.unmount();
  });

  it("keeps journey inspection available without outbox permission", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/v1/outbox/")) {
          return new Response(JSON.stringify({ detail: "missing scope: outbox:read" }), {
            status: 403,
            headers: { "Content-Type": "application/json" },
          });
        }
        const payload = url.endsWith("/v1/processes")
          ? [summary]
          : url.endsWith("/v1/reviews")
            ? [review]
            : detail;
        return new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    const wrapper = mount(HomeView);

    await wrapper.get('[data-testid="tenant-id"]').setValue("tenant-id");
    await wrapper.get('[data-testid="actor-id"]').setValue("actor-id");
    await wrapper.get('[data-testid="connect"]').trigger("click");
    await flushPromises();

    expect(wrapper.get('[data-testid="process-detail"]').text()).toContain(
      "enquiry to booking",
    );
    expect(wrapper.get('[data-testid="delivery-operations"]').text()).toContain(
      "Delivery operations unavailable",
    );
    expect(wrapper.text()).toContain("missing scope: outbox:read");
    expect(wrapper.find('[role="alert"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it("describes Wake as reevaluation and submits guidance without claiming a fact change", async () => {
    const waitingSummary = { ...summary, status: "waiting", pending_reviews: 0 };
    const waitingDetail = {
      ...detail,
      ...waitingSummary,
      interventions: [],
      current_wake_conditions: [{ type: "event", event_type: "payment.completed" }],
      authoritative_facts: { ...detail.authoritative_facts, "payment.status": "pending" },
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const payload = url.endsWith("/v1/processes")
        ? [waitingSummary]
        : url.endsWith("/v1/reviews") ||
            url.endsWith("/v1/outbox/dead-letters") ||
            url.endsWith("/v1/outbox/recovery-commands")
          ? []
          : url.endsWith(`/v1/processes/${processId}/controls`)
            ? { command_id: "55555555-5555-4555-8555-555555555555", command_type: "wake" }
            : waitingDetail;
      return new Response(JSON.stringify(payload), {
        status: init?.method === "POST" ? 202 : 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(HomeView);

    await wrapper.get('[data-testid="tenant-id"]').setValue("tenant-id");
    await wrapper.get('[data-testid="actor-id"]').setValue("actor-id");
    await wrapper.get('[data-testid="connect"]').trigger("click");
    await flushPromises();

    const controls = wrapper.get('[data-testid="intervention-controls"]');
    expect(controls.text()).toContain("does not change authoritative business facts");
    const guidance = "I received cash; please reconsider what should happen next.";
    await controls
      .get('textarea[placeholder^="Required reason; Wake guidance"]')
      .setValue(guidance);
    await controls
      .findAll("button")
      .find((button) => button.text() === "Wake and re-evaluate")
      ?.trigger("click");
    await flushPromises();

    const controlCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        String(input).endsWith(`/v1/processes/${processId}/controls`) && init?.method === "POST",
    );
    expect(controlCall).toBeDefined();
    expect(JSON.parse(String(controlCall?.[1]?.body))).toEqual({
      command_type: "wake",
      reason: guidance,
      intervention_id: null,
    });
    expect(wrapper.text()).toContain(
      "Reevaluation queued; authoritative facts are unchanged.",
    );
    expect(wrapper.text()).toContain("payment.status");
    expect(wrapper.text()).toContain("pending");
    wrapper.unmount();
  });

  it("polls changed process state without hiding content and pauses in hidden tabs", async () => {
    vi.useFakeTimers();
    let processListCalls = 0;
    let releasePoll: ((response: Response) => void) | undefined;
    const updatedSummary = {
      ...summary,
      status: "waiting",
      state_version: 3,
      updated_at: "2026-08-30T10:01:00Z",
    };
    const updatedDetail = {
      ...detail,
      ...updatedSummary,
      current_wake_conditions: [{ type: "event", event_type: "customer.email_received" }],
    };
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/v1/processes")) {
        processListCalls += 1;
        if (processListCalls === 2) {
          return new Promise<Response>((resolve) => {
            releasePoll = resolve;
          });
        }
        return Promise.resolve(
          new Response(JSON.stringify(processListCalls > 2 ? [updatedSummary] : [summary])),
        );
      }
      const payload = url.endsWith("/v1/reviews")
        ? [review]
        : url.endsWith("/v1/outbox/dead-letters") ||
            url.endsWith("/v1/outbox/recovery-commands")
          ? []
          : processListCalls > 1
            ? updatedDetail
            : detail;
      return Promise.resolve(new Response(JSON.stringify(payload)));
    });
    vi.stubGlobal("fetch", fetchMock);
    const wrapper = mount(HomeView);

    await wrapper.get('[data-testid="tenant-id"]').setValue("tenant-id");
    await wrapper.get('[data-testid="actor-id"]').setValue("actor-id");
    await wrapper.get('[data-testid="connect"]').trigger("click");
    await flushPromises();

    expect(wrapper.get('[data-testid="process-detail"]').text()).toContain("state 2");
    vi.advanceTimersByTime(2_000);
    await flushPromises();
    expect(wrapper.get('[data-testid="sync-status"]').text()).toContain("Live");
    expect(wrapper.get('[data-testid="connect"]').text()).toContain("Refresh now");
    expect(wrapper.get('[data-testid="connect"]').attributes("disabled")).toBeUndefined();
    expect(wrapper.get('[data-testid="process-detail"]').text()).toContain("state 2");

    releasePoll?.(new Response(JSON.stringify([updatedSummary])));
    await flushPromises();
    expect(wrapper.get('[data-testid="sync-status"]').text()).toContain("Live");
    expect(wrapper.get('[data-testid="process-detail"]').text()).toContain("state 3");
    expect(wrapper.get('[data-testid="wake-panel"]').text()).toContain(
      "Event · customer.email_received",
    );

    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    document.dispatchEvent(new Event("visibilitychange"));
    const callsBeforeHiddenWait = processListCalls;
    vi.advanceTimersByTime(5_000);
    await flushPromises();
    expect(processListCalls).toBe(callsBeforeHiddenWait);

    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    document.dispatchEvent(new Event("visibilitychange"));
    await flushPromises();
    expect(processListCalls).toBe(callsBeforeHiddenWait + 1);
    wrapper.unmount();
  });
});
