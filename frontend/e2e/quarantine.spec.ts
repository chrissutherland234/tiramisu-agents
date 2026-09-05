import { expect, test } from "@playwright/test";

const tenantId = "00000000-0000-0000-0000-000000000001";
const actorId = "00000000-0000-0000-0000-000000000002";

test("resolves a quarantined event and routes future events using its audited reference", async ({ page }) => {
  const suffix = `quarantine-${Date.now()}`;
  const headers = { "X-Tiramisu-Tenant-ID": tenantId, "X-Tiramisu-Actor-ID": actorId };
  const thread = { provider: "playwright", resource_type: "thread", external_id: suffix };
  const event = {
    event_type: "customer.email_received", source: "playwright.quarantine", source_event_id: suffix,
    occurred_at: new Date().toISOString(), external_references: [thread],
    payload: { body: "Please attach this reply to my enquiry." },
  };
  const quarantined = await page.request.post("/api/v1/events", { headers, data: event });
  expect(quarantined.status()).toBe(202);
  const receipt = await quarantined.json();
  expect(receipt.correlation_status).toBe("pending");
  expect(receipt.delivery_scheduled).toBe(false);
  const enquiry = await page.request.post("/api/v1/events", { headers, data: {
    event_type: "enquiry.created", source: "playwright.quarantine", source_event_id: `${suffix}-enquiry`,
    occurred_at: new Date().toISOString(), external_references: [
      { provider: "playwright", resource_type: "enquiry", external_id: suffix },
    ],
  } });
  expect(enquiry.status()).toBe(202);
  const processId = (await enquiry.json()).process_instance_id;

  await page.goto("/quarantine");
  await page.getByTestId("tenant-id").fill(tenantId);
  await page.getByTestId("actor-id").fill(actorId);
  await page.getByTestId("connect").click();
  await page.getByRole("button", { name: new RegExp(suffix) }).click();
  await expect(page.getByRole("region", { name: "Event inspection" })).toContainText("no process match");
  await page.getByTestId("destination").fill(processId);
  await page.getByRole("checkbox", { name: new RegExp(suffix) }).check();
  await page.getByTestId("resolution-reason").fill("Verified this thread belongs to the enquiry.");
  await page.getByTestId("resolve").click();
  await expect(page.getByTestId("resolution-audit")).toContainText("Verified this thread belongs to the enquiry.");
  await expect(page.getByRole("status")).toContainText("queued for delivery");
  await page.reload();
  await page.getByTestId("queue-state").selectOption("resolved");
  await page.getByRole("button", { name: new RegExp(suffix) }).click();
  await expect(page.getByTestId("resolution-audit")).toContainText(actorId);
  await expect(page.getByTestId("resolve-form")).toHaveCount(0);
  await page.screenshot({ path: "/tmp/tiramisu-quarantine-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId("resolution-audit")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "/tmp/tiramisu-quarantine-mobile.png", fullPage: true });

  const duplicate = await page.request.post("/api/v1/events", { headers, data: event });
  expect((await duplicate.json()).event_id).toBe(receipt.event_id);
  const later = await page.request.post("/api/v1/events", { headers, data: { ...event, source_event_id: `${suffix}-later` } });
  const laterReceipt = await later.json();
  expect(laterReceipt.correlation_status).toBe("matched");
  expect(laterReceipt.process_instance_id).toBe(processId);
  const original = await page.request.get(`/api/v1/quarantine/${receipt.event_id}`, { headers });
  expect((await original.json()).event.payload).toEqual(event.payload);
});
