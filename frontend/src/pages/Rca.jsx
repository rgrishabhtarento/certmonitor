import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  ClipboardList,
  RefreshCw,
  Repeat,
  TrendingUp,
} from 'lucide-react'
import clsx from 'clsx'

import { CategoryBadge, RcaStatusBadge } from '../components/rca'
import {
  Clamp,
  EmptyState,
  ErrorState,
  LoadingBlock,
  PageHeader,
  Pagination,
  SearchInput,
  Spinner,
} from '../components/ui'
import { rcaApi } from '../lib/api'
import { formatDateTime, formatNumber, formatRelative } from '../lib/format'
import { SLOW_INTERVAL, useAutoRefresh } from '../hooks/useAutoRefresh'

// Scope only. State lives on the tiles and the status select - three
// controls for one concept was what made them contradict each other.
const TABS = [
  { id: 'all', label: 'All' },
  { id: 'mine', label: 'Mine' },
]

const TILE_TONES = {
  neutral: 'text-slate-900 dark:text-slate-50',
  warn: 'text-amber-600 dark:text-amber-400',
  info: 'text-blue-600 dark:text-blue-400',
  good: 'text-green-600 dark:text-green-400',
  bad: 'text-red-600 dark:text-red-400',
  muted: 'text-slate-500 dark:text-slate-400',
}

/**
 * A count, which may or may not be a filter.
 *
 * The distinction is the point. Previously every tile rendered as a button
 * with a hover shadow while only two of the six did anything, so four of them
 * invited a click and then ignored it. A tile that does not filter is now a
 * plain div with no hover affordance and no `aria-pressed`, and one that does
 * says so - including to a screen reader.
 */
function Tile({ label, value, tone = 'neutral', onClick, to, active, hint }) {
  const body = (
    <>
      <span className="block truncate text-xs font-medium text-slate-500 dark:text-slate-400">
        {label}
      </span>
      <span className={clsx('block text-2xl font-semibold leading-tight', TILE_TONES[tone])}>
        {value}
      </span>
      {hint ? (
        <span className="mt-0.5 block truncate text-[11px] text-slate-400">{hint}</span>
      ) : null}
    </>
  )

  const base = 'card p-3 text-left'
  const interactive = 'transition-shadow hover:shadow-md'

  if (to) {
    return (
      <Link to={to} className={clsx(base, interactive)}>
        {body}
      </Link>
    )
  }

  if (!onClick) {
    // Not a filter. No hover lift, so it does not promise a click it cannot
    // honour.
    return <div className={base}>{body}</div>
  }

  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={Boolean(active)}
      className={clsx(base, interactive, active && 'ring-2 ring-brand-500')}
    >
      {body}
    </button>
  )
}

