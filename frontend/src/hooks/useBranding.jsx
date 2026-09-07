/**
 * Deployment branding: the name and logo an administrator sets in Settings.
 *
 * Held in a provider rather than fetched per component so the header, the
 * sign-in screen and the browser tab agree, and so the request happens once
 * per page load. It is deliberately tolerant: if the lookup fails the app
 * renders under its built-in name instead of blocking on branding.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'

import { brandingApi } from '../lib/api'

export const DEFAULT_APP_NAME = 'InfraSight'

const BrandingContext = createContext(null)

export function BrandingProvider({ children }) {
  const [branding, setBranding] = useState({
    app_name: DEFAULT_APP_NAME,
    logo_text: '',
  })

  const load = useCallback(
    () =>
      brandingApi
        .get()
        .then((data) => {
          setBranding({
            app_name: (data?.app_name || '').trim() || DEFAULT_APP_NAME,
            logo_text: (data?.logo_text || '').trim(),
          })
        })
        .catch(() => {
          // Keep whatever is showing. A failed branding fetch must never be
          // the reason someone cannot sign in.
        }),
    [],
  )

  useEffect(() => {
    load()
  }, [load])

  // The tab title follows the name, replacing the build-time one in
  // index.html. Two deployments open side by side are then tellable apart.
  useEffect(() => {
    document.title = branding.app_name
  }, [branding.app_name])

  // `refresh` is what the Settings page calls after saving, so the header and
  // tab title change immediately instead of on the next page load.
  const value = useMemo(() => ({ ...branding, refresh: load }), [branding, load])
  return <BrandingContext.Provider value={value}>{children}</BrandingContext.Provider>
}

export function useBranding() {
  // Falling back to the default rather than throwing keeps this usable from
  // components that render outside the provider, such as an error boundary.
  return (
    useContext(BrandingContext) ?? {
      app_name: DEFAULT_APP_NAME,
      logo_text: '',
      refresh: () => Promise.resolve(),
    }
  )
}
