"use client";

import React from "react";
import type { SlashCommand } from "../lib/slash-commands";

type Props = {
  visible: boolean;
  commands: SlashCommand[];
  selectedIndex: number;
  disabledMap: Map<string, string | null>;
  onSelect: (cmd: SlashCommand) => void;
  onClose: () => void;
};

export function SlashCommandMenu({
  visible,
  commands,
  selectedIndex,
  disabledMap,
  onSelect,
  onClose,
}: Props) {
  if (!visible || commands.length === 0) return null;
  return (
    <div
      role="listbox"
      aria-label="Slash commands"
      data-testid="slash-command-menu"
      className="absolute bottom-full left-0 right-0 mb-2 z-30 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-lg overflow-hidden"
      onMouseDown={(e) => e.preventDefault()}
    >
      <div className="px-3 py-2 text-xs text-gray-500 dark:text-gray-400 border-b border-gray-100 dark:border-gray-800 flex items-center justify-between">
        <span>Slash commands (↑↓ Enter, Esc to close)</span>
        <button
          type="button"
          className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
          onClick={onClose}
          aria-label="Close slash menu"
        >
          ×
        </button>
      </div>
      <ul className="max-h-72 overflow-y-auto">
        {commands.map((cmd, i) => {
          const disabled = disabledMap.get(cmd.name);
          const isSelected = i === selectedIndex;
          return (
            <li
              key={cmd.name}
              role="option"
              aria-selected={isSelected}
              aria-disabled={!!disabled}
              title={disabled || undefined}
              onClick={() => !disabled && onSelect(cmd)}
              className={
                "px-3 py-2 cursor-pointer text-sm flex flex-col " +
                (isSelected ? "bg-blue-50 dark:bg-blue-900/30 " : "") +
                (disabled
                  ? "opacity-50 cursor-not-allowed "
                  : "hover:bg-gray-50 dark:hover:bg-gray-800 ")
              }
            >
              <div className="font-mono text-blue-600 dark:text-blue-400">
                {cmd.name}
              </div>
              <div className="text-gray-600 dark:text-gray-400 text-xs">
                {cmd.description}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