export default function Rca() {
  const [searchParams, setSearchParams] = useSearchParams()

  const [tab, setTab] = useState(searchParams.get('tab') || 'all')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [overdueOnly, setOverdueOnly] = useState(false)
  const [category, setCategory] = useState('')

  /** Tiles toggle: pressing the active one clears the filter. */
  const toggleStatus = (value) => {
    setOverdueOnly(false)
    setStatusFilter((current) => (current === value ? '' : value))
  }

  const [data, setData] = useState(null)
  const [board, setBoard] = useState(null)
  const [analytics, setAnalytics] = useState(null)
  const [options, setOptions] = useState(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    const next = {}
    if (tab !== 'all') next.tab = tab
    setSearchParams(next, { replace: true })
  }, [tab, setSearchParams])

  const loadAside = useCallback(() => {
    rcaApi.dashboard().then(setBoard).catch(() => {})
    rcaApi.analytics(90).then(setAnalytics).catch(() => {})
    rcaApi.options().then(setOptions).catch(() => {})
  }, [])

  const load = useCallback(
    async ({ silent = false } = {}) => {
      if (silent) setRefreshing(true)
      else setLoading(true)
      setError(null)
      try {
        setData(
          await rcaApi.list({
            page,
            page_size: pageSize,
            search,
            // Tabs are *whose*; tiles and the select are *what state*. They
            // used to overlap - a tab could silently override the status
            // filter, so the Pending tile listed In progress rows too.
            mine: tab === 'mine' ? true : undefined,
            status: statusFilter || undefined,
            overdue: overdueOnly || undefined,
            category: category || undefined,
          }),
        )
      } catch (err) {
        setError(err.message)
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [page, pageSize, search, tab, statusFilter, overdueOnly, category],
  )

  useEffect(() => {
    load()
    loadAside()
  }, [load, loadAside])

  useAutoRefresh(
    () => Promise.all([load({ silent: true }), loadAside()]),
    { interval: SLOW_INTERVAL },
  )

  useEffect(() => {
    setPage(1)
  }, [tab, search, statusFilter, overdueOnly, category, pageSize])

  const items = data?.items || []

  return (
    <>
      <PageHeader
        title="Root Cause Analysis"
        description="Optional, and independent of the incident. An incident can close with its RCA still open — that is normal, not a gap."
        actions={
          <button
            type="button"
            className="btn-secondary"
            onClick={() => {
              load({ silent: true })
              loadAside()
            }}
            disabled={refreshing}
          >
            {refreshing ? <Spinner size={15} /> : <RefreshCw size={15} />}
            <span className="hidden sm:inline">Refresh</span>
          </button>
        }
      />

      {/* --------------------------------------------------- overview */}
      {board ? (
        <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          {/* Context, not a filter: this counts incidents, and the list
              below holds RCAs. */}
          <Tile label="Total incidents" value={formatNumber(board.total_incidents)} />

          {/* Also incidents rather than RCAs, so it cannot filter this list -
              but it is the actionable number here, and the Incidents page is
              where an RCA gets requested. */}
          <Tile
            label="No RCA record"
            value={formatNumber(board.not_requested)}
            tone="muted"
            to="/incidents"
            hint="Request from Incidents"
          />

          <Tile
            label="Pending"
            value={formatNumber(board.pending)}
            tone={board.pending > 0 ? 'warn' : 'neutral'}
            active={statusFilter === 'pending'}
            onClick={() => toggleStatus('pending')}
          />
          <Tile
            label="In progress"
            value={formatNumber(board.in_progress)}
            tone="info"
            active={statusFilter === 'in_progress'}
            onClick={() => toggleStatus('in_progress')}
          />
          <Tile
            label="Completed"
            value={formatNumber(board.completed)}
            tone="good"
            active={statusFilter === 'completed'}
            onClick={() => toggleStatus('completed')}
          />
          <Tile
            label="Overdue"
            value={formatNumber(board.overdue)}
            tone={board.overdue > 0 ? 'bad' : 'neutral'}
            active={overdueOnly}
            onClick={() => {
              setOverdueOnly((value) => !value)
              setStatusFilter('')
            }}
          />
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          {/* ------------------------------------------------ tabs */}
          <div
            className="mb-3 flex gap-1 overflow-x-auto border-b border-slate-200 dark:border-slate-800"
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
                // Scope and status are independent now, so switching between
                // All and Mine keeps whatever status filter is applied.
                onClick={() => setTab(item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>

          <div className="card mb-3 p-3">
            <div className="grid gap-3 sm:grid-cols-3">
              <SearchInput
                value={search}
                onChange={setSearch}
                placeholder="Search endpoint, cause, owner…"
              />
              {/* Always enabled, and the same state the tiles drive - so
                  picking a status here lights the matching tile, and pressing
                  a tile moves this. One filter, two ways in. */}
              <select
                className="input"
                value={statusFilter}
                onChange={(event) => {
                  setOverdueOnly(false)
                  setStatusFilter(event.target.value)
                }}
                aria-label="Filter by status"
              >
                <option value="">Any status</option>
                <option value="pending">Pending</option>
                <option value="in_progress">In progress</option>
                <option value="completed">Completed</option>
                <option value="not_required">Not required</option>
              </select>
              <select
                className="input"
                value={category}
                onChange={(event) => setCategory(event.target.value)}
                aria-label="Filter by root cause category"
              >
                <option value="">Any category</option>
                {(options?.categories || []).map((item) => (
                  <option key={item} value={item}>
                    {item.replace(/_/g, ' ')}
                  </option>
                ))}
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
                icon={ClipboardList}
                title={
                  tab === 'mine'
                    ? 'No RCA is assigned to you or your team'
                    : 'No RCA records yet'
                }
                description="An RCA is requested from an incident. It is never required, and it never blocks the incident from being resolved or closed."
              />
            ) : (
              <>
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>RCA</th>
                        <th>Endpoint</th>
                        <th>Application</th>
                        <th>Owner</th>
                        <th>Category</th>
                        <th>Status</th>
                        <th className="text-right">Age</th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((rca) => (
                        <tr key={rca.id}>
                          <td className="whitespace-nowrap">
                            <Link
                              to={`/rca/${rca.id}`}
                              className="font-mono text-xs font-semibold text-brand-600 hover:underline dark:text-brand-400"
                            >
                              RCA-{rca.id}
                            </Link>
                            <p className="text-[11px] text-slate-400">
                              INC-{rca.incident_id}
                            </p>
                          </td>
                          <td>
                            <Clamp width="14rem" title={rca.endpoint_name}>
                              {rca.endpoint_name || '—'}
                            </Clamp>
                          </td>
                          <td>
                            <Clamp width="10rem" title={rca.application}>
                              {rca.application || '—'}
                            </Clamp>
                          </td>
                          <td className="whitespace-nowrap">
                            {rca.owner_label || (
                              <span className="text-slate-400">Unassigned</span>
                            )}
                            {rca.owner_type ? (
                              <p className="text-[11px] text-slate-400">
                                {rca.owner_type}
                              </p>
                            ) : null}
                          </td>
                          <td>
                            <CategoryBadge category={rca.root_cause_category} />
                          </td>
                          <td>
                            <RcaStatusBadge
                              status={rca.status}
                              overdue={rca.is_overdue}
                            />
                          </td>
                          <td className="tnum whitespace-nowrap text-right">
                            {rca.age_days != null ? `${rca.age_days}d` : '—'}
                            <p className="text-[11px] text-slate-400">
                              {formatRelative(rca.created_at)}
                            </p>
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
        </div>

        {/* --------------------------------------------------- sidebar */}
        <div className="space-y-4">
          {board?.pending_queue?.length ? (
            <div className="card">
              <div className="card-header">
                <h2 className="card-title">Pending queue</h2>
              </div>
              <ol className="divide-y divide-slate-100 dark:divide-slate-800">
                {board.pending_queue.map((rca) => (
                  <li key={rca.id} className="px-4 py-2 text-sm">
                    <Link
                      to={`/rca/${rca.id}`}
                      className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                    >
                      RCA-{rca.id}
                    </Link>{' '}
                    <span className="text-slate-600 dark:text-slate-300">
                      {rca.endpoint_name}
                    </span>
                    <p className="text-xs text-slate-400">
                      {rca.owner_label || 'Unassigned'}
                      {rca.due_at
                        ? ` · due ${formatDateTime(rca.due_at, 'dd MMM')}`
                        : ''}
                      {rca.is_overdue ? ' · overdue' : ''}
                    </p>
                  </li>
                ))}
              </ol>
            </div>
          ) : null}

          {analytics ? (
            <>
              <div className="card">
                <div className="card-header">
                  <h2 className="card-title flex items-center gap-1.5">
                    <TrendingUp size={15} /> Reporting
                  </h2>
                  <span className="text-xs text-slate-400">
                    last {analytics.window_days} days
                  </span>
                </div>
                <div className="space-y-2 p-4">
                  <p className="text-sm text-slate-700 dark:text-slate-200">
                    Completion rate:{' '}
                    <span className="font-semibold">
                      {analytics.completion_rate_percent != null
                        ? `${analytics.completion_rate_percent}%`
                        : '—'}
                    </span>
                    {analytics.eligible ? (
                      <span className="text-slate-400">
                        {' '}
                        ({analytics.completed} of {analytics.eligible})
                      </span>
                    ) : null}
                  </p>
                  <p className="text-sm text-slate-700 dark:text-slate-200">
                    Average time to complete:{' '}
                    <span className="font-semibold">
                      {analytics.average_completion_days != null
                        ? `${analytics.average_completion_days} days`
                        : '—'}
                    </span>
                  </p>
                  {analytics.deployment_related_percent != null ? (
                    <p className="text-sm text-slate-700 dark:text-slate-200">
                      Deployment-related:{' '}
                      <span className="font-semibold">
                        {analytics.deployment_related_percent}%
                      </span>
                    </p>
                  ) : null}

                  {analytics.top_root_causes.length ? (
                    <div className="pt-1">
                      <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                        Top root causes
                      </p>
                      <ul className="space-y-1">
                        {analytics.top_root_causes.map((item) => (
                          <li key={item.category} className="text-sm">
                            <span className="inline-block w-32 capitalize text-slate-700 dark:text-slate-200">
                              {item.category.replace(/_/g, ' ')}
                            </span>
                            <span className="tnum font-semibold">
                              {item.percent}%
                            </span>
                            <span className="ml-1 text-xs text-slate-400">
                              ({item.count})
                            </span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : (
                    <p className="pt-1 text-xs text-slate-500 dark:text-slate-400">
                      No completed RCA has recorded a category yet — these
                      reports show only what people have actually written.
                    </p>
                  )}
                </div>
              </div>

              {analytics.recurring_root_causes?.length ? (
                <div className="card">
                  <div className="card-header">
                    <h2 className="card-title flex items-center gap-1.5">
                      <Repeat size={15} /> Recurring root causes
                    </h2>
                  </div>
                  <ul className="divide-y divide-slate-100 dark:divide-slate-800">
                    {analytics.recurring_root_causes.map((item, index) => (
                      <li key={index} className="px-4 py-2.5">
                        <p className="text-sm font-medium text-slate-900 dark:text-slate-50">
                          {item.root_cause}
                        </p>
                        <p className="text-xs text-slate-500 dark:text-slate-400">
                          {item.occurrences} occurrences ·{' '}
                          {item.application_count} application
                          {item.application_count === 1 ? '' : 's'} · last{' '}
                          {formatRelative(item.last_occurrence)}
                        </p>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </>
          ) : null}
        </div>
      </div>
    </>
  )
}
