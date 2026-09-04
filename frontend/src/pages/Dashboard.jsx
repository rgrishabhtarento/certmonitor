import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock,
  ServerCog,
  ShieldAlert,
  ShieldCheck,
  Timer,
  XCircle,
  Zap,
} from 'lucide-react'
import clsx from 'clsx'

import {
  AvailabilityBars,
  ChartFrame,
  FailureBars,
  FailuresOverTimeChart,
  GroupTable,
  LatencyBreakdownChart,
  Legend,
  ResponseTimeChart,
  SeriesTable,
  SslExpiryChart,
  STATUS,
  StatusDistribution,
  UptimeChart,
  useChartMode,
  withSeriesLabels,
} from '../components/charts'
import { EmptyState, ErrorState, LoadingBlock, PageHeader, StatusBadge } from '../components/ui'
import InfraSearch from '../components/InfraSearch'
import LiveIndicator from '../components/LiveIndicator'
import SmartSummary from '../components/SmartSummary'
import { dashboardApi, endpointsApi } from '../lib/api'
import {
  formatDuration,
  formatMs,
  formatNumber,
  formatPercent,
  formatRelative,
  humanise,
  FAILURE_REASON_LABELS,
} from '../lib/format'
import { SLOW_INTERVAL, useAutoRefresh } from '../hooks/useAutoRefresh'
import { useToast } from '../hooks/useToast'

const WINDOWS = [
  { value: '24h', label: 'Last 24 hours' },
  { value: '7d', label: 'Last 7 days' },
  { value: '30d', label: 'Last 30 days' },
  { value: '90d', label: 'Last 90 days' },
]

/** KPI tile. A single number is a stat tile, never a one-bar chart. */
function StatTile({ icon: Icon, label, value, sub, tone = 'neutral', to }) {
  const tones = {
    neutral: 'text-slate-900 dark:text-slate-50',
    good: 'text-green-600 dark:text-green-400',
    warn: 'text-amber-600 dark:text-amber-400',
    bad: 'text-red-600 dark:text-red-400',
    info: 'text-brand-600 dark:text-brand-400',
  }
  const iconTones = {
    neutral: 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400',
    good: 'bg-green-50 text-green-600 dark:bg-green-900/30 dark:text-green-400',
    warn: 'bg-amber-50 text-amber-600 dark:bg-amber-900/30 dark:text-amber-400',
    bad: 'bg-red-50 text-red-600 dark:bg-red-900/30 dark:text-red-400',
    info: 'bg-brand-50 text-brand-600 dark:bg-brand-900/30 dark:text-brand-400',
  }

  const body = (
    <div className="card flex items-center gap-3 p-3.5 transition-shadow hover:shadow-md">
      <span className={clsx('grid h-9 w-9 shrink-0 place-items-center rounded-lg', iconTones[tone])}>
        <Icon size={18} />
      </span>
      <div className="min-w-0">
        <p className="truncate text-xs font-medium text-slate-500 dark:text-slate-400">
          {label}
        </p>
        {/* Proportional figures on a display-size number: tabular digits make
            a value like 121 look loose. */}
        <p className={clsx('text-xl font-semibold leading-tight', tones[tone])}>{value}</p>
        {sub ? (
          <p className="truncate text-[11px] text-slate-400 dark:text-slate-500">{sub}</p>
        ) : null}
      </div>
    </div>
  )

  return to ? (
    <Link to={to} className="block">
      {body}
    </Link>
  ) : (
    body
  )
}

