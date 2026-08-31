import { expect, test } from "@playwright/test";

const tenantId = "00000000-0000-0000-0000-000000000001";
const actorId = "00000000-0000-0000-0000-000000000002";

test("ingests and displays a real process through the local stack", async ({ page }) => {
  const sourceEventId = `playwright-enquiry-${Date.now()}`;
  const ingestion = await page.request.post("/api/v1/events", {
    headers: {
      "X-Tiramisu-Tenant-ID": tenantId,
    },
    data: {
      event_type: "enquiry.created",
      source: "playwright.smoke",
      source_event_id: sourceEventId,
      occurred_at: new Date().toISOString(),
      external_references: [
        {
          provider: "playwright.smoke",
          resource_type: "enquiry",
          external_id: sourceEventId,
        },
      ],
      facts: [
        {
          key: "customer.email",
          kind: "authoritative",
          value: "playwright@example.test",
        },
      ],
    },
  });
  expect(ingestion.status()).toBe(202);

  await page.goto("/");
  await page.getByTestId("tenant-id").fill(tenantId);
  await page.getByTestId("actor-id").fill(actorId);
  await page.getByTestId("connect").click();

  await expect(page.getByText("Local identity")).toBeVisible();
  await expect(page.getByRole("heading", { name: /processes$/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh" })).toBeVisible();
  await expect(page.getByTestId("process-list")).toContainText("enquiry to booking");
  await expect(page.getByTestId("process-detail")).toContainText("enquiry to booking");
  await expect(page.getByTestId("timeline")).toContainText("enquiry.created");
  await expect(page.getByRole("alert")).toHaveCount(0);

  const controls = page.getByTestId("intervention-controls");
  await controls.getByPlaceholder("Required audit reason…").fill("Operator checking this journey");
  await controls.getByRole("button", { name: "Pause and take over" }).click();
  await expect(page.getByRole("status")).toContainText("paused for operator takeover");
  await expect(page.getByTestId("process-detail").locator(".large-status")).toHaveText("paused");

  await controls.getByPlaceholder("Required audit reason…").fill("Operator check complete");
  await controls.getByRole("button", { name: "Resume agent" }).click();
  await expect(page.getByRole("status")).toContainText("resumed and queued to wake");
  await expect(page.getByTestId("process-detail").locator(".large-status")).toHaveText("active");
});
