// Chromium reports a conventional wheel notch as 100 CSS pixels (or three lines).
// Trackpad pixel deltas accumulate to the same two-notch distance.
export const CHAT_SCROLL_WHEEL_NOTCH_PX = 100;
export const CHAT_SCROLL_UNLOCK_NOTCHES = 2;
export const CHAT_SCROLL_BOTTOM_THRESHOLD_PX = 1;

export interface ChatScrollMetrics {
  scrollTop: number;
  clientHeight: number;
  scrollHeight: number;
}

export function isChatScrolledNearBottom(
  metrics: ChatScrollMetrics,
  thresholdPx: number = CHAT_SCROLL_BOTTOM_THRESHOLD_PX,
): boolean {
  return metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight <= thresholdPx;
}

function nestedViewportConsumesWheel(container: HTMLElement, event: WheelEvent): boolean {
  for (const target of event.composedPath()) {
    if (target === container) break;
    if (!(target instanceof Element)) continue;
    const viewport = target as HTMLElement;
    if (viewport.scrollHeight <= viewport.clientHeight) continue;
    if (!/^(auto|scroll)$/.test(getComputedStyle(viewport).overflowY)) continue;
    if (event.deltaY < 0 ? viewport.scrollTop > 0 : !isChatScrolledNearBottom(viewport)) return true;
  }
  return false;
}

/** One persistent lock per page; scroll/layout events cannot unlock a running turn. */
export function attachChatScrollFollow(container: HTMLElement, content?: HTMLElement | null, footer?: HTMLElement | null) {
  let following = true;
  let active = false;
  let previousRequest: string | undefined;
  let upwardDistance = 0;
  let awaitingDeparture = false;

  const lock = () => {
    following = true;
    upwardDistance = 0;
    awaitingDeparture = false;
  };
  const refresh = () => {
    if (following && !isChatScrolledNearBottom(container)) {
      container.scrollTo({ top: container.scrollHeight, behavior: 'instant' });
    }
  };
  const onScroll = () => {
    if (active && following) {
      refresh();
    } else if (isChatScrolledNearBottom(container)) {
      // Ignore stale programmatic bottom events until the unlocking wheel moves away.
      if (!awaitingDeparture) lock();
    } else {
      awaitingDeparture = false;
      if (!active) following = false;
    }
  };
  const onWheel = (event: WheelEvent) => {
    if (event.ctrlKey || event.deltaY === 0) return;
    // Tool details/input fields with their own scrollbar retain their normal controls.
    if (typeof Element !== 'undefined' && nestedViewportConsumesWheel(container, event)) return;
    if (!active) {
      if (event.deltaY < 0) {
        following = false;
        awaitingDeparture = true;
      }
      return;
    }
    if (!following) return;
    if (event.deltaY > 0) {
      upwardDistance = 0;
      return;
    }
    if (container.scrollHeight <= container.clientHeight + CHAT_SCROLL_BOTTOM_THRESHOLD_PX) {
      // There is no older content to enter yet; keep following as output grows.
      upwardDistance = 0;
      event.preventDefault();
      return;
    }
    const unit = event.deltaMode === 1
      ? CHAT_SCROLL_WHEEL_NOTCH_PX / 3
      : event.deltaMode === 2 ? container.clientHeight : 1;
    upwardDistance += -event.deltaY * unit;
    if (upwardDistance >= CHAT_SCROLL_WHEEL_NOTCH_PX * CHAT_SCROLL_UNLOCK_NOTCHES) {
      following = false;
      awaitingDeparture = true;
      upwardDistance = 0;
    } else {
      // A single notch must neither release the lock nor move the locked viewport.
      event.preventDefault();
    }
  };

  container.addEventListener('wheel', onWheel, { passive: false });
  container.addEventListener('scroll', onScroll);
  const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(refresh);
  observer?.observe(container);
  if (content) observer?.observe(content);
  if (footer) observer?.observe(footer);

  return {
    update(nextActive: boolean, requestKey?: string) {
      if (nextActive && (!active || requestKey !== previousRequest)) lock();
      active = nextActive;
      previousRequest = requestKey;
      refresh();
    },
    dispose() {
      observer?.disconnect();
      container.removeEventListener('wheel', onWheel);
      container.removeEventListener('scroll', onScroll);
    },
  };
}
