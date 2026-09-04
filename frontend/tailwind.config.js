/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Semantic status colours, used everywhere a health state is shown so
        // "green" means the same thing on every screen.
        up: { DEFAULT: '#16a34a', soft: '#dcfce7', dark: '#166534' },
        down: { DEFAULT: '#dc2626', soft: '#fee2e2', dark: '#991b1b' },
        warn: { DEFAULT: '#d97706', soft: '#fef3c7', dark: '#92400e' },
        unknown: { DEFAULT: '#64748b', soft: '#f1f5f9', dark: '#334155' },
        brand: {
          50: '#eff6ff',
          100: '#dbeafe',
          200: '#bfdbfe',
          300: '#93c5fd',
          400: '#60a5fa',
          500: '#3b82f6',
          600: '#2563eb',
          700: '#1d4ed8',
          800: '#1e40af',
          900: '#1e3a8a',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      boxShadow: {
        // Slightly deeper than a hairline now that cards sit on a tinted
        // ground rather than white - enough to read as raised, not enough to
        // draw attention to itself.
        card: '0 1px 2px 0 rgb(15 23 42 / 0.05), 0 2px 8px -2px rgb(15 23 42 / 0.08)',
      },
      animation: {
        'pulse-slow': 'pulse 2.5s cubic-bezier(0.4, 0, 0.6, 1) infinite',
      },
    },
  },
  plugins: [],
}
