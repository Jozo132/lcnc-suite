import js from "@eslint/js";
import tseslint from "typescript-eslint";
import pluginVue from "eslint-plugin-vue";
import globals from "globals";

// Flat-config ESLint for the Vue 3 + TS frontend (issue #26).
//
// vue-tsc -b (the build) already enforces types and unused-symbol errors, so
// ESLint here focuses on REAL correctness smells and Vue best practices, with
// the high-volume opinion/style rules relaxed so the lint passes on the
// existing code. Tighten incrementally rather than gating on a big cleanup.
export default tseslint.config(
  {
    ignores: [
      "dist/**",
      "node_modules/**",
      "**/*.d.ts",
      "*.config.ts",
      "*.config.js",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  // `essential` = bug-prevention rules only. `recommended` adds ~4k formatting
  // opinions (attribute order, self-closing, line breaks) that don't match this
  // codebase's hand-formatted templates — not worth the churn. Formatting is a
  // separate concern from linting here.
  ...pluginVue.configs["flat/essential"],
  {
    files: ["**/*.{ts,vue}"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: { ...globals.browser, ...globals.worker },
      parserOptions: {
        parser: tseslint.parser, // parse <script lang="ts"> inside .vue
      },
    },
    rules: {
      // Intentional single-word component names (Btn, Gate, Toolbar, …).
      "vue/multi-word-component-names": "off",
      // The gateway protocol is dynamically typed at the WS boundary; `any` is
      // used deliberately there. Not worth blocking the lint.
      "@typescript-eslint/no-explicit-any": "off",
      // Unused vars are already a hard build error via vue-tsc (TS6133);
      // double-reporting here just adds noise.
      "@typescript-eslint/no-unused-vars": "off",
      "no-empty": ["warn", { allowEmptyCatch: true }],
    },
  },
);
