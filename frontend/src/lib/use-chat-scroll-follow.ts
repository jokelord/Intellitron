'use client';

import { useLayoutEffect, useRef } from 'react';
import { attachChatScrollFollow } from './chat-scroll-follow';

type ScrollMessage = { id: string; role: string };

/** Shared by Text and Vision so their streaming follow/unlock behavior stays identical. */
export function useChatScrollFollow(messages: readonly ScrollMessage[], active: boolean) {
  const messageScrollContainerRef = useRef<HTMLDivElement>(null);
  const messageFooterRef = useRef<HTMLElement>(null);
  const messageContentRef = useRef<HTMLElement>(null);
  const followerRef = useRef<ReturnType<typeof attachChatScrollFollow> | null>(null);
  let requestKey: string | undefined;
  for (let index = messages.length - 1; index >= 0; index--) {
    if (messages[index].role === 'user') {
      requestKey = messages[index].id;
      break;
    }
  }

  useLayoutEffect(() => {
    const container = messageScrollContainerRef.current;
    if (!container) return;
    const follower = attachChatScrollFollow(container, messageContentRef.current, messageFooterRef.current);
    followerRef.current = follower;
    return () => {
      follower.dispose();
      followerRef.current = null;
    };
  }, []);

  useLayoutEffect(() => {
    followerRef.current?.update(active, requestKey);
  }, [messages, active, requestKey]);

  return { messageScrollContainerRef, messageContentRef, messageFooterRef };
}
