import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{vue,js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        'bg-primary': 'rgb(var(--bg-primary) / <alpha-value>)',
        'bg-secondary': 'rgb(var(--bg-secondary) / <alpha-value>)',
        'bg-tertiary': 'rgb(var(--bg-tertiary) / <alpha-value>)',
        'bg-elevated': 'rgb(var(--bg-elevated) / <alpha-value>)',
        'bg-hover': 'rgb(var(--bg-hover) / <alpha-value>)',
        'border-subtle': 'rgb(var(--border-subtle) / <alpha-value>)',
        'border-default': 'rgb(var(--border-default) / <alpha-value>)',
        'border-strong': 'rgb(var(--border-strong) / <alpha-value>)',
        'text-primary': 'rgb(var(--text-primary) / <alpha-value>)',
        'text-secondary': 'rgb(var(--text-secondary) / <alpha-value>)',
        'text-muted': 'rgb(var(--text-muted) / <alpha-value>)',
        'text-inverted': 'rgb(var(--text-inverted) / <alpha-value>)',
        accent: 'rgb(var(--accent) / <alpha-value>)',
        'accent-hover': 'rgb(var(--accent-hover) / <alpha-value>)',
        'accent-muted': 'rgb(var(--accent-muted) / <alpha-value>)',
        'accent-fg': 'rgb(var(--accent-fg) / <alpha-value>)',
        success: 'rgb(var(--success) / <alpha-value>)',
        'success-muted': 'rgb(var(--success-muted) / <alpha-value>)',
        warning: 'rgb(var(--warning) / <alpha-value>)',
        'warning-muted': 'rgb(var(--warning-muted) / <alpha-value>)',
        danger: 'rgb(var(--danger) / <alpha-value>)',
        'danger-muted': 'rgb(var(--danger-muted) / <alpha-value>)',
        info: 'rgb(var(--info) / <alpha-value>)',
        'info-muted': 'rgb(var(--info-muted) / <alpha-value>)',
      },
      boxShadow: {
        'soft-sm': '0 1px 2px 0 rgb(var(--shadow-color) / .05)',
        'soft-md': '0 2px 6px -1px rgb(var(--shadow-color) / .08), 0 1px 4px -1px rgb(var(--shadow-color) / .04)',
        'soft-lg': '0 12px 32px -12px rgb(var(--shadow-color) / .18)',
      },
      fontFamily: {
        sans: ['Inter', 'SF Pro Display', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'ui-monospace', 'SFMono-Regular', 'Consolas', 'monospace'],
      },
    },
  },
  plugins: [],
} satisfies Config
