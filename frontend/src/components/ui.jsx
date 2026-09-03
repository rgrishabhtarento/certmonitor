import { useEffect, useRef, useState } from 'react'
import { AlertCircle, ChevronLeft, ChevronRight, Loader2, Search, X } from 'lucide-react'
import clsx from 'clsx'

import {
  SSL_STATUS_LABELS,
  STATUS_LABELS,
  humanise,
  tagColor,
} from '../lib/format'

/** Shared presentational building blocks used across every page. */

// ------------------------------------------------------------------ status
const STATUS_DOT = {
  up: 'bg-green-500',
  down: 'bg-red-500',
  degraded: 'bg-amber-500',
  unknown: 'bg-slate-400',
  paused: 'bg-slate-300',
}

const STATUS_BADGE = {
  up: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300',
  down: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  degraded: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  unknown: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  paused: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400',
}

/**
 * Status indicator.
 *
 * Colour alone never carries the meaning - the label is always rendered - so
 * the dashboard stays readable for colour-blind users.
 */
export function StatusBadge({ status, size = 'md', pulse = false }) {
  const key = status || 'unknown'
  return (
    <span
      className={clsx(
        'badge',
        STATUS_BADGE[key] || STATUS_BADGE.unknown,
        size === 'sm' && 'text-[11px]',
      )}
    >
      <span
        className={clsx(
          'h-1.5 w-1.5 rounded-full',
          STATUS_DOT[key] || STATUS_DOT.unknown,
          pulse && key === 'down' && 'animate-pulse-slow',
        )}
        aria-hidden="true"
      />
      {humanise(key, STATUS_LABELS)}
    </span>
  )
}

const SSL_BADGE = {
  valid: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300',
  expiring_soon: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  critical: 'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300',
  expired: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  invalid: 'bg-red-100 text-red-900 dark:bg-red-900/50 dark:text-red-200',
  unable_to_check: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  not_applicable: 'bg-slate-50 text-slate-500 dark:bg-slate-900 dark:text-slate-500',
}

export function SslBadge({ status }) {
  const key = status || 'unable_to_check'
  return (
    <span className={clsx('badge', SSL_BADGE[key] || SSL_BADGE.unable_to_check)}>
      {humanise(key, SSL_STATUS_LABELS)}
    </span>
  )
}

const SEVERITY_BADGE = {
  info: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
  warning: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  critical: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
}

export function SeverityBadge({ severity }) {
  const key = severity || 'info'
  return (
    <span className={clsx('badge capitalize', SEVERITY_BADGE[key] || SEVERITY_BADGE.info)}>
      {key}
    </span>
  )
}

export function TagChip({ name, onRemove }) {
  const color = tagColor(name)
  return (
    <span
      className="chip"
      style={{ backgroundColor: `${color}1a`, color }}
      title={name}
    >
      {name}
      {onRemove ? (
        <button
          type="button"
          onClick={onRemove}
          className="ml-0.5 opacity-70 hover:opacity-100"
          aria-label={`Remove tag ${name}`}
        >
          <X size={11} />
        </button>
      ) : null}
    </span>
  )
}

// ------------------------------------------------------------------ layout
export function Card({ title, actions, children, className, bodyClassName }) {
  return (
    <section className={clsx('card', className)}>
      {title || actions ? (
        <header className="card-header">
          <h2 className="card-title">{title}</h2>
          {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
        </header>
      ) : null}
      <div className={clsx(bodyClassName ?? 'p-4')}>{children}</div>
    </section>
  )
}

export function PageHeader({ title, description, actions }) {
  return (
    <div className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <h1 className="truncate text-xl font-semibold text-slate-900 dark:text-slate-50">
          {title}
        </h1>
        {description ? (
          <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  )
}

export function Spinner({ size = 18, className }) {
  return (
    <Loader2
      size={size}
      className={clsx('animate-spin text-slate-400', className)}
      aria-hidden="true"
    />
  )
}

export function LoadingBlock({ label = 'Loading…', rows = 4 }) {
  return (
    <div className="space-y-2" role="status" aria-live="polite" aria-label={label}>
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="skeleton h-9 w-full" />
      ))}
    </div>
  )
}

export function EmptyState({ icon: Icon, title, description, action }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-12 text-center">
      {Icon ? <Icon size={30} className="text-slate-300 dark:text-slate-600" /> : null}
      <p className="text-sm font-medium text-slate-700 dark:text-slate-200">{title}</p>
      {description ? (
        <p className="max-w-md text-sm text-slate-500 dark:text-slate-400">{description}</p>
      ) : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  )
}

