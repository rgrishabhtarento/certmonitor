import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  Clock,
  Pause,
  Pencil,
  Play,
  RefreshCw,
  ShieldCheck,
  Stethoscope,
  Trash2,
  Zap,
} from 'lucide-react'
import clsx from 'clsx'

import DiagnosticsPanel from '../components/DiagnosticsPanel'
import EndpointForm from '../components/EndpointForm'
import {
  ChartFrame,
  LatencyBreakdownChart,
  SeriesTable,
  UptimeChart,
  useChartMode,
  withSeriesLabels,
} from '../components/charts'
import {
  Card,
  ConfirmDialog,
  DetailRow,
  EmptyState,
  ErrorState,
  LoadingBlock,
  Metric,
  PageHeader,
  Pagination,
  Spinner,
  SslBadge,
  StatusBadge,
  TagChip,
} from '../components/ui'
import { endpointsApi, incidentsApi, settingsApi } from '../lib/api'
import {
  FAILURE_REASON_LABELS,
  formatBytes,
  formatDateTime,
  formatDaysRemaining,
  formatDuration,
  formatInterval,
  formatMs,
  formatNumber,
  formatPercent,
  formatRelative,
  humanise,
} from '../lib/format'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'

const WINDOW_LABELS = {
  '24h': 'Last 24 hours',
  '7d': 'Last 7 days',
  '30d': 'Last 30 days',
  '90d': 'Last 90 days',
}

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'history', label: 'Check history' },
  { id: 'incidents', label: 'Incidents' },
  { id: 'certificate', label: 'Certificate' },
  { id: 'configuration', label: 'Configuration' },
]

