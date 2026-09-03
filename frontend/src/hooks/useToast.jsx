import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from 'lucide-react'

const ToastContext = createContext(null)

const ICONS = {
  success: CheckCircle2,
  error: XCircle,
  warning: AlertTriangle,
  info: Info,
}

const STYLES = {
  success: 'border-green-200 bg-green-50 text-green-900 dark:border-green-900 dark:bg-green-950 dark:text-green-100',
  error: 'border-red-200 bg-red-50 text-red-900 dark:border-red-900 dark:bg-red-950 dark:text-red-100',
  warning: 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100',
  info: 'border-slate-200 bg-white text-slate-900 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100',
}

let nextId = 1

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])

  const dismiss = useCallback((id) => {
    setToasts((current) => current.filter((toast) => toast.id !== id))
  }, [])

  const push = useCallback(
    (message, { type = 'info', duration = 5000, title } = {}) => {
      const id = nextId++
      setToasts((current) => [...current.slice(-4), { id, message, type, title }])
      // Errors stay until dismissed: an operator should not miss the reason a
      // change was rejected because a toast timed out.
      if (duration > 0 && type !== 'error') {
        setTimeout(() => dismiss(id), duration)
      }
      return id
    },
    [dismiss],
  )

  const value = useMemo(
    () => ({
      push,
      dismiss,
      success: (message, options) => push(message, { ...options, type: 'success' }),
      error: (message, options) => push(message, { ...options, type: 'error' }),
      warning: (message, options) => push(message, { ...options, type: 'warning' }),
      info: (message, options) => push(message, { ...options, type: 'info' }),
    }),
    [push, dismiss],
  )

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-full max-w-sm flex-col gap-2"
        role="region"
        aria-label="Notifications"
      >
        {toasts.map((toast) => {
          const Icon = ICONS[toast.type] || Info
          return (
            <div
              key={toast.id}
              role={toast.type === 'error' ? 'alert' : 'status'}
              className={`pointer-events-auto flex items-start gap-2.5 rounded-lg border px-3 py-2.5 shadow-lg ${STYLES[toast.type]}`}
            >
              <Icon size={17} className="mt-0.5 shrink-0" aria-hidden="true" />
              <div className="min-w-0 flex-1 text-sm">
                {toast.title ? <p className="font-semibold">{toast.title}</p> : null}
                <p className="break-words">{toast.message}</p>
              </div>
              <button
                type="button"
                onClick={() => dismiss(toast.id)}
                className="shrink-0 rounded p-0.5 opacity-60 hover:opacity-100"
                aria-label="Dismiss notification"
              >
                <X size={15} />
              </button>
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast() {
  const context = useContext(ToastContext)
  if (!context) throw new Error('useToast must be used inside a ToastProvider')
  return context
}
