import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/ibm-plex-mono/400.css'
import './styles/tokens.css'
import './styles/global.css'
import './styles/ui.css'
import './styles/shell.css'
import './styles/pages.css'
import './styles/twin.css'
import './styles/anomaly.css'
import { applyTheme } from './services/theme'

applyTheme()

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import { ErrorBoundary } from './components/ErrorBoundary'

createRoot(document.getElementById('root') as HTMLElement).render(
  <StrictMode>
    <ErrorBoundary label="Application">
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
