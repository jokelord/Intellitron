import React from 'react';

interface ToggleSwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: React.ReactNode;
  disabled?: boolean;
}

export function ToggleSwitch({ checked, onChange, label, disabled = false }: ToggleSwitchProps) {
  return (
    <div className="flex items-center space-x-3">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => !disabled && onChange(!checked)}
        className={`
          relative inline-flex h-6 w-11 items-center rounded-full transition-colors duration-200 ease-in-out
          ${checked ? 'bg-nvidia-500' : 'bg-gray-300 dark:bg-gray-600'}
          ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
        `}
      >
        <span
          className={`
            inline-block h-5 w-5 transform rounded-full bg-white shadow-md transition-transform duration-200 ease-in-out
            ${checked ? 'translate-x-5' : 'translate-x-0.5'}
          `}
        />
      </button>
      <label className="text-sm font-medium text-gray-700 dark:text-gray-300">
        {label}
      </label>
    </div>
  )
}
