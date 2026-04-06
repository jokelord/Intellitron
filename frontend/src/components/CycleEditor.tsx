"use client";

import React, { useState, useEffect, useRef } from "react";
import CycleStepEditor from "./CycleStepEditor";
import {
  CycleStep,
  CycleEditorState,
  CycleExecutionStatus,
  createWaitStep,
  createMessageStep,
  createCycleStep,
} from "@/lib/cycle-types";

interface CycleEditorProps {
  isVisionMode: boolean;
  maxDepth: number;
  defaultSteps: CycleStep[];
  executionStatus: CycleExecutionStatus;
  onStart: (steps: CycleStep[], shareContext: boolean) => void;
  onStop: () => void;
}

const STORAGE_KEY = "cycle-editor-state";

export default function CycleEditor({
  isVisionMode,
  maxDepth,
  defaultSteps,
  executionStatus,
  onStart,
  onStop,
}: CycleEditorProps) {
  const [steps, setSteps] = useState<CycleStep[]>([]);
  const [shareContext, setShareContext] = useState(true);
  const [isStarting, setIsStarting] = useState(false);

  const handleStart = async () => {
    if (isStarting) return; // 防抖：防止双击发起多个请求
    setIsStarting(true);
    try {
      await onStart(steps, shareContext);
    } finally {
      // 短暂延迟后重置状态，避免快速连续点击
      setTimeout(() => setIsStarting(false), 1000);
    }
  };

  // Track whether we've already loaded from localStorage or defaults
  const hasLoadedRef = useRef(false);
  const defaultStepsJson = JSON.stringify(defaultSteps);

  // Load from localStorage on mount, fallback to defaultSteps
  useEffect(() => {
    if (hasLoadedRef.current) return;
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) {
        const state: CycleEditorState = JSON.parse(saved);
        if (state.steps && state.steps.length > 0) {
          setSteps(state.steps);
          setShareContext(state.shareContext);
          hasLoadedRef.current = true;
          return;
        }
      }
    } catch {
      // Fall through to defaults
    }
    // Use defaults only if we have some (API may not have loaded yet)
    if (defaultSteps.length > 0) {
      setSteps(defaultSteps);
      hasLoadedRef.current = true;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultStepsJson]);

  // Save to localStorage on change
  useEffect(() => {
    try {
      const state: CycleEditorState = { steps, shareContext };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch {
      // Ignore
    }
  }, [steps, shareContext]);

  const addStep = (type: CycleStep["type"]) => {
    let newStep: CycleStep;
    if (type === "wait") newStep = createWaitStep();
    else if (type === "message") newStep = createMessageStep();
    else newStep = createCycleStep();
    setSteps([...steps, newStep]);
  };

  const updateStep = (index: number, step: CycleStep) => {
    const newSteps = [...steps];
    newSteps[index] = step;
    setSteps(newSteps);
  };

  const removeStep = (index: number) => {
    setSteps(steps.filter((_, i) => i !== index));
  };

  const resetToDefault = () => {
    setSteps(defaultSteps);
    setShareContext(true);
  };

  const isRunning = executionStatus === "running";
  const hasPhotoSteps = checkHasPhoto(steps);
  const showPhotoWarning = !isVisionMode && hasPhotoSteps;

  return (
    <div className="space-y-3">
      {/* Steps list */}
      <div className="space-y-1 max-h-60 overflow-y-auto">
        {steps.map((step, i) => (
          <CycleStepEditor
            key={i}
            step={step}
            onChange={(s) => updateStep(i, s)}
            onRemove={() => removeStep(i)}
            depth={0}
            maxDepth={maxDepth}
            isVisionMode={isVisionMode}
          />
        ))}
      </div>

      {/* Add step buttons */}
      {!isRunning && (
        <div className="flex gap-2">
          <button
            onClick={() => addStep("wait")}
            className="text-xs bg-gray-700 hover:bg-gray-600 text-gray-300 rounded px-2 py-1"
          >
            + Wait
          </button>
          <button
            onClick={() => addStep("message")}
            className="text-xs bg-gray-700 hover:bg-gray-600 text-gray-300 rounded px-2 py-1"
          >
            + Message
          </button>
          <button
            onClick={() => addStep("cycle")}
            className="text-xs bg-gray-700 hover:bg-gray-600 text-gray-300 rounded px-2 py-1"
          >
            + Cycle
          </button>
        </div>
      )}

      {/* Warning for text mode with photo steps */}
      {showPhotoWarning && (
        <div className="text-yellow-400 text-xs bg-yellow-400/10 rounded px-2 py-1">
          Text mode: photo steps will be skipped
        </div>
      )}

      {/* Controls */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          {/* Share context toggle */}
          <label className="flex items-center gap-1.5 text-sm text-gray-400">
            <input
              type="checkbox"
              checked={shareContext}
              onChange={(e) => setShareContext(e.target.checked)}
              disabled={isRunning}
              className="rounded"
            />
            Share context
          </label>

          {!isRunning && (
            <button
              onClick={resetToDefault}
              className="text-xs text-gray-500 hover:text-gray-400"
            >
              Reset
            </button>
          )}
        </div>

        {/* Start/Stop button */}
        {isRunning ? (
          <button
            onClick={onStop}
            className="bg-red-600 hover:bg-red-500 text-white text-sm font-medium rounded px-4 py-1.5"
          >
            Stop
          </button>
        ) : (
          <button
            onClick={handleStart}
            disabled={steps.length === 0 || isStarting}
            className="bg-blue-600 hover:bg-blue-500 disabled:bg-gray-600 disabled:text-gray-400 text-white text-sm font-medium rounded px-4 py-1.5"
          >
            {isStarting ? "Starting..." : "Start"}
          </button>
        )}
      </div>
    </div>
  );
}

/** Check if any step (recursively) contains photo */
function checkHasPhoto(steps: CycleStep[]): boolean {
  for (const step of steps) {
    if (step.type === "message" && step.photo && step.photo.length > 0) {
      return true;
    }
    if (step.type === "cycle" && checkHasPhoto(step.cycle_steps)) {
      return true;
    }
  }
  return false;
}
