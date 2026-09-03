import { useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { Activity, AlertCircle, Eye, EyeOff, Lock, User } from 'lucide-react'

import { Spinner } from '../components/ui'
import { useAuth } from '../hooks/useAuth'

export default function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event) => {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const data = await login(username.trim(), password)
      // A first sign-in with the default credential lands on the password
      // screen; everything else returns to wherever the user was headed.
      if (data.must_change_password) {
        navigate('/change-password', { replace: true })
      } else {
        navigate(location.state?.from || '/', { replace: true })
      }
    } catch (err) {
      setError(err.message)
      setPassword('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid min-h-screen place-items-center bg-slate-100 px-4 py-10 dark:bg-slate-950">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-2 text-center">
          <span className="grid h-12 w-12 place-items-center rounded-xl bg-brand-600 text-white shadow-lg">
            <Activity size={24} />
          </span>
          <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
            CertMonitor
          </h1>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Endpoint and SSL certificate monitoring
          </p>
        </div>

        <form onSubmit={submit} className="card space-y-4 p-5">
          {error ? (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/50 dark:text-red-200"
            >
              <AlertCircle size={16} className="mt-0.5 shrink-0" />
              <span>{error}</span>
            </div>
          ) : null}

          <div>
            <label htmlFor="username" className="label">
              Username
            </label>
            <div className="relative">
              <User
                size={15}
                className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400"
              />
              <input
                id="username"
                name="username"
                className="input pl-8"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="username"
                autoFocus
                required
                disabled={busy}
              />
            </div>
          </div>

          <div>
            <label htmlFor="password" className="label">
              Password
            </label>
            <div className="relative">
              <Lock
                size={15}
                className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400"
              />
              <input
                id="password"
                name="password"
                type={showPassword ? 'text' : 'password'}
                className="input px-8"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                required
                disabled={busy}
              />
              <button
                type="button"
                className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-slate-400 hover:text-slate-600"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
                tabIndex={-1}
              >
                {showPassword ? <EyeOff size={15} /> : <Eye size={15} />}
              </button>
            </div>
          </div>

          <button type="submit" className="btn-primary w-full" disabled={busy}>
            {busy ? <Spinner size={16} className="text-white" /> : null}
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <p className="mt-4 text-center text-xs text-slate-400 dark:text-slate-500">
          Repeated failed attempts temporarily lock the account.
        </p>
      </div>
    </div>
  )
}
