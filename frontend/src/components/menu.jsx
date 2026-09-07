import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import clsx from 'clsx'

const MENU_GAP = 4
const MENU_EDGE = 8

/**
 * A row-level action menu that escapes its scroll container.
 *
 * The obvious implementation - `absolute` inside a `relative` cell - is what
 * this replaces. Table bodies live inside `.table-wrap`, which is
 * `overflow-x: auto`; per the CSS overflow spec, a non-visible value on one
 * axis forces the other from `visible` to `auto`, so that element clips
 * vertically too. The menu was therefore cut off, and on the last or only row
 * it disappeared entirely. No z-index fixes that, because clipping by an
 * ancestor happens regardless of stacking order.
 *
 * So the panel is portalled to `document.body`, positioned from the trigger's
 * viewport rect, and flipped above the trigger when there is no room below.
 *
 * `children` is called with `{ close }` so callers keep their own items and
 * dismiss the menu after acting.
 */
export function ActionMenu({
  label,
  icon,
  children,
  width = 176,
  disabled = false,
  align = 'right',
}) {
  const [open, setOpen] = useState(false)
  const [placement, setPlacement] = useState(null)
  const triggerRef = useRef(null)
  const panelRef = useRef(null)

  const close = useCallback(() => setOpen(false), [])

  const position = useCallback(() => {
    const trigger = triggerRef.current
    if (!trigger) return
    const rect = trigger.getBoundingClientRect()
    const height = panelRef.current?.offsetHeight ?? 0

    const below = window.innerHeight - rect.bottom
    // Flip up only when there is genuinely more room above, otherwise a menu
    // taller than the viewport ends up pinned off the top instead.
    const flip = height > 0 && below < height + MENU_GAP + MENU_EDGE && rect.top > below
    const left = align === 'right' ? rect.right - width : rect.left

    setPlacement({
      top: flip
        ? Math.max(MENU_EDGE, rect.top - height - MENU_GAP)
        : rect.bottom + MENU_GAP,
      left: Math.min(
        Math.max(MENU_EDGE, left),
        Math.max(MENU_EDGE, window.innerWidth - width - MENU_EDGE),
      ),
    })
  }, [align, width])

  // Measured after the panel exists, so the flip decision uses its real
  // height. A layout effect runs before paint, so a flipped menu is never
  // briefly visible in the wrong place.
  useLayoutEffect(() => {
    if (!open) {
      setPlacement(null)
      return
    }
    position()
  }, [open, position])

  useEffect(() => {
    if (!open) return undefined

    const onKey = (event) => {
      if (event.key === 'Escape') {
        setOpen(false)
        triggerRef.current?.focus()
      }
    }
    const onPointerDown = (event) => {
      if (
        panelRef.current?.contains(event.target) ||
        triggerRef.current?.contains(event.target)
      ) {
        return
      }
      setOpen(false)
    }
    // Closed on scroll rather than followed: the panel is fixed to a rect
    // captured when it opened, and tracking the trigger would need a rAF loop
    // for a menu that is dismissed within a click or two anyway.
    const onScrollOrResize = () => setOpen(false)

    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onPointerDown, true)
    window.addEventListener('scroll', onScrollOrResize, true)
    window.addEventListener('resize', onScrollOrResize)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onPointerDown, true)
      window.removeEventListener('scroll', onScrollOrResize, true)
      window.removeEventListener('resize', onScrollOrResize)
    }
  }, [open])

  // Move focus into the panel so the menu is keyboard-operable and Tab
  // continues from the menu rather than from the row behind it.
  useEffect(() => {
    if (!open || !placement) return
    panelRef.current?.querySelector('a[href], button:not([disabled])')?.focus()
  }, [open, placement])

  const onPanelKeyDown = (event) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    const items = Array.from(
      panelRef.current?.querySelectorAll('a[href], button:not([disabled])') ?? [],
    )
    if (!items.length) return
    event.preventDefault()
    const index = items.indexOf(document.activeElement)
    const next =
      event.key === 'ArrowDown'
        ? items[(index + 1) % items.length]
        : items[(index - 1 + items.length) % items.length]
    next?.focus()
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="btn-ghost p-1.5"
        onClick={() => setOpen((value) => !value)}
        disabled={disabled}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        {icon}
      </button>

      {open
        ? createPortal(
            <div
              ref={panelRef}
              role="menu"
              aria-label={label}
              onKeyDown={onPanelKeyDown}
              style={{
                position: 'fixed',
                width,
                top: placement?.top ?? -9999,
                left: placement?.left ?? -9999,
                // Hidden until measured, so the first paint of a flipped menu
                // is not in the pre-flip position.
                visibility: placement ? 'visible' : 'hidden',
              }}
              className={clsx(
                'z-50 overflow-hidden rounded-lg border border-slate-200 bg-white text-left',
                'shadow-lg dark:border-slate-700 dark:bg-slate-800',
              )}
            >
              {children({ close })}
            </div>,
            document.body,
          )
        : null}
    </>
  )
}

/** Shared item styling, so callers do not re-derive it per menu. */
export const MENU_ITEM =
  'block w-full px-3 py-2 text-left text-sm hover:bg-slate-50 dark:hover:bg-slate-700'

export const MENU_ITEM_DANGER =
  'block w-full px-3 py-2 text-left text-sm text-red-600 hover:bg-red-50 ' +
  'dark:text-red-400 dark:hover:bg-red-900/30'
