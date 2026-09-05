import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { EventResolution, QuarantineDetail } from "@/api";
import QuarantineView from "./QuarantineView.vue";

const eventId = "11111111-1111-4111-8111-111111111111";
const processId = "22222222-2222-4222-8222-222222222222";
const otherId = "33333333-3333-4333-8333-333333333333";
const now = "2026-09-05T02:00:00Z";
const detail: QuarantineDetail = {
  id: eventId, event_type: "customer.email_received", source: "mail.test", source_event_id: "message-1",
  correlation_status: "pending", correlation_reason: "ambiguous_external_references",
  process_instance_id: null, received_at: now, resolution: null, can_resolve: true,
  event: { event_id: eventId, process_instance_id: null, occurred_at: now, sensitivity: "confidential",
    facts: [], payload: { body: "Original customer message" }, external_references: [] },
  references: [
    { reference: { provider: "mail", resource_type: "thread", external_id: "unassigned-thread" }, process_instance_id: null },
    { reference: { provider: "mail", resource_type: "thread", external_id: "other-thread" }, process_instance_id: otherId },
  ],
  candidates: [
    { id: processId, process_type: "enquiry_to_booking", status: "waiting", deployment_id: "test" },
    { id: otherId, process_type: "enquiry_to_booking", status: "active", deployment_id: "test" },
  ],
};
const resolution: EventResolution = {
  id: "44444444-4444-4444-8444-444444444444", event_id: eventId, process_instance_id: processId,
  actor_id: otherId, reason: "Verified case", previous_status: "pending",
  previous_reason: "ambiguous_external_references", bound_references: [detail.references[0]!.reference],
  delivery_scheduled: true, created_at: now,
};

function mockApi(options: { readOnly?: boolean; lostResponse?: boolean; terminal?: boolean; deny?: boolean } = {}) {
  let resolved = false;
  let posts = 0;
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (options.deny) return new Response(JSON.stringify({ detail: "credential lacks required scope: quarantine:read" }), { status: 403 });
    if (init?.method === "POST") {
      posts++;
      if (options.lostResponse && posts === 1) throw new Error("Network response lost");
      resolved = true;
      return new Response(JSON.stringify({ ...resolution, delivery_scheduled: !options.terminal }), { status: 202 });
    }
    if (url.includes("/v1/quarantine?")) {
      const history = url.includes("state=resolved");
      return new Response(JSON.stringify({
        items: resolved ? (history ? [{ ...detail, resolution }] : []) : [detail],
        total: resolved ? (history ? 1 : 0) : 1, offset: 0, limit: 25, can_resolve: !options.readOnly,
      }));
    }
    return new Response(JSON.stringify({ ...detail, can_resolve: !options.readOnly,
      candidates: detail.candidates.map((candidate) => ({ ...candidate, status: options.terminal ? "completed" : candidate.status })),
      resolution: resolved ? { ...resolution, delivery_scheduled: !options.terminal } : null,
    }));
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

async function openEvent() {
  const wrapper = mount(QuarantineView);
  await wrapper.get('[data-testid="tenant-id"]').setValue("test-tenant");
  await wrapper.get('[data-testid="actor-id"]').setValue("test-actor");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  await wrapper.get(".quarantine-event").trigger("click");
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: vi.fn() });
});

describe("event quarantine", () => {
  it("inspects original content, protects conflicting references, and retains resolution history", async () => {
    const fetch = mockApi();
    const wrapper = await openEvent();
    expect(wrapper.text()).toContain("ambiguous external references");
    expect(wrapper.get("pre").text()).toContain("Original customer message");
    expect(wrapper.get('[data-testid="resolve"]').attributes("disabled")).toBeDefined();
    await wrapper.get('[data-testid="candidate"]').setValue(processId);
    const boxes = wrapper.findAll('input[type="checkbox"]');
    expect(boxes[1]!.attributes("disabled")).toBeDefined();
    await boxes[0]!.setValue(true);
    await wrapper.get('[data-testid="resolution-reason"]').setValue("Verified case");
    await wrapper.get('[data-testid="resolve-form"]').trigger("submit");
    await flushPromises();
    const post = fetch.mock.calls.find(([, init]) => init?.method === "POST");
    const body = JSON.parse(post![1]!.body as string);
    expect(body).toMatchObject({ process_instance_id: processId, reason: "Verified case", bind_references: [detail.references[0]!.reference] });
    expect(body.command_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(wrapper.text()).toContain("No unresolved events");
    expect(wrapper.get('[data-testid="resolution-audit"]').text()).toContain("Verified case");
    expect(wrapper.get('[data-testid="resolution-audit"]').text()).toContain(otherId);
    expect(wrapper.get('[data-testid="resolution-audit"] a').attributes("href")).toBe(`/processes?process=${processId}`);
    await wrapper.get('[data-testid="queue-state"]').setValue("resolved");
    await flushPromises();
    await wrapper.get(".quarantine-event").trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-testid="resolve-form"]').exists()).toBe(false);
    expect(wrapper.get('[data-testid="resolution-audit"]').text()).toContain("Verified case");
    wrapper.unmount();
  });

  it("retries a lost response with the same command identity", async () => {
    const fetch = mockApi({ lostResponse: true });
    const wrapper = await openEvent();
    await wrapper.get('[data-testid="destination"]').setValue(processId);
    await wrapper.get('[data-testid="resolution-reason"]').setValue("Verified case");
    await wrapper.get('[data-testid="resolve-form"]').trigger("submit");
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toContain("Network response lost");
    await wrapper.get('[data-testid="resolve-form"]').trigger("submit");
    await flushPromises();
    const posts = fetch.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts).toHaveLength(2);
    expect(posts[0]![1]!.body).toBe(posts[1]![1]!.body);
    expect(wrapper.find('[data-testid="resolution-audit"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("allows read-only inspection without a resolution form", async () => {
    const fetch = mockApi({ readOnly: true });
    const wrapper = await openEvent();
    expect(wrapper.get('[data-testid="read-only"]').text()).toContain("cannot resolve events");
    expect(wrapper.find('[data-testid="resolve-form"]').exists()).toBe(false);
    expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
    wrapper.unmount();
  });

  it("explains record-only handling for a terminal destination", async () => {
    mockApi({ terminal: true });
    const wrapper = await openEvent();
    await wrapper.get('[data-testid="candidate"]').setValue(processId);
    expect(wrapper.text()).toContain("recorded without waking it");
    await wrapper.get('[data-testid="resolution-reason"]').setValue("Verified closed case");
    expect(wrapper.get('[data-testid="resolve"]').text()).toBe("Resolve and record");
    await wrapper.get('[data-testid="resolve-form"]').trigger("submit");
    await flushPromises();
    expect(wrapper.get('[role="status"]').text()).toContain("no delivery was scheduled");
    wrapper.unmount();
  });

  it("reports denied queue access without claiming the queue is empty", async () => {
    mockApi({ deny: true });
    const wrapper = mount(QuarantineView);
    await wrapper.get('[data-testid="tenant-id"]').setValue("tenant");
    await wrapper.get('[data-testid="actor-id"]').setValue("actor");
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toContain("quarantine:read");
    expect(wrapper.text()).not.toContain("No unresolved events");
    wrapper.unmount();
  });
});
