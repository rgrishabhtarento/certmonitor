import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { CheckCircle2, RefreshCw } from 'lucide-react'

import {
  Clamp,
  EmptyState,
  ErrorState,
  LoadingBlock,
  Modal,
  PageHeader,
  Pagination,
  SearchInput,
  Spinner,
  StatusBadge,
} from '../components/ui'
import { endpointsApi, incidentsApi } from '../lib/api'
import {
  FAILURE_REASON_LABELS,
  formatDateTime,
  formatDuration,
  formatMs,
  formatRelative,
  humanise,
} from '../lib/format'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'

export default function Incidents() {
  const { can } = useAuth()
  const toast = useToast()
  const [searchParams, setSearchParams] = useSearchParams()
  const canWrite = can('incident:write')

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState(searchParams.get('status') || '')
  const [environment, setEnvironment] = useState('')
  const [minDuration, setMinDuration] = useState('')

  const [data, setData] = useState(null)
  const [filters, setFilters] = useState({ environments: [] })
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState(null)

  const [selected, setSelected] = useState(null)
  const [notes, setNotes] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    endpointsApi.filters().then(setFilters).catch(() => {})
  }, [])

  useEffect(() => {
    setSearchParams(status ? { status } : {}, { replace: true })
  }, [status, setSearchParams])

  const load = useCallback(
    async ({ silent = false } = {}) => {
      if (silent) setRefreshing(true)
      else setLoading(true)
      setError(null)
      try {
        const payload = await incidentsApi.list({
          page,
          page_size: pageSize,
          search,
          status: status || undefined,
          environment: environment || undefined,
          min_duration_seconds: minDuration || undefined,
        })
        setData(payload)
      } catch (err) {
        setError(err.message)
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [page, pageSize, search, status, environment, minDuration],
  )

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    setPage(1)
  }, [search, status, environment, minDuration, pageSize])

  const openIncident = async (incident) => {
    try {
      const full = await incidentsApi.get(incident.id)
      setSelected(full)
      setNotes(full.notes || '')
    } catch (err) {
      toast.error(err.message)
    }
  }

  const saveIncident = async ({ acknowledge } = {}) => {
    if (!selected) return
    setSaving(true)
    try {
      const payload = { notes }
      if (acknowledge !== undefined) payload.acknowledge = acknowledge
      const updated = await incidentsApi.update(selected.id, payload)
      setSelected(updated)
      toast.success('Incident updated.')
      load({ silent: true })
    } catch (err) {
      toast.error(err.message)
    } finally {
      setSaving(false)
    }
  }

  const items = data?.items || []

  return (
    <>
      <PageHeader
        title="Incidents"
        description={
          data
            ? `${data.meta.total.toLocaleString()} recorded · one incident spans a continuous outage`
            : 'Outage history'
        }
        actions={
          <button
            type="button"
            className="btn-secondary"
            onClick={() => load({ silent: true })}
            disabled={refreshing}
          >
            {refreshing ? <Spinner size={15} /> : <RefreshCw size={15} />}
            <span className="hidden sm:inline">Refresh</span>
          </button>
        }
      />

      <div className="card mb-4 p-3">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search endpoint or error…"
          />
          <select
            className="input"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            aria-label="Filter by incident status"
          >
            <option value="">All incidents</option>
            <option value="open">Open only</option>
            <option value="resolved">Resolved only</option>
          </select>
          <select
            className="input"
            value={environment}
            onChange={(event) => setEnvironment(event.target.value)}
            aria-label="Filter by environment"
          >
            <option value="">All environments</option>
            {filters.environments.map((item) => (
              <option key={item.id} value={item.id}>
                {item.display_name || item.name}
              </option>
            ))}
          </select>
          <select
            className="input"
            value={minDuration}
            onChange={(event) => setMinDuration(event.target.value)}
            aria-label="Filter by minimum duration"
          >
            <option value="">Any duration</option>
            <option value="60">Longer than 1 minute</option>
            <option value="300">Longer than 5 minutes</option>
            <option value="1800">Longer than 30 minutes</option>
            <option value="3600">Longer than 1 hour</option>
          </select>
        </div>
      </div>

      <div className="card">
        {loading && !data ? (
          <div className="p-4">
            <LoadingBlock rows={6} />
          </div>
        ) : error && !data ? (
          <div className="p-4">
            <ErrorState message={error} onRetry={() => load()} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            icon={CheckCircle2}
            title="No incidents"
            description={
              status === 'open'
                ? 'Nothing is currently failing.'
                : 'An incident opens once an endpoint fails its configured number of consecutive checks.'
            }
          />
        ) : (
          <>
            <div className={`table-wrap ${refreshing ? 'opacity-70' : ''}`}>
              <table className="table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Endpoint</th>
                    <th>Status</th>
                    <th>Reason</th>
                    <th>Started</th>
                    <th>Resolved</th>
                    <th className="text-right">Duration</th>
                    <th className="text-right">Failed checks</th>
                    <th className="w-10" />
                  </tr>
                </thead>
                <tbody>
                  {items.map((incident) => (
                    <tr key={incident.id}>
                      <td className="tnum text-slate-400">#{incident.id}</td>
                      <td>
                        <div className="max-w-[16rem]">
                          <Clamp width="16rem" title={incident.endpoint?.name}>
                            <Link
                              to={`/endpoints/${incident.endpoint_id}`}
                              className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                            >
                              {incident.endpoint?.name || 'Endpoint'}
                            </Link>
                          </Clamp>
                          <p
                            className="truncate text-[11px] text-slate-400"
                            title={incident.endpoint?.url}
                          >
                            {incident.endpoint?.url}
                          </p>
                        </div>
                      </td>
                      <td>
                        <StatusBadge status={incident.status === 'open' ? 'down' : 'up'} />
                        {incident.acknowledged_at ? (
                          <p className="text-[11px] text-slate-400">acknowledged</p>
                        ) : null}
                      </td>
                      <td className="max-w-[14rem]">
                        <span className="text-slate-700 dark:text-slate-200">
                          {incident.reason_label ||
                            humanise(incident.reason, FAILURE_REASON_LABELS)}
                        </span>
                        {incident.error_message ? (
                          <p
                            className="truncate text-[11px] text-slate-400"
                            title={incident.error_message}
                          >
                            {incident.error_message}
                          </p>
                        ) : null}
                      </td>
                      <td className="whitespace-nowrap">
                        {formatDateTime(incident.started_at, 'dd MMM HH:mm')}
                        <p className="text-[11px] text-slate-400">
                          {formatRelative(incident.started_at)}
                        </p>
                      </td>
                      <td className="whitespace-nowrap">
                        {incident.resolved_at
                          ? formatDateTime(incident.resolved_at, 'dd MMM HH:mm')
                          : '—'}
                      </td>
                      <td className="tnum whitespace-nowrap text-right font-medium">
                        {incident.status === 'open' ? (
                          <span className="text-red-600 dark:text-red-400">
                            {formatDuration(
                              (Date.now() - new Date(incident.started_at).getTime()) / 1000,
                            )}
                          </span>
                        ) : (
                          formatDuration(incident.duration_seconds)
                        )}
                      </td>
                      <td className="tnum text-right">{incident.failed_check_count}</td>
                      <td className="text-right">
                        <button
                          type="button"
                          className="btn-ghost btn-sm"
                          onClick={() => openIncident(incident)}
                        >
                          Open
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              meta={data.meta}
              onPageChange={setPage}
              onPageSizeChange={setPageSize}
            />
          </>
        )}
      </div>

      {/* -------------------------------------------- incident dialog */}
      <Modal
        open={Boolean(selected)}
        onClose={() => setSelected(null)}
        title={selected ? `Incident #${selected.id}` : ''}
        size="lg"
        footer={
          canWrite && selected ? (
            <>
              <button
                type="button"
                className="btn-secondary mr-auto"
                onClick={() => saveIncident({ acknowledge: !selected.acknowledged_at })}
                disabled={saving}
              >
                {selected.acknowledged_at ? 'Un-acknowledge' : 'Acknowledge'}
              </button>
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setSelected(null)}
              >
                Close
              </button>
              <button
                type="button"
                className="btn-primary"
                onClick={() => saveIncident()}
                disabled={saving}
              >
                {saving ? <Spinner size={15} className="text-white" /> : null}
                Save notes
              </button>
            </>
          ) : (
            <button type="button" className="btn-secondary" onClick={() => setSelected(null)}>
              Close
            </button>
          )
        }
      >
        {selected ? (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={selected.status === 'open' ? 'down' : 'up'} />
              <Link
                to={`/endpoints/${selected.endpoint_id}`}
                className="font-medium text-brand-600 hover:underline dark:text-brand-400"
              >
                {selected.endpoint?.name}
              </Link>
              {selected.endpoint?.environment ? (
                <span className="chip">{selected.endpoint.environment}</span>
              ) : null}
            </div>

            <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-xs text-slate-500">Started</dt>
                <dd className="font-medium">{formatDateTime(selected.started_at)}</dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">Resolved</dt>
                <dd className="font-medium">
                  {selected.resolved_at ? formatDateTime(selected.resolved_at) : 'Ongoing'}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">Duration</dt>
                <dd className="tnum font-medium">
                  {selected.status === 'open'
                    ? formatDuration(
                        (Date.now() - new Date(selected.started_at).getTime()) / 1000,
                      )
                    : formatDuration(selected.duration_seconds)}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">Failed checks</dt>
                <dd className="tnum font-medium">{selected.failed_check_count}</dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">Reason</dt>
                <dd className="font-medium">
                  {selected.reason_label ||
                    humanise(selected.reason, FAILURE_REASON_LABELS)}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">Recovery</dt>
                <dd className="font-medium">
                  {selected.recovery_status_code
                    ? `HTTP ${selected.recovery_status_code}${
                        selected.recovery_response_time_ms
                          ? ` in ${formatMs(selected.recovery_response_time_ms)}`
                          : ''
                      }`
                    : '—'}
                </dd>
              </div>
            </dl>

            {selected.error_message ? (
              <div>
                <p className="label">Error</p>
                <pre className="overflow-x-auto rounded bg-slate-50 p-2 font-mono text-xs text-slate-700 dark:bg-slate-800 dark:text-slate-300">
                  {selected.error_message}
                </pre>
              </div>
            ) : null}

            {selected.timeline?.length ? (
              <div>
                <p className="label">Timeline</p>
                <ol className="space-y-1 border-l-2 border-slate-200 pl-3 dark:border-slate-700">
                  {selected.timeline.map((entry, index) => (
                    <li key={index} className="text-xs text-slate-600 dark:text-slate-300">
                      <span className="font-medium">
                        {formatDateTime(entry.at, 'dd MMM HH:mm:ss')}
                      </span>{' '}
                      — <span className="capitalize">{entry.kind.replace(/_/g, ' ')}</span>:{' '}
                      {entry.detail}
                    </li>
                  ))}
                </ol>
              </div>
            ) : null}

            <div>
              <label htmlFor="incident-notes" className="label">
                Notes
              </label>
              <textarea
                id="incident-notes"
                className="input"
                rows={4}
                value={notes}
                onChange={(event) => setNotes(event.target.value)}
                placeholder="Root cause, follow-up actions, links to the post-mortem…"
                disabled={!canWrite}
              />
              <p className="hint">
                Incidents are opened and closed by the monitoring worker from observed
                state. Notes and acknowledgement are the human record.
              </p>
            </div>
          </div>
        ) : null}
      </Modal>
    </>
  )
}
