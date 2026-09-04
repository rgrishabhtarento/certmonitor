import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  Ban,
  CheckCircle2,
  MessageSquare,
  Pencil,
  Rocket,
  Send,
  ShieldCheck,
  ThumbsDown,
  ThumbsUp,
  XCircle,
} from 'lucide-react'
import clsx from 'clsx'

import ChangeForm from '../components/ChangeForm'
import {
  ACTIVITY_LABELS,
  ChangeStatusBadge,
  RiskBadge,
  activityTone,
} from '../components/change'
import {
  Card,
  ConfirmDialog,
  DetailRow,
  ErrorState,
  Field,
  LoadingBlock,
  Modal,
  PageHeader,
  Spinner,
  StatusBadge,
} from '../components/ui'
import LiveIndicator from '../components/LiveIndicator'
import { changesApi, taxonomyApi } from '../lib/api'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import { formatDateTime, formatMs, formatRelative } from '../lib/format'
import { useToast } from '../hooks/useToast'

export default function ChangeDetail() {
  const { changeId } = useParams()
  const toast = useToast()

  const [change, setChange] = useState(null)
  const [environments, setEnvironments] = useState([])
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [comment, setComment] = useState('')

  const [formOpen, setFormOpen] = useState(false)
  const [rejectOpen, setRejectOpen] = useState(false)
  const [rejectReason, setRejectReason] = useState('')
  const [completeOpen, setCompleteOpen] = useState(false)
  const [completeNotes, setCompleteNotes] = useState('')
  const [failOpen, setFailOpen] = useState(false)
  const [failReason, setFailReason] = useState('')
  const [confirmStart, setConfirmStart] = useState(false)
  const [confirmCancel, setConfirmCancel] = useState(false)
  const [health, setHealth] = useState(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      setChange(await changesApi.get(changeId))
    } catch (err) {
      setError(err.message)
    }
  }, [changeId])

  // A change record is a conversation - comments, activity, someone else
  // starting the deployment - so it polls on the fast cadence.
  //
  // Paused while a dialog is open or an action is in flight. Replacing the
  // record underneath an open Reject or Complete dialog would swap the
  // permissions those buttons were rendered from, and the comment draft is
  // separate state so it survives a refresh untouched.
  const { refreshing, lastRefreshedAt, refreshNow } = useAutoRefresh(load, {
    paused:
      busy || formOpen || rejectOpen || completeOpen || failOpen ||
      confirmStart || confirmCancel,
  })

  useEffect(() => {
    load()
    taxonomyApi.environments().then(setEnvironments).catch(() => {})
  }, [load])

  /** Wrap an action so every button gets the same busy/error/refresh handling. */
  const run = async (fn, successMessage) => {
    setBusy(true)
    try {
      const result = await fn()
      if (successMessage) toast.success(successMessage)
      // Deployment transitions return a wrapper carrying the monitoring effect.
      if (result?.change) {
        setChange(result.change)
        if (result.health_check?.length) setHealth(result.health_check)
      } else if (result?.id) {
        setChange(result)
      } else {
        await load()
      }
      return result
    } catch (err) {
      toast.error(err.message)
      return null
    } finally {
      setBusy(false)
    }
  }

  if (!change && !error) {
    return (
      <>
        <PageHeader title="Change" />
        <LoadingBlock rows={8} />
      </>
    )
  }
  if (error && !change) {
    return (
      <>
        <PageHeader title="Change" />
        <ErrorState message={error} onRetry={load} />
      </>
    )
  }

  const deploying = change.status === 'deployment_in_progress'

  return (
    <>
      <Link
        to="/changes"
        className="mb-3 inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-800 dark:hover:text-slate-200"
      >
        <ArrowLeft size={15} /> All changes
      </Link>

      <PageHeader
        title={`${change.reference} — ${change.title}`}
        description={`${change.application}${change.environment ? ` · ${change.environment}` : ''}`}
        actions={
          <>
            <LiveIndicator
              refreshing={refreshing}
              lastRefreshedAt={lastRefreshedAt}
              onRefresh={refreshNow}
              showToggle
            />
            {change.can_edit ? (
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setFormOpen(true)}
              >
                <Pencil size={15} /> Edit
              </button>
            ) : null}
            {change.can_submit ? (
              <button
                type="button"
                className="btn-primary"
                onClick={() =>
                  run(
                    () => changesApi.submit(change.id),
                    change.requires_approval
                      ? 'Submitted for approval.'
                      : 'Approved — this environment does not require approval.',
                  )
                }
                disabled={busy}
              >
                {busy ? <Spinner size={15} className="text-white" /> : <Send size={15} />}
                Submit
              </button>
            ) : null}
            {change.can_approve ? (
              <>
                <button
                  type="button"
                  className="btn-danger"
                  onClick={() => setRejectOpen(true)}
                  disabled={busy}
                >
                  <ThumbsDown size={15} /> Reject
                </button>
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() =>
                    run(() => changesApi.approve(change.id), 'Change approved.')
                  }
                  disabled={busy}
                >
                  <ThumbsUp size={15} /> Approve
                </button>
              </>
            ) : null}
            {change.can_deploy ? (
              <button
                type="button"
                className="btn-primary"
                onClick={() => setConfirmStart(true)}
                disabled={busy}
              >
                <Rocket size={15} /> Start deployment
              </button>
            ) : null}
            {change.can_finish ? (
              <>
                <button
                  type="button"
                  className="btn-danger"
                  onClick={() => setFailOpen(true)}
                  disabled={busy}
                >
                  <XCircle size={15} /> Mark failed
                </button>
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() => setCompleteOpen(true)}
                  disabled={busy}
                >
                  <CheckCircle2 size={15} /> Complete deployment
                </button>
              </>
            ) : null}
            {change.can_cancel ? (
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setConfirmCancel(true)}
                disabled={busy}
              >
                <Ban size={15} /> Cancel
              </button>
            ) : null}
          </>
        }
      />

      {/* ---------------------------------------------- status banner */}
      <div
        className={clsx(
          'card mb-4 border-l-4 p-4',
          deploying
            ? 'border-l-indigo-500'
            : change.status === 'completed'
              ? 'border-l-green-500'
              : change.status === 'failed' || change.status === 'rejected'
                ? 'border-l-red-500'
                : change.status === 'pending_approval'
                  ? 'border-l-amber-500'
                  : 'border-l-slate-400',
        )}
      >
        <div className="flex flex-wrap items-center gap-2">
          <ChangeStatusBadge status={change.status} />
          <RiskBadge risk={change.risk} />
          {deploying ? (
            <span className="badge bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
              Monitoring paused for {change.endpoints.length} endpoint(s)
            </span>
          ) : null}
          {change.requires_approval &&
          change.status === 'pending_approval' ? (
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {change.environment} requires approval before deployment
            </span>
          ) : null}
        </div>

        {change.rejection_reason ? (
          <p className="mt-2 rounded bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/40 dark:text-red-200">
            <span className="font-medium">Rejected:</span> {change.rejection_reason}
          </p>
        ) : null}
        {change.failure_reason ? (
          <p className="mt-2 rounded bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/40 dark:text-red-200">
            <span className="font-medium">Failure reason:</span> {change.failure_reason}
          </p>
        ) : null}
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        {/* --------------------------------------------------- left */}
        <div className="space-y-4 xl:col-span-2">
          <Card title="Change details">
            <dl>
              <DetailRow label="Change ID" mono>
                {change.reference}
              </DetailRow>
              <DetailRow label="Title">{change.title}</DetailRow>
              <DetailRow label="Application">{change.application}</DetailRow>
              <DetailRow label="Environment">{change.environment}</DetailRow>
              <DetailRow label="Description">
                <p className="whitespace-pre-wrap">{change.description}</p>
              </DetailRow>
              <DetailRow label="Requester">{change.requester_name}</DetailRow>
              <DetailRow label="Risk">{change.risk}</DetailRow>
              <DetailRow label="Expected deployment">
                {formatDateTime(change.expected_start_at)}
              </DetailRow>
              <DetailRow label="Expected duration">
                {change.expected_duration_minutes} minutes
              </DetailRow>
              <DetailRow label="Approver">
                {change.approver_name
                  ? `${change.approver_name} · ${formatDateTime(change.approved_at)}`
                  : null}
              </DetailRow>
              <DetailRow label="Deployer">{change.deployer_name}</DetailRow>
              <DetailRow label="Actual start">
                {formatDateTime(change.started_at)}
              </DetailRow>
              <DetailRow label="Actual completion">
                {change.completed_at
                  ? `${formatDateTime(change.completed_at)}${
                      change.actual_duration_minutes !== null
                        ? ` (${change.actual_duration_minutes} min)`
                        : ''
                    }`
                  : null}
              </DetailRow>
              <DetailRow label="Rollback plan">
                {change.rollback_plan ? (
                  <p className="whitespace-pre-wrap">{change.rollback_plan}</p>
                ) : null}
              </DetailRow>
              <DetailRow label="Deployment notes">
                {change.deployment_notes ? (
                  <p className="whitespace-pre-wrap">{change.deployment_notes}</p>
                ) : null}
              </DetailRow>
            </dl>
          </Card>

          <Card
            title={`Affected endpoints (${change.endpoints.length})`}
            bodyClassName="p-0"
          >
            {change.endpoints.length === 0 ? (
              <p className="px-4 py-6 text-center text-sm text-slate-400">
                No endpoints linked. Monitoring is not affected by this change.
              </p>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Endpoint</th>
                      <th>Environment</th>
                      <th>Status</th>
                      <th>Monitoring</th>
                    </tr>
                  </thead>
                  <tbody>
                    {change.endpoints.map((endpoint) => (
                      <tr key={endpoint.id}>
                        <td>
                          <Link
                            to={`/endpoints/${endpoint.id}`}
                            className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                          >
                            {endpoint.name}
                          </Link>
                          <p className="truncate font-mono text-[11px] text-slate-400">
                            {endpoint.url}
                          </p>
                        </td>
                        <td className="text-slate-600 dark:text-slate-300">
                          {endpoint.environment || '—'}
                        </td>
                        <td>
                          <StatusBadge status={endpoint.current_status} />
                        </td>
                        <td>
                          {endpoint.is_paused ? (
                            <>
                              <span className="badge bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
                                Paused
                              </span>
                              {endpoint.pause_reason ? (
                                <p className="mt-0.5 text-[11px] text-slate-400">
                                  {endpoint.pause_reason}
                                </p>
                              ) : null}
                            </>
                          ) : (
                            <span className="text-xs text-slate-500">Active</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          {/* ------------------------------------- post-deploy health */}
          {(health || change.health_check)?.length ? (
            <Card
              title={
                <span className="flex items-center gap-1.5">
                  <ShieldCheck size={15} /> Post-deployment health check
                </span>
              }
              bodyClassName="p-0"
            >
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Endpoint</th>
                      <th>Result</th>
                      <th className="text-right">HTTP</th>
                      <th className="text-right">Response</th>
                      <th>Certificate</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(health || change.health_check).map((row) => (
                      <tr key={row.endpoint_id}>
                        <td>{row.name}</td>
                        <td>
                          <StatusBadge status={row.status} />
                          {row.error ? (
                            <p className="text-[11px] text-red-500">{row.error}</p>
                          ) : null}
                        </td>
                        <td className="tnum text-right">{row.http_status || '—'}</td>
                        <td className="tnum text-right">
                          {formatMs(row.response_time_ms)}
                        </td>
                        <td className="text-xs text-slate-600 dark:text-slate-300">
                          {row.ssl_status
                            ? `${row.ssl_status}${
                                row.ssl_days_remaining !== null &&
                                row.ssl_days_remaining !== undefined
                                  ? ` · ${row.ssl_days_remaining}d`
                                  : ''
                              }`
                            : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          ) : null}

          {/* ------------------------------------------------ comments */}
          <Card
            title={
              <span className="flex items-center gap-1.5">
                <MessageSquare size={15} /> Comments ({change.comments.length})
              </span>
            }
          >
            {change.comments.length === 0 ? (
              <p className="mb-3 text-sm text-slate-400">
                No comments yet. Anyone who can see this change can add one.
              </p>
            ) : (
              <ol className="mb-4 space-y-3">
                {change.comments.map((entry) => (
                  <li key={entry.id} className="flex gap-2.5">
                    <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full bg-slate-200 text-[11px] font-semibold uppercase text-slate-700 dark:bg-slate-700 dark:text-slate-200">
                      {(entry.username || '?').slice(0, 2)}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="text-xs">
                        <span className="font-semibold text-slate-800 dark:text-slate-100">
                          {entry.username || 'unknown'}
                        </span>
                        <span className="ml-1.5 text-slate-400">
                          {formatDateTime(entry.created_at, 'dd MMM HH:mm')}
                        </span>
                      </p>
                      <p className="whitespace-pre-wrap break-words text-sm text-slate-700 dark:text-slate-200">
                        {entry.body}
                      </p>
                    </div>
                  </li>
                ))}
              </ol>
            )}

            {change.can_comment ? (
              <form
                className="flex flex-col gap-2 sm:flex-row"
                onSubmit={async (event) => {
                  event.preventDefault()
                  if (!comment.trim()) return
                  const result = await run(
                    () => changesApi.comment(change.id, comment.trim()),
                  )
                  if (result) setComment('')
                }}
              >
                <input
                  className="input flex-1"
                  placeholder="Add a comment…"
                  value={comment}
                  onChange={(event) => setComment(event.target.value)}
                  maxLength={4000}
                />
                <button
                  type="submit"
                  className="btn-primary"
                  disabled={busy || !comment.trim()}
                >
                  Comment
                </button>
              </form>
            ) : null}
          </Card>
        </div>

        {/* -------------------------------------------------- right */}
        <div>
          <Card title="Activity">
            {change.activity.length === 0 ? (
              <p className="text-sm text-slate-400">Nothing recorded yet.</p>
            ) : (
              <ol className="space-y-3">
                {change.activity.map((entry) => (
                  <li key={entry.id} className="flex gap-2.5">
                    <span className="relative mt-1 flex flex-col items-center">
                      <span
                        className={clsx(
                          'h-2 w-2 shrink-0 rounded-full',
                          activityTone(entry.action),
                        )}
                        aria-hidden="true"
                      />
                    </span>
                    <div className="min-w-0 flex-1 border-b border-slate-100 pb-2 last:border-0 dark:border-slate-800">
                      <p className="text-xs font-medium text-slate-800 dark:text-slate-100">
                        {ACTIVITY_LABELS[entry.action] || entry.action}
                      </p>
                      {entry.detail ? (
                        <p className="break-words text-xs text-slate-600 dark:text-slate-300">
                          {entry.detail}
                        </p>
                      ) : null}
                      <p className="tnum mt-0.5 text-[11px] text-slate-400">
                        {formatDateTime(entry.created_at, 'dd MMM HH:mm')} ·{' '}
                        {formatRelative(entry.created_at)}
                      </p>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </Card>
        </div>
      </div>

      {/* ------------------------------------------------------ dialogs */}
      <ChangeForm
        open={formOpen}
        change={change}
        environments={environments}
        onClose={() => setFormOpen(false)}
        onSaved={(saved) => {
          setFormOpen(false)
          setChange(saved)
        }}
      />

      <ConfirmDialog
        open={confirmStart}
        onClose={() => setConfirmStart(false)}
        busy={busy}
        title="Start this deployment?"
        confirmLabel="Start deployment"
        message={
          `Monitoring will be paused immediately for ${change.endpoints.length} endpoint(s), ` +
          'so no incidents or alerts are raised while you deploy. You will be recorded as the deployer.'
        }
        onConfirm={async () => {
          const result = await run(
            () => changesApi.startDeployment(change.id),
            'Deployment started — monitoring paused.',
          )
          if (result) setConfirmStart(false)
        }}
      />

      <ConfirmDialog
        open={confirmCancel}
        onClose={() => setConfirmCancel(false)}
        busy={busy}
        danger
        title="Cancel this change?"
        confirmLabel="Cancel change"
        message="The change is closed without being deployed. It stays in the record."
        onConfirm={async () => {
          const result = await run(
            () => changesApi.cancel(change.id), 'Change cancelled.',
          )
          if (result) setConfirmCancel(false)
        }}
      />

      <Modal
        open={rejectOpen}
        onClose={() => setRejectOpen(false)}
        title={`Reject ${change.reference}`}
        size="sm"
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setRejectOpen(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn-danger"
              disabled={busy || !rejectReason.trim()}
              onClick={async () => {
                const result = await run(
                  () => changesApi.reject(change.id, rejectReason.trim()),
                  'Change rejected.',
                )
                if (result) {
                  setRejectOpen(false)
                  setRejectReason('')
                }
              }}
            >
              Reject change
            </button>
          </>
        }
      >
        <Field label="Reason" required hint="The requester sees this.">
          <textarea
            className="input"
            rows={3}
            value={rejectReason}
            onChange={(event) => setRejectReason(event.target.value)}
            autoFocus
          />
        </Field>
      </Modal>

      <Modal
        open={completeOpen}
        onClose={() => setCompleteOpen(false)}
        title={`Complete ${change.reference}`}
        size="sm"
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setCompleteOpen(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={busy}
              onClick={async () => {
                const result = await run(
                  () => changesApi.complete(change.id, completeNotes.trim() || null),
                  'Deployment completed — monitoring resumed.',
                )
                if (result) {
                  setCompleteOpen(false)
                  setCompleteNotes('')
                }
              }}
            >
              {busy ? <Spinner size={15} className="text-white" /> : null}
              Complete deployment
            </button>
          </>
        }
      >
        <Field label="Deployment notes" hint="What was deployed, anything worth remembering.">
          <textarea
            className="input"
            rows={3}
            value={completeNotes}
            onChange={(event) => setCompleteNotes(event.target.value)}
            autoFocus
          />
        </Field>
        <p className="mt-2 rounded-lg bg-slate-50 px-2.5 py-2 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
          Monitoring resumes for the endpoints this change paused, and each one is
          checked immediately so a broken deployment shows up straight away.
        </p>
      </Modal>

      <Modal
        open={failOpen}
        onClose={() => setFailOpen(false)}
        title={`Mark ${change.reference} failed`}
        size="sm"
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setFailOpen(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn-danger"
              disabled={busy || !failReason.trim()}
              onClick={async () => {
                const result = await run(
                  () => changesApi.fail(change.id, failReason.trim()),
                  'Deployment marked failed — monitoring resumed.',
                )
                if (result) {
                  setFailOpen(false)
                  setFailReason('')
                }
              }}
            >
              Mark failed
            </button>
          </>
        }
      >
        <Field label="Failure reason" required>
          <textarea
            className="input"
            rows={3}
            value={failReason}
            onChange={(event) => setFailReason(event.target.value)}
            autoFocus
          />
        </Field>
        <p className="mt-2 rounded-lg bg-slate-50 px-2.5 py-2 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
          Monitoring resumes and the affected endpoints are checked immediately, so
          you can see what state the rollback left them in.
        </p>
      </Modal>
    </>
  )
}
