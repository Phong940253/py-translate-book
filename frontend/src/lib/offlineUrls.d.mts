export function docUrl(path: string, n: number): string;
export function readerApiUrl(pathname: string, path: string, extra?: Record<string, string>): string;
export function coverUrl(path: string): string;
export function decodeHtmlUrl(url: string): string;
export function extractResUrls(html: string): string[];
export function extractCssResUrls(css: string): string[];
export function withPath(url: string, path: string): string;
export function urlBelongsToBook(url: string, path: string): boolean;
