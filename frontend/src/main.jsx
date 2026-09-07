import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import App from './App'
import { AuthProvider } from './hooks/useAuth'
import { BrandingProvider } from './hooks/useBranding'
import { FeaturesProvider } from './hooks/useFeatures'
import { ToastProvider } from './hooks/useToast'
import './index.css'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <BrowserRouter>
      <ToastProvider>
        <BrandingProvider>
          <AuthProvider>
            <FeaturesProvider>
              <App />
            </FeaturesProvider>
          </AuthProvider>
        </BrandingProvider>
      </ToastProvider>
    </BrowserRouter>
  </StrictMode>,
)
