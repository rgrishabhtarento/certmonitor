import { useEffect, useState } from 'react'
import { Pause, Play, RefreshCw } from 'lucide-react'
import clsx from 'clsx'

import { Spinner } from './ui'
import { useLiveUpdates } from '../hooks/useAutoRefresh'

/**
 * "Updated 4s ago", plus a refresh that visibly does something.
 *
 * The counter is the point. Without it, pressing refresh on data that has not
 * changed is indistinguishable from a broken button - which is exactly how
 * the old Smart Summary refresh looked. A ticking age proves the screen is
 * current even when nothing on it moved.
 */

/** Ticks once a second so the age stays honest without re-fetching. */
function useTick(active) {
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!active) return undefined
    const timer = setInterval(() => setTick((value) => value + 1), 1000)
    return () => clearInterval(timer)
  }, [active])
}

function age(at) {
  if (!at) return null
  const seconds = Math.max(0, Math.round((Date.now() - at.getTime()) / 1000))
  if (seconds < 5) return 'just now'
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  return `${Math.round(minutes / 60)}h ago`
}

export default function LiveIndicator({
  refreshing,
  lastRefreshedAt,
  onRefresh,
  live = true,
  showToggle = false,
  className,
}) {
  const [enabled, setEnabled] = useLiveUpdates()
  useTick(Boolean(lastRefreshedAt))

  const stamp = age(lastRefreshedAt)

  return (
    <span className={clsx('flex items-center gap-2', className)}>
      <span className="flex items-center gap-1.5 text-xs text-slate-400">
        {live && enabled ? (
          <span
            className={clsx(
              'h-1.5 w-1.5 rounded-full',
              refreshing ? 'bg-brand-500' : 'bg-green-500',
            )}
            aria-hidden="true"
          />
        ) : null}
        {refreshing ? 'Updating…' : stamp ? `Updated ${stamp}` : null}
      </span>

      {showToggle ? (
        <button
          type="button"
          className="rounded p-1 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-slate-800 dark:hover:text-slate-200"
          onClick={() => setEnabled(!enabled)}
          aria-pressed={enabled}
          title={
            enabled
              ? 'Live updates on — pause them'
              : 'Live updates paused — resume them'
          }
          aria-label={enabled ? 'Pause live updates' : 'Resume live updates'}
        >
          {enabled ? <Pause size={13} /> : <Play size={13} />}
        </button>
      ) : null}

      {onRefresh ? (
        <button
          type="button"
          className="btn-secondary py-1 text-xs"
          onClick={onRefresh}
          disabled={refreshing}
        >
          {refreshing ? <Spinner size={13} /> : <RefreshCw size={13} />}
          <span className="hidden sm:inline">Refresh</span>
        </button>
      ) : null}
    </span>
  )
}
