'use client';

import type { VisionAutomationPhase } from '@/lib/vision-automation/types';
import { getVisionAutomationStatus } from '@/lib/vision-automation/ui-contracts';

export interface VisionAutomationStatusRingProps {
  phase: VisionAutomationPhase;
  armed: boolean;
  className?: string;
}

export default function VisionAutomationStatusRing({
  phase,
  armed,
  className = '',
}: VisionAutomationStatusRingProps) {
  const status = getVisionAutomationStatus(phase, armed);
  if (status === null) return null;
  return (
    <span
      role="status"
      aria-label={status.label}
      title={status.label}
      className={`inline-block h-3 w-3 rounded-full border-2 border-r-transparent ${status.className} ${className}`}
    />
  );
}
