import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

import {
  authApi,
  setPasswordChangeHandler,
  setSessionExpiredHandler,
  tokenStore,
} from '../lib/api'

const AuthContext = createContext(null)

/**
 * Holds the session and exposes permission checks.
 *
 * Permissions come from the server with the user object; the UI uses them only
 * to hide controls a viewer cannot use. Every action is still authorised
 * server-side - hiding a button is a courtesy, not a security boundary.
 */
export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => tokenStore.user)
  const [loading, setLoading] = useState(() => Boolean(tokenStore.access))
  const [mustChangePassword, setMustChangePassword] = useState(
    () => tokenStore.user?.must_change_password ?? false,
  )

  const signOutLocal = useCallback(() => {
    tokenStore.clear()
    setUser(null)
    setMustChangePassword(false)
  }, [])

  useEffect(() => {
    setSessionExpiredHandler(signOutLocal)
    setPasswordChangeHandler(() => setMustChangePassword(true))
  }, [signOutLocal])

  // Re-validate the stored token on load: it may have expired, or the user's
  // role may have changed since it was issued.
  useEffect(() => {
    let cancelled = false
    if (!tokenStore.access) {
      setLoading(false)
      return () => {
        cancelled = true
      }
    }
    authApi
      .me()
      .then((me) => {
        if (cancelled) return
        setUser(me)
        setMustChangePassword(Boolean(me.must_change_password))
        tokenStore.save({ user: me })
      })
      .catch(() => {
        if (!cancelled) signOutLocal()
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [signOutLocal])

  const login = useCallback(async (username, password) => {
    const data = await authApi.login(username, password)
    tokenStore.save(data)
    setUser(data.user)
    setMustChangePassword(Boolean(data.must_change_password))
    return data
  }, [])

  const logout = useCallback(async () => {
    try {
      await authApi.logout()
    } catch {
      // Signing out locally matters more than recording it server-side.
    }
    signOutLocal()
  }, [signOutLocal])

  const changePassword = useCallback(async (currentPassword, newPassword) => {
    const data = await authApi.changePassword(currentPassword, newPassword)
    tokenStore.save(data)
    setUser(data.user)
    setMustChangePassword(false)
    return data
  }, [])

  const value = useMemo(() => {
    const permissions = new Set(user?.permissions || [])
    return {
      user,
      loading,
      mustChangePassword,
      isAuthenticated: Boolean(user),
      isAdmin: user?.role === 'admin',
      can: (code) => permissions.has(code),
      canAny: (...codes) => codes.some((code) => permissions.has(code)),
      login,
      logout,
      changePassword,
    }
  }, [user, loading, mustChangePassword, login, logout, changePassword])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used inside an AuthProvider')
  }
  return context
}
