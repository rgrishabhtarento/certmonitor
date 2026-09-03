import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertCircle, ArrowLeft, Check, KeyRound, X } from 'lucide-react'

import { Spinner } from '../components/ui'
import { authApi } from '../lib/api'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'

/** Mirrors the server-side policy so the user gets feedback before submitting. */
function buildRules(minLength) {
  return [
    { id: 'length', label: `At least ${minLength} characters`, test: (v) => v.length >= minLength },
    { id: 'lower', label: 'A lowercase letter', test: (v) => /[a-z]/.test(v) },
    { id: 'upper', label: 'An uppercase letter', test: (v) => /[A-Z]/.test(v) },
    { id: 'digit', label: 'A digit', test: (v) => /[0-9]/.test(v) },
    { id: 'special', label: 'A special character', test: (v) => /[^A-Za-z0-9]/.test(v) },
  ]
}

export default function ChangePassword() {
  const { mustChangePassword, changePassword, user, logout } = useAuth()
  const navigate = useNavigate()
  const toast = useToast()

  const [minLength, setMinLength] = useState(10)
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    authApi
      .passwordPolicy()
      .then((policy) => setMinLength(policy.min_length || 10))
      .catch(() => {})
  }, [])

  const rules = useMemo(() => buildRules(minLength), [minLength])
  const failing = rules.filter((rule) => !rule.test(next))
  const mismatch = confirm.length > 0 && next !== confirm
  const canSubmit =
    current.length > 0 && next.length > 0 && !failing.length && !mismatch && !busy

  const submit = async (event) => {
    event.preventDefault()
    setError(null)
    if (next !== confirm) {
      setError('The new passwords do not match.')
      return
    }
    setBusy(true)
    try {
      await changePassword(current, next)
      toast.success('Password changed. Your other sessions have been signed out.')
      navigate('/', { replace: true })
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid min-h-screen place-items-center bg-slate-100 px-4 py-10 dark:bg-slate-950">
      <div className="w-full max-w-md">
        <div className="mb-5 flex flex-col items-center gap-2 text-center">
          <span className="grid h-11 w-11 place-items-center rounded-xl bg-brand-600 text-white">
            <KeyRound size={21} />
          </span>
          <h1 className="text-lg font-semibold text-slate-900 dark:text-slate-50">
            {mustChangePassword ? 'Choose a new password' : 'Change your password'}
          </h1>
          {mustChangePassword ? (
            <p className="max-w-sm text-sm text-slate-600 dark:text-slate-400">
              This account is still using its initial password. Set a new one to
              continue.
            </p>
          ) : (
            <p className="text-sm text-slate-500 dark:text-slate-400">
              Signed in as <span className="font-medium">{user?.username}</span>
            </p>
          )}
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
            <label htmlFor="current" className="label">
              Current password
            </label>
            <input
              id="current"
              type="password"
              className="input"
              value={current}
              onChange={(event) => setCurrent(event.target.value)}
              autoComplete="current-password"
              required
              autoFocus
              disabled={busy}
            />
          </div>

          <div>
            <label htmlFor="next" className="label">
              New password
            </label>
            <input
              id="next"
              type="password"
              className="input"
              value={next}
              onChange={(event) => setNext(event.target.value)}
              autoComplete="new-password"
              required
              disabled={busy}
            />
            <ul className="mt-2 space-y-1">
              {rules.map((rule) => {
                const ok = rule.test(next)
                return (
                  <li
                    key={rule.id}
                    className={`flex items-center gap-1.5 text-xs ${
                      ok
                        ? 'text-green-600 dark:text-green-400'
                        : 'text-slate-500 dark:text-slate-400'
                    }`}
                  >
                    {ok ? <Check size={13} /> : <X size={13} className="opacity-50" />}
                    {rule.label}
                  </li>
                )
              })}
            </ul>
          </div>

          <div>
            <label htmlFor="confirm" className="label">
              Confirm new password
            </label>
            <input
              id="confirm"
              type="password"
              className="input"
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              autoComplete="new-password"
              required
              disabled={busy}
            />
            {mismatch ? <p className="field-error">The passwords do not match.</p> : null}
          </div>

          <button type="submit" className="btn-primary w-full" disabled={!canSubmit}>
            {busy ? <Spinner size={16} className="text-white" /> : null}
            Update password
          </button>

          <div className="flex justify-center pt-1">
            {mustChangePassword ? (
              <button type="button" className="btn-ghost btn-sm" onClick={logout}>
                Sign out instead
              </button>
            ) : (
              <button
                type="button"
                className="btn-ghost btn-sm"
                onClick={() => navigate(-1)}
              >
                <ArrowLeft size={14} /> Back
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  )
}
