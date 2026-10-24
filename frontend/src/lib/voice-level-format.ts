function padIntegerMagnitude(value: number): string {
  return String(value).padStart(2, "0");
}

export function formatInputGainLabel(value: number): string {
  if (!Number.isFinite(value)) return "gain +00 dB";

  const roundedMagnitude = Math.round(Math.abs(value));
  const sign = value < 0 && roundedMagnitude !== 0 ? "-" : "+";
  return `gain ${sign}${padIntegerMagnitude(roundedMagnitude)} dB`;
}

export function formatPostGainEstimate(value: number | null): string {
  if (value === null || !Number.isFinite(value)) {
    return "Post-gain estimate -- dBFS";
  }

  const roundedMagnitude = Math.round(Math.abs(value) * 10) / 10;
  const sign = value < 0 && roundedMagnitude !== 0 ? "-" : "";
  const [integer, decimal] = roundedMagnitude.toFixed(1).split(".");
  return `Post-gain estimate ${sign}${padIntegerMagnitude(Number(integer))}.${decimal} dBFS`;
}
