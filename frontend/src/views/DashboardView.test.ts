import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DashboardView from "./DashboardView.vue";

describe("operator dashboard", () => {
  beforeEach(() => {
    const values = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    });
  });

  it("summarises the tenant's durable work and links to the process console", async () => {
    const process = {
      id: "11111111-1111-4111-8111-111111111111",
      process_type: "enquiry_to_booking",
      definition_version: "1",
      status: "review",
      state_version: 2,
      memory_summary: "Waiting for a human approval.",
      open_commitments: [],
      pending_reviews: 1,
      updated_at: "2026-09-01T10:00:00Z",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const payload = url.endsWith("/v1/processes")
          ? [process]
          : url.endsWith("/v1/reviews")
            ? [{ process_instance_id: process.id }]
            : [];
        return new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );

    const wrapper = mount(DashboardView);
    await wrapper.get('[data-testid="tenant-id"]').setValue("tenant-id");
    await wrapper.get('[data-testid="actor-id"]').setValue("actor-id");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(wrapper.text()).toContain("All journeys");
    expect(wrapper.text()).toContain("Needs attention");
    expect(wrapper.text()).toContain("enquiry to booking");
    expect(wrapper.get(`a[href="/processes?process=${process.id}"]`).text()).toContain(
      "enquiry to booking",
    );
  });
});
