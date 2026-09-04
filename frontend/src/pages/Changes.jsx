import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle,
  CalendarClock,
  CheckCircle2,
  ClipboardList,
  Plus,
  RefreshCw,
  Rocket,
  XCircle,
} from 'lucide-react'
import clsx from 'clsx'

import ChangeForm from '../components/ChangeForm'
import { ChangeStatusBadge, RiskBadge } from '../components/change'
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
import { changesApi, taxonomyApi } from '../lib/api'
import { formatDateTime, formatNumber, formatRelative } from '../lib/format'
import { useAuth } from '../hooks/useAuth'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

const TABS = [
  { id: 'all', label: 'All changes' },
  { id: 'mine', label: 'My changes' },
  { id: 'pending', label: 'Pending approval' },
]

function Tile({ icon: Icon, label, value, tone = 'neutral', onClick, active }) {
  const tones = {
    neutral: 'text-slate-900 dark:text-slate-50',
    warn: 'text-amber-600 dark:text-amber-400',
    info: 'text-brand-600 dark:text-brand-400',
    good: 'text-green-600 dark:text-green-400',
    bad: 'text-red-600 dark:text-red-400',
    active: 'text-indigo-600 dark:text-indigo-400',
  }
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={clsx(
        'card flex items-center gap-3 p-3 text-left transition-shadow hover:shadow-md',
        active && 'ring-2 ring-brand-500',
      )}
    >
      <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-slate-100 dark:bg-slate-800">
        <Icon size={17} className={tones[tone]} />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-xs font-medium text-slate-500 dark:text-slate-400">
          {label}
        </span>
        <span className={clsx('block text-xl font-semibold leading-tight', tones[tone])}>
          {value}
        </span>
      </span>
    </button>
  )
}

