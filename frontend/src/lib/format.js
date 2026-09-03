import { format, formatDistanceToNowStrict, parseISO } from 'date-fns'

/** Presentation helpers. Kept in one place so every screen agrees on units. */

export function parseDate(value) {
  if (!value) return null
  if (value instanceof Date) return value
  try {
    const parsed = typeof value === 'string' ? parseISO(value) : new Date(value)
    return Number.isNaN(parsed.getTime()) ? null : parsed
  } catch {
    return null
  }
}

/** "03 Sep 2026 15:45:20" - unambiguous for an international team. */
export function formatDateTime(value, pattern = 'dd MMM yyyy HH:mm:ss') {
  const date = parseDate(value)
  return date ? format(date, pattern) : '—'
}

export function formatDate(value) {
  return formatDateTime(value, 'dd MMM yyyy')
}

export function formatTime(value) {
  return formatDateTime(value, 'HH:mm:ss')
}

/** "2 minutes ago". Falls back to an em dash rather than "Invalid Date". */
export function formatRelative(value) {
  const date = parseDate(value)
  if (!date) return '—'
  try {
    return `${formatDistanceToNowStrict(date)} ago`
  } catch {
    return '—'
  }
}

export function formatMs(value, { decimals = 0 } = {}) {
  if (value === null || value === undefined) return '—'
  const number = Number(value)
  if (Number.isNaN(number)) return '—'
  if (number >= 1000) return `${(number / 1000).toFixed(2)} s`
  return `${number.toFixed(decimals)} ms`
}

export function formatPercent(value, { decimals = 2 } = {}) {
  if (value === null || value === undefined) return '—'
  const number = Number(value)
  if (Number.isNaN(number)) return '—'
  return `${number.toFixed(decimals)}%`
}

export function formatNumber(value) {
  if (value === null || value === undefined) return '—'
  return Number(value).toLocaleString()
}

export function formatBytes(value) {
  if (value === null || value === undefined) return '—'
  const bytes = Number(value)
  if (Number.isNaN(bytes)) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let size = bytes / 1024
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(1)} ${units[unit]}`
}

/** Human duration: "5m 12s", "2h 5m", "3d 4h". */
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return '—'
  const total = Math.max(0, Math.floor(Number(seconds)))
  if (Number.isNaN(total)) return '—'
  if (total < 60) return `${total}s`
  const minutes = Math.floor(total / 60)
  const secs = total % 60
  if (minutes < 60) return secs ? `${minutes}m ${secs}s` : `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const mins = minutes % 60
  if (hours < 24) return mins ? `${hours}h ${mins}m` : `${hours}h`
  const days = Math.floor(hours / 24)
  return `${days}d ${hours % 24}h`
}

export function formatInterval(seconds) {
  if (!seconds) return '—'
  const value = Number(seconds)
  if (value < 60) return `${value}s`
  if (value < 3600) return `${value / 60} min`
  if (value < 86400) return `${value / 3600} h`
  return `${value / 86400} d`
}

/** Remaining-days wording that reads correctly for expired certificates. */
export function formatDaysRemaining(days) {
  if (days === null || days === undefined) return '—'
  const value = Number(days)
  if (Number.isNaN(value)) return '—'
  if (value < 0) return `Expired ${Math.abs(value)}d ago`
  if (value === 0) return 'Expires today'
  return `${value} day${value === 1 ? '' : 's'}`
}

// ------------------------------------------------------------ status maps
export const STATUS_LABELS = {
  up: 'Healthy',
  down: 'Down',
  degraded: 'Degraded',
  unknown: 'Unknown',
  paused: 'Paused',
}

export const SSL_STATUS_LABELS = {
  valid: 'Valid',
  expiring_soon: 'Expiring Soon',
  critical: 'Critical',
  expired: 'Expired',
  invalid: 'Invalid',
  unable_to_check: 'Unable to Check',
  not_applicable: 'N/A',
}

export const ALERT_TYPE_LABELS = {
  endpoint_down: 'Endpoint down',
  endpoint_recovered: 'Endpoint recovered',
  high_response_time: 'High response time',
  repeated_failures: 'Repeated failures',
  ssl_expiring: 'SSL expiring',
  ssl_expired: 'SSL expired',
  ssl_invalid: 'SSL invalid',
}

export const FAILURE_REASON_LABELS = {
  none: '—',
  dns_failure: 'DNS resolution failed',
  connection_refused: 'Connection refused',
  connection_timeout: 'Connection timeout',
  read_timeout: 'Read timeout',
  tls_error: 'TLS error',
  cert_expired: 'Certificate expired',
  cert_invalid: 'Certificate invalid',
  http_status_mismatch: 'Unexpected HTTP status',
  too_many_redirects: 'Too many redirects',
  slow_response: 'Slow response',
  blocked_target: 'Target not permitted',
  config_error: 'Configuration error',
  unknown_error: 'Unknown error',
}

export function humanise(value, map) {
  if (!value) return '—'
  return map?.[value] || String(value).replace(/_/g, ' ')
}

/** Chart colour for a status; matches the Tailwind semantic palette. */
export const STATUS_COLORS = {
  up: '#16a34a',
  down: '#dc2626',
  degraded: '#d97706',
  unknown: '#64748b',
  paused: '#94a3b8',
  valid: '#16a34a',
  expiring_soon: '#d97706',
  critical: '#ea580c',
  expired: '#dc2626',
  invalid: '#b91c1c',
  unable_to_check: '#64748b',
  not_applicable: '#cbd5e1',
}

/** Deterministic colour for a tag name, so a tag looks the same everywhere. */
export function tagColor(name) {
  const palette = [
    '#2563eb', '#7c3aed', '#0891b2', '#059669',
    '#d97706', '#dc2626', '#db2777', '#4f46e5',
  ]
  let hash = 0
  for (let i = 0; i < String(name).length; i += 1) {
    hash = (hash * 31 + String(name).charCodeAt(i)) % 100000
  }
  return palette[hash % palette.length]
}

export function truncate(value, length = 60) {
  const text = String(value ?? '')
  return text.length > length ? `${text.slice(0, length - 1)}…` : text
}
