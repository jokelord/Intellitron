export type MermaidThemeName = 'default' | 'dark';

export function getMermaidThemeName(prefersDark: boolean): MermaidThemeName {
  return prefersDark ? 'dark' : 'default';
}

export function subscribeToDarkScheme(
  queryList: {
    matches: boolean;
    addEventListener(type: 'change', listener: () => void): void;
    removeEventListener(type: 'change', listener: () => void): void;
  },
  onChange: (prefersDark: boolean) => void,
): () => void {
  const listener = () => {
    onChange(queryList.matches);
  };

  onChange(queryList.matches);
  queryList.addEventListener('change', listener);

  let unsubscribed = false;
  return () => {
    if (unsubscribed) {
      return;
    }
    unsubscribed = true;
    queryList.removeEventListener('change', listener);
  };
}
