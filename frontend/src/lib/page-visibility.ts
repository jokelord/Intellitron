"use client";

import { useEffect, useState } from "react";

export interface PageVisibilityDocument {
  readonly visibilityState: DocumentVisibilityState;
  addEventListener(type: "visibilitychange", listener: () => void): void;
  removeEventListener(type: "visibilitychange", listener: () => void): void;
}

function isPageVisible(pageDocument: PageVisibilityDocument): boolean {
  return pageDocument.visibilityState === "visible";
}

export function subscribePageVisibility(
  pageDocument: PageVisibilityDocument,
  listener: (visible: boolean) => void,
): () => void {
  let visible = isPageVisible(pageDocument);
  const handleVisibilityChange = () => {
    const nextVisible = isPageVisible(pageDocument);
    if (nextVisible === visible) return;
    visible = nextVisible;
    listener(visible);
  };
  pageDocument.addEventListener("visibilitychange", handleVisibilityChange);
  listener(visible);
  return () => pageDocument.removeEventListener("visibilitychange", handleVisibilityChange);
}

export function usePageVisibility(): boolean {
  const [visible, setVisible] = useState(() => (
    typeof document === "undefined" || document.visibilityState === "visible"
  ));
  useEffect(() => subscribePageVisibility(document, setVisible), []);
  return visible;
}
