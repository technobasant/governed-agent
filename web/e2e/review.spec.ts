import { expect, test } from "@playwright/test";

test("a proposed refund is executed only after a reviewer approves it", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Question").fill("Refund order 1 in full, it arrived damaged.");
  await page.getByRole("button", { name: "Ask", exact: true }).click();

  await expect(page).toHaveURL(/\/runs\/run_[0-9a-f]+$/);
  const runUrl = new URL(page.url()).pathname;
  await expect(page.locator(".meta").getByText("Held for review")).toBeVisible();

  // The number in the finding is linked to the cell it came from.
  const check = page.locator(".checks li.pass", { hasText: "610.48" }).first();
  await check.getByRole("link", { name: "show source" }).click();
  await expect(page.locator("td:target")).toHaveText("610.48");

  // Proposed, not executed.
  const proposed = page
    .getByRole("region", { name: "Proposed actions" })
    .getByRole("article")
    .filter({ hasText: "issue_refund" });
  await expect(proposed.locator(".badge")).toHaveText("pending");

  // Decide from the review queue.
  await page.getByRole("link", { name: "Review queue" }).click();
  const card = page.locator("article", { has: page.locator(`a[href="${runUrl}"]`) }).filter({
    hasText: "issue_refund",
  });
  await card.getByLabel("Reviewer", { exact: true }).fill("dana");
  await card.getByLabel("Reason", { exact: true }).fill("Photo from the customer confirms the damage");
  await card.getByRole("button", { name: "Approve and execute" }).click();
  await expect(card).toHaveCount(0);

  // The outbox worker executes it, once.
  await page.goto(runUrl);
  await expect(proposed.locator(".badge")).toHaveText("executed", { timeout: 15_000 });
  await expect(proposed).toContainText("Approved by dana");
  await expect(proposed).toContainText("effect #");

  const trail = page.locator(".trail");
  await expect(trail).toContainText("action.approved");
  await expect(trail).toContainText("action.executed");
});

test("a reviewer must give a reason", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Question").fill("Refund order 2 in full, it arrived damaged.");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  const proposed = page
    .getByRole("region", { name: "Proposed actions" })
    .getByRole("article")
    .filter({ hasText: "issue_refund" });
  await proposed.getByLabel("Reviewer", { exact: true }).fill("dana");
  await expect(proposed.getByRole("button", { name: "Approve and execute" })).toBeDisabled();
});
