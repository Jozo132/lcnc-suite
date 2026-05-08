// Dialog open-state + open/close helpers, extracted from App.vue.
//
// The "navigation dialogs" (settings, gcodeRef, messages) are mutually
// exclusive — opening one closes the others. The "confirmation dialogs"
// (shutdown, compensation toggle) and the gcode-stats dialog are
// independent. All of them live in one composable because they share the
// same DOM concept (overlay + dialog) and concentrating ownership keeps
// App.vue from sprouting six tiny ref/function clusters.
//
// Side effects requested by callers (markMessagesRead when the messages
// dialog opens; sending a set_compensation command on confirm) are
// injected as factory args so this composable stays free of imports
// from lcncWs.ts.

import { ref, watch } from "vue";
import type { WsCommand } from "./lcnc";

interface UseDialogStateOptions {
  /** Called when the messages dialog opens via openDialog("messages"). */
  markMessagesRead: () => void;
  /** Called when the operator confirms a compensation toggle. */
  send: (cmd: WsCommand) => void;
}

export function useDialogState(opts: UseDialogStateOptions) {
  // ── Navigation dialogs (mutually exclusive) ──
  const settingsDialogOpen = ref(false);
  const settingsInitialTab = ref<string | null>(null);
  const gcodeRefOpen = ref(false);
  const gcodeRefInitialSearch = ref("");
  const messagesDialogOpen = ref(false);

  function closeAllDialogs() {
    settingsDialogOpen.value = false;
    gcodeRefOpen.value = false;
    messagesDialogOpen.value = false;
  }

  function openDialog(name: "settings" | "gcodeRef" | "messages") {
    const isOpen = (name === "settings" && settingsDialogOpen.value)
      || (name === "gcodeRef" && gcodeRefOpen.value)
      || (name === "messages" && messagesDialogOpen.value);
    closeAllDialogs();
    if (!isOpen) {
      if (name === "settings") settingsDialogOpen.value = true;
      else if (name === "gcodeRef") gcodeRefOpen.value = true;
      else if (name === "messages") {
        messagesDialogOpen.value = true;
        opts.markMessagesRead();
      }
    }
  }

  function openSettingsTab(tab: string) {
    settingsInitialTab.value = tab;
    settingsDialogOpen.value = true;
  }

  // Reset the initialTab anchor when the dialog closes so a subsequent
  // open without a tab argument doesn't land on the previous selection.
  watch(settingsDialogOpen, (open) => {
    if (!open) settingsInitialTab.value = null;
  });

  function openGcodeRef(code?: string) {
    gcodeRefInitialSearch.value = code ?? "";
    openDialog("gcodeRef");
  }

  // ── Standalone confirmation / status dialogs ──
  const showShutdownConfirm = ref(false);
  const statsDialogOpen = ref(false);

  // Compensation toggle: null = no dialog; boolean = pending operator
  // confirmation of enabling/disabling compensation.
  const compConfirmPending = ref<boolean | null>(null);

  function requestCompToggle(enable: boolean) {
    compConfirmPending.value = enable;
  }
  function confirmCompToggle() {
    if (compConfirmPending.value !== null) {
      opts.send({ cmd: "set_compensation", enable: compConfirmPending.value });
      compConfirmPending.value = null;
    }
  }
  function cancelCompToggle() {
    compConfirmPending.value = null;
  }

  return {
    // Navigation dialogs
    settingsDialogOpen,
    settingsInitialTab,
    gcodeRefOpen,
    gcodeRefInitialSearch,
    messagesDialogOpen,
    closeAllDialogs,
    openDialog,
    openSettingsTab,
    openGcodeRef,
    // Standalone dialogs
    showShutdownConfirm,
    statsDialogOpen,
    compConfirmPending,
    requestCompToggle,
    confirmCompToggle,
    cancelCompToggle,
  };
}
