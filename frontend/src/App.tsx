import { useEffect } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Link, Route, Routes, useLocation } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { AdvancedConfigPage } from "./features/config/AdvancedConfigPage";
import { ConfigSettingsPage } from "./features/config/ConfigSettingsPage";
import { KeysPage } from "./features/config/KeysPage";
import { DashboardPage } from "./features/dashboard/DashboardPage";
import { IllustrationsPage } from "./features/illustrations/IllustrationsPage";
import { JobPage } from "./features/jobs/JobPage";
import { NewJobPage } from "./features/jobs/NewJobPage";
import { LibraryPage } from "./features/library/LibraryPage";
import { MusicPage } from "./features/music/MusicPage";
import { MusicPlayer } from "./features/music/MusicPlayer";
import { ReaderPage } from "./features/reader/ReaderPage";
import { SettingsPage } from "./features/settings/SettingsPage";
import { ThemeProvider } from "./lib/theme";
import { LanguageProvider, useLanguage } from "./lib/language";
import { translateDocument } from "./lib/copy";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 20_000,
      retry: 1,
    },
  },
});

function NotFound() {
  return (
    <div className="card p-10 text-center">
      <p className="label">404</p>
      <h1 className="mt-2 text-2xl font-bold text-ink">Không tìm thấy trang</h1>
      <Link to="/" className="btn btn-primary mt-5">Về tổng quan</Link>
    </div>
  );
}

function RouteTranslation() {
  const location = useLocation();
  const { locale } = useLanguage();

  // Translate static inline strings whenever the language/route changes. copy.ts
  // strings are already language-aware through the `copy` proxy.
  useEffect(() => {
    if (locale !== "en") return;
    translateDocument();
    const observer = new MutationObserver(() => translateDocument());
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    return () => observer.disconnect();
  }, [location.pathname, locale]);

  return null;
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <LanguageProvider>
        <ThemeProvider>
          <BrowserRouter basename="/v2">
            <RouteTranslation />
            <Routes>
              <Route element={<AppShell />}>
                <Route index element={<DashboardPage />} />
                <Route path="jobs/new" element={<NewJobPage />} />
                <Route path="jobs/:id" element={<JobPage />} />
                <Route path="library" element={<LibraryPage />} />
                <Route path="illustrations" element={<IllustrationsPage />} />
                <Route path="music" element={<MusicPage />} />
                <Route path="keys" element={<KeysPage />} />
                <Route path="config/settings" element={<ConfigSettingsPage />} />
                <Route path="config" element={<AdvancedConfigPage />} />
                <Route path="settings" element={<SettingsPage />} />
                <Route path="*" element={<NotFound />} />
              </Route>
              <Route path="read" element={<ReaderPage />} />
            </Routes>
            <MusicPlayer />
          </BrowserRouter>
        </ThemeProvider>
      </LanguageProvider>
    </QueryClientProvider>
  );
}
