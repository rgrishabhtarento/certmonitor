import clsx from 'clsx'

/**
 * The deployment's logo: the uploaded image when there is one, otherwise the
 * built-in mark passed as `fallback`.
 *
 * `logoUrl` already carries a content hash, so it can be cached hard and a
 * new upload is simply a different URL.
 */
export function BrandMark({ logoUrl, fallback, size = 32, className, alt = '' }) {
  if (logoUrl) {
    return (
      <img
        src={logoUrl}
        alt={alt}
        width={size}
        height={size}
        // `contain` so a wide or tall logo is letterboxed rather than
        // stretched; the tile stays square so the header does not reflow
        // when a deployment swaps its logo.
        className={clsx('shrink-0 rounded-lg object-contain', className)}
        style={{ height: size, width: size }}
      />
    )
  }
  return (
    <span
      className={clsx(
        'grid shrink-0 place-items-center overflow-hidden rounded-lg',
        'bg-brand-600 text-white',
        className,
      )}
      style={{ height: size, width: size }}
      aria-hidden="true"
    >
      {fallback}
    </span>
  )
}

/** A user's avatar: the first two characters of their username. */
export function UserAvatar({ user, size = 28, className }) {
  return (
    <span
      className={clsx(
        'grid shrink-0 place-items-center rounded-full bg-slate-200 font-semibold uppercase',
        'leading-none text-slate-700 dark:bg-slate-700 dark:text-slate-200',
        className,
      )}
      style={{ height: size, width: size, fontSize: size * 0.4 }}
      title={user?.full_name || user?.username || ''}
    >
      {(user?.username || '?').slice(0, 2)}
    </span>
  )
}
