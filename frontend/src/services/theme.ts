/** Appearance: the original dark design, a light theme, or follow the operating system. */
export type ThemeChoice = 'dark' | 'light' | 'system'

const KEY = 'ldt.theme'

export function themeChoice(): ThemeChoice {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'light' || v === 'system' ? v : 'dark'
  } catch {
    return 'dark'
  }
}

export function applyTheme(choice: ThemeChoice = themeChoice()): void {
  const light = choice === 'light' || (choice === 'system' && window.matchMedia?.('(prefers-color-scheme: light)').matches)
  document.documentElement.dataset.theme = light ? 'light' : 'dark'
}

export function setTheme(choice: ThemeChoice): void {
  try {
    localStorage.setItem(KEY, choice)
  } catch {
    /* storage blocked: applies for this page view only */
  }
  applyTheme(choice)
}

if (typeof window !== 'undefined') {
  window.matchMedia?.('(prefers-color-scheme: light)').addEventListener?.('change', () => applyTheme())
}
