import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// In development the UI runs on Vite and proxies the API to a running engine
// (`opr serve --port 8765`). In production the engine serves the built files itself.
const engine = process.env.OPR_ENGINE ?? 'http://127.0.0.1:8765';

export default defineConfig({
  root: import.meta.dirname,
  base: './',
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true, sourcemap: false, chunkSizeWarningLimit: 900 },
  server: { port: 5173, proxy: { '/api': { target: engine, changeOrigin: false } } },
});
