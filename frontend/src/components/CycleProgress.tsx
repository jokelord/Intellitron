"use client";

import React from "react";
import {
  CycleSSEEvent,
  CycleExecutionStatus,
} from "@/lib/cycle-types";

interface CycleProgressProps {
  status: CycleExecutionStatus;
  events: CycleSSEEvent[];
}

export default function CycleProgress({ status, events }: CycleProgressProps) {
  if (events.length === 0) return null;

  const lastEvent = events[events.length - 1];

  const statusLabel: Record<CycleExecutionStatus, string> = {
    idle: "",
    running: "Running...",
    completed: "Completed",
    stopped: "Stopped",
    error: "Error",
  };

  const statusColor: Record<CycleExecutionStatus, string> = {
    idle: "text-gray-400",
    running: "text-blue-400",
    completed: "text-green-400",
    stopped: "text-yellow-400",
    error: "text-red-400",
  };

  // Count completed steps
  const stepEnds = events.filter((e) => e.type === "step_end").length;
  const totalDuration =
    lastEvent.type === "cycle_completed"
      ? lastEvent.total_duration
      : null;

  return (
    <div className="bg-gray-800/50 border border-gray-700 rounded-lg p-3 text-sm">
      <div className="flex items-center justify-between mb-1">
        <span className="text-gray-300 font-medium">Cycle</span>
        <span className={statusColor[status]}>{statusLabel[status]}</span>
      </div>

      <div className="text-gray-500 text-xs space-y-0.5">
        <div>Steps completed: {stepEnds}</div>
        {totalDuration !== null && (
          <div>Total: {totalDuration.toFixed(1)}s</div>
        )}

        {/* Current step info */}
        {status === "running" && lastEvent.type === "step_begin" && (
          <div className="text-blue-400/70">
            Current: {lastEvent.step_type} [{lastEvent.path.join(",")}]
          </div>
        )}

        {/* Error info */}
        {lastEvent.type === "cycle_error" && (
          <div className="text-red-400/80 mt-1">
            Error: {lastEvent.error}
          </div>
        )}
      </div>
    </div>
  );
}
