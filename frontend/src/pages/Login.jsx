import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import {
  Activity,
  AlertCircle,
  ClipboardList,
  Eye,
  EyeOff,
  Lock,
  Moon,
  Radar,
  ShieldCheck,
  Stethoscope,
  Sun,
  User,
} from 'lucide-react'
import clsx from 'clsx'

import { Spinner } from '../components/ui'
import { healthApi } from '../lib/api'
import { useAuth } from '../hooks/useAuth'

const THEME_KEY = 'infrasight.theme'

/**
 * Sign-in.
 *
 * Two decisions shape this page.
 *
 * **It shows the system's own health before you type.** InfraSight is a
 * monitoring tool; if its API is down, the useful thing is to say so up front
 * rather than let someone enter credentials and blame themselves for a
 * failure that was never theirs. The indicator reads the public `/health`
 * probe - the same one the dashboard uses.
 *
 * **It handles the states a real sign-in actually hits.** Caps Lock, a locked
 * account, an unreachable backend and a wrong password are four different
 * problems, and one "invalid credentials" for all of them wastes the
 * operator's time. What it will *not* do is distinguish an unknown user from
 * a wrong password - that is deliberate, and the API refuses to as well.
 */

const CAPABILITIES = [
  {
    icon: Radar,
    title: 'Endpoint & SSL monitoring',
    detail: 'Real checks on a schedule, with per-phase timings and certificate expiry.',
  },
  {
    icon: ClipboardList,
    title: 'Change management',
    detail: 'Deployments pause monitoring for exactly the endpoints they touch.',
  },
  {
    icon: Stethoscope,
    title: 'Smart diagnosis',
    detail: 'Layer-by-layer investigation with ranked causes and the evidence behind each.',
  },
  {
    icon: ShieldCheck,
    title: 'Root cause analysis',
    detail: 'Optional, never blocking, and draftable from data already collected.',
  },
]

function useTheme() {
  const [theme, setTheme] = useState(() => {
    try {
      return (
        localStorage.getItem(THEME_KEY) ||
        // Pre-rename key, so an upgrade does not reset the chosen theme.
        localStorage.getItem('certmonitor.theme') ||
        (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
      )
    } catch {
      return 'light'
    }
  })

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    try {
      localStorage.setItem(THEME_KEY, theme)
    } catch {
      /* ignore */
    }
  }, [theme])

  return [theme, () => setTheme((value) => (value === 'dark' ? 'light' : 'dark'))]
}

/** The public health probe, so a down backend is visible before sign-in. */
function SystemStatus() {
  const [health, setHealth] = useState(undefined)

  useEffect(() => {
    let cancelled = false
    healthApi
      .health()
      .then((data) => !cancelled && setHealth(data))
      .catch(() => !cancelled && setHealth(null))
    return () => {
      cancelled = true
    }
  }, [])

  if (health === undefined) return null

  const reachable = Boolean(health)
  const status = health?.status
  const healthy = reachable && status === 'healthy'

  return (
    <p
      className={clsx(
        'flex items-center justify-center gap-1.5 text-center text-xs',
        !reachable
          ? 'text-red-700 dark:text-red-300'
          : healthy
            ? 'text-slate-500 dark:text-slate-400'
            : 'text-amber-700 dark:text-amber-300',
      )}
      role="status"
    >
      <span
        className={clsx(
          'h-1.5 w-1.5 shrink-0 rounded-full',
          !reachable ? 'bg-red-500' : healthy ? 'bg-green-500' : 'bg-amber-500',
        )}
        aria-hidden="true"
      />
      {!reachable
        ? 'The API is not responding — signing in will fail until it recovers.'
        : healthy
          ? 'All systems operational'
          : `System ${status} — worker ${health.monitoring_worker || 'unknown'}, database ${health.database || 'unknown'}`}
    </p>
  )
}