export function ErrorState({ message, onRetry, requestId, status }) {
  return (
    <div
      role="alert"
      className="flex flex-col items-center gap-3 rounded-lg border border-red-200 bg-red-50 px-4 py-8 text-center dark:border-red-900 dark:bg-red-950/40"
    >
      <AlertCircle className="text-red-500" size={26} />
      <p className="text-sm font-medium text-red-800 dark:text-red-200">{message}</p>
      {/* The API never echoes exception text, so the request id is the only
          way to find the matching traceback in the logs. Showing it here saves
          guessing which log line belongs to this failure. */}
      {requestId ? (
        <p className="font-mono text-[11px] text-red-600/80 dark:text-red-300/70">
          {status ? `HTTP ${status} · ` : ''}request {requestId}
          <br />
          <span className="font-sans">
            find it with: docker compose logs backend | grep {requestId}
          </span>
        </p>
      ) : null}
      {onRetry ? (
        <button type="button" className="btn-secondary btn-sm" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  )
}

// -------------------------------------------------------------- form bits
export function Field({ label, error, hint, required, children, className }) {
  return (
    <div className={className}>
      {label ? (
        <label className="label">
          {label}
          {required ? <span className="ml-0.5 text-red-500">*</span> : null}
        </label>
      ) : null}
      {children}
      {error ? <p className="field-error">{error}</p> : null}
      {!error && hint ? <p className="hint">{hint}</p> : null}
    </div>
  )
}

export function Toggle({ checked, onChange, label, description, disabled }) {
  return (
    <label
      className={clsx(
        'flex items-start gap-2.5',
        disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer',
      )}
    >
      <input
        type="checkbox"
        className="mt-0.5 h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800"
        checked={Boolean(checked)}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="min-w-0">
        <span className="block text-sm font-medium text-slate-800 dark:text-slate-200">
          {label}
        </span>
        {description ? (
          <span className="block text-xs text-slate-500 dark:text-slate-400">
            {description}
          </span>
        ) : null}
      </span>
    </label>
  )
}

/** Debounced search box: fires onChange after typing settles. */
export function SearchInput({ value, onChange, placeholder = 'Search…', delay = 350 }) {
  const [local, setLocal] = useState(value ?? '')
  const timer = useRef(null)

  useEffect(() => {
    setLocal(value ?? '')
  }, [value])

  useEffect(() => () => clearTimeout(timer.current), [])

  const handle = (next) => {
    setLocal(next)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => onChange(next), delay)
  }

  return (
    <div className="relative">
      <Search
        size={15}
        className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400"
        aria-hidden="true"
      />
      <input
        type="search"
        className="input pl-8"
        value={local}
        placeholder={placeholder}
        aria-label={placeholder}
        onChange={(event) => handle(event.target.value)}
      />
    </div>
  )
}

/** Free-form tag entry: comma or Enter commits a tag. */
export function TagInput({ value = [], onChange, suggestions = [], placeholder = 'Add a tag…' }) {
  const [draft, setDraft] = useState('')

  const add = (raw) => {
    const name = String(raw).trim().toLowerCase()
    if (!name) return
    if (!value.includes(name)) onChange([...value, name])
    setDraft('')
  }

  const available = suggestions
    .filter((s) => !value.includes(s))
    .filter((s) => (draft ? s.includes(draft.toLowerCase()) : true))
    .slice(0, 8)

  return (
    <div>
      <div className="flex min-h-[38px] flex-wrap items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-2 py-1.5 dark:border-slate-700 dark:bg-slate-800">
        {value.map((name) => (
          <TagChip
            key={name}
            name={name}
            onRemove={() => onChange(value.filter((v) => v !== name))}
          />
        ))}
        <input
          className="min-w-[8rem] flex-1 bg-transparent text-sm outline-none placeholder:text-slate-400"
          value={draft}
          placeholder={value.length ? '' : placeholder}
          aria-label="Add a tag"
          onChange={(event) => {
            const next = event.target.value
            if (next.includes(',')) {
              next.split(',').forEach(add)
            } else {
              setDraft(next)
            }
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              add(draft)
            } else if (event.key === 'Backspace' && !draft && value.length) {
              onChange(value.slice(0, -1))
            }
          }}
        />
      </div>
      {available.length ? (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {available.map((name) => (
            <button
              key={name}
              type="button"
              className="chip hover:ring-1 hover:ring-brand-400"
              onClick={() => add(name)}
            >
              + {name}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}

// ------------------------------------------------------------------ modal
export function Modal({ open, onClose, title, children, footer, size = 'md' }) {
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const onKey = (event) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    // Prevent the page behind the dialog from scrolling.
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    ref.current?.focus()
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [open, onClose])

  if (!open) return null

  const widths = { sm: 'max-w-md', md: 'max-w-2xl', lg: 'max-w-4xl', xl: 'max-w-6xl' }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-slate-900/50 p-4 backdrop-blur-sm sm:items-center"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={ref}
        tabIndex={-1}
        className={clsx(
          'my-4 w-full rounded-xl border border-slate-200 bg-white shadow-2xl dark:border-slate-700 dark:bg-slate-900',
          widths[size],
        )}
      >
        <header className="flex items-center justify-between gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
          <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">{title}</h2>
          <button
            type="button"
            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-slate-800"
            onClick={onClose}
            aria-label="Close dialog"
          >
            <X size={17} />
          </button>
        </header>
        <div className="max-h-[70vh] overflow-y-auto px-4 py-4">{children}</div>
        {footer ? (
          <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-slate-200 px-4 py-3 dark:border-slate-800">
            {footer}
          </footer>
        ) : null}
      </div>
    </div>
  )
}

export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  message,
  confirmLabel = 'Confirm',
  danger = false,
  busy = false,
}) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      size="sm"
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className={danger ? 'btn-danger' : 'btn-primary'}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? <Spinner size={15} className="text-white" /> : null}
            {confirmLabel}
          </button>
        </>
      }
    >
      <p className="text-sm text-slate-600 dark:text-slate-300">{message}</p>
    </Modal>
  )
}

