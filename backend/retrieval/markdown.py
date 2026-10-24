"""Lossless Markdown fragments bounded by Unicode characters and encoder tokens."""
import bisect
import re
from .config import ChunkingConfig
from .contracts import MemoryChunk


def split_markdown(text: str, *, source_path: str, tokenizer, config: ChunkingConfig) -> list[MemoryChunk]:
    if not text.strip():
        return []
    line_starts = [0] + [m.end() for m in re.finditer('\n', text)]
    structural = {m.end() for m in re.finditer(r'\n\s*\n|\n(?=#{1,6}\s)|\n(?=```|~~~)', text)}
    heading_positions, heading_values = [], []
    parents = {}
    fence = None
    position = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            symbol = marker.group(1)
            if fence is None:
                fence = symbol
            elif symbol[0] == fence[0] and len(symbol) >= len(fence):
                fence = None
        elif fence is None:
            heading = re.match(r'^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\n|$)', line)
            if heading:
                depth = len(heading.group(1))
                parents = {level: title for level, title in parents.items() if level < depth}
                parents[depth] = heading.group(2)
                heading_positions.append(position)
                heading_values.append(' / '.join(parents[level] for level in sorted(parents)))
        position += len(line)
    chunks = []
    start = 0
    last_end = 0
    while start < len(text):
        maximum = min(len(text), start + config.max_chars)
        # Token counts are not strictly monotonic for all tokenizers: always verify
        # the chosen exact substring and shrink until it fits, without truncation.
        end = maximum
        while end > start and len(tokenizer.encode('passage: ' + text[start:end], add_special_tokens=True)) > config.max_tokens:
            end -= max(1, (end - start) // 10)
        if end <= start:
            raise ValueError('chunk token budget cannot fit an input character')
        boundaries = [x for x in structural if start + (end - start) // 2 <= x <= end and x > last_end]
        if boundaries and end < len(text):
            end = max(boundaries)
        while end > start and len(tokenizer.encode('passage: ' + text[start:end], add_special_tokens=True)) > config.max_tokens:
            end -= 1
        if end <= start:
            raise ValueError('chunk token budget cannot fit an input character')
        if end <= last_end:
            # Huge overlap cannot prevent progress under the token cap.
            start = last_end
            continue
        begin_line = bisect.bisect_right(line_starts, start)
        end_line = bisect.bisect_right(line_starts, max(start, end - 1))
        heading_index = bisect.bisect_right(heading_positions, start) - 1
        heading = heading_values[heading_index] if heading_index >= 0 else ''
        chunks.append(MemoryChunk(text[start:end], source_path, begin_line, end_line, len(chunks), heading))
        last_end = end
        if end == len(text):
            break
        start = max(start + 1, end - min(config.overlap_chars, max(0, (end - start) // 2)))
    return chunks
