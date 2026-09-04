import clsx from 'clsx'

/** Shared RCA presentation pieces. */

export const RCA_STATUS_LABELS = {
  not_requested: 'Not requested',
  pending: 'Pending',
  in_progress: 'In progress',
  completed: 'Completed',
  not_required: 'Not required',
}

// Status carries meaning, so it is never conveyed by colour alone - the label
// is always rendered alongside.
const STATUS_STYLE = {
  not_requested: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400',
  pending: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  in_progress: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
  completed: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300',
  not_required: 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400',
}

export function RcaStatusBadge({ status, overdue = false }) {
  const key = status || 'not_requested'
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={clsx('badge', STATUS_STYLE[key] || STATUS_STYLE.not_requested)}>
        {key === 'completed' ? '✓ ' : ''}
        {RCA_STATUS_LABELS[key] || key}
      </span>
      {overdue ? (
        <span className="badge bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300">
          Overdue
        </span>
      ) : null}
    </span>
  )
}

export const CATEGORY_LABELS = {
  application: 'Application',
  infrastructure: 'Infrastructure',
  network: 'Network',
  database: 'Database',
  deployment: 'Deployment',
  configuration: 'Configuration',
  ssl_tls: 'SSL / TLS',
  security: 'Security',
  dependency: 'Dependency',
  human_error: 'Human error',
  external_dependency: 'External dependency',
  unknown: 'Unknown',
}

export function CategoryBadge({ category }) {
  if (!category) return <span className="text-slate-400">—</span>
  return (
    <span className="badge bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300">
      {CATEGORY_LABELS[category] || category}
    </span>
  )
}

export const PRIORITY_STYLE = {
  critical: {
    dot: 'bg-red-600',
    chip: 'bg-red-600 text-white',
    label: 'CRITICAL',
  },
  high: {
    dot: 'bg-orange-500',
    chip: 'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300',
    label: 'HIGH',
  },
  medium: {
    dot: 'bg-amber-500',
    chip: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
    label: 'MEDIUM',
  },
  low: {
    dot: 'bg-green-500',
    chip: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300',
    label: 'LOW',
  },
}

/** Timeline entries say where they came from, so derived facts are visible
 *  as derived rather than reading like something a person wrote. */
export const TIMELINE_SOURCE_LABELS = {
  monitoring: 'Monitoring',
  change: 'Deployment',
  diagnosis: 'Diagnose',
  comment: 'Comment',
  incident: 'Incident',
  manual: 'Added manually',
}

export function timelineTone(kind) {
  if (['incident_started', 'deployment_failed'].includes(kind)) return 'bg-red-500'
  if (['incident_resolved'].includes(kind)) return 'bg-green-500'
  if (['deployment_started', 'deployment_completed'].includes(kind)) {
    return 'bg-indigo-500'
  }
  if (kind === 'diagnosis') return 'bg-blue-500'
  if (kind === 'comment') return 'bg-slate-400'
  return 'bg-slate-400'
}