export default function EndpointDetail() {
  const { endpointId } = useParams()
  const navigate = useNavigate()
  const { can } = useAuth()
  const toast = useToast()
  const mode = useChartMode()

  const canWrite = can('endpoint:write')
  const canCheck = can('endpoint:check')
  const canDelete = can('endpoint:delete')

  const [tab, setTab] = useState('overview')
  const [window_, setWindow] = useState('24h')

  const [endpoint, setEndpoint] = useState(null)
  const [stats, setStats] = useState(null)
  const [certificate, setCertificate] = useState(null)
  const [certificateError, setCertificateError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [checking, setChecking] = useState(false)

  const [history, setHistory] = useState(null)
  const [historyPage, setHistoryPage] = useState(1)
  const [historyStatus, setHistoryStatus] = useState('')
  const [incidents, setIncidents] = useState(null)
  const [config, setConfig] = useState(null)
  const [filters, setFilters] = useState({ environments: [], tags: [] })

  const [formOpen, setFormOpen] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const [diagnosing, setDiagnosing] = useState(false)
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false)
  const [diagnostics, setDiagnostics] = useState(null)
  const [diagnosticsError, setDiagnosticsError] = useState(null)

  const runDiagnostics = async () => {
    setDiagnosticsOpen(true)
    setDiagnosing(true)
    setDiagnostics(null)
    setDiagnosticsError(null)
    try {
      setDiagnostics(await endpointsApi.diagnose(endpointId))
    } catch (err) {
      setDiagnosticsError(err.message)
    } finally {
      setDiagnosing(false)
    }
  }

  // ------------------------------------------------------------- loaders
  const loadEndpoint = useCallback(async () => {
    const payload = await endpointsApi.get(endpointId)
    setEndpoint(payload)
    return payload
  }, [endpointId])

  const loadStats = useCallback(async () => {
    const payload = await endpointsApi.stats(endpointId, window_)
    setStats(payload)
  }, [endpointId, window_])

  const loadCertificate = useCallback(async () => {
    try {
      const payload = await endpointsApi.ssl(endpointId)
      setCertificate(payload)
      setCertificateError(null)
    } catch (err) {
      setCertificate(null)
      setCertificateError(err.message)
    }
  }, [endpointId])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    Promise.all([loadEndpoint(), loadStats()])
      .then(() => {
        if (!cancelled) loadCertificate()
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [loadEndpoint, loadStats, loadCertificate])

  useEffect(() => {
    if (canWrite) {
      endpointsApi.filters().then(setFilters).catch(() => {})
      settingsApi
        .get()
        .then((payload) => setConfig(payload.effective))
        .catch(() => {})
    }
  }, [canWrite])

  useEffect(() => {
    if (tab !== 'history') return
    endpointsApi
      .history(endpointId, {
        page: historyPage,
        page_size: 50,
        status: historyStatus || undefined,
        include_headers: true,
      })
      .then(setHistory)
      .catch((err) => toast.error(err.message))
  }, [tab, endpointId, historyPage, historyStatus, toast])

  useEffect(() => {
    if (tab !== 'incidents') return
    incidentsApi
      .list({ endpoint_id: endpointId, page: 1, page_size: 50 })
      .then(setIncidents)
      .catch((err) => toast.error(err.message))
  }, [tab, endpointId, toast])

  // ------------------------------------------------------------- actions
  const runCheck = async () => {
    setChecking(true)
    try {
      const result = await endpointsApi.check(endpointId, true)
      const detail = [
        result.status.toUpperCase(),
        result.http_status_code ? `HTTP ${result.http_status_code}` : null,
        result.response_time_ms ? `${Math.round(result.response_time_ms)} ms` : null,
        // Say so when the answer came from a different path, otherwise a pass
        // against a URL the operator knows 404s looks like a bug.
        result.resolved_path ? `via ${result.resolved_path}` : null,
      ]
        .filter(Boolean)
        .join(' · ')
      if (result.status === 'up') toast.success(detail)
      else if (result.status === 'degraded') toast.warning(detail)
      else toast.error(`${detail}${result.error_message ? ` — ${result.error_message}` : ''}`)

      await Promise.all([loadEndpoint(), loadStats(), loadCertificate()])
      if (tab === 'history') setHistoryPage(1)
    } catch (err) {
      toast.error(err.message)
    } finally {
      setChecking(false)
    }
  }

  const toggleMonitoring = async () => {
    try {
      await endpointsApi.setMonitoring(endpointId, {
        is_paused: !endpoint.is_paused,
        monitoring_enabled: true,
      })
      toast.success(endpoint.is_paused ? 'Monitoring resumed.' : 'Monitoring paused.')
      loadEndpoint()
    } catch (err) {
      toast.error(err.message)
    }
  }

  const doDelete = async () => {
    setDeleting(true)
    try {
      await endpointsApi.remove(endpointId)
      toast.success(`'${endpoint.name}' deleted.`)
      navigate('/endpoints', { replace: true })
    } catch (err) {
      toast.error(err.message)
      setDeleting(false)
    }
  }

  const series = useMemo(
    () => withSeriesLabels(stats?.series, stats?.bucket_seconds || 3600),
    [stats],
  )

  if (loading && !endpoint) {
    return (
      <>
        <PageHeader title="Endpoint" />
        <LoadingBlock rows={8} />
      </>
    )
  }

  if (error && !endpoint) {
    return (
      <>
        <PageHeader title="Endpoint" />
        <ErrorState message={error} onRetry={() => loadEndpoint()} />
      </>
    )
  }

  const windows = stats?.windows || {}
  const current = windows[window_] || {}

  return (
    <>
      <Link
        to="/endpoints"
        className="mb-3 inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-800 dark:hover:text-slate-200"
      >
        <ArrowLeft size={15} /> All endpoints
      </Link>

      <PageHeader
        title={endpoint.name}
        description={endpoint.url}
        actions={
          <>
            {canCheck ? (
              <>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={runCheck}
                  disabled={checking}
                >
                  {checking ? <Spinner size={15} /> : <Zap size={15} />}
                  Check now
                </button>
                <button
                  type="button"
                  className={clsx(
                    'btn-secondary',
                    endpoint.current_status === 'down' &&
                      'border-red-300 text-red-700 dark:border-red-800 dark:text-red-300',
                  )}
                  onClick={runDiagnostics}
                  disabled={diagnosing}
                  title="Isolate which layer is failing and what to do about it"
                >
                  {diagnosing ? <Spinner size={15} /> : <Stethoscope size={15} />}
                  Diagnose
                </button>
              </>
            ) : null}
            {canWrite ? (
              <>
                <button type="button" className="btn-secondary" onClick={toggleMonitoring}>
                  {endpoint.is_paused ? <Play size={15} /> : <Pause size={15} />}
                  {endpoint.is_paused ? 'Resume' : 'Pause'}
                </button>
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() => setFormOpen(true)}
                >
                  <Pencil size={15} /> Edit
                </button>
              </>
            ) : null}
            {canDelete ? (
              <button
                type="button"
                className="btn-danger"
                onClick={() => setConfirmDelete(true)}
              >
                <Trash2 size={15} />
                <span className="hidden sm:inline">Delete</span>
              </button>
            ) : null}
          </>
        }
      />

      {/* ------------------------------------------------ current status */}
      <div
        className={clsx(
          'card mb-4 border-l-4 p-4',
          endpoint.current_status === 'up'
            ? 'border-l-green-500'
            : endpoint.current_status === 'down'
              ? 'border-l-red-500'
              : endpoint.current_status === 'degraded'
                ? 'border-l-amber-500'
                : 'border-l-slate-400',
        )}
      >
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Current status
            </p>
            <div className="mt-1">
              <StatusBadge status={endpoint.current_status} size="md" pulse />
            </div>
            {endpoint.has_open_incident ? (
              <p className="mt-1 text-xs font-medium text-red-600 dark:text-red-400">
                Open incident
              </p>
            ) : null}
          </div>
          <Metric
            label="HTTP status"
            value={endpoint.last_status_code || '—'}
            sub={endpoint.expected_status_codes ? `expects ${endpoint.expected_status_codes}` : undefined}
          />
          <Metric
            label="Response time"
            value={formatMs(endpoint.last_response_time_ms)}
            tone={
              endpoint.response_time_threshold_ms &&
              endpoint.last_response_time_ms > endpoint.response_time_threshold_ms
                ? 'warn'
                : undefined
            }
          />
          <Metric
            label="Last check"
            value={formatRelative(endpoint.last_checked_at)}
            sub={formatDateTime(endpoint.last_checked_at)}
          />
          <Metric
            label="Next check"
            value={
              endpoint.is_paused || !endpoint.monitoring_enabled
                ? 'Paused'
                : formatRelative(endpoint.next_check_at)
            }
            sub={`every ${formatInterval(endpoint.interval_seconds)}`}
          />
        </div>

        {endpoint.is_paused && endpoint.pause_reason ? (
          <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
            <span>
              <span className="font-medium">Monitoring paused:</span>{' '}
              {endpoint.pause_reason}
            </span>
            {endpoint.paused_by_change_id ? (
              <Link
                to={`/changes/${endpoint.paused_by_change_id}`}
                className="font-medium underline"
              >
                View change
              </Link>
            ) : null}
          </div>
        ) : null}

        {endpoint.last_error ? (
          <div className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/40 dark:text-red-200">
            <span className="font-medium">Last error:</span> {endpoint.last_error}
          </div>
        ) : null}

        <div className="mt-3 flex flex-wrap items-center gap-2">
          {endpoint.environment ? (
            <span className="chip">
              {endpoint.environment.display_name || endpoint.environment.name}
            </span>
          ) : null}
          {(endpoint.tags || []).map((tag) => (
            <TagChip key={tag.id} name={tag.name} />
          ))}
          {endpoint.owner ? (
            <span className="text-xs text-slate-500">Owner: {endpoint.owner}</span>
          ) : null}
          {endpoint.team ? (
            <span className="text-xs text-slate-500">Team: {endpoint.team}</span>
          ) : null}
        </div>
      </div>

      {/* ------------------------------------------------------- tabs */}
      <div
        className="mb-4 flex gap-1 overflow-x-auto border-b border-slate-200 dark:border-slate-800"
        role="tablist"
      >
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            className={clsx(
              'whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium transition-colors',
              tab === item.id
                ? 'border-brand-600 text-brand-700 dark:border-brand-400 dark:text-brand-300'
                : 'border-transparent text-slate-500 hover:text-slate-800 dark:hover:text-slate-200',
            )}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {/* --------------------------------------------------- overview */}
      {tab === 'overview' ? (
        <div className="space-y-4">
          <Card
            title="Availability"
            actions={
              <select
                className="input w-auto py-1 text-xs"
                value={window_}
                onChange={(event) => setWindow(event.target.value)}
                aria-label="Availability window"
              >
                {Object.entries(WINDOW_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            }
            bodyClassName="p-0"
          >
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Window</th>
                    <th className="text-right">Uptime</th>
                    <th className="text-right">Downtime</th>
                    <th className="text-right">Failures</th>
                    <th className="text-right">Incidents</th>
                    <th className="text-right">Avg</th>
                    <th className="text-right">Min</th>
                    <th className="text-right">Max</th>
                    <th className="text-right">p95</th>
                    <th className="text-right">Checks</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(WINDOW_LABELS).map(([key, label]) => {
                    const stat = windows[key] || {}
                    return (
                      <tr
                        key={key}
                        className={key === window_ ? 'bg-brand-50/60 dark:bg-brand-900/20' : ''}
                      >
                        <td className="font-medium">{label}</td>
                        <td className="tnum text-right">{formatPercent(stat.uptime_percent)}</td>
                        <td className="tnum text-right">
                          {formatDuration(stat.downtime_seconds)}
                        </td>
                        <td className="tnum text-right">{formatNumber(stat.failed_checks)}</td>
                        <td className="tnum text-right">{formatNumber(stat.incident_count)}</td>
                        <td className="tnum text-right">
                          {formatMs(stat.avg_response_time_ms)}
                        </td>
                        <td className="tnum text-right">
                          {formatMs(stat.min_response_time_ms)}
                        </td>
                        <td className="tnum text-right">
                          {formatMs(stat.max_response_time_ms)}
                        </td>
                        <td className="tnum text-right">
                          {formatMs(stat.p95_response_time_ms)}
                        </td>
                        <td className="tnum text-right">{formatNumber(stat.total_checks)}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </Card>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <ChartFrame
              title="Response time"
              subtitle={`Average and maximum · ${WINDOW_LABELS[window_].toLowerCase()}`}
              table={<SeriesTable series={series} />}
            >
              <LatencyBreakdownChart data={series} mode={mode} />
            </ChartFrame>

            <ChartFrame
              title="Uptime"
              subtitle="Share of checks that succeeded"
              table={<SeriesTable series={series} />}
            >
              <UptimeChart data={series} mode={mode} />
            </ChartFrame>
          </div>

          <Card title="Lifetime counters">
            <div className="grid gap-4 sm:grid-cols-4">
              <Metric label="Total checks" value={formatNumber(endpoint.total_checks)} />
              <Metric
                label="Total failures"
                value={formatNumber(endpoint.total_failures)}
                tone={endpoint.total_failures > 0 ? 'warn' : undefined}
              />
              <Metric
                label="Consecutive failures"
                value={formatNumber(endpoint.consecutive_failures)}
                tone={endpoint.consecutive_failures > 0 ? 'bad' : 'good'}
                sub={`opens an incident at ${endpoint.failure_threshold}`}
              />
              <Metric
                label="Created"
                value={formatDateTime(endpoint.created_at, 'dd MMM yyyy')}
                sub={endpoint.created_by ? `by ${endpoint.created_by}` : undefined}
              />
            </div>
          </Card>
        </div>
      ) : null}

      {/* ---------------------------------------------------- history */}
      {tab === 'history' ? (
        <Card
          title="Check history"
          actions={
            <select
              className="input w-auto py-1 text-xs"
              value={historyStatus}
              onChange={(event) => {
                setHistoryStatus(event.target.value)
                setHistoryPage(1)
              }}
              aria-label="Filter history by result"
            >
              <option value="">All results</option>
              <option value="up">Successful</option>
              <option value="degraded">Degraded</option>
              <option value="down">Failed</option>
            </select>
          }
          bodyClassName="p-0"
        >
          {!history ? (
            <div className="p-4">
              <LoadingBlock rows={6} />
            </div>
          ) : history.items.length === 0 ? (
            <EmptyState
              icon={Clock}
              title="No checks recorded yet"
              description="The monitoring worker writes a row here after every check."
            />
          ) : (
            <>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Checked at</th>
                      <th>Result</th>
                      <th className="text-right">HTTP</th>
                      <th className="text-right">Total</th>
                      <th className="text-right">DNS</th>
                      <th className="text-right">Connect</th>
                      <th className="text-right">TLS</th>
                      <th className="text-right">TTFB</th>
                      <th>Resolved IP</th>
                      <th className="text-right">Size</th>
                      <th>Detail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.items.map((row) => (
                      <tr key={row.id}>
                        <td className="whitespace-nowrap">
                          {formatDateTime(row.checked_at)}
                          {row.is_manual ? (
                            <span className="chip ml-1">manual</span>
                          ) : null}
                        </td>
                        <td>
                          <StatusBadge status={row.status} size="sm" />
                        </td>
                        <td className="tnum text-right">{row.http_status_code || '—'}</td>
                        <td className="tnum text-right">{formatMs(row.response_time_ms)}</td>
                        <td className="tnum text-right">
                          {formatMs(row.dns_time_ms, { decimals: 1 })}
                        </td>
                        <td className="tnum text-right">
                          {formatMs(row.connect_time_ms, { decimals: 1 })}
                        </td>
                        <td className="tnum text-right">
                          {formatMs(row.tls_time_ms, { decimals: 1 })}
                        </td>
                        <td className="tnum text-right">{formatMs(row.ttfb_ms)}</td>
                        <td className="font-mono text-[11px]">{row.resolved_ip || '—'}</td>
                        <td className="tnum text-right">{formatBytes(row.content_length)}</td>
                        <td className="max-w-[16rem]">
                          {row.error_message ? (
                            <span className="text-xs text-red-600 dark:text-red-400">
                              {row.failure_reason_label ||
                                humanise(row.failure_reason, FAILURE_REASON_LABELS)}
                              : {row.error_message}
                            </span>
                          ) : (
                            <span className="text-xs text-slate-400">
                              {row.redirect_count > 0
                                ? `${row.redirect_count} redirect(s)`
                                : row.tls_version || '—'}
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pagination meta={history.meta} onPageChange={setHistoryPage} />
            </>
          )}
        </Card>
      ) : null}

      {/* -------------------------------------------------- incidents */}
      {tab === 'incidents' ? (
        <Card title="Incident history" bodyClassName="p-0">
          {!incidents ? (
            <div className="p-4">
              <LoadingBlock rows={5} />
            </div>
          ) : incidents.items.length === 0 ? (
            <EmptyState
              icon={ShieldCheck}
              title="No incidents"
              description="This endpoint has not failed long enough to open one."
            />
          ) : (
            <div className="divide-y divide-slate-100 dark:divide-slate-800">
              {incidents.items.map((incident) => (
                <div key={incident.id} className="p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="tnum text-sm font-semibold text-slate-500">
                      Incident #{incident.id}
                    </span>
                    <StatusBadge status={incident.status === 'open' ? 'down' : 'up'} size="sm" />
                    <span className="text-xs text-slate-500">
                      {incident.status === 'open' ? 'Ongoing' : 'Resolved'}
                    </span>
                    <span className="tnum ml-auto text-xs text-slate-500">
                      {incident.status === 'open'
                        ? formatDuration(
                            (Date.now() - new Date(incident.started_at).getTime()) / 1000,
                          )
                        : formatDuration(incident.duration_seconds)}
                    </span>
                  </div>

                  <dl className="mt-2 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
                    <div className="flex gap-2">
                      <dt className="text-slate-500">Started</dt>
                      <dd className="font-medium">{formatDateTime(incident.started_at)}</dd>
                    </div>
                    <div className="flex gap-2">
                      <dt className="text-slate-500">Resolved</dt>
                      <dd className="font-medium">
                        {incident.resolved_at ? formatDateTime(incident.resolved_at) : '—'}
                      </dd>
                    </div>
                    <div className="flex gap-2">
                      <dt className="text-slate-500">Reason</dt>
                      <dd className="font-medium">
                        {incident.reason_label ||
                          humanise(incident.reason, FAILURE_REASON_LABELS)}
                      </dd>
                    </div>
                    <div className="flex gap-2">
                      <dt className="text-slate-500">Failed checks</dt>
                      <dd className="tnum font-medium">{incident.failed_check_count}</dd>
                    </div>
                    {incident.recovery_status_code ? (
                      <div className="flex gap-2">
                        <dt className="text-slate-500">Recovery</dt>
                        <dd className="font-medium">
                          HTTP {incident.recovery_status_code}
                          {incident.recovery_response_time_ms
                            ? ` in ${formatMs(incident.recovery_response_time_ms)}`
                            : ''}
                        </dd>
                      </div>
                    ) : null}
                  </dl>

                  {incident.error_message ? (
                    <p className="mt-2 rounded bg-slate-50 px-2 py-1.5 font-mono text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                      {incident.error_message}
                    </p>
                  ) : null}

                  {incident.timeline?.length ? (
                    <ol className="mt-2 space-y-1 border-l-2 border-slate-200 pl-3 dark:border-slate-700">
                      {incident.timeline.map((entry, index) => (
                        <li key={index} className="text-xs text-slate-500">
                          <span className="font-medium text-slate-700 dark:text-slate-300">
                            {formatDateTime(entry.at, 'dd MMM HH:mm:ss')}
                          </span>{' '}
                          — {entry.kind}: {entry.detail}
                        </li>
                      ))}
                    </ol>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </Card>
      ) : null}

      {/* ------------------------------------------------ certificate */}
      {tab === 'certificate' ? (
        <Card title="SSL/TLS certificate">
          {!endpoint.ssl_monitoring_enabled ? (
            <EmptyState
              icon={ShieldCheck}
              title="Certificate monitoring is off for this endpoint"
              description={
                endpoint.protocol === 'https'
                  ? 'Enable it in the endpoint settings to start tracking expiry.'
                  : 'This endpoint is not served over HTTPS.'
              }
            />
          ) : !certificate ? (
            <EmptyState
              icon={ShieldCheck}
              title="No certificate captured yet"
              description={
                certificateError ||
                'The worker records the certificate on its next successful handshake.'
              }
              action={
                canCheck ? (
                  <button type="button" className="btn-secondary" onClick={runCheck}>
                    <RefreshCw size={15} /> Check now
                  </button>
                ) : null
              }
            />
          ) : (
            <>
              <div className="mb-4 flex flex-wrap items-center gap-3">
                <SslBadge status={certificate.status} />
                <span className="tnum text-sm font-medium">
                  {formatDaysRemaining(certificate.days_remaining)}
                </span>
                {certificate.is_self_signed ? (
                  <span className="chip">Self-signed</span>
                ) : null}
                {certificate.is_wildcard ? <span className="chip">Wildcard</span> : null}
                {certificate.chain_verified === false ? (
                  <span className="badge bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300">
                    Chain not verified
                  </span>
                ) : null}
                {certificate.hostname_matches === false ? (
                  <span className="badge bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300">
                    Hostname mismatch
                  </span>
                ) : null}
              </div>

              <dl>
                <DetailRow label="Common name">{certificate.common_name}</DetailRow>
                <DetailRow label="Subject" mono>
                  {certificate.subject}
                </DetailRow>
                <DetailRow label="Issuer" mono>
                  {certificate.issuer}
                </DetailRow>
                <DetailRow label="Issuer organisation">
                  {certificate.issuer_organization}
                </DetailRow>
                <DetailRow label="Valid from">
                  {formatDateTime(certificate.valid_from)}
                </DetailRow>
                <DetailRow label="Expires">{formatDateTime(certificate.valid_to)}</DetailRow>
                <DetailRow label="Remaining">
                  {formatDaysRemaining(certificate.days_remaining)}
                </DetailRow>
                <DetailRow label="Subject alternative names">
                  {certificate.san?.length ? (
                    <div className="flex flex-wrap gap-1">
                      {certificate.san.map((name) => (
                        <span key={name} className="chip font-mono">
                          {name}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </DetailRow>
                <DetailRow label="TLS version">{certificate.tls_version}</DetailRow>
                <DetailRow label="Cipher" mono>
                  {certificate.tls_cipher}
                </DetailRow>
                <DetailRow label="Signature algorithm">
                  {certificate.signature_algorithm}
                </DetailRow>
                <DetailRow label="Key">
                  {certificate.key_algorithm
                    ? `${certificate.key_algorithm}${certificate.key_size ? ` · ${certificate.key_size} bits` : ''}`
                    : null}
                </DetailRow>
                <DetailRow label="Serial number" mono>
                  {certificate.serial_number}
                </DetailRow>
                <DetailRow label="SHA-256 fingerprint" mono>
                  {certificate.fingerprint_sha256}
                </DetailRow>
                <DetailRow label="Verification">
                  {certificate.verification_status}
                  {certificate.verification_error ? (
                    <p className="mt-1 text-xs text-red-600 dark:text-red-400">
                      {certificate.verification_error}
                    </p>
                  ) : null}
                </DetailRow>
                <DetailRow label="Chain">
                  {certificate.chain?.length ? (
                    <ol className="space-y-1">
                      {certificate.chain.map((link) => (
                        <li key={link.position} className="text-xs">
                          <span className="font-medium">{link.position}.</span>{' '}
                          {link.common_name || link.subject}
                          {link.is_self_signed ? (
                            <span className="chip ml-1">root</span>
                          ) : null}
                        </li>
                      ))}
                    </ol>
                  ) : (
                    `${certificate.chain_length ?? 0} intermediate certificate(s) reported`
                  )}
                </DetailRow>
                <DetailRow label="First seen">
                  {formatDateTime(certificate.first_seen_at)}
                </DetailRow>
                <DetailRow label="Last inspected">
                  {formatDateTime(certificate.checked_at)}
                </DetailRow>
              </dl>
            </>
          )}
        </Card>
      ) : null}

      {/* ---------------------------------------------- configuration */}
      {tab === 'configuration' ? (
        <Card title="Monitoring configuration">
          <dl>
            <DetailRow label="URL" mono>
              {endpoint.url}
            </DetailRow>
            <DetailRow label="Hostname / port" mono>
              {endpoint.hostname}:{endpoint.port}
            </DetailRow>
            <DetailRow label="Path" mono>
              {endpoint.path}
            </DetailRow>
            {endpoint.resolved_health_path ? (
              <DetailRow label="Health path in use" mono>
                {endpoint.resolved_health_path}
                <p className="mt-0.5 font-sans text-xs text-slate-500 dark:text-slate-400">
                  The configured path returned 404, so this one was found and
                  adopted. Checks probe it directly; the URL above is unchanged.
                </p>
              </DetailRow>
            ) : null}
            <DetailRow label="Check type">{endpoint.check_type}</DetailRow>
            <DetailRow label="HTTP method">{endpoint.http_method}</DetailRow>
            <DetailRow label="Expected status">{endpoint.expected_status_codes}</DetailRow>
            <DetailRow label="Expected content">
              {endpoint.expected_body_substring}
            </DetailRow>
            <DetailRow label="Interval">
              {formatInterval(endpoint.interval_seconds)}
            </DetailRow>
            <DetailRow label="Timeout">{endpoint.timeout_seconds}s</DetailRow>
            <DetailRow label="Follow redirects">
              {endpoint.follow_redirects ? 'Yes' : 'No'}
            </DetailRow>
            <DetailRow label="Verify certificate chain">
              {endpoint.verify_ssl ? 'Yes' : 'No'}
            </DetailRow>
            <DetailRow label="Certificate monitoring">
              {endpoint.ssl_monitoring_enabled ? 'Enabled' : 'Disabled'}
            </DetailRow>
            <DetailRow label="Custom headers">
              {endpoint.custom_headers && Object.keys(endpoint.custom_headers).length ? (
                <pre className="overflow-x-auto rounded bg-slate-50 p-2 font-mono text-[11px] dark:bg-slate-800">
                  {JSON.stringify(endpoint.custom_headers, null, 2)}
                </pre>
              ) : null}
            </DetailRow>
            <DetailRow label="Authentication">
              {endpoint.auth_type === 'none' ? (
                'None'
              ) : (
                <>
                  <span className="capitalize">{endpoint.auth_type}</span>
                  {endpoint.auth_username ? ` · user ${endpoint.auth_username}` : ''}
                  {endpoint.auth_header_name ? ` · header ${endpoint.auth_header_name}` : ''}
                  {/* The stored credential is never returned by the API - only
                      this masked hint exists client-side. */}
                  {endpoint.has_auth_secret ? (
                    <p className="mt-0.5 font-mono text-xs text-slate-400">
                      credential stored: {endpoint.auth_secret_hint || 'hidden'}
                    </p>
                  ) : null}
                </>
              )}
            </DetailRow>
            <DetailRow label="Failure threshold">
              {endpoint.failure_threshold} consecutive failures
            </DetailRow>
            <DetailRow label="Response time threshold">
              {endpoint.response_time_threshold_ms
                ? `${endpoint.response_time_threshold_ms} ms`
                : 'Global default'}
            </DetailRow>
            <DetailRow label="SSL thresholds">
              {endpoint.ssl_warning_days || endpoint.ssl_critical_days
                ? `warning ${endpoint.ssl_warning_days ?? 'default'} d · critical ${endpoint.ssl_critical_days ?? 'default'} d`
                : 'Global defaults'}
            </DetailRow>
            <DetailRow label="Alerting">
              {endpoint.alerts_enabled ? 'Enabled' : 'Disabled'}
            </DetailRow>
            <DetailRow label="Description">{endpoint.description}</DetailRow>
            <DetailRow label="Created">
              {formatDateTime(endpoint.created_at)}
              {endpoint.created_by ? ` by ${endpoint.created_by}` : ''}
            </DetailRow>
            <DetailRow label="Last modified">
              {formatDateTime(endpoint.updated_at)}
              {endpoint.updated_by ? ` by ${endpoint.updated_by}` : ''}
            </DetailRow>
          </dl>
        </Card>
      ) : null}

      <EndpointForm
        open={formOpen}
        endpoint={endpoint}
        filters={filters}
        config={config}
        onClose={() => setFormOpen(false)}
        onSaved={() => {
          setFormOpen(false)
          loadEndpoint()
          loadStats()
          loadCertificate()
        }}
      />

      <DiagnosticsPanel
        open={diagnosticsOpen}
        onClose={() => setDiagnosticsOpen(false)}
        report={diagnostics}
        loading={diagnosing}
        error={diagnosticsError}
      />

      <ConfirmDialog
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        onConfirm={doDelete}
        busy={deleting}
        danger
        title="Delete this endpoint?"
        confirmLabel="Delete endpoint"
        message={`'${endpoint.name}' and all of its monitoring history, certificates and incidents will be permanently deleted.`}
      />
    </>
  )
}
