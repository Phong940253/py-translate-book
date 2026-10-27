import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type Theme = "light" | "sepia" | "dark" | "black" | "system" | "custom";

const THEMES: Theme[] = ["light", "sepia", "dark", "black", "system", "custom"];
const STORAGE_KEY = "theme";
const BG_KEY = "theme.bg";
const FG_KEY = "theme.fg";
const CUSTOM_BG = "#101418";
const CUSTOM_FG = "#d7dbe0";

const CUSTOM_VARS = [
  "--color-bg",
  "--color-surface",
  "--color-surface-2",
  "--color-surface-3",
  "--color-line",
  "--color-line-strong",
  "--color-ink",
  "--color-muted",
  "--color-faint",
  "--color-accent",
  "--color-accent-soft",
  "--color-accent-ink",
];

type ThemeContextValue = {
  theme: Theme;
  resolvedTheme: string;
  setTheme: (theme: Theme) => void;
  cycleTheme: () => void;
  customBg: string;
  customFg: string;
  setCustomColors: (bg: string, fg: string) => void;
};

const ThemeContext = createContext<ThemeContextValue | null>(null);

function read(key: string, fallback: string) {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}

function validTheme(value: string | null): value is Theme {
  return Boolean(value && THEMES.includes(value as Theme));
}

function normalizeHex(value: string, fallback: string) {
  return /^#[0-9a-f]{6}$/i.test(value) ? value : fallback;
}

function rgb(value: string) {
  const hex = normalizeHex(value, "#000000").slice(1);
  return [0, 2, 4].map((offset) => parseInt(hex.slice(offset, offset + 2), 16));
}

function mix(a: string, b: string, weight: number) {
  const first = rgb(a);
  const second = rgb(b);
  const channels = first.map((value, index) => Math.round(value + (second[index] - value) * weight));
  return `#${channels.map((value) => Math.max(0, Math.min(255, value)).toString(16).padStart(2, "0")).join("")}`;
}

function luminance(value: string) {
  const [red, green, blue] = rgb(value).map((channel) => {
    const normalized = channel / 255;
    return normalized <= 0.03928 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (validTheme(saved)) return saved;
  } catch {
    // storage can be unavailable in private mode
  }
  return "system";
}

function initialSystemDark() {
  try {
    return !window.matchMedia?.("(prefers-color-scheme: light)").matches;
  } catch {
    return true;
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(initialTheme);
  const [systemDark, setSystemDark] = useState(initialSystemDark);
  const [customBg, setCustomBgState] = useState(() => normalizeHex(read(BG_KEY, CUSTOM_BG), CUSTOM_BG));
  const [customFg, setCustomFgState] = useState(() => normalizeHex(read(FG_KEY, CUSTOM_FG), CUSTOM_FG));

  useEffect(() => {
    let media: MediaQueryList | null = null;
    try {
      media = window.matchMedia("(prefers-color-scheme: dark)");
    } catch {
      return;
    }
    const update = () => setSystemDark(media!.matches);
    update();
    media.addEventListener?.("change", update);
    return () => media?.removeEventListener?.("change", update);
  }, []);

  const resolvedTheme = theme === "system" ? (systemDark ? "dark" : "light") : theme;

  useEffect(() => {
    const root = document.documentElement;
    for (const name of CUSTOM_VARS) root.style.removeProperty(name);

    if (theme === "custom") {
      const light = luminance(customBg) > 0.5;
      const surface = mix(customBg, "#ffffff", light ? 0.54 : 0.06);
      const surface2 = mix(customBg, "#ffffff", light ? 0.36 : 0.11);
      const surface3 = mix(customBg, light ? "#000000" : "#ffffff", light ? 0.07 : 0.17);
      const line = mix(customBg, light ? "#000000" : "#ffffff", light ? 0.13 : 0.22);
      const lineStrong = mix(customBg, light ? "#000000" : "#ffffff", light ? 0.25 : 0.34);
      const accent = light ? "#0f9f8f" : "#2dd4bf";
      const values: Record<string, string> = {
        "--color-bg": customBg,
        "--color-surface": surface,
        "--color-surface-2": surface2,
        "--color-surface-3": surface3,
        "--color-line": line,
        "--color-line-strong": lineStrong,
        "--color-ink": customFg,
        "--color-muted": mix(customFg, customBg, 0.36),
        "--color-faint": mix(customFg, customBg, 0.55),
        "--color-accent": accent,
        "--color-accent-soft": mix(customBg, accent, light ? 0.17 : 0.24),
        "--color-accent-ink": light ? "#ffffff" : "#04211d",
      };
      for (const [name, value] of Object.entries(values)) root.style.setProperty(name, value);
    }

    root.dataset.theme = resolvedTheme;
    root.style.colorScheme = resolvedTheme === "light" || resolvedTheme === "sepia" ? "light" : "dark";
    try {
      localStorage.setItem(STORAGE_KEY, theme);
      localStorage.setItem(BG_KEY, customBg);
      localStorage.setItem(FG_KEY, customFg);
    } catch {
      // ignore
    }
  }, [customBg, customFg, resolvedTheme, theme]);

  const value = useMemo<ThemeContextValue>(() => ({
    theme,
    resolvedTheme,
    setTheme: setThemeState,
    cycleTheme: () => setThemeState((current) => THEMES[(THEMES.indexOf(current) + 1) % THEMES.length]),
    customBg,
    customFg,
    setCustomColors: (bg, fg) => {
      setCustomBgState(normalizeHex(bg, customBg));
      setCustomFgState(normalizeHex(fg, customFg));
    },
  }), [customBg, customFg, resolvedTheme, theme]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used inside ThemeProvider");
  return value;
}
