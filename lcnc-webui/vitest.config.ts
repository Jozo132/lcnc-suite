import { defineConfig } from "vitest/config";

// Unit tests for pure logic (permissions evaluation, command classification).
// No DOM needed, so the lightweight node environment is enough.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
