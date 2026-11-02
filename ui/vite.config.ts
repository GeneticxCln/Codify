import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
  },
  envPrefix: ["VITE_", "TAURI_"],
  build: {
    target: "esnext",
    minify: !process.env.TAURI_DEBUG ? "esbuild" : false,
    sourcemap: !!process.env.TAURI_DEBUG,
    rollupOptions: {
      output: {
        // The single entry chunk was 901 kB: React, icons, the editor and xterm all rode in one file, so
        // the first paint waited for all of them. These groups are vendor-only; the app's own code stays in
        // the entry chunk, and each group can be cached independently when one dependency moves.
        manualChunks: (id: string) => {
          if (id.includes("node_modules")) {
            // `lucide-react` before `react`, or the icon package matches the substring and rides
            // in the framework chunk.
            if (id.includes("lucide-react")) return "icons";
            if (id.includes("react") || id.includes("scheduler")) return "react";
            if (id.includes("@tauri-apps")) return "tauri";
            return undefined;
          }
          if (id.includes("/src/components/ChatTimeline")) return "timeline";
          if (id.includes("/src/components/Settings")) return "settings";
          if (id.includes("/src/components/")) return "components";
          return undefined;
        },
      },
    },
  },
});
