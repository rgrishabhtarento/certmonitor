/**
 * Which optional modules an administrator has switched on.
 *
 * Read from its own endpoint rather than from the settings payload, because
 * the navigation needs it for every role and most roles hold no
 * settings:read. Fetched once the user is authenticated, and re-fetched when
 * the identity changes so a sign-out/sign-in picks up a change.
 *
 * Everything defaults to enabled: a failed lookup should leave the app as it
 * was, not hide half of it.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'

import { settingsApi } from '../lib/api'
import { useAuth } from './useAuth'

const DEFAULTS = { change_management: true, rca: true }

const FeaturesContext = createContext(null)

export function FeaturesProvider({ children }) {
  const { isAuthenticated, user } = useAuth()
  const [features, setFeatures] = useState(DEFAULTS)

  const load = useCallback(() => {
    if (!isAuthenticated) {
      setFeatures(DEFAULTS)
      return Promise.resolve()
    }
    return settingsApi
      .features()
      .then((data) =>
        setFeatures({
          change_management: data?.change_management !== false,
          rca: data?.rca !== false,
        }),
      )
      .catch(() => {
        // Leave the current values. Hiding modules because one request failed
        // would be worse than showing a module whose API then refuses.
      })
  }, [isAuthenticated])

  useEffect(() => {
    load()
  }, [load, user?.id])

  const value = useMemo(() => ({ ...features, refresh: load }), [features, load])
  return <FeaturesContext.Provider value={value}>{children}</FeaturesContext.Provider>
}

export function useFeatures() {
  return useContext(FeaturesContext) ?? { ...DEFAULTS, refresh: () => Promise.resolve() }
}
