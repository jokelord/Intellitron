export interface RetryDynamicImportOptions {
  attempts?: number;
  delayMs?: number;
}

function sleep(ms: number): Promise<void> {
  if (ms <= 0) return Promise.resolve();
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

export async function retryDynamicImport<T>(
  loader: () => Promise<T>,
  options: RetryDynamicImportOptions = {},
): Promise<T> {
  const attempts = Math.max(1, options.attempts ?? 3);
  const delayMs = Math.max(0, options.delayMs ?? 300);
  let lastError: unknown;

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await loader();
    } catch (error) {
      lastError = error;
      if (attempt < attempts) {
        await sleep(delayMs * attempt);
      }
    }
  }

  throw lastError;
}