export default function Dashboard() {
  const toast = useToast()
  const mode = useChartMode()

  const [window_, setWindow] = useState('24h')
  const [environmentId, setEnvironmentId] = useState('')
  const [tagId, setTagId] = useState('')
  const [statusFilter, setStatusFilter] = useState('')

  const [data, setData] = useState(null)
  const [filters, setFilters] = useState({ environments: [], tags: [] })
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    endpointsApi
      .filters()
      .then(setFilters)
      .catch(() => {})
  }, [])

  const load = useCallback(
    async ({ silent = false, background = false } = {}) => {
      if (silent) setRefreshing(true)
      else setLoading(true)
      setError(null)
      try {
        const payload = await dashboardApi.get({
          window: window_,
          environment: environmentId || undefined,
          tag: tagId || undefined,
          status: statusFilter || undefined,
        })
        setData(payload)
      } catch (err) {
        setError(err)
        // A failed *background* poll stays quiet. Toasting every 30 seconds
        // during a backend blip would bury the screen in notifications about
        // something the user never asked for.
        if (silent && !background) {
          toast.error(`Could not refresh the dashboard: ${err.message}`)
        }
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [window_, environmentId, tagId, statusFilter, toast],
  )

  useEffect(() => {
    load()
  }, [load])

  // The charts are the heaviest queries on the page, so they refresh on the
  // slow cadence - the Smart Summary above them carries the urgent numbers.
  const { lastRefreshedAt, refreshNow } = useAutoRefresh(
    () => load({ silent: true, background: true }),
    { interval: SLOW_INTERVAL },
  )

  const summary = data?.summary
  const bucketSeconds = useMemo(() => {
    const points = data?.response_time_series || []
    if (points.length < 2) return 3600
    const first = new Date(points[0].timestamp).getTime()
    const second = new Date(points[1].timestamp).getTime()
    return Math.max(60, Math.round((second - first) / 1000))
  }, [data])

  const series = useMemo(
    () => withSeriesLabels(data?.response_time_series, bucketSeconds),
    [data, bucketSeconds],
  )

  const statusCounts = useMemo(() => {
    if (!summary) return {}
    return {
      up: summary.healthy,
      down: summary.down,
      degraded: summary.degraded,
      unknown: summary.unknown,
      paused: summary.paused,
    }
  }, [summary])

  if (loading && !data) {
    return (
      <>
        <PageHeader title="Dashboard" description="Infrastructure health at a glance" />
        <LoadingBlock rows={6} />
      </>
    )
  }

  if (error && !data) {
    return (
      <>
        <PageHeader title="Dashboard" />
        <ErrorState
          message={error.message}
          status={error.status}
          requestId={error.requestId}
          onRetry={() => load()}
        />
      </>
    )
  }

  return (
    <>
      <PageHeader
        title="Dashboard"
        description="Infrastructure health at a glance"
        actions={
          <LiveIndicator
            refreshing={refreshing}
            // Falls back to when the server generated the payload, so the
            // counter is honest before the first poll lands.
            lastRefreshedAt={
              lastRefreshedAt ||
              (data?.generated_at ? new Date(data.generated_at) : null)
            }
            onRefresh={refreshNow}
            showToggle
          />
        }
      />

      {/* Locally computed operational intelligence, above the charts because
          it answers "what needs my attention" - the question an operator
          opens this page with. Everything in it is a count of real rows on
          this server; nothing is sent anywhere. */}
      <SmartSummary />
      <InfraSearch />

      {/* One filter row above everything it scopes, so every chart below
          re-renders against the same slice. */}
      <div className="card mb-4 flex flex-wrap items-end gap-3 p-3">
        <div className="min-w-[9rem]">
          <label htmlFor="window" className="label">
            Time window
          </label>
          <select
            id="window"
            className="input"
            value={window_}
            onChange={(event) => setWindow(event.target.value)}
          >
            {WINDOWS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <div className="min-w-[9rem]">
          <label htmlFor="env" className="label">
            Environment
          </label>
          <select
            id="env"
            className="input"
            value={environmentId}
            onChange={(event) => setEnvironmentId(event.target.value)}
          >
            <option value="">All environments</option>
            {filters.environments.map((environment) => (
              <option key={environment.id} value={environment.id}>
                {environment.display_name || environment.name} ({environment.endpoint_count})
              </option>
            ))}
          </select>
        </div>
        <div className="min-w-[9rem]">
          <label htmlFor="tag" className="label">
            Tag
          </label>
          <select
            id="tag"
            className="input"
            value={tagId}
            onChange={(event) => setTagId(event.target.value)}
          >
            <option value="">All tags</option>
            {filters.tags.map((tag) => (
              <option key={tag.id} value={tag.id}>
                {tag.name} ({tag.endpoint_count})
              </option>
            ))}
          </select>
        </div>
        <div className="min-w-[9rem]">
          <label htmlFor="status" className="label">
            Status
          </label>
          <select
            id="status"
            className="input"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}
          >
            <option value="">Any status</option>
            <option value="up">Healthy</option>
            <option value="degraded">Degraded</option>
            <option value="down">Down</option>
            <option value="unknown">Unknown</option>
          </select>
        </div>
        {environmentId || tagId || statusFilter ? (
          <button
            type="button"
            className="btn-ghost btn-sm"
            onClick={() => {
              setEnvironmentId('')
              setTagId('')
              setStatusFilter('')
            }}
          >
            Clear filters
          </button>
        ) : null}
      </div>

      {summary?.total_endpoints === 0 ? (
        <div className="card">
          <EmptyState
            icon={ServerCog}
            title="No endpoints are being monitored yet"
            description="Add your first endpoint, or bulk-import a CSV, and the monitoring worker will start checking it within a minute."
            action={
              <div className="flex gap-2">
                <Link to="/endpoints" className="btn-primary">
                  Add an endpoint
                </Link>
                <Link to="/import-export" className="btn-secondary">
                  Import a CSV
                </Link>
              </div>
            }
          />
        </div>
      ) : (
        <div className="space-y-4">
          {/* Nothing dims on refresh. Fading the page to 70% was feedback for
              a manual press; with a poll every 30 seconds it became a strobe.
              The refresh button spins and the indicator says "Updating…" —
              that is enough, and it does not move the content. */}
          {/* ------------------------------------------------- KPI row */}
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <StatTile
              icon={ServerCog}
              label="Endpoints"
              value={formatNumber(summary.total_endpoints)}
              sub={`${formatNumber(summary.paused)} paused`}
              to="/endpoints"
            />
            <StatTile
              icon={CheckCircle2}
              label="Healthy"
              value={formatNumber(summary.healthy)}
              tone="good"
              to="/endpoints?status=up"
            />
            <StatTile
              icon={XCircle}
              label="Down"
              value={formatNumber(summary.down)}
              tone={summary.down > 0 ? 'bad' : 'neutral'}
              sub={`${formatNumber(summary.open_incidents)} open incidents`}
              to="/endpoints?status=down"
            />
            <StatTile
              icon={AlertTriangle}
              label="Degraded"
              value={formatNumber(summary.degraded)}
              tone={summary.degraded > 0 ? 'warn' : 'neutral'}
              to="/endpoints?status=degraded"
            />
            <StatTile
              icon={Timer}
              label="Avg response"
              value={formatMs(summary.average_response_time_ms)}
              sub={`${formatNumber(summary.total_checks)} checks`}
              tone="info"
            />
            <StatTile
              icon={Activity}
              label="Overall uptime"
              value={formatPercent(summary.overall_uptime_percent)}
              sub={data.sla_target ? `SLA ${data.sla_target}%` : undefined}
              tone={
                summary.overall_uptime_percent === null
                  ? 'neutral'
                  : summary.overall_uptime_percent >= (data.sla_target ?? 99.9)
                    ? 'good'
                    : 'warn'
              }
            />
          </div>

          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatTile
              icon={ShieldCheck}
              label="Certificates"
              value={formatNumber(summary.ssl_certificates)}
              sub={`${formatNumber(summary.ssl_valid)} valid`}
              to="/ssl"
            />
            <StatTile
              icon={ShieldAlert}
              label="Expiring soon"
              value={formatNumber(summary.ssl_expiring_soon + summary.ssl_critical)}
              tone={summary.ssl_expiring_soon + summary.ssl_critical > 0 ? 'warn' : 'neutral'}
              to="/ssl?status=expiring_soon,critical"
            />
            <StatTile
              icon={ShieldAlert}
              label="Expired / invalid"
              value={formatNumber(summary.ssl_expired + summary.ssl_invalid)}
              tone={summary.ssl_expired + summary.ssl_invalid > 0 ? 'bad' : 'neutral'}
              to="/ssl?status=expired,invalid"
            />
            <StatTile
              icon={Zap}
              label="Open incidents"
              value={formatNumber(summary.open_incidents)}
              tone={summary.open_incidents > 0 ? 'bad' : 'good'}
              to="/incidents?status=open"
            />
          </div>

          {/* -------------------------------------- unhealthy endpoints */}
          {data.open_incidents?.length ? (
            <section className="card border-red-200 dark:border-red-900/70">
              <header className="card-header">
                <h2 className="card-title flex items-center gap-1.5 text-red-700 dark:text-red-300">
                  <XCircle size={15} />
                  Currently failing ({data.open_incidents.length})
                </h2>
                <Link to="/incidents?status=open" className="btn-ghost btn-sm">
                  All incidents
                </Link>
              </header>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Endpoint</th>
                      <th>Environment</th>
                      <th>Reason</th>
                      <th>Started</th>
                      <th className="text-right">Duration</th>
                      <th className="text-right">Failed checks</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.open_incidents.slice(0, 8).map((incident) => (
                      <tr key={incident.id}>
                        <td>
                          <Link
                            to={`/endpoints/${incident.endpoint_id}`}
                            className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                          >
                            {incident.endpoint?.name || 'Endpoint'}
                          </Link>
                          <p className="truncate text-xs text-slate-400">
                            {incident.endpoint?.url}
                          </p>
                        </td>
                        <td className="text-slate-600 dark:text-slate-300">
                          {incident.endpoint?.environment || '—'}
                        </td>
                        <td>
                          <span className="text-slate-700 dark:text-slate-200">
                            {incident.reason_label ||
                              humanise(incident.reason, FAILURE_REASON_LABELS)}
                          </span>
                          {incident.error_message ? (
                            <p className="truncate text-xs text-slate-400" title={incident.error_message}>
                              {incident.error_message}
                            </p>
                          ) : null}
                        </td>
                        <td className="whitespace-nowrap text-slate-600 dark:text-slate-300">
                          {formatRelative(incident.started_at)}
                        </td>
                        <td className="tnum whitespace-nowrap text-right">
                          {formatDuration(
                            (Date.now() - new Date(incident.started_at).getTime()) / 1000,
                          )}
                        </td>
                        <td className="tnum text-right">{incident.failed_check_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}

          {/* -------------------------------------------- time series */}
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            {/* Response time and uptime are separate charts on purpose: two
                y-scales on one plot would imply a correlation that is not in
                the data. */}
            <ChartFrame
              title="Response time"
              subtitle={`Average across matching endpoints · ${WINDOWS.find((w) => w.value === window_)?.label.toLowerCase()}`}
              table={<SeriesTable series={series} />}
            >
              <ResponseTimeChart data={series} mode={mode} />
            </ChartFrame>

            <ChartFrame
              title="Uptime"
              subtitle="Share of checks that succeeded"
              table={<SeriesTable series={series} />}
            >
              <UptimeChart data={series} slaTarget={data.sla_target} mode={mode} />
            </ChartFrame>
          </div>

          {/* ---------------------------------- when, and which layer */}
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            {/* A total of "47 failed checks" cannot distinguish one outage
                from a week of flapping. The distribution over time can. */}
            <ChartFrame
              title="When checks failed"
              subtitle="Failed and degraded per interval — a cluster is an outage, a spread is flapping"
              table={<SeriesTable series={series} />}
            >
              <FailuresOverTimeChart data={series} mode={mode} />
            </ChartFrame>

            {/* Splits total response time into its phases, which is the
                difference between "the network is slow" and "the application
                is slow" - two answers that send you to different teams. */}
            <ChartFrame
              title="Latency breakdown"
              subtitle="Average, maximum, and the DNS / connect / TLS phases beneath them"
              table={<SeriesTable series={series} />}
            >
              <LatencyBreakdownChart data={series} mode={mode} />
            </ChartFrame>
          </div>

          {/* ------------------------------- distribution and SSL timeline */}
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <section className="card">
              <header className="card-header">
                <h2 className="card-title">Status distribution</h2>
                <span className="tnum text-xs text-slate-400">
                  {formatNumber(summary.total_endpoints)} endpoints
                </span>
              </header>
              <div className="p-4">
                <StatusDistribution
                  counts={statusCounts}
                  total={summary.total_endpoints}
                />
              </div>
            </section>

            <ChartFrame
              title="SSL expiry timeline"
              subtitle="Certificates grouped by remaining validity"
              legend={
                <Legend
                  items={[
                    { label: 'Act now', color: STATUS.critical },
                    { label: 'Plan renewal', color: STATUS.warning },
                    { label: 'Healthy', color: STATUS.good },
                  ]}
                />
              }
              height={220}
              table={
                <table className="table">
                  <thead>
                    <tr>
                      <th>Remaining validity</th>
                      <th className="text-right">Certificates</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(data.ssl_expiry_timeline || []).map((bucket) => (
                      <tr key={bucket.bucket}>
                        <td>{bucket.bucket}</td>
                        <td className="tnum text-right">{bucket.count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              }
            >
              <SslExpiryChart buckets={data.ssl_expiry_timeline} mode={mode} />
            </ChartFrame>
          </div>

          {/* --------------------------------- availability by dimension */}
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <ChartFrame
              title="Availability by environment"
              subtitle="Observed uptime over the selected window"
              height={240}
              autoHeight
              table={<GroupTable groups={data.availability_by_environment} groupLabel="Environment" />}
            >
              <AvailabilityBars groups={data.availability_by_environment} mode={mode} />
            </ChartFrame>

            <ChartFrame
              title="Availability by tag"
              subtitle="Observed uptime over the selected window"
              height={240}
              autoHeight
              table={<GroupTable groups={data.availability_by_tag} groupLabel="Tag" />}
            >
              <AvailabilityBars groups={data.availability_by_tag} mode={mode} />
            </ChartFrame>
          </div>

          {/* The API has always returned this and nothing rendered it. Team
              is the grouping that maps to who gets called, which makes it the
              one most likely to change what happens next. */}
          {data.availability_by_team?.length ? (
            <ChartFrame
              title="Availability by team"
              subtitle="Observed uptime over the selected window, grouped by owning team"
              height={240}
              autoHeight
              table={<GroupTable groups={data.availability_by_team} groupLabel="Team" />}
            >
              <AvailabilityBars groups={data.availability_by_team} mode={mode} />
            </ChartFrame>
          ) : null}

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <ChartFrame
              title="Most failed checks"
              subtitle="Endpoints with the most failures in this window"
              height={220}
              autoHeight
              table={
                <table className="table">
                  <thead>
                    <tr>
                      <th>Endpoint</th>
                      <th className="text-right">Failed checks</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(data.top_failing_endpoints || []).map((row) => (
                      <tr key={row.endpoint_id}>
                        <td>{row.name}</td>
                        <td className="tnum text-right">{row.failed_checks}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              }
            >
              <FailureBars endpoints={data.top_failing_endpoints} mode={mode} />
            </ChartFrame>

            <section className="card">
              <header className="card-header">
                <h2 className="card-title">Slowest endpoints</h2>
                <Link to="/endpoints?sort_by=response_time&sort_dir=desc" className="btn-ghost btn-sm">
                  View all
                </Link>
              </header>
              <div className="table-wrap">
                {data.slowest_endpoints?.length ? (
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Endpoint</th>
                        <th className="text-right">Avg response</th>
                        <th className="text-right">Checks</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.slowest_endpoints.map((row) => (
                        <tr key={row.endpoint_id}>
                          <td>
                            <Link
                              to={`/endpoints/${row.endpoint_id}`}
                              className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                            >
                              {row.name}
                            </Link>
                            <p className="truncate text-xs text-slate-400">{row.url}</p>
                          </td>
                          <td className="tnum text-right">
                            {formatMs(row.avg_response_time_ms)}
                          </td>
                          <td className="tnum text-right">{formatNumber(row.checks)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <EmptyState
                    icon={Clock}
                    title="No latency data yet"
                    description="Response times appear once the worker has completed a few checks."
                  />
                )}
              </div>
            </section>
          </div>

          {/* ------------------------------------------ recent incidents */}
          <section className="card">
            <header className="card-header">
              <h2 className="card-title">Recent incidents</h2>
              <Link to="/incidents" className="btn-ghost btn-sm">
                All incidents
              </Link>
            </header>
            <div className="table-wrap">
              {data.recent_incidents?.length ? (
                <table className="table">
                  <thead>
                    <tr>
                      <th>#</th>
                      <th>Endpoint</th>
                      <th>Status</th>
                      <th>Reason</th>
                      <th>Started</th>
                      <th className="text-right">Duration</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.recent_incidents.map((incident) => (
                      <tr key={incident.id}>
                        <td className="tnum text-slate-400">#{incident.id}</td>
                        <td>
                          <Link
                            to={`/endpoints/${incident.endpoint_id}`}
                            className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                          >
                            {incident.endpoint?.name || 'Endpoint'}
                          </Link>
                        </td>
                        <td>
                          <StatusBadge
                            status={incident.status === 'open' ? 'down' : 'up'}
                          />
                        </td>
                        <td className="text-slate-600 dark:text-slate-300">
                          {incident.reason_label ||
                            humanise(incident.reason, FAILURE_REASON_LABELS)}
                        </td>
                        <td className="whitespace-nowrap text-slate-600 dark:text-slate-300">
                          {formatRelative(incident.started_at)}
                        </td>
                        <td className="tnum whitespace-nowrap text-right">
                          {incident.status === 'open'
                            ? 'Ongoing'
                            : formatDuration(incident.duration_seconds)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <EmptyState
                  icon={CheckCircle2}
                  title="No incidents recorded"
                  description="Nothing has failed long enough to open an incident."
                />
              )}
            </div>
          </section>
        </div>
      )}
    </>
  )
}