export default function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [theme, toggleTheme] = useTheme()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [capsLock, setCapsLock] = useState(false)
  const [error, setError] = useState(null)
  const [errorStatus, setErrorStatus] = useState(null)
  const [busy, setBusy] = useState(false)
  const passwordRef = useRef(null)

  const submit = async (event) => {
    event.preventDefault()
    setError(null)
    setErrorStatus(null)
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
      setErrorStatus(err.status)
      setPassword('')
      // Put the caret back where the correction has to happen.
      requestAnimationFrame(() => passwordRef.current?.focus())
    } finally {
      setBusy(false)
    }
  }

  // 423 is a lockout and 429 a rate limit: both are waiting games rather than
  // typos, so they get calmer styling than a plain rejection.
  const isThrottled = errorStatus === 423 || errorStatus === 429

  return (
    <div className="min-h-screen bg-white dark:bg-slate-950 lg:grid lg:grid-cols-[1.1fr_1fr]">
      {/* ================================================= brand panel */}
      <aside className="relative hidden overflow-hidden bg-slate-900 p-10 text-white lg:flex lg:flex-col">
        {/* Depth without motion - nothing here competes with the form, and
            nothing moves for anyone who asked that it not. */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 opacity-[0.18]"
          style={{
            backgroundImage:
              'radial-gradient(circle at 18% 22%, #3b82f6 0, transparent 42%), radial-gradient(circle at 82% 78%, #1d4ed8 0, transparent 46%)',
          }}
        />
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 opacity-[0.05]"
          style={{
            backgroundImage:
              'linear-gradient(to right, #fff 1px, transparent 1px), linear-gradient(to bottom, #fff 1px, transparent 1px)',
            backgroundSize: '44px 44px',
          }}
        />

        <div className="relative flex items-center gap-2.5">
          <span className="grid h-10 w-10 place-items-center rounded-xl bg-brand-600 shadow-lg shadow-brand-900/40">
            <Activity size={21} />
          </span>
          <span className="text-lg font-semibold tracking-tight">InfraSight</span>
        </div>

        <div className="relative my-auto max-w-md py-10">
          <h2 className="text-3xl font-semibold leading-tight tracking-tight">
            Know what broke, why, and what to do about it.
          </h2>
          <p className="mt-3 text-[15px] leading-relaxed text-slate-300">
            Endpoint and certificate monitoring for teams who would rather
            investigate from evidence than from a red square on a wall.
          </p>

          <ul className="mt-8 space-y-4">
            {CAPABILITIES.map((item) => (
              <li key={item.title} className="flex gap-3">
                <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-white/10">
                  <item.icon size={16} aria-hidden="true" />
                </span>
                <span>
                  <span className="block text-sm font-medium">{item.title}</span>
                  <span className="block text-sm leading-snug text-slate-400">
                    {item.detail}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </div>

        <p className="relative text-xs text-slate-500">
          All analysis runs on this server. No external AI, and no
          infrastructure data leaves your network.
        </p>
      </aside>

      {/* ================================================== form panel */}
      <main className="flex min-h-screen flex-col px-5 py-8 sm:px-8 lg:min-h-0 lg:justify-center">
        <div className="flex items-center justify-between lg:justify-end">
          {/* The mark repeats here because the brand panel is hidden below
              lg, where this column is the whole page. */}
          <span className="flex items-center gap-2 lg:hidden">
            <span className="grid h-9 w-9 place-items-center rounded-xl bg-brand-600 text-white">
              <Activity size={19} />
            </span>
            <span className="text-base font-semibold text-slate-900 dark:text-slate-50">
              InfraSight
            </span>
          </span>
          <button
            type="button"
            onClick={toggleTheme}
            className="rounded-lg p-2 text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-900 dark:hover:bg-slate-800 dark:hover:text-slate-100"
            aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          >
            {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
          </button>
        </div>

        <div className="mx-auto my-auto w-full max-w-[22rem] py-10">
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
            Sign in
          </h1>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Use your InfraSight account to continue.
          </p>

          <form onSubmit={submit} className="mt-7 space-y-4" noValidate>
            {/* aria-live, so a screen reader announces the failure instead of
                the user having to go looking for it. */}
            <div aria-live="polite">
              {error ? (
                <div
                  role="alert"
                  className={clsx(
                    'mb-4 flex items-start gap-2 rounded-lg border px-3 py-2.5 text-sm',
                    isThrottled
                      ? 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/50 dark:text-amber-200'
                      : 'border-red-200 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/50 dark:text-red-200',
                  )}
                >
                  <AlertCircle size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
                  <span>{error}</span>
                </div>
              ) : null}
            </div>

            <div>
              <label htmlFor="username" className="label">
                Username
              </label>
              <div className="relative">
                <User
                  size={15}
                  className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                  aria-hidden="true"
                />
                <input
                  id="username"
                  name="username"
                  className="input pl-9"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  autoComplete="username"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck="false"
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
                  className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
                  aria-hidden="true"
                />
                <input
                  id="password"
                  name="password"
                  ref={passwordRef}
                  type={showPassword ? 'text' : 'password'}
                  className="input px-9"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  onKeyUp={(event) =>
                    setCapsLock(Boolean(event.getModifierState?.('CapsLock')))
                  }
                  onBlur={() => setCapsLock(false)}
                  autoComplete="current-password"
                  required
                  disabled={busy}
                  aria-describedby={capsLock ? 'caps-warning' : undefined}
                />
                <button
                  type="button"
                  className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1.5 text-slate-400 transition-colors hover:text-slate-700 dark:hover:text-slate-200"
                  onClick={() => setShowPassword((value) => !value)}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  aria-pressed={showPassword}
                  tabIndex={-1}
                >
                  {showPassword ? <EyeOff size={15} /> : <Eye size={15} />}
                </button>
              </div>
              {/* A silent Caps Lock is the most common cause of a "wrong
                  password" that is not actually wrong. */}
              {capsLock ? (
                <p
                  id="caps-warning"
                  className="mt-1.5 text-xs text-amber-700 dark:text-amber-400"
                >
                  Caps Lock is on.
                </p>
              ) : null}
            </div>

            <button
              type="submit"
              className="btn-primary h-10 w-full"
              disabled={busy || !username.trim() || !password}
            >
              {busy ? <Spinner size={16} className="text-white" /> : null}
              {busy ? 'Signing in…' : 'Sign in'}
            </button>
          </form>

          <div className="mt-8 space-y-2 border-t border-slate-200 pt-5 dark:border-slate-800">
            <SystemStatus />
            <p className="text-center text-xs text-slate-400 dark:text-slate-500">
              Repeated failed attempts temporarily lock the account. An
              administrator can unlock it or reset your password.
            </p>
          </div>
        </div>
      </main>
    </div>
  )
}