// ------------------------------------------------------------- pagination
export function Pagination({ meta, onPageChange, onPageSizeChange }) {
  if (!meta) return null
  const { page, pages, total, page_size, has_next, has_previous } = meta
  const first = total === 0 ? 0 : (page - 1) * page_size + 1
  const last = Math.min(page * page_size, total)

  return (
    <div className="flex flex-col gap-2 border-t border-slate-200 px-3 py-2.5 text-sm dark:border-slate-800 sm:flex-row sm:items-center sm:justify-between">
      <p className="tnum text-slate-500 dark:text-slate-400">
        {total === 0 ? 'No results' : `${first}–${last} of ${total.toLocaleString()}`}
      </p>
      <div className="flex items-center gap-2">
        {onPageSizeChange ? (
          <select
            className="input w-auto py-1 text-xs"
            value={page_size}
            aria-label="Rows per page"
            onChange={(event) => onPageSizeChange(Number(event.target.value))}
          >
            {[25, 50, 100, 200].map((size) => (
              <option key={size} value={size}>
                {size} / page
              </option>
            ))}
          </select>
        ) : null}
        <button
          type="button"
          className="btn-secondary btn-sm"
          disabled={!has_previous}
          onClick={() => onPageChange(page - 1)}
          aria-label="Previous page"
        >
          <ChevronLeft size={15} />
        </button>
        <span className="tnum px-1 text-xs text-slate-600 dark:text-slate-300">
          {page} / {pages || 1}
        </span>
        <button
          type="button"
          className="btn-secondary btn-sm"
          disabled={!has_next}
          onClick={() => onPageChange(page + 1)}
          aria-label="Next page"
        >
          <ChevronRight size={15} />
        </button>
      </div>
    </div>
  )
}

/** Sortable column header. */
export function SortHeader({ label, field, sortBy, sortDir, onSort, align = 'left' }) {
  const active = sortBy === field
  const right = align === 'right'
  return (
    <th className={right ? 'text-right' : undefined} aria-sort={active ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button
        type="button"
        className={clsx(
          // Tailwind's preflight makes a button inherit font family, size and
          // weight - but NOT text-transform or letter-spacing, which the UA
          // stylesheet resets on form controls. Without restating them here a
          // sortable header renders in title case while its plain-<th>
          // neighbours render uppercase, which is the mismatch this fixes.
          'inline-flex items-center gap-1 uppercase tracking-wide',
          // Fill the cell so a right-aligned header sits directly over the
          // right-aligned numbers below it, not floating mid-cell.
          right && 'w-full justify-end',
          'hover:text-slate-900 dark:hover:text-slate-100',
          active && 'text-brand-600 dark:text-brand-400',
        )}
        onClick={() => onSort(field, active && sortDir === 'asc' ? 'desc' : 'asc')}
      >
        {label}
        <span aria-hidden="true" className={active ? undefined : 'opacity-0'}>
          {sortDir === 'asc' && active ? '↑' : '↓'}
        </span>
      </button>
    </th>
  )
}

/** Small metric readout used in detail panels. */
export function Metric({ label, value, sub, tone }) {
  const tones = {
    good: 'text-green-600 dark:text-green-400',
    bad: 'text-red-600 dark:text-red-400',
    warn: 'text-amber-600 dark:text-amber-400',
  }
  return (
    <div>
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {label}
      </p>
      <p className={clsx('tnum mt-0.5 text-lg font-semibold', tones[tone] || 'text-slate-900 dark:text-slate-100')}>
        {value}
      </p>
      {sub ? <p className="text-xs text-slate-500 dark:text-slate-400">{sub}</p> : null}
    </div>
  )
}

/** Definition-list row for the many detail panels. */
export function DetailRow({ label, children, mono = false }) {
  return (
    <div className="flex flex-col gap-0.5 border-b border-slate-100 py-2 last:border-0 sm:flex-row sm:gap-4 dark:border-slate-800">
      <dt className="w-full shrink-0 text-xs font-medium text-slate-500 sm:w-52 dark:text-slate-400">
        {label}
      </dt>
      <dd
        className={clsx(
          'min-w-0 flex-1 break-words text-sm text-slate-800 dark:text-slate-200',
          mono && 'font-mono text-xs',
        )}
      >
        {children ?? '—'}
      </dd>
    </div>
  )
}
