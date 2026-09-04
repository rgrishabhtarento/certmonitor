import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ClipboardList, FileSearch, MessageSquare, Ban } from 'lucide-react'

import { RcaStatusBadge } from './rca'
import { Field, Modal, Spinner } from './ui'
import { rcaApi } from '../lib/api'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import { formatDateTime } from '../lib/format'
import { useToast } from '../hooks/useToast'

/**
 * The RCA block on an incident.
 *
 * The important behaviour here is what it does *not* do: nothing in this
 * panel blocks resolving or closing the incident. "Not requested" is the
 * normal resting state for most incidents, and the panel says so rather than
 * nagging - an RCA process that pesters is one people learn to ignore.
 */
export default function IncidentRcaPanel({ incidentId, canWrite, teams = [] }) {
  const toast = useToast()
  const [rca, setRca] = useState(null)
  const [comments, setComments] = useState([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [comment, setComment] = useState('')

  const [requestOpen, setRequestOpen] = useState(false)
  const [declineOpen, setDeclineOpen] = useState(false)
  const [declineReason, setDeclineReason] = useState('')
  const [request, setRequest] = useState({
    owner_type: 'team',
    owner_team: '',
    due_in_days: '',
  })

  const load = useCallback(
    async ({ silent = false } = {}) => {
      if (!silent) setLoading(true)
      try {
        const [record, thread] = await Promise.all([
          rcaApi.forIncident(incidentId),
          rcaApi.comments(incidentId),
        ])
        setRca(record)
        setComments(thread || [])
      } catch {
        if (!silent) setRca(null)
      } finally {
        setLoading(false)
      }
    },
    [incidentId],
  )

  // The comment thread is a live conversation, so it polls on the fast
  // cadence. The draft in the input is separate state and is never touched;
  // polling pauses anyway while a dialog is open.
  useAutoRefresh(() => load({ silent: true }), {
    paused: busy || requestOpen || declineOpen,
  })

  useEffect(() => {
    load()
  }, [load])

  const run = async (fn, message) => {
    setBusy(true)
    try {
      const result = await fn()
      if (message) toast.success(message)
      await load()
      return result
    } catch (err) {
      toast.error(err.message)
      return null
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-700">
        <Spinner size={16} />
      </div>
    )
  }

  const status = rca?.status || 'not_requested'

  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-700">
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 px-3 py-2 dark:border-slate-700">
        <span className="flex items-center gap-1.5 text-sm font-semibold text-slate-800 dark:text-slate-100">
          <FileSearch size={15} /> Root cause analysis
        </span>
        <RcaStatusBadge status={status} overdue={rca?.is_overdue} />
        {rca?.owner_label ? (
          <span className="text-xs text-slate-500 dark:text-slate-400">
            {rca.owner_label}
          </span>
        ) : null}
        <span className="ml-auto flex gap-1.5">
          {rca ? (
            <Link to={`/rca/${rca.id}`} className="btn-secondary py-1 text-xs">
              <ClipboardList size={13} /> Open RCA
            </Link>
          ) : null}
          {canWrite && status === 'not_requested' ? (
            <>
              <button
                type="button"
                className="btn-secondary py-1 text-xs"
                onClick={() => setDeclineOpen(true)}
              >
                <Ban size={13} /> Not required
              </button>
              <button
                type="button"
                className="btn-primary py-1 text-xs"
                onClick={() => setRequestOpen(true)}
              >
                Request RCA
              </button>
            </>
          ) : null}
          {canWrite && status === 'not_required' ? (
            <button
              type="button"
              className="btn-secondary py-1 text-xs"
              onClick={() => setRequestOpen(true)}
            >
              Request anyway
            </button>
          ) : null}
        </span>
      </div>

      <div className="px-3 py-2.5">
        {status === 'not_requested' ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">
            No RCA has been requested. RCA is optional and never blocks
            resolving or closing this incident.
          </p>
        ) : status === 'not_required' ? (
          <p className="text-sm text-slate-600 dark:text-slate-300">
            Recorded as not requiring an RCA
            {rca?.not_required_reason ? `: ${rca.not_required_reason}` : '.'}
          </p>
        ) : status === 'completed' ? (
          <div className="space-y-1.5 text-sm">
            <p>
              <span className="font-medium">Root cause:</span>{' '}
              {(rca.root_cause || '').split('\n')[0]}
            </p>
            {rca.resolution ? (
              <p>
                <span className="font-medium">Resolution:</span>{' '}
                {rca.resolution.split('\n')[0]}
              </p>
            ) : null}
            {rca.preventive_actions?.length ? (
              <p>
                <span className="font-medium">Preventive actions:</span>{' '}
                {rca.preventive_actions.length}
              </p>
            ) : null}
            <p className="text-xs text-slate-400">
              Completed {formatDateTime(rca.completed_at)} by {rca.completed_by}
            </p>
          </div>
        ) : (
          <p className="text-sm text-slate-600 dark:text-slate-300">
            {rca?.owner_label
              ? `Assigned to ${rca.owner_label}.`
              : 'Not yet assigned.'}{' '}
            {rca?.due_at
              ? `Due ${formatDateTime(rca.due_at, 'dd MMM yyyy')}.`
              : ''}{' '}
            Open the RCA to fill it in — a draft can be generated from the
            monitoring, deployment and incident records.
          </p>
        )}
      </div>

      {/* ---------------------------------------------------- comments */}
      <div className="border-t border-slate-200 px-3 py-2.5 dark:border-slate-700">
        <p className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          <MessageSquare size={13} /> Comments ({comments.length})
        </p>
        {comments.length ? (
          <ol className="mb-2 space-y-1.5">
            {comments.map((entry) => (
              <li key={entry.id} className="text-sm">
                <span className="font-semibold text-slate-800 dark:text-slate-100">
                  {entry.username || 'unknown'}
                </span>
                <span className="ml-1.5 text-[11px] text-slate-400">
                  {formatDateTime(entry.created_at, 'dd MMM HH:mm')}
                </span>
                <p className="whitespace-pre-wrap break-words text-slate-700 dark:text-slate-200">
                  {entry.body}
                </p>
              </li>
            ))}
          </ol>
        ) : (
          <p className="mb-2 text-sm text-slate-400">
            Nothing yet. What gets said during the investigation is usually the
            best material an RCA can have.
          </p>
        )}

        <form
          className="flex gap-2"
          onSubmit={async (event) => {
            event.preventDefault()
            if (!comment.trim()) return
            const created = await run(
              () => rcaApi.addComment(incidentId, comment.trim()),
            )
            if (created) setComment('')
          }}
        >
          <input
            className="input flex-1 text-sm"
            placeholder="Add a comment…"
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            maxLength={4000}
          />
          <button type="submit" className="btn-secondary" disabled={busy || !comment.trim()}>
            Comment
          </button>
        </form>
      </div>

      {/* ---------------------------------------------------- dialogs */}
      <Modal
        open={requestOpen}
        onClose={() => setRequestOpen(false)}
        title="Request an RCA"
        size="sm"
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setRequestOpen(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={busy}
              onClick={async () => {
                const result = await run(
                  () =>
                    rcaApi.request(incidentId, {
                      owner_type: request.owner_type,
                      owner_team:
                        request.owner_type === 'team'
                          ? request.owner_team || null
                          : null,
                      due_in_days: request.due_in_days
                        ? Number(request.due_in_days)
                        : null,
                    }),
                  'RCA requested.',
                )
                if (result) setRequestOpen(false)
              }}
            >
              Request RCA
            </button>
          </>
        }
      >
        <p className="mb-3 text-sm text-slate-600 dark:text-slate-300">
          This opens an RCA against the incident. It changes nothing about the
          incident itself, which can still be resolved and closed normally.
        </p>
        <Field label="Assign to a team" hint="Optional — you can assign it later.">
          <input
            className="input"
            value={request.owner_team}
            onChange={(event) =>
              setRequest((current) => ({ ...current, owner_team: event.target.value }))
            }
            list="incident-rca-teams"
            placeholder="DevOps"
            maxLength={64}
          />
          <datalist id="incident-rca-teams">
            {teams.map((team) => (
              <option key={team} value={team} />
            ))}
          </datalist>
        </Field>
        <Field label="Due in (days)" hint="Optional. Without one, an RCA is never overdue.">
          <input
            type="number"
            min={1}
            max={365}
            className="input"
            value={request.due_in_days}
            onChange={(event) =>
              setRequest((current) => ({ ...current, due_in_days: event.target.value }))
            }
          />
        </Field>
      </Modal>

      <Modal
        open={declineOpen}
        onClose={() => setDeclineOpen(false)}
        title="No RCA required"
        size="sm"
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setDeclineOpen(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={busy}
              onClick={async () => {
                const result = await run(
                  () => rcaApi.notRequired(incidentId, declineReason.trim() || null),
                  'Recorded as not requiring an RCA.',
                )
                if (result) {
                  setDeclineOpen(false)
                  setDeclineReason('')
                }
              }}
            >
              Record decision
            </button>
          </>
        }
      >
        <p className="mb-3 text-sm text-slate-600 dark:text-slate-300">
          Worth recording rather than leaving blank: &ldquo;we looked and decided
          not to&rdquo; is a different state from &ldquo;nobody has looked&rdquo;,
          and only the first should leave the pending queue.
        </p>
        <Field label="Reason" hint="Optional.">
          <textarea
            className="input"
            rows={3}
            value={declineReason}
            onChange={(event) => setDeclineReason(event.target.value)}
            maxLength={2000}
            placeholder="Brief, self-resolved blip during a known maintenance window."
          />
        </Field>
      </Modal>
    </div>
  )
}