export default function Changes() {
  const { can } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()

  const canWrite = can('change:write')

  const [tab, setTab] = useState(searchParams.get('tab') || 'all')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState(searchParams.get('status') || '')
  const [environment, setEnvironment] = useState('')
  const [risk, setRisk] = useState('')

  const [data, setData] = useState(null)
  const [summary, setSummary] = useState(null)
  const [environments, setEnvironments] = useState([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState(null)
  const [formOpen, setFormOpen] = useState(false)

  useEffect(() => {
    taxonomyApi.environments().then(setEnvironments).catch(() => {})
  }, [])

  useEffect(() => {
    const next = {}
    if (tab !== 'all') next.tab = tab
    if (statusFilter) next.status = statusFilter
    setSearchParams(next, { replace: true })
  }, [tab, statusFilter, setSearchParams])

  const loadSummary = useCallback(() => {
    changesApi.dashboard().then(setSummary).catch(() => {})
  }, [])

  const load = useCallback(
    async ({ silent = false } = {}) => {
      if (silent) setRefreshing(true)
      else setLoading(true)
      setError(null)
      try {
        const payload = await changesApi.list({
          page,
          page_size: pageSize,
          search,
          // The tabs are the same endpoint with different filters, so a tab
          // and a filter compose rather than fight.
          mine: tab === 'mine' ? true : undefined,
          status:
            tab === 'pending' ? 'pending_approval' : statusFilter || undefined,
          environment: environment || undefined,
          risk: risk || undefined,
        })
        setData(payload)
      } catch (err) {
        setError(err.message)
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [page, pageSize, search, tab, statusFilter, environment, risk],
  )

  useEffect(() => {
    load()
    loadSummary()
  }, [load, loadSummary])

  // Deployments move while you watch them, so the list stays current on its
  // own. Paused while the create/edit dialog is open.
  useAutoRefresh(
    () => Promise.all([load({ silent: true }), loadSummary()]),
    { paused: formOpen },
  )

  useEffect(() => {
    setPage(1)
  }, [tab, search, statusFilter, environment, risk, pageSize])

  const items = data?.items || []

  return (
    <>
      <PageHeader
        title="Change Management"
        description="Approve before production, record who deployed what, and pause monitoring automatically while it happens."
        actions={
          <>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => {
                load({ silent: true })
                loadSummary()
              }}
              disabled={refreshing}
            >
              {refreshing ? <Spinner size={15} /> : <RefreshCw size={15} />}
              <span className="hidden sm:inline">Refresh</span>
            </button>
            {canWrite ? (
              <button
                type="button"
                className="btn-primary"
                onClick={() => setFormOpen(true)}
              >
                <Plus size={16} /> New change
              </button>
            ) : null}
          </>
        }
      />

      {/* ------------------------------------------------ active banner */}
      {summary?.active?.length ? (
        <div className="card mb-4 border-indigo-200 bg-indigo-50 p-3 dark:border-indigo-900 dark:bg-indigo-950/30">
          <p className="mb-2 flex items-center gap-1.5 text-sm font-semibold text-indigo-900 dark:text-indigo-200">
            <Rocket size={15} />
            {summary.active.length} deployment
            {summary.active.length === 1 ? '' : 's'} in progress — monitoring paused
          </p>
          <ul className="space-y-1">
            {summary.active.map((change) => (
              <li key={change.id} className="text-sm">
                <Link
                  to={`/changes/${change.id}`}
                  className="font-medium text-brand-700 hover:underline dark:text-brand-300"
                >
                  {change.reference}
                </Link>{' '}
                <span className="text-slate-700 dark:text-slate-300">
                  {change.application}
                  {change.environment ? ` · ${change.environment}` : ''} — started by{' '}
                  {change.deployer_name} {formatRelative(change.started_at)}
                </span>
                {summary.overrunning?.some((o) => o.id === change.id) ? (
                  <span className="badge ml-2 bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300">
                    <AlertTriangle size={11} /> running over{' '}
                    {summary.max_pause_minutes} min
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* -------------------------------------------------- summary row */}
      {summary ? (
        <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
          <Tile
            icon={ClipboardList}
            label="Pending approval"
            value={formatNumber(summary.pending_approval)}
            tone={summary.pending_approval > 0 ? 'warn' : 'neutral'}
            active={tab === 'pending'}
            onClick={() => setTab('pending')}
          />
          <Tile
            icon={CheckCircle2}
            label="Approved"
            value={formatNumber(summary.approved)}
            tone="info"
            active={statusFilter === 'approved'}
            onClick={() => {
              setTab('all')
              setStatusFilter(statusFilter === 'approved' ? '' : 'approved')
            }}
          />
          <Tile
            icon={Rocket}
            label="Active deployments"
            value={formatNumber(summary.active_deployments)}
            tone={summary.active_deployments > 0 ? 'active' : 'neutral'}
            active={statusFilter === 'deployment_in_progress'}
            onClick={() => {
              setTab('all')
              setStatusFilter(
                statusFilter === 'deployment_in_progress'
                  ? ''
                  : 'deployment_in_progress',
              )
            }}
          />
          <Tile
            icon={CheckCircle2}
            label="Completed today"
            value={formatNumber(summary.completed_today)}
            tone="good"
          />
          <Tile
            icon={XCircle}
            label="Failed today"
            value={formatNumber(summary.failed_today)}
            tone={summary.failed_today > 0 ? 'bad' : 'neutral'}
          />
        </div>
      ) : null}

      {/* ---------------------------------------------------- upcoming */}
      {summary?.upcoming?.length ? (
        <div className="card mb-4">
          <div className="card-header">
            <h2 className="card-title flex items-center gap-1.5">
              <CalendarClock size={15} /> Upcoming deployments
            </h2>
          </div>
          <div className="divide-y divide-slate-100 dark:divide-slate-800">
            {summary.upcoming.slice(0, 5).map((change) => (
              <div
                key={change.id}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-sm"
              >
                <span className="tnum w-28 shrink-0 font-medium">
                  {formatDateTime(change.expected_start_at, 'dd MMM HH:mm')}
                </span>
                <Link
                  to={`/changes/${change.id}`}
                  className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                >
                  {change.application}
                </Link>
                {change.environment ? (
                  <span className="chip">{change.environment}</span>
                ) : null}
                <RiskBadge risk={change.risk} />
                <span className="ml-auto">
                  <ChangeStatusBadge status={change.status} size="sm" />
                </span>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {/* -------------------------------------------------------- tabs */}
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
            onClick={() => {
              setTab(item.id)
              if (item.id === 'pending') setStatusFilter('')
            }}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="card mb-4 p-3">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search reference, title, application…"
          />
          <select
            className="input"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}
            disabled={tab === 'pending'}
            aria-label="Filter by status"
          >
            <option value="">Any status</option>
            <option value="draft">Draft</option>
            <option value="pending_approval">Pending approval</option>
            <option value="approved">Approved</option>
            <option value="deployment_in_progress">Deploying</option>
            <option value="completed">Completed</option>
            <option value="failed">Failed</option>
            <option value="rejected">Rejected</option>
            <option value="cancelled">Cancelled</option>
          </select>
          <select
            className="input"
            value={environment}
            onChange={(event) => setEnvironment(event.target.value)}
            aria-label="Filter by environment"
          >
            <option value="">All environments</option>
            {environments.map((item) => (
              <option key={item.id} value={item.id}>
                {item.display_name || item.name}
              </option>
            ))}
          </select>
          <select
            className="input"
            value={risk}
            onChange={(event) => setRisk(event.target.value)}
            aria-label="Filter by risk"
          >
            <option value="">Any risk</option>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
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
              tab === 'pending'
                ? 'Nothing is waiting for approval'
                : tab === 'mine'
                  ? 'You have not raised any changes'
                  : 'No change requests yet'
            }
            description="A change records what is being deployed, who approved it, and pauses the affected monitoring while it runs."
            action={
              canWrite ? (
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() => setFormOpen(true)}
                >
                  <Plus size={16} /> New change
                </button>
              ) : null
            }
          />
        ) : (
          <>
            <div className={`table-wrap ${refreshing ? 'opacity-70' : ''}`}>
              <table className="table">
                <thead>
                  <tr>
                    <th>Reference</th>
                    <th>Title</th>
                    <th>Application</th>
                    <th>Environment</th>
                    <th>Risk</th>
                    <th>Status</th>
                    <th>Planned</th>
                    <th>Requester</th>
                    <th className="text-right">Endpoints</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((change) => (
                    <tr key={change.id}>
                      <td className="whitespace-nowrap">
                        <Link
                          to={`/changes/${change.id}`}
                          className="font-mono text-xs font-semibold text-brand-600 hover:underline dark:text-brand-400"
                        >
                          {change.reference}
                        </Link>
                      </td>
                      <td>
                        <Clamp width="20rem" title={change.title}>
                          {change.title}
                        </Clamp>
                      </td>
                      <td>
                        <Clamp width="12rem" title={change.application}>
                          {change.application}
                        </Clamp>
                      </td>
                      <td className="whitespace-nowrap text-slate-600 dark:text-slate-300">
                        {change.environment || '—'}
                      </td>
                      <td>
                        <RiskBadge risk={change.risk} />
                      </td>
                      <td>
                        <ChangeStatusBadge status={change.status} />
                      </td>
                      <td className="whitespace-nowrap">
                        {formatDateTime(change.expected_start_at, 'dd MMM HH:mm')}
                        <p className="text-[11px] text-slate-400">
                          {change.expected_duration_minutes} min
                        </p>
                      </td>
                      <td className="whitespace-nowrap text-slate-600 dark:text-slate-300">
                        {change.requester_name || '—'}
                        {change.deployer_name ? (
                          <p className="text-[11px] text-slate-400">
                            deployed by {change.deployer_name}
                          </p>
                        ) : null}
                      </td>
                      <td className="tnum text-right">{change.endpoint_count}</td>
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

      <ChangeForm
        open={formOpen}
        environments={environments}
        onClose={() => setFormOpen(false)}
        onSaved={() => {
          setFormOpen(false)
          load({ silent: true })
          loadSummary()
        }}
      />
    </>
  )
}
