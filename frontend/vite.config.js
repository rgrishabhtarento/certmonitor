import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server proxies /api to the backend so the browser sees a single
// origin - the same topology nginx provides in production. That keeps cookies,
// CORS and relative URLs behaving identically in both environments.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://localhost:8000',
        changeOrigin: true,
      },
      '/health': { target: process.env.VITE_API_TARGET || 'http://localhost:8000' },
      '/ready': { target: process.env.VITE_API_TARGET || 'http://localhost:8000' },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // Charts are heavy and only needed on two screens; splitting them
        // keeps the initial bundle small.
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          charts: ['recharts'],
        },
      },
    },
  },
})
