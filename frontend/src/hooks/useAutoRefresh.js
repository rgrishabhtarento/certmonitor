import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Keep a screen current without the operator pressing anything.
 *
 * Monitoring data goes stale in seconds and a comment thread is a
 * conversation, so "reload the page to see if anything changed" is the wrong
 * shape for both. This polls instead - and reloading the *page* would be
 * worse than the problem, because it throws away scroll position, open
 * dialogs and half-typed input.
 *
 * The rules that make polling tolerable rather than annoying:
 *
 * **Nothing runs in a hidden tab.** A dashboard left open on a second monitor
 * overnight should not spend the night querying. Polling stops on
 * `visibilitychange` and resumes - with an immediate fetch - when the tab
 * comes back, so returning to it shows current data rather than a countdown.
 *
 * **A refresh never interrupts.** Callers pass `paused` while the user is
 * mid-edit; a background fetch that overwrites a half-written comment or an
 * unsaved RCA is far worse than slightly stale data.
 *
 * **Overlaps are skipped, not queued.** If a fetch is slower than the
 * interval, the next tick is dropped rather than stacking requests on a
 * backend that is evidently already struggling.
 */

/** Conversation surfaces feel broken above ~10s; aggregates can be lazier. */
export const LIVE_INTERVAL = 10000
export const SLOW_INTERVAL = 30000

/**
 * Poll `callback` on an interval.
 *
 * @param callback   async fetcher. Held in a ref, so an inline arrow does not
 *                   restart the timer on every render.
 * @param interval   milliseconds between ticks.
 * @param enabled    per-screen switch.
 * @param paused     true while the user is mid-edit - skip this tick.
 *
 * Returns `{ refreshing, lastRefreshedAt, refreshNow }`. `refreshNow` exists
 * for code that needs to force a read after an action; screens no longer
 * offer it as a button, because the data keeps itself current.
 */
export function useAutoRefresh(
  callback,
  { interval = LIVE_INTERVAL, enabled = true, paused = false } = {},
) {
  const [refreshing, setRefreshing] = useState(false)
  const [lastRefreshedAt, setLastRefreshedAt] = useState(null)

  const callbackRef = useRef(callback)
  const inFlight = useRef(false)
  const pausedRef = useRef(paused)

  useEffect(() => {
    callbackRef.current = callback
  }, [callback])

  useEffect(() => {
    pausedRef.current = paused
  }, [paused])

  const run = useCallback(async ({ manual = false } = {}) => {
    // A manual press always runs; a tick defers to whatever the user is doing.
    if (inFlight.current) return
    if (!manual && pausedRef.current) return

    inFlight.current = true
    setRefreshing(true)
    try {
      await callbackRef.current?.()
      setLastRefreshedAt(new Date())
    } catch {
      // Swallowed on purpose. A failed background poll must not replace what
      // is on screen with an error - the next tick will most likely succeed,
      // and the caller's own loader still reports failures it cares about.
    } finally {
      inFlight.current = false
      setRefreshing(false)
    }
  }, [])

  const active = enabled

  useEffect(() => {
    if (!active) return undefined

    let timer = null

    const start = () => {
      stop()
      timer = setInterval(() => {
        if (!document.hidden) run()
      }, interval)
    }
    const stop = () => {
      if (timer) clearInterval(timer)
      timer = null
    }

    const onVisibility = () => {
      if (document.hidden) {
        stop()
      } else {
        // Coming back to the tab should show current data straight away
        // rather than whatever was true when it was hidden.
        run()
        start()
      }
    }

    if (!document.hidden) start()
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [active, interval, run])

  return {
    refreshing,
    lastRefreshedAt,
    refreshNow: () => run({ manual: true }),
    live: active,
  }
}
