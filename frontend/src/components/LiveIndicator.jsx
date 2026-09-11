import { useEffect, useState } from 'react'
import clsx from 'clsx'

/**
 * "Updated 4s ago".
 *
 * The counter is the point. Screens refresh themselves, so the question a
 * reader has is not "can I refresh this" but "is what I am looking at
 * current" - and a ticking age answers that even when nothing on screen
 * moved. There is deliberately no refresh button and no pause control: the
 * data keeps itself current, and a button that appears to do nothing on
 * unchanged data reads as broken.
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

export default function LiveIndicator({ refreshing, lastRefreshedAt, className }) {
  useTick(Boolean(lastRefreshedAt))

  const stamp = age(lastRefreshedAt)

  return (
    <span
      className={clsx(
        'flex items-center gap-1.5 text-xs text-slate-400',
        className,
      )}
    >
      <span
        className={clsx(
          'h-1.5 w-1.5 rounded-full',
          refreshing ? 'bg-brand-500' : 'bg-green-500',
        )}
        aria-hidden="true"
      />
      {refreshing ? 'Updating…' : stamp ? `Updated ${stamp}` : null}
    </span>
  )
}
