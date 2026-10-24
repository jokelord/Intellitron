"use client";

import React, { useState, useEffect, useCallback } from "react";
import {
  CycleStep,
  CycleWaitStep,
  CycleMessageStep,
  CycleCycleStep,
  createWaitStep,
  createMessageStep,
  createCycleStep,
} from "@/lib/cycle-types";

interface CycleStepEditorProps {
  step: CycleStep;
  onChange: (step: CycleStep) => void;
  onRemove: () => void;
  depth: number;
  maxDepth: number;
  isVisionMode: boolean;
}

export default function CycleStepEditor({
  step,
  onChange,
  onRemove,
  depth,
  maxDepth,
  isVisionMode,
}: CycleStepEditorProps) {
  const depthFills = [
    "bg-blue-500/[0.07]",
    "bg-green-500/[0.07]",
    "bg-yellow-500/[0.07]",
    "bg-purple-500/[0.07]",
    "bg-pink-500/[0.07]",
  ];

  return (
    <div className={`rounded-md ${depthFills[depth % depthFills.length]} pl-3 pr-2 py-2 space-y-2`}>
      {/* Header: type selector + delete */}
      <div className="flex items-center gap-2">
        <select
          value={step.type}
          onChange={(e) => {
            const newType = e.target.value as CycleStep["type"];
            if (newType === "wait") onChange(createWaitStep());
            else if (newType === "message") onChange(createMessageStep());
            else if (newType === "cycle") onChange(createCycleStep());
          }}
          className="cycle-input"
        >
          <option value="wait">Wait</option>
          <option value="message">Message</option>
          {depth < maxDepth && <option value="cycle">Cycle</option>}
        </select>
        <button
          onClick={onRemove}
          className="cycle-button text-red-700 hover:text-red-600 dark:text-red-300 dark:hover:text-red-200"
          title="Delete"
        >
          x
        </button>
      </div>

      {/* Step-specific fields */}
      {step.type === "wait" && (
        <WaitEditor
          step={step}
          onChange={(s) => onChange(s)}
        />
      )}
      {step.type === "message" && (
        <MessageEditor
          step={step}
          onChange={(s) => onChange(s)}
          isVisionMode={isVisionMode}
        />
      )}
      {step.type === "cycle" && (
        <CycleNestedEditor
          step={step}
          onChange={(s) => onChange(s)}
          depth={depth}
          maxDepth={maxDepth}
          isVisionMode={isVisionMode}
        />
      )}
    </div>
  );
}

function WaitEditor({
  step,
  onChange,
}: {
  step: CycleWaitStep;
  onChange: (s: CycleWaitStep) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <label className="cycle-muted text-sm">Seconds:</label>
      <input
        type="number"
        min={0}
        step={0.5}
        value={step.wait}
        onChange={(e) =>
          onChange({ ...step, wait: parseFloat(e.target.value) || 0 })
        }
        className="cycle-input w-20"
      />
    </div>
  );
}

function MessageEditor({
  step,
  onChange,
  isVisionMode,
}: {
  step: CycleMessageStep;
  onChange: (s: CycleMessageStep) => void;
  isVisionMode: boolean;
}) {
  // Local text state avoids the controlled-input problem where typing a
  // comma immediately re-renders and eats it (e.g. "0," → parsed as [0] →
  // displayed as "0").  We commit on blur / Enter instead.
  const [cameraText, setCameraText] = useState(step.photo?.join(", ") ?? "");

  // Sync when step.photo changes externally (reset, type switch, etc.)
  useEffect(() => {
    setCameraText(step.photo?.join(", ") ?? "");
  }, [JSON.stringify(step.photo)]);

  const commitCameras = useCallback(() => {
    const val = cameraText.trim();
    if (!val) {
      const { photo, ...rest } = step;
      onChange({ ...rest, type: "message" });
      return;
    }
    const indices = val
      .split(",")
      .map((s) => parseInt(s.trim()))
      .filter((n) => !isNaN(n));
    if (indices.length > 0) {
      onChange({ ...step, photo: indices });
    } else {
      const { photo, ...rest } = step;
      onChange({ ...rest, type: "message" });
    }
  }, [cameraText, step, onChange]);

  return (
    <div className="space-y-2">
      <textarea
        value={step.message}
        onChange={(e) => onChange({ ...step, message: e.target.value })}
        placeholder="Enter message..."
        rows={2}
        className="cycle-input w-full resize-y"
      />
      {isVisionMode && (
        <div className="flex items-center gap-2">
          <label className="cycle-muted text-sm">Cameras:</label>
          <input
            type="text"
            value={cameraText}
            onChange={(e) => setCameraText(e.target.value)}
            onBlur={commitCameras}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                commitCameras();
              }
            }}
            placeholder="e.g. 0, 1"
            className="cycle-input w-32"
          />
        </div>
      )}
    </div>
  );
}

function CycleNestedEditor({
  step,
  onChange,
  depth,
  maxDepth,
  isVisionMode,
}: {
  step: CycleCycleStep;
  onChange: (s: CycleCycleStep) => void;
  depth: number;
  maxDepth: number;
  isVisionMode: boolean;
}) {
  const addStep = (type: CycleStep["type"]) => {
    let newStep: CycleStep;
    if (type === "wait") newStep = createWaitStep();
    else if (type === "message") newStep = createMessageStep();
    else newStep = createCycleStep();
    onChange({
      ...step,
      cycle_steps: [...step.cycle_steps, newStep],
    });
  };

  const updateChild = (index: number, child: CycleStep) => {
    const newSteps = [...step.cycle_steps];
    newSteps[index] = child;
    onChange({ ...step, cycle_steps: newSteps });
  };

  const removeChild = (index: number) => {
    const newSteps = step.cycle_steps.filter((_, i) => i !== index);
    onChange({ ...step, cycle_steps: newSteps });
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <label className="cycle-muted text-sm">Repeat:</label>
        <input
          type="number"
          min={1}
          value={step.cycle_count}
          onChange={(e) =>
            onChange({
              ...step,
              cycle_count: parseInt(e.target.value) || 1,
            })
          }
          className="cycle-input w-16"
        />
        <span className="cycle-muted text-sm">times</span>
      </div>

      <div className="ml-1 space-y-1">
        {step.cycle_steps.map((child, i) => (
          <CycleStepEditor
            key={i}
            step={child}
            onChange={(s) => updateChild(i, s)}
            onRemove={() => removeChild(i)}
            depth={depth + 1}
            maxDepth={maxDepth}
            isVisionMode={isVisionMode}
          />
        ))}
      </div>

      {/* Add step buttons */}
      <div className="flex gap-1">
        <button
          onClick={() => addStep("wait")}
          className="cycle-button"
        >
          + Wait
        </button>
        <button
          onClick={() => addStep("message")}
          className="cycle-button"
        >
          + Message
        </button>
        {depth + 1 < maxDepth && (
          <button
            onClick={() => addStep("cycle")}
            className="cycle-button"
          >
            + Cycle
          </button>
        )}
      </div>
    </div>
  );
}
