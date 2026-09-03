import clsx from 'clsx'

/** Shared change-management presentation pieces. */

export const CHANGE_STATUS_LABELS = {
  draft: 'Draft',
  pending_approval: 'Pending approval',
  approved: 'Approved',
  rejected: 'Rejected',
  deployment_in_progress: 'Deploying',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

// Status carries meaning, so it is never conveyed by colour alone - the label
// is always rendered alongside.
const STATUS_STYLE = {
  draft: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  pending_approval:
    'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  approved: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
  rejected: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  deployment_in_progress:
    'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-300',
  completed: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300',
  failed: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  cancelled: 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400',
}

export function ChangeStatusBadge({ status, size = 'md' }) {
  const key = status || 'draft'
  return (
    <span
      className={clsx(
        'badge',
        STATUS_STYLE[key] || STATUS_STYLE.draft,
        size === 'sm' && 'text-[11px]',
      )}
    >
      {key === 'deployment_in_progress' ? (
        <span
          className="h-1.5 w-1.5 animate-pulse-slow rounded-full bg-indigo-500"
          aria-hidden="true"
        />
      ) : null}
      {CHANGE_STATUS_LABELS[key] || key}
    </span>
  )
}

const RISK_STYLE = {
  low: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  medium: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  high: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
}

export function RiskBadge({ risk }) {
  const key = (risk || 'low').toLowerCase()
  return (
    <span className={clsx('badge capitalize', RISK_STYLE[key] || RISK_STYLE.low)}>
      {key} risk
    </span>
  )
}

export const ACTIVITY_LABELS = {
  created: 'Change created',
  updated: 'Change updated',
  submitted: 'Submitted for approval',
  approved: 'Approved',
  rejected: 'Rejected',
  deployment_started: 'Deployment started',
  monitoring_paused: 'Monitoring paused',
  deployment_completed: 'Deployment completed',
  deployment_failed: 'Deployment failed',
  monitoring_resumed: 'Monitoring resumed',
  health_check: 'Health check',
  cancelled: 'Cancelled',
  commented: 'Comment',
}

/** Dot colour for the activity timeline - decorative; the label carries it. */
export function activityTone(action) {
  if (['deployment_failed', 'rejected', 'cancelled'].includes(action)) {
    return 'bg-red-500'
  }
  if (['approved', 'deployment_completed', 'health_check'].includes(action)) {
    return 'bg-green-500'
  }
  if (['deployment_started', 'monitoring_paused'].includes(action)) {
    return 'bg-indigo-500'
  }
  if (action === 'monitoring_resumed') return 'bg-blue-500'
  return 'bg-slate-400'
}
