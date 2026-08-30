import { expect, test } from "@playwright/test";

const tenantId = "00000000-0000-0000-0000-000000000001";
const actorId = "00000000-0000-0000-0000-000000000002";

test("connects the operator console to the live local API", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("tenant-id").fill(tenantId);
  await page.getByTestId("actor-id").fill(actorId);
  await page.getByTestId("connect").click();

  await expect(page.getByText("Local identity")).toBeVisible();
  await expect(page.getByRole("heading", { name: /processes$/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh" })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});
