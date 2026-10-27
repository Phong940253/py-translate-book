import { createContext, useCallback, useContext, useEffect, useState } from "react";

const STORAGE_KEY = "v2.lang";

export type Locale = "vi" | "en";

export const LOCALES: Record<Locale, string> = { vi: "Tiếng Việt", en: "English" };

export function readLocale(): Locale {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    if (value === "vi" || value === "en") return value;
  } catch {
    // private mode / quota
  }
  return "vi";
}

export function writeLocale(value: Locale) {
  try { localStorage.setItem(STORAGE_KEY, value); } catch { /* ignore */ }
}

export function localeLabel(locale: Locale) {
  return LOCALES[locale];
}

type LanguageContextValue = {
  locale: Locale;
  setLanguage: (next: Locale) => void;
};

const LanguageContext = createContext<LanguageContextValue | null>(null);

export function LanguageProvider({ children }: { children: React.ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(readLocale);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLanguage = useCallback((next: Locale) => {
    writeLocale(next);
    // Reload keeps the implementation simple and guarantees every rendered
    // string (including copy.ts) switches language at once.
    window.location.reload();
  }, []);

  return (
    <LanguageContext.Provider value={{ locale, setLanguage }}>
      {children}
    </LanguageContext.Provider>
  );
}

export function useLanguage() {
  const value = useContext(LanguageContext);
  if (!value) throw new Error("useLanguage must be used inside LanguageProvider");
  return value;
}
