import clsx from 'clsx'

/**
 * The deployment's logo: the emoji or initials an administrator set in
 * Settings, falling back to the built-in mark passed as `fallback`.
 */
export function BrandMark({ text, fallback, size = 32, className }) {
  const label = (text || '').trim()
  return (
    <span
      className={clsx(
        'grid place-items-center overflow-hidden rounded-lg leading-none',
        'bg-brand-600 text-white',
        className,
      )}
      style={{
        height: size,
        width: size,
        // Initials have to shrink to fit; a single emoji can fill the tile.
        fontSize: label.length > 2 ? size * 0.34 : size * 0.55,
      }}
      aria-hidden="true"
    >
      {label || fallback}
    </span>
  )
}

/**
 * A user's avatar: the emoji they chose, or the first two characters of their
 * username when they have not chosen one.
 */
export function UserAvatar({ user, size = 28, className }) {
  const emoji = (user?.avatar_emoji || '').trim()
  const initials = (user?.username || '?').slice(0, 2)
  return (
    <span
      className={clsx(
        'grid place-items-center rounded-full leading-none',
        emoji
          ? 'bg-brand-50 dark:bg-slate-700'
          : 'bg-slate-200 font-semibold uppercase text-slate-700 dark:bg-slate-700 dark:text-slate-200',
        className,
      )}
      style={{ height: size, width: size, fontSize: emoji ? size * 0.6 : size * 0.4 }}
      title={user?.full_name || user?.username || ''}
    >
      {emoji || initials}
    </span>
  )
}

// A deliberately curated set rather than a free text field: an open input
// would let anyone store text or markup where an avatar is expected. The
// server-side check that rejects non-emoji is the safety net, not the UI.
export const AVATAR_EMOJI = [
  '🙂', '😎', '🤓', '🧑‍💻', '👩‍💻', '👨‍💻', '🦸', '🕵️',
  '🚀', '🛡️', '🔧', '⚙️', '📡', '🖥️', '🗄️', '🔔',
  '🐧', '🐳', '🦊', '🦉', '🐙', '🐝', '🌻', '🌵',
  '⚡', '🔥', '❄️', '🌊', '☕', '🍵', '🍕', '🎯',
]

/** Grid of avatar choices, plus a way back to plain initials. */
export function EmojiPicker({ value, onChange, disabled = false }) {
  return (
    <div>
      <div className="grid grid-cols-8 gap-1.5">
        {AVATAR_EMOJI.map((emoji) => {
          const selected = emoji === value
          return (
            <button
              key={emoji}
              type="button"
              disabled={disabled}
              onClick={() => onChange(emoji)}
              aria-pressed={selected}
              aria-label={`Use ${emoji} as your avatar`}
              className={clsx(
                'grid h-9 place-items-center rounded-lg border text-lg leading-none transition',
                selected
                  ? 'border-brand-500 bg-brand-50 ring-1 ring-brand-400 dark:bg-slate-700'
                  : 'border-transparent hover:bg-slate-100 dark:hover:bg-slate-700',
              )}
            >
              {emoji}
            </button>
          )
        })}
      </div>
      <button
        type="button"
        className="btn-ghost mt-2 text-xs"
        onClick={() => onChange('')}
        disabled={disabled || !value}
      >
        Use my initials instead
      </button>
    </div>
  )
}
