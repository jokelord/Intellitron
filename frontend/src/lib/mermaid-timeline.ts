const TIMELINE_HEADER = /^\s*timeline\b/i;
const DIRECTIVE_LINE = /^\s*(?:%%|#|title\s|section\s|accTitle\b|accDescr\b)/i;
const ACCDESCR_BLOCK_OPEN = /^\s*accDescr\s*\{/i;
const ACCDESCR_BLOCK_CLOSE = /^\s*\}/;
const EVENT_SEPARATOR = /:(?=\s|$)/;
const FULLWIDTH_COLON = '：';

function normalizePeriodLine(line: string): string {
  const separator = EVENT_SEPARATOR.exec(line);
  if (!separator) {
    return line.replace(/:/g, FULLWIDTH_COLON);
  }
  return `${line.slice(0, separator.index).replace(/:/g, FULLWIDTH_COLON)}${line.slice(separator.index)}`;
}

export function normalizeMermaidTimelineChart(chart: string): string {
  const lines = chart.split('\n');
  if (!TIMELINE_HEADER.test(lines[0] ?? '')) {
    return chart;
  }

  let inAccDescrBlock = false;
  return lines
    .map((line, index) => {
      if (index === 0) {
        return line;
      }
      if (inAccDescrBlock) {
        if (ACCDESCR_BLOCK_CLOSE.test(line)) {
          inAccDescrBlock = false;
        }
        return line;
      }
      if (ACCDESCR_BLOCK_OPEN.test(line)) {
        if (!line.includes('}')) {
          inAccDescrBlock = true;
        }
        return line;
      }
      if (DIRECTIVE_LINE.test(line) || !line.includes(':')) {
        return line;
      }
      return normalizePeriodLine(line);
    })
    .join('\n');
}
