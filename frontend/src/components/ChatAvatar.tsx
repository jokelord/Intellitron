'use client';

import { Bot, User } from 'lucide-react';
import { getWebUIAvatarPath, getWebUIAvatarSize, type WebUIConfig } from '@/lib/web-ui-config';

interface ChatAvatarProps {
  role: 'user' | 'assistant';
  config?: WebUIConfig;
}

export default function ChatAvatar({ role, config }: ChatAvatarProps) {
  const imagePath = getWebUIAvatarPath(config, role);
  const sizePx = getWebUIAvatarSize(config);
  const iconSizePx = Math.max(16, Math.round(sizePx * 0.62));

  if (imagePath) {
    return (
      <img
        src={imagePath}
        alt={role === 'user' ? 'User avatar' : 'Assistant avatar'}
        className="rounded-lg object-cover object-center"
        style={{ width: sizePx, height: sizePx }}
      />
    );
  }

  return (
    <div
      className={`flex items-center justify-center rounded-lg ${role === 'user' ? 'bg-blue-500' : 'bg-nvidia-500'}`}
      style={{ width: sizePx, height: sizePx }}
    >
      {role === 'user' ? (
        <User className="text-white" style={{ width: iconSizePx, height: iconSizePx }} />
      ) : (
        <Bot className="text-white" style={{ width: iconSizePx, height: iconSizePx }} />
      )}
    </div>
  );
}
