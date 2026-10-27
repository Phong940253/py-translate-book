import type { Annotation, ReaderChapter } from "../../lib/api";

export type ReaderItem = ReaderChapter["items"][number];
export type BlockMap = {
  byItem: Map<number, HTMLElement>;
  byElement: Map<HTMLElement, number>;
};

const BLOCK_TAGS = new Set(["H1", "H2", "H3", "H4", "H5", "H6", "P", "BLOCKQUOTE", "PRE", "LI"]);

function normalize(value: string) {
  return value.replace(/\s+/g, " ").trim();
}

export function textOfBlock(element: HTMLElement): string {
  const doc = element.ownerDocument;
  const walker = doc.createTreeWalker(element, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => {
      const parent = node.parentElement;
      if (!parent || /^(SCRIPT|STYLE|NOSCRIPT)$/.test(parent.tagName)) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const parts: string[] = [];
  let node = walker.nextNode() as Text | null;
  while (node) {
    const value = normalize(node.data);
    if (value) parts.push(value);
    node = walker.nextNode() as Text | null;
  }
  return parts.join(" ");
}

/**
 * Build a mapping between the server's paragraph items and the sanitized
 * iframe DOM. The server emits the outermost block (a blockquote containing
 * <p> is one paragraph), so nested blocks are filtered out here too.
 */
export function buildBlockMap(frame: HTMLIFrameElement | null, items: ReaderItem[]): BlockMap {
  const map: BlockMap = { byItem: new Map(), byElement: new Map() };
  const body = frame?.contentDocument?.body;
  if (!body) return map;
  const all = Array.from(body.querySelectorAll<HTMLElement>("h1,h2,h3,h4,h5,h6,p,blockquote,pre,li,div"));
  const blocks = all.filter((element) => {
    let parent = element.parentElement;
    while (parent && parent !== body) {
      if (BLOCK_TAGS.has(parent.tagName)) return false;
      parent = parent.parentElement;
    }
    if (element.tagName === "DIV") {
      return !element.querySelector("h1,h2,h3,h4,h5,h6,p,blockquote,pre,li,div") && !element.querySelector("img");
    }
    return true;
  }).filter((element) => Boolean(normalize(element.textContent || "")));

  const paragraphItems = items
    .map((item, index) => ({ item, index }))
    .filter(({ item }) => item.type === "p" && normalize(item.text || ""));

  const used = new Set<HTMLElement>();
  if (blocks.length === paragraphItems.length) {
    paragraphItems.forEach(({ index }, position) => {
      const element = blocks[position];
      if (!element) return;
      map.byItem.set(index, element);
      map.byElement.set(element, index);
      used.add(element);
    });
    return map;
  }

  // The DOM was sanitized differently (e.g. an image wrapper). Match by text
  // first, then fall back to document order so highlighting still works.
  let cursor = 0;
  for (const { item, index } of paragraphItems) {
    const wanted = normalize(item.text || "");
    let match = blocks.find((element) => !used.has(element) && textOfBlock(element) === wanted);
    if (!match) {
      while (cursor < blocks.length && used.has(blocks[cursor])) cursor += 1;
      match = blocks[cursor];
      cursor += 1;
    }
    if (!match) continue;
    map.byItem.set(index, match);
    map.byElement.set(match, index);
    used.add(match);
  }
  return map;
}

export type TextPosition = { node: Text; offset: number };
export type TextMap = { text: string; positions: TextPosition[] };

/**
 * Build the exact collapsed string BeautifulSoup's ``get_text(" ")`` produced
 * on the server, together with a character → text-node mapping for range
 * creation. Whitespace runs collapse to one space; adjacent text nodes are
 * separated by a space.
 */
export function textMapOf(element: HTMLElement): TextMap {
  const doc = element.ownerDocument;
  const walker = doc.createTreeWalker(element, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => {
      const parent = node.parentElement;
      if (!parent || /^(SCRIPT|STYLE|NOSCRIPT)$/.test(parent.tagName)) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const positions: TextPosition[] = [];
  let text = "";
  const appendSpace = (node: Text, offset: number) => {
    if (!text || text.endsWith(" ")) return;
    text += " ";
    positions.push({ node, offset });
  };

  let node = walker.nextNode() as Text | null;
  while (node) {
    const raw = node.data;
    let sawContent = false;
    for (let index = 0; index < raw.length; index += 1) {
      const char = raw[index];
      if (/\s/.test(char)) {
        if (sawContent) appendSpace(node, Math.max(0, index - 1));
        continue;
      }
      if (!sawContent) appendSpace(node, index);
      text += char;
      positions.push({ node, offset: index });
      sawContent = true;
    }
    node = walker.nextNode() as Text | null;
  }
  return { text: text.trim(), positions: positions.slice(0, text.trim().length) };
}

export function rangeForSpan(element: HTMLElement, start: number, end: number): Range | null {
  const map = textMapOf(element);
  if (!map.positions.length) return null;
  const from = Math.max(0, Math.min(map.positions.length - 1, start));
  let to = Math.max(from + 1, Math.min(map.positions.length, end));
  // Do not end on a synthetic join space: browsers would include it.
  while (to > from + 1 && map.text[to - 1] === " ") to -= 1;
  const first = map.positions[from];
  const last = map.positions[to - 1];
  if (!first || !last) return null;
  const range = element.ownerDocument.createRange();
  try {
    range.setStart(first.node, first.offset);
    range.setEnd(last.node, last.offset + 1);
  } catch {
    return null;
  }
  return range;
}

export function wrapRange(range: Range, className: string, id?: string): HTMLElement | null {
  if (range.collapsed) return null;
  const span = range.startContainer.ownerDocument!.createElement("span");
  span.className = className;
  if (id) span.dataset.annId = id;
  try {
    range.surroundContents(span);
  } catch {
    try {
      span.append(range.extractContents());
      range.insertNode(span);
    } catch {
      return null;
    }
  }
  return span;
}

export function clearMarks(doc: Document | null | undefined, className: string) {
  if (!doc) return;
  for (const mark of Array.from(doc.querySelectorAll<HTMLElement>(`.${className}`))) {
    const parent = mark.parentNode;
    if (!parent) continue;
    while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
    parent.removeChild(mark);
    parent.normalize?.();
  }
}

export function highlightParagraph(doc: Document | null | undefined, element: HTMLElement | null, scroll = true) {
  if (!doc) return;
  for (const active of Array.from(doc.querySelectorAll<HTMLElement>(".rd-tts-active"))) {
    active.classList.remove("rd-tts-active");
  }
  if (!element) return;
  element.classList.add("rd-tts-active");
  if (scroll) {
    try {
      element.scrollIntoView({ block: "center", behavior: "smooth" });
    } catch {
      element.scrollIntoView();
    }
  }
}

export function highlightSentence(doc: Document | null | undefined, element: HTMLElement | null, start: number, end: number) {
  if (!doc) return;
  clearMarks(doc, "rd-tts-sent");
  if (!element) return;
  const range = rangeForSpan(element, start, end);
  if (range) wrapRange(range, "rd-tts-sent");
}

export function findQuote(
  blockMap: BlockMap,
  annotation: Annotation,
): { element: HTMLElement; start: number; end: number } | null {
  const candidates: HTMLElement[] = [];
  if (annotation.item != null) {
    const hinted = blockMap.byItem.get(annotation.item);
    if (hinted) candidates.push(hinted);
  }
  for (const element of blockMap.byItem.values()) {
    if (!candidates.includes(element)) candidates.push(element);
  }
  const quote = normalize(annotation.quote);
  if (!quote) return null;
  for (const element of candidates) {
    const map = textMapOf(element);
    let start = map.text.indexOf(quote);
    if (start < 0) continue;
    if (annotation.pre) {
      const before = map.text.slice(Math.max(0, start - annotation.pre.length - 4), start);
      if (!before.includes(normalize(annotation.pre))) {
        const later = map.text.indexOf(quote, start + 1);
        if (later >= 0) start = later;
      }
    }
    return { element, start, end: start + quote.length };
  }
  return null;
}

export function applyAnnotations(frame: HTMLIFrameElement | null, annotations: Annotation[], blockMap: BlockMap, chapter: number) {
  const doc = frame?.contentDocument;
  if (!doc) return;
  clearMarks(doc, "rd-ann-mark");
  for (const annotation of annotations) {
    if (annotation.ch !== chapter) continue;
    const found = findQuote(blockMap, annotation);
    if (!found) continue;
    const range = rangeForSpan(found.element, found.start, found.end);
    if (!range) continue;
    const span = wrapRange(range, `rd-ann-mark rd-ann-${annotation.color}`, annotation.id);
    if (span && annotation.note) span.title = annotation.note;
  }
}

export function selectionInfo(frame: HTMLIFrameElement | null, blockMap: BlockMap) {
  const doc = frame?.contentDocument;
  const selection = doc?.getSelection();
  if (!doc || !selection || selection.rangeCount === 0 || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  const node = range.commonAncestorContainer;
  const element = (node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement) as HTMLElement | null;
  let block: HTMLElement | null = null;
  let item: number | null = null;
  let cursor: HTMLElement | null = element;
  while (cursor && cursor !== doc.body) {
    const found = blockMap.byElement.get(cursor);
    if (found != null) {
      block = cursor;
      item = found;
      break;
    }
    cursor = cursor.parentElement;
  }
  if (!block) return null;
  const map = textMapOf(block);
  const indexFor = (container: Node, offset: number, isEnd: boolean) => {
    let best = isEnd ? map.positions.length : 0;
    for (let index = 0; index < map.positions.length; index += 1) {
      const position = map.positions[index];
      if (position.node === container) {
        if (!isEnd && position.offset >= offset) { best = index; break; }
        if (isEnd && position.offset + 1 > offset) { best = index; break; }
      }
    }
    return best;
  };
  let start = indexFor(range.startContainer, range.startOffset, false);
  let end = indexFor(range.endContainer, range.endOffset, true);
  if (end <= start) {
    const raw = normalize(selection.toString());
    const at = map.text.indexOf(raw);
    if (at >= 0) {
      start = at;
      end = at + raw.length;
    }
  }
  let quote = map.text.slice(start, end).trim();
  if (!quote) return null;
  if (quote !== map.text.slice(start, end)) {
    start += map.text.slice(start, end).indexOf(quote);
    end = start + quote.length;
  }
  return {
    item,
    quote,
    pre: map.text.slice(Math.max(0, start - 30), start),
    post: map.text.slice(end, end + 30),
    rect: range.getBoundingClientRect(),
  };
}

export const READER_MARK_CSS = `
.rd-tts-active{background:color-mix(in srgb, var(--rd-fg) 6%, transparent);border-radius:3px}
.rd-tts-sent{background:rgba(250,204,21,.45);border-radius:2px;box-shadow:0 0 0 1px rgba(250,204,21,.25)}
.rd-ann-mark{background:rgba(250,204,21,.28);border-radius:2px;cursor:pointer}
.rd-ann-yellow{background:rgba(250,204,21,.32)}
.rd-ann-green{background:rgba(52,211,153,.30)}
.rd-ann-blue{background:rgba(96,165,250,.30)}
.rd-ann-pink{background:rgba(244,114,182,.30)}
.rd-qhit{background:rgba(96,165,250,.5);border-radius:2px}
.rd-qhit-current{background:rgba(250,204,21,.65);box-shadow:0 0 0 1px rgba(250,204,21,.45)}
.rd-fnmark{background:rgba(45,212,191,.28);border-radius:3px;box-shadow:0 0 0 1px rgba(45,212,191,.4)}
`;

export function clearQueryMarks(doc: Document | null | undefined) {
  clearMarks(doc, "rd-qhit");
  clearMarks(doc, "rd-qhit-current");
}

/** Wrap every occurrence of ``query`` in the current chapter and return the
 *  created spans in document order. Wrapping is done back-to-front per block
 *  so earlier offsets remain valid. */
export function highlightQuery(
  frame: HTMLIFrameElement | null,
  blockMap: BlockMap,
  query: string,
): HTMLElement[] {
  const doc = frame?.contentDocument;
  if (!doc) return [];
  clearQueryMarks(doc);
  const needle = normalize(query).toLowerCase();
  if (!needle) return [];
  const out: HTMLElement[] = [];
  for (const element of blockMap.byItem.values()) {
    const map = textMapOf(element);
    const haystack = map.text.toLowerCase();
    const positions: Array<{ start: number; end: number }> = [];
    let from = 0;
    while (from <= haystack.length) {
      const at = haystack.indexOf(needle, from);
      if (at < 0) break;
      positions.push({ start: at, end: at + needle.length });
      from = at + Math.max(1, needle.length);
    }
    for (const position of positions.sort((a, b) => b.start - a.start)) {
      const range = rangeForSpan(element, position.start, position.end);
      if (!range) continue;
      const span = wrapRange(range, "rd-qhit");
      if (span) out.push(span);
    }
  }
  return out.reverse();
}

export function focusQueryHit(span: HTMLElement) {
  const doc = span.ownerDocument;
  for (const current of Array.from(doc.querySelectorAll<HTMLElement>(".rd-qhit-current"))) {
    current.classList.remove("rd-qhit-current");
  }
  span.classList.add("rd-qhit-current");
  try {
    span.scrollIntoView({ block: "center", behavior: "smooth" });
  } catch {
    span.scrollIntoView();
  }
}

