import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  CheckCircle2,
  ClipboardList,
  FileText,
  MessageSquare,
  Plus,
  Save,
  UserCog,
  X,
} from 'lucide-react'
import clsx from 'clsx'

import {
  CATEGORY_LABELS,
  RcaStatusBadge,
  TIMELINE_SOURCE_LABELS,
  timelineTone,
} from '../components/rca'
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
} from '../components/ui'
import LiveIndicator from '../components/LiveIndicator'
import { rcaApi, usersApi } from '../lib/api'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import { formatDateTime } from '../lib/format'
import { useToast } from '../hooks/useToast'

export default function RcaDetail() {
  const { rcaId } = useParams()
  const toast = useToast()

  const [rca, setRca] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [dirty, setDirty] = useState(false)

  const [form, setForm] = useState({
    root_cause: '',
    root_cause_category: '',
    impact: '',
    resolution: '',
  })
  const [actions, setActions] = useState([])
  const [newAction, setNewAction] = useState('')
  const [timeline, setTimeline] = useState([])
  const [comment, setComment] = useState('')

  const [draftNotice, setDraftNotice] = useState(null)
  const [assignOpen, setAssignOpen] = useState(false)
  const [confirmComplete, setConfirmComplete] = useState(false)
  const [users, setUsers] = useState([])
  const [teams, setTeams] = useState([])
  const [assign, setAssign] = useState({
    owner_type: 'team',
    owner_user_id: '',
    owner_team: '',
    due_in_days: '',
  })

  const hydrate = useCallback((payload) => {
    setRca(payload)
    setForm({
      root_cause: payload.root_cause || '',
      root_cause_category: payload.root_cause_category || '',
      impact: payload.impact || '',
      resolution: payload.resolution || '',
    })
    setActions(payload.preventive_actions || [])
    setTimeline(payload.timeline || [])
    setDirty(false)
  }, [])

  const load = useCallback(async () => {
    setError(null)
    try {
      hydrate(await rcaApi.get(rcaId))
    } catch (err) {
      setError(err.message)
    }
  }, [rcaId, hydrate])

  // `hydrate` replaces the form, so this must NEVER run over unsaved work.
  // Losing a half-written root cause to a background poll would be far worse
  // than not seeing a new comment for a minute, so `dirty` hard-stops it -
  // and the manual Refresh stays available for when the user is ready.
  const { refreshing, lastRefreshedAt, refreshNow } = useAutoRefresh(load, {
    paused: dirty || busy || assignOpen || confirmComplete,
  })

  useEffect(() => {
    load()
    usersApi.list({ page: 1, page_size: 200 }).then((data) => {
      const rows = data.items || []
      setUsers(rows)
      setTeams([...new Set(rows.map((u) => u.team).filter(Boolean))].sort())
    }).catch(() => {})
    rcaApi.options().then((data) => {
      setTeams((current) => [...new Set([...current, ...(data.teams || [])])].sort())
    }).catch(() => {})
  }, [load])

  const set = (key) => (event) => {
    setForm((current) => ({ ...current, [key]: event.target.value }))
    setDirty(true)
  }

  const run = async (fn, message) => {
    setBusy(true)
    try {
      const result = await fn()
      if (message) toast.success(message)
      if (result?.id) hydrate(result)
      return result
    } catch (err) {
      toast.error(err.message)
      return null
    } finally {
      setBusy(false)
    }
  }

  const save = () =>
    run(
      () =>
        rcaApi.update(rca.id, {
          ...form,
          root_cause_category: form.root_cause_category || null,
          preventive_actions: actions,
          timeline,
        }),
      'RCA saved.',
    )

  if (!rca && !error) {
    return (
      <>
        <PageHeader title="RCA" />
        <LoadingBlock rows={8} />
      </>
    )
  }
  if (error && !rca) {
    return (
      <>
        <PageHeader title="RCA" />
        <ErrorState message={error} onRetry={load} />
      </>
    )
  }

  const readOnly = !rca.can_edit

  return (
    <>
      <Link
        to="/rca"
        className="mb-3 inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-800 dark:hover:text-slate-200"
      >
        <ArrowLeft size={15} /> All RCAs
      </Link>

      <PageHeader
        title={`RCA-${rca.id} — ${rca.endpoint_name || 'Incident'}`}
        description={
          `Incident INC-${rca.incident_id}` +
          (rca.application ? ` · ${rca.application}` : '') +
          (rca.environment ? ` · ${rca.environment}` : '')
        }
        actions={
          <>
            <LiveIndicator
              refreshing={refreshing}
              lastRefreshedAt={lastRefreshedAt}
              onRefresh={refreshNow}
              showToggle
            />
            {rca.can_assign ? (
              <button
                type="button"
                className="btn-secondary"
                onClick={() => {
                  setAssign({
                    owner_type: rca.owner_type || 'team',
                    owner_user_id: '',
                    owner_team: rca.owner_team || '',
                    due_in_days: '',
                  })
                  setAssignOpen(true)
                }}
              >
                <UserCog size={15} /> Assign
              </button>
            ) : null}
            {rca.can_edit ? (
              <>
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={busy}
                  onClick={async () => {
                    const draft = await run(() => rcaApi.draft(rca.id))
                    if (!draft) return
                    setForm({
                      root_cause: draft.root_cause || '',
                      root_cause_category: draft.root_cause_category || '',
                      impact: draft.impact || '',
                      resolution: draft.resolution || '',
                    })
                    setActions(draft.preventive_actions || [])
                    setTimeline(draft.timeline || [])
                    setDraftNotice(draft.notice)
                    setDirty(true)
                  }}
                >
                  {busy ? <Spinner size={15} /> : <FileText size={15} />}
                  Generate draft
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={save}
                  disabled={busy || !dirty}
                >
                  <Save size={15} /> Save
                </button>
              </>
            ) : null}
            {rca.can_complete ? (
              <button
                type="button"
                className="btn-primary"
                onClick={() => setConfirmComplete(true)}
                disabled={busy}
              >
                <CheckCircle2 size={15} /> Complete RCA
              </button>
            ) : null}
          </>
        }
      />

      {/* ---------------------------------------------- status banner */}
      <div className="card mb-4 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <RcaStatusBadge status={rca.status} overdue={rca.is_overdue} />
          <span className="text-sm text-slate-600 dark:text-slate-300">
            Owner:{' '}
            <span className="font-medium">
              {rca.owner_label || 'Unassigned'}
            </span>
            {rca.owner_type ? (
              <span className="text-slate-400"> ({rca.owner_type})</span>
            ) : null}
          </span>
          {rca.due_at ? (
            <span className="text-sm text-slate-500 dark:text-slate-400">
              Due {formatDateTime(rca.due_at, 'dd MMM yyyy')}
            </span>
          ) : null}
          {rca.completed_at ? (
            <span className="text-sm text-slate-500 dark:text-slate-400">
              Completed {formatDateTime(rca.completed_at)} by {rca.completed_by}
            </span>
          ) : null}
        </div>
        {rca.incident ? (
          <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400">
            The incident is <span className="font-medium">{rca.incident.status}</span>.
            RCA and incident lifecycles are independent — completing this changes
            nothing about the incident.
          </p>
        ) : null}
      </div>

      {draftNotice ? (
        <div className="card mb-4 border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          <span className="font-medium">Draft generated.</span> {draftNotice}
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        {/* --------------------------------------------------- form */}
        <div className="space-y-4 xl:col-span-2">
          <Card title="Root cause analysis">
            <div className="space-y-3">
              <Field
                label="Root cause"
                required
                hint="What actually caused it. Required to complete."
              >
                <textarea
                  className="input"
                  rows={5}
                  value={form.root_cause}
                  onChange={set('root_cause')}
                  disabled={readOnly}
                  placeholder="Describe what went wrong and why."
                />
              </Field>

              <Field label="Category" hint="Optional — it is what makes reporting possible.">
                <select
                  className="input"
                  value={form.root_cause_category}
                  onChange={set('root_cause_category')}
                  disabled={readOnly}
                >
                  <option value="">Not categorised</option>
                  {Object.entries(CATEGORY_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </Field>

              <Field label="Impact">
                <textarea
                  className="input"
                  rows={4}
                  value={form.impact}
                  onChange={set('impact')}
                  disabled={readOnly}
                  placeholder="Who and what was affected, and for how long."
                />
              </Field>

              <Field
                label="Resolution"
                required
                hint="What fixed it. Required to complete."
              >
                <textarea
                  className="input"
                  rows={4}
                  value={form.resolution}
                  onChange={set('resolution')}
                  disabled={readOnly}
                  placeholder="What was done to restore service."
                />
              </Field>
            </div>
          </Card>

          {/* ----------------------------------- preventive actions */}
          <Card title={`Preventive actions (${actions.length})`}>
            {actions.length === 0 ? (
              <p className="mb-3 text-sm text-slate-400">
                Nothing recorded yet. These are what stop the same incident
                happening a fourth time.
              </p>
            ) : (
              <ul className="mb-3 space-y-1.5">
                {actions.map((action, index) => (
                  <li key={index} className="flex items-start gap-2">
                    <input
                      type="checkbox"
                      className="mt-1 h-3.5 w-3.5 shrink-0 rounded border-slate-300"
                      checked={Boolean(action.done)}
                      disabled={readOnly}
                      onChange={() => {
                        setActions((current) =>
                          current.map((item, i) =>
                            i === index ? { ...item, done: !item.done } : item,
                          ),
                        )
                        setDirty(true)
                      }}
                    />
                    <span
                      className={clsx(
                        'min-w-0 flex-1 text-sm',
                        action.done
                          ? 'text-slate-400 line-through'
                          : 'text-slate-800 dark:text-slate-100',
                      )}
                    >
                      {action.text}
                    </span>
                    {!readOnly ? (
                      <button
                        type="button"
                        className="shrink-0 text-slate-400 hover:text-red-600"
                        aria-label={`Remove: ${action.text}`}
                        onClick={() => {
                          setActions((current) => current.filter((_, i) => i !== index))
                          setDirty(true)
                        }}
                      >
                        <X size={14} />
                      </button>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}

            {!readOnly ? (
              <form
                className="flex gap-2"
                onSubmit={(event) => {
                  event.preventDefault()
                  const text = newAction.trim()
                  if (!text) return
                  setActions((current) => [...current, { text, done: false }])
                  setNewAction('')
                  setDirty(true)
                }}
              >
                <input
                  className="input flex-1"
                  placeholder="Add a preventive action…"
                  value={newAction}
                  onChange={(event) => setNewAction(event.target.value)}
                  maxLength={500}
                />
                <button type="submit" className="btn-secondary" disabled={!newAction.trim()}>
                  <Plus size={15} /> Add
                </button>
              </form>
            ) : null}
          </Card>

          {/* ------------------------------------------- comments */}
          <Card
            title={
              <span className="flex items-center gap-1.5">
                <MessageSquare size={15} /> Incident comments ({rca.comments.length})
              </span>
            }
          >
            {rca.comments.length === 0 ? (
              <p className="mb-3 text-sm text-slate-400">
                No comments on the incident. The conversation during an
                investigation is usually the best raw material for an RCA.
              </p>
            ) : (
              <ol className="mb-4 space-y-3">
                {rca.comments.map((entry) => (
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

            <form
              className="flex flex-col gap-2 sm:flex-row"
              onSubmit={async (event) => {
                event.preventDefault()
                if (!comment.trim()) return
                const created = await run(
                  () => rcaApi.addComment(rca.incident_id, comment.trim()),
                )
                if (created) {
                  setComment('')
                  load()
                }
              }}
            >
              <input
                className="input flex-1"
                placeholder="Add a comment to the incident…"
                value={comment}
                onChange={(event) => setComment(event.target.value)}
                maxLength={4000}
              />
              <button type="submit" className="btn-primary" disabled={busy || !comment.trim()}>
                Comment
              </button>
            </form>
          </Card>
        </div>

        {/* -------------------------------------------------- sidebar */}
        <div className="space-y-4">
          {/* evidence */}
          {rca.incident ? (
            <Card title="Evidence">
              <dl>
                <DetailRow label="Incident">
                  INC-{rca.incident.id} · {rca.incident.status}
                </DetailRow>
                <DetailRow label="Started">
                  {formatDateTime(rca.incident.started_at)}
                </DetailRow>
                <DetailRow label="Resolved">
                  {rca.incident.resolved_at
                    ? formatDateTime(rca.incident.resolved_at)
                    : null}
                </DetailRow>
                <DetailRow label="Duration">
                  {rca.incident.duration_seconds
                    ? `${Math.round(rca.incident.duration_seconds / 60)} minutes`
                    : null}
                </DetailRow>
                <DetailRow label="Reason">{rca.incident.reason}</DetailRow>
                <DetailRow label="Error">{rca.incident.error_message}</DetailRow>
                <DetailRow label="Failed checks">
                  {rca.incident.failed_check_count}
                </DetailRow>
                <DetailRow label="Endpoint">
                  {rca.incident.endpoint_id ? (
                    <Link
                      to={`/endpoints/${rca.incident.endpoint_id}`}
                      className="text-brand-600 hover:underline dark:text-brand-400"
                    >
                      {rca.endpoint_name}
                    </Link>
                  ) : (
                    rca.endpoint_name
                  )}
                </DetailRow>
                {rca.change_id ? (
                  <DetailRow label="Deployment">
                    <Link
                      to={`/changes/${rca.change_id}`}
                      className="text-brand-600 hover:underline dark:text-brand-400"
                    >
                      View the change
                    </Link>
                  </DetailRow>
                ) : null}
              </dl>
            </Card>
          ) : null}

          {/* timeline */}
          <Card title={`Timeline (${timeline.length})`}>
            {timeline.length === 0 ? (
              <p className="text-sm text-slate-400">
                Generate a draft to assemble a timeline from the monitoring,
                deployment and incident records.
              </p>
            ) : (
              <ol className="space-y-2.5">
                {timeline.map((entry, index) => (
                  <li key={index} className="flex gap-2.5">
                    <span
                      className={clsx(
                        'mt-1.5 h-2 w-2 shrink-0 rounded-full',
                        timelineTone(entry.kind),
                      )}
                      aria-hidden="true"
                    />
                    <div className="min-w-0 flex-1 border-b border-slate-100 pb-2 last:border-0 dark:border-slate-800">
                      <p className="tnum text-[11px] text-slate-400">
                        {entry.at ? formatDateTime(entry.at, 'dd MMM HH:mm:ss') : '—'}
                        <span className="ml-1.5">
                          · {TIMELINE_SOURCE_LABELS[entry.source] || entry.source}
                        </span>
                      </p>
                      <p className="break-words text-sm text-slate-700 dark:text-slate-200">
                        {entry.detail}
                      </p>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </Card>

          {/* similar past RCAs */}
          {rca.similar_past?.length ? (
            <Card
              title={
                <span className="flex items-center gap-1.5">
                  <ClipboardList size={15} /> Similar past incidents
                </span>
              }
            >
              <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">
                Historical context only. A similar incident before does not mean
                this one has the same cause.
              </p>
              <ul className="space-y-2.5">
                {rca.similar_past.map((item) => (
                  <li key={item.rca_id} className="border-b border-slate-100 pb-2 last:border-0 dark:border-slate-800">
                    <p className="text-xs text-slate-400">
                      <Link
                        to={`/rca/${item.rca_id}`}
                        className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                      >
                        RCA-{item.rca_id}
                      </Link>
                      {' · '}
                      {item.days_ago != null ? `${item.days_ago} days ago` : ''}
                      {item.same_endpoint ? ' · same endpoint' : ''}
                    </p>
                    <p className="text-sm text-slate-700 dark:text-slate-200">
                      {(item.root_cause || '').split('\n')[0]}
                    </p>
                    {item.resolution ? (
                      <p className="text-xs text-slate-500 dark:text-slate-400">
                        Fixed by: {item.resolution.split('\n')[0]}
                      </p>
                    ) : null}
                  </li>
                ))}
              </ul>
            </Card>
          ) : null}
        </div>
      </div>

      {/* -------------------------------------------------- dialogs */}
      <Modal
        open={assignOpen}
        onClose={() => setAssignOpen(false)}
        title={`Assign RCA-${rca.id}`}
        size="sm"
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setAssignOpen(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={
                busy ||
                (assign.owner_type === 'team' && !assign.owner_team.trim()) ||
                (assign.owner_type === 'individual' && !assign.owner_user_id)
              }
              onClick={async () => {
                const result = await run(
                  () =>
                    rcaApi.assign(rca.id, {
                      owner_type: assign.owner_type,
                      owner_user_id: assign.owner_user_id || null,
                      owner_team: assign.owner_team || null,
                      due_in_days: assign.due_in_days
                        ? Number(assign.due_in_days)
                        : null,
                    }),
                  'RCA assigned.',
                )
                if (result) setAssignOpen(false)
              }}
            >
              Assign
            </button>
          </>
        }
      >
        <Field label="Owner type">
          <select
            className="input"
            value={assign.owner_type}
            onChange={(event) =>
              setAssign((current) => ({ ...current, owner_type: event.target.value }))
            }
          >
            <option value="team">Team</option>
            <option value="individual">Individual</option>
          </select>
        </Field>

        {assign.owner_type === 'team' ? (
          <Field label="Team" required hint="Anyone whose team label matches can edit this RCA.">
            <input
              className="input"
              value={assign.owner_team}
              onChange={(event) =>
                setAssign((current) => ({ ...current, owner_team: event.target.value }))
              }
              list="rca-teams"
              placeholder="DevOps"
              maxLength={64}
            />
            <datalist id="rca-teams">
              {teams.map((team) => (
                <option key={team} value={team} />
              ))}
            </datalist>
          </Field>
        ) : (
          <Field label="User" required>
            <select
              className="input"
              value={assign.owner_user_id}
              onChange={(event) =>
                setAssign((current) => ({
                  ...current,
                  owner_user_id: event.target.value,
                }))
              }
            >
              <option value="">Select a user…</option>
              {users.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.username}
                  {item.team ? ` (${item.team})` : ''}
                </option>
              ))}
            </select>
          </Field>
        )}

        <Field label="Due in (days)" hint="Optional. Without one, an RCA is never overdue.">
          <input
            type="number"
            min={0}
            max={365}
            className="input"
            value={assign.due_in_days}
            onChange={(event) =>
              setAssign((current) => ({ ...current, due_in_days: event.target.value }))
            }
          />
        </Field>
      </Modal>

      <ConfirmDialog
        open={confirmComplete}
        onClose={() => setConfirmComplete(false)}
        busy={busy}
        title="Complete this RCA?"
        confirmLabel="Complete RCA"
        message="Confirm that the root cause and resolution are documented. This does not change the incident — the two lifecycles are independent."
        onConfirm={async () => {
          if (dirty) {
            const saved = await save()
            if (!saved) return
          }
          const result = await run(
            () => rcaApi.complete(rca.id), 'RCA completed.',
          )
          if (result) setConfirmComplete(false)
        }}
      />
    </>
  )
}
