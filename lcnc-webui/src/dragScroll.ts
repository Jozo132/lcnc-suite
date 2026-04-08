/**
 * Drag-to-scroll for touch screens and mouse.
 * Auto-attaches to all `.scroll-thin` elements via MutationObserver.
 * Uses pointer events so it works with both touch and mouse.
 */

const DRAG_THRESHOLD = 5 // px — must move this far before drag starts

interface DragState {
  pointerId: number
  startX: number
  startY: number
  scrollLeft: number
  scrollTop: number
  dragging: boolean
}

const attached = new WeakSet<HTMLElement>()

function attach(el: HTMLElement) {
  if (attached.has(el)) return
  attached.add(el)

  let state: DragState | null = null

  el.addEventListener('pointerdown', (e: PointerEvent) => {
    // Only primary button (touch or left-click)
    if (e.button !== 0) return
    // Skip if target is interactive (buttons, inputs, etc.)
    const tag = (e.target as HTMLElement).closest(
      'button, input, select, textarea, a, [contenteditable], .no-drag-scroll'
    )
    if (tag) return

    state = {
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      scrollLeft: el.scrollLeft,
      scrollTop: el.scrollTop,
      dragging: false,
    }
    // Don't capture yet — let native scrollbar interaction work.
    // Capture is deferred to pointermove once the drag threshold is met.
  })

  el.addEventListener('pointermove', (e: PointerEvent) => {
    if (!state || e.pointerId !== state.pointerId) return

    const dx = e.clientX - state.startX
    const dy = e.clientY - state.startY

    if (!state.dragging) {
      if (Math.abs(dx) < DRAG_THRESHOLD && Math.abs(dy) < DRAG_THRESHOLD) return
      state.dragging = true
      // Capture now that we know this is a drag, not a scrollbar click
      el.setPointerCapture(e.pointerId)
      el.style.cursor = 'grabbing'
      el.style.userSelect = 'none'
    }

    el.scrollLeft = state.scrollLeft - dx
    el.scrollTop = state.scrollTop - dy
  })

  function end(e: PointerEvent) {
    if (!state || e.pointerId !== state.pointerId) return
    if (state.dragging) {
      el.style.cursor = ''
      el.style.userSelect = ''
    }
    state = null
  }

  el.addEventListener('pointerup', end)
  el.addEventListener('pointercancel', end)
}

function scanAndAttach(root: Element | Document = document) {
  for (const el of root.querySelectorAll<HTMLElement>('.scroll-thin')) {
    attach(el)
  }
}

export function initDragScroll() {
  // Attach to existing elements
  scanAndAttach()

  // Watch for dynamically added .scroll-thin elements
  new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (!(node instanceof HTMLElement)) continue
        if (node.classList.contains('scroll-thin')) attach(node)
        scanAndAttach(node)
      }
    }
  }).observe(document.body, { childList: true, subtree: true })
}
