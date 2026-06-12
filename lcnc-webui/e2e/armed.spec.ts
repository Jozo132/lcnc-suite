import { test, expect } from "@playwright/test";

// Armed-state E2E (issue #26): point the app at the mock gateway (e2e/
// mock-gateway.mjs), which pushes an armed + ready machine state. Asserts the
// default-deny gating OPENS — the inverse of, and complement to, the
// disconnected smoke test — exercising the full
// backend-permissions -> applyClientOverlay -> fieldset[disabled] pipeline in a
// real browser. The mock serves the app on its own port so the app's
// `location.host/ws` connection lands on the mock WS.
const MOCK = "http://localhost:4174/";

test("connected + armed + ready lifts default-deny on machine controls", async ({ page }) => {
  await page.goto(MOCK);
  // Outer armed gate opens (navigation + content interactive)...
  await expect(page.locator('fieldset[data-gate="armed"]').first()).not.toBeDisabled();
  // ...and the inner ready gate (SpindleStrip) opens because the broadcast
  // permissions carry ready=true and the client is armed.
  await expect(page.locator('fieldset[data-gate="ready"]').first()).not.toBeDisabled();
});
