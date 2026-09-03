import { useEffect, useMemo, useState } from 'react'
import { AlertCircle, Search } from 'lucide-react'

import { Field, Modal, Spinner } from './ui'
import { changesApi, endpointsApi } from '../lib/api'
import { useToast } from '../hooks/useToast'

const RISKS = [
  { value: 'low', label: 'Low — routine, easily reversed' },
  { value: 'medium', label: 'Medium — user-visible, has a rollback' },
  { value: 'high', label: 'High — hard to reverse, or wide blast radius' },
]

/** Split an ISO timestamp into the date and time inputs the form uses. */
function splitDateTime(iso) {
  if (!iso) return { date: '', time: '' }
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return { date: '', time: '' }
  const pad = (n) => String(n).padStart(2, '0')
  return {
    date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
    time: `${pad(d.getHours())}:${pad(d.getMinutes())}`,
  }
}

export default function ChangeForm({ open, onClose, onSaved, change, environments }) {
  const toast = useToast()
  const isEdit = Boolean(change?.id)

  const [form, setForm] = useState({
    title: '',
    application: '',
    environment: '',
    description: '',
    date: '',
    time: '',
    expected_duration_minutes: 30,
    risk: 'low',
    rollback_plan: '',
    deployment_notes: '',
  })
  const [selected, setSelected] = useState([])
  const [endpointSearch, setEndpointSearch] = useState('')
  const [endpoints, setEndpoints] = useState([])
  const [applications, setApplications] = useState([])
  const [errors, setErrors] = useState({})
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    setErrors({})
    setError(null)
    if (change) {
      const { date, time } = splitDateTime(change.expected_start_at)
      setForm({
        title: change.title || '',
        application: change.application || '',
        // The change carries the environment *name*; the select is keyed by id,
        // so this is resolved to an id at render time (see `environmentValue`).
        environment: change.environment || '',
        description: change.description || '',
        date,
        time,
        expected_duration_minutes: change.expected_duration_minutes ?? 30,
        risk: change.risk || 'low',
        rollback_plan: change.rollback_plan || '',
        deployment_notes: change.deployment_notes || '',
      })
      setSelected((change.endpoints || []).map((e) => e.id))
    } else {
      const now = new Date(Date.now() + 60 * 60 * 1000)
      const pad = (n) => String(n).padStart(2, '0')
      setForm({
        title: '',
        application: '',
        environment: '',
        description: '',
        date: `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`,
        time: `${pad(now.getHours())}:00`,
        expected_duration_minutes: 30,
        risk: 'low',
        rollback_plan: '',
        deployment_notes: '',
      })
      setSelected([])
    }
    // `environments` is deliberately not a dependency: it arrives from its own
    // fetch, and re-running this would wipe whatever the user had typed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, change])

  // Load endpoints so the requester can mark which ones the deployment
  // touches - that list is what gets paused when it starts.
  useEffect(() => {
    if (!open) return
    endpointsApi
      .list({ page: 1, page_size: 200, include_uptime: false })
      .then((data) => setEndpoints(data.items || []))
      .catch(() => setEndpoints([]))
    // Existing application names, so the free-text field stays consistent
    // instead of accumulating spelling variants.
    changesApi
      .options()
      .then((data) => setApplications(data.applications || []))
      .catch(() => setApplications([]))
  }, [open])

  const visibleEndpoints = useMemo(() => {
    const needle = endpointSearch.trim().toLowerCase()
    const matches = endpoints.filter(
      (e) =>
        !needle ||
        e.name.toLowerCase().includes(needle) ||
        e.url.toLowerCase().includes(needle),
    )
    // Keep chosen endpoints visible even when they fall outside the search.
    const chosen = endpoints.filter(
      (e) => selected.includes(e.id) && !matches.includes(e),
    )
    return [...chosen, ...matches].slice(0, 80)
  }, [endpoints, endpointSearch, selected])

  // On edit the stored value is an environment name; once the option list has
  // loaded it resolves to that environment's id so the select shows it.
  const environmentValue = useMemo(() => {
    if (!form.environment) return ''
    const list = environments || []
    if (list.some((item) => item.id === form.environment)) return form.environment
    const byName = list.find((item) => item.name === form.environment)
    return byName ? byName.id : ''
  }, [form.environment, environments])

  const set = (key) => (event) =>
    setForm((current) => ({ ...current, [key]: event.target.value }))

  const submit = async (event) => {
    event.preventDefault()
    setError(null)

    const next = {}
    if (!form.title.trim()) next.title = 'A title is required.'
    if (!form.application.trim()) next.application = 'An application is required.'
    if (!form.description.trim()) next.description = 'A description is required.'
    if (!form.date || !form.time) next.date = 'A deployment date and time is required.'
    setErrors(next)
    if (Object.keys(next).length) return

    // The datetime-local pair is local time; toISOString converts to UTC,
    // which is what the API stores.
    const expected = new Date(`${form.date}T${form.time}`)
    if (Number.isNaN(expected.getTime())) {
      setErrors({ date: 'That date and time is not valid.' })
      return
    }

    const payload = {
      title: form.title.trim(),
      application: form.application.trim(),
      environment: form.environment || null,
      description: form.description.trim(),
      expected_start_at: expected.toISOString(),
      expected_duration_minutes: Number(form.expected_duration_minutes) || 30,
      risk: form.risk,
      endpoint_ids: selected,
      rollback_plan: form.rollback_plan || null,
      deployment_notes: form.deployment_notes || null,
    }

    setBusy(true)
    try {
      const saved = isEdit
        ? await changesApi.update(change.id, payload)
        : await changesApi.create(payload)
      toast.success(
        isEdit ? `${saved.reference} updated.` : `${saved.reference} created as a draft.`,
      )
      onSaved(saved)
    } catch (err) {
      setError(err.message)
      if (err.fields) setErrors(err.fields)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={isEdit ? `Edit ${change?.reference}` : 'New change request'}
      size="lg"
      footer={
        <>
          <button type="button" className="btn-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" form="change-form" className="btn-primary" disabled={busy}>
            {busy ? <Spinner size={15} className="text-white" /> : null}
            {isEdit ? 'Save changes' : 'Create draft'}
          </button>
        </>
      }
    >
      <form id="change-form" onSubmit={submit} className="space-y-4">
        {error ? (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/50 dark:text-red-200"
          >
            <AlertCircle size={16} className="mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        ) : null}

        <Field label="Change title" required error={errors.title}>
          <input
            className="input"
            value={form.title}
            onChange={set('title')}
            placeholder="Translation API — release 2.4.0"
            maxLength={200}
            autoFocus
          />
        </Field>

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Application" required error={errors.application}>
            <input
              className="input"
              value={form.application}
              onChange={set('application')}
              placeholder="Translation API"
              maxLength={128}
              list="change-applications"
            />
            <datalist id="change-applications">
              {applications.map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
          </Field>
          <Field label="Environment">
            <select className="input" value={environmentValue} onChange={set('environment')}>
              <option value="">Unassigned</option>
              {(environments || []).map((environment) => (
                <option key={environment.id} value={environment.id}>
                  {environment.display_name || environment.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Risk">
            <select className="input" value={form.risk} onChange={set('risk')}>
              {RISKS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <Field label="Description" required error={errors.description}>
          <textarea
            className="input"
            rows={3}
            value={form.description}
            onChange={set('description')}
            placeholder="What is changing, and why."
          />
        </Field>

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Deployment date" required error={errors.date}>
            <input type="date" className="input" value={form.date} onChange={set('date')} />
          </Field>
          <Field label="Deployment time" required>
            <input type="time" className="input" value={form.time} onChange={set('time')} />
          </Field>
          <Field label="Expected duration (minutes)">
            <input
              type="number"
              min={1}
              max={1440}
              className="input"
              value={form.expected_duration_minutes}
              onChange={set('expected_duration_minutes')}
            />
          </Field>
        </div>

        {/* ------------------------------------------- affected endpoints */}
        <Field
          label={`Affected endpoints (${selected.length} selected)`}
          hint="Monitoring for these is paused automatically while the deployment runs, so it raises no false incidents or alerts."
        >
          <div className="rounded-lg border border-slate-300 dark:border-slate-700">
            <div className="relative border-b border-slate-200 p-2 dark:border-slate-700">
              <Search
                size={14}
                className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-slate-400"
              />
              <input
                className="input pl-7 text-xs"
                placeholder="Filter endpoints…"
                value={endpointSearch}
                onChange={(event) => setEndpointSearch(event.target.value)}
              />
            </div>
            <div className="max-h-52 overflow-y-auto p-2">
              {visibleEndpoints.length === 0 ? (
                <p className="px-1 py-2 text-xs text-slate-400">
                  No endpoints match.
                </p>
              ) : (
                visibleEndpoints.map((endpoint) => (
                  <label
                    key={endpoint.id}
                    className="flex cursor-pointer items-center gap-2 rounded px-1 py-1 hover:bg-slate-50 dark:hover:bg-slate-800"
                  >
                    <input
                      type="checkbox"
                      className="h-3.5 w-3.5 shrink-0 rounded border-slate-300"
                      checked={selected.includes(endpoint.id)}
                      onChange={() =>
                        setSelected((current) =>
                          current.includes(endpoint.id)
                            ? current.filter((id) => id !== endpoint.id)
                            : [...current, endpoint.id],
                        )
                      }
                    />
                    <span className="min-w-0 flex-1 truncate text-xs">
                      <span className="font-medium text-slate-800 dark:text-slate-200">
                        {endpoint.name}
                      </span>
                      <span className="ml-1.5 font-mono text-[11px] text-slate-400">
                        {endpoint.hostname}
                      </span>
                    </span>
                    {endpoint.environment ? (
                      <span className="chip shrink-0">
                        {endpoint.environment.name}
                      </span>
                    ) : null}
                  </label>
                ))
              )}
            </div>
          </div>
        </Field>

        <Field label="Rollback plan" hint="How this is undone if it goes wrong.">
          <textarea
            className="input"
            rows={2}
            value={form.rollback_plan}
            onChange={set('rollback_plan')}
          />
        </Field>

        <Field label="Deployment notes">
          <textarea
            className="input"
            rows={2}
            value={form.deployment_notes}
            onChange={set('deployment_notes')}
          />
        </Field>
      </form>
    </Modal>
  )
}
