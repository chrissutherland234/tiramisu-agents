import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import HomeView from "./HomeView.vue";

const processId = "11111111-1111-4111-8111-111111111111";
const threadId = "22222222-2222-4222-8222-222222222222";
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
      id: "44444444-4444-4444-8444-444444444444",
      kind: "decision",
      occurred_at: now,
      title: "Agent decision · version 2",
      status: "review",
      detail: { wake_conditions: [{ type: "human", interaction: "approval" }] },
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
  });
});
