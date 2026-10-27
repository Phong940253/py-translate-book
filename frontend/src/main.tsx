import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// PWA: the service worker controls /v2/ only.  Register on loopback or HTTPS;
// plain-HTTP LAN origins cannot install a service worker, but the manifest
// still allows adding the app to a home screen.
if (import.meta.env.PROD && "serviceWorker" in navigator) {
  const host = window.location.hostname;
  const secure = window.location.protocol === "https:" || host === "localhost" || host === "127.0.0.1" || host === "[::1]";
  if (secure) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/v2/sw.js", { scope: "/v2/" }).catch(() => undefined);
    });
  }
}
