const LAST_BOOK_KEY = "v2.lastBook";

export function rememberBook(path: string) {
  try {
    localStorage.setItem(LAST_BOOK_KEY, path);
  } catch {
    // ignore private mode
  }
}

export function lastBookPath() {
  try {
    return localStorage.getItem(LAST_BOOK_KEY) || "";
  } catch {
    return "";
  }
}

export function legacyReaderHref() {
  const path = lastBookPath();
  return path ? `/read?path=${encodeURIComponent(path)}` : "/library";
}
