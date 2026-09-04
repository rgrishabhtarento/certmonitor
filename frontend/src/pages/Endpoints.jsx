import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  Download,
  MoreHorizontal,
  Pause,
  Play,
  Plus,
  RefreshCw,
  ServerCog,
  Trash2,
  Zap,
} from 'lucide-react'

import EndpointForm from '../components/EndpointForm'
import {
  Clamp,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingBlock,
  PageHeader,
  Pagination,
  SearchInput,
  SortHeader,
  Spinner,
  SslBadge,
  StatusBadge,
  TagChip,
} from '../components/ui'
import { endpointsApi, settingsApi } from '../lib/api'
import {
  formatDaysRemaining,
  formatInterval,
  formatMs,
  formatPercent,
  formatRelative,
} from '../lib/format'
import { useAuth } from '../hooks/useAuth'
import { SLOW_INTERVAL, useAutoRefresh } from '../hooks/useAutoRefresh'
import { useToast } from '../hooks/useToast'

export default function Endpoints() {
  const { can } = useAuth()
  const toast = useToast()
  const [searchParams, setSearchParams] = useSearchParams()

  const canWrite = can('endpoint:write')
  const canDelete = can('endpoint:delete')
  const canCheck = can('endpoint:check')

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const [search, setSearch] = useState(searchParams.get('search') || '')
  const [environment, setEnvironment] = useState(searchParams.get('environment') || '')
  const [tag, setTag] = useState(searchParams.get('tag') || '')
  const [status, setStatus] = useState(searchParams.get('status') || '')
  const [sslStatus, setSslStatus] = useState(searchParams.get('ssl_status') || '')
  const [owner, setOwner] = useState('')
  const [sortBy, setSortBy] = useState(searchParams.get('sort_by') || 'name')
  const [sortDir, setSortDir] = useState(searchParams.get('sort_dir') || 'asc')

  const [data, setData] = useState(null)
  const [filters, setFilters] = useState({ environments: [], tags: [], owners: [] })
  const [config, setConfig] = useState(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState(null)

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [confirmDelete, setConfirmDelete] = useState(null)
  const [deleting, setDeleting] = useState(false)
  const [checkingId, setCheckingId] = useState(null)
  const [selected, setSelected] = useState(() => new Set())
  const [menuFor, setMenuFor] = useState(null)

  useEffect(() => {
    endpointsApi.filters().then(setFilters).catch(() => {})
    settingsApi
      .get()
      .then((payload) => setConfig(payload.effective))
      .catch(() => {
        // A viewer may not hold settings:read; the form falls back to its own
        // defaults in that case.
      })
  }, [])

  // Keep the URL in step with the filters so a filtered view is shareable.
  useEffect(() => {
    const next = {}
    if (search) next.search = search
    if (environment) next.environment = environment
    if (tag) next.tag = tag
    if (status) next.status = status
    if (sslStatus) next.ssl_status = sslStatus
    if (sortBy !== 'name') next.sort_by = sortBy
    if (sortDir !== 'asc') next.sort_dir = sortDir
    setSearchParams(next, { replace: true })
  }, [search, environment, tag, status, sslStatus, sortBy, sortDir, setSearchParams])

  const load = useCallback(
    async ({ silent = false } = {}) => {
      if (silent) setRefreshing(true)
      else setLoading(true)
      setError(null)
      try {
        const payload = await endpointsApi.list({
          page,
          page_size: pageSize,
          search,
          environment: environment || undefined,
          tag: tag || undefined,
          status: status || undefined,
          ssl_status: sslStatus || undefined,
          owner: owner || undefined,
          sort_by: sortBy,
          sort_dir: sortDir,
        })
        setData(payload)
      } catch (err) {
        setError(err.message)
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [page, pageSize, search, environment, tag, status, sslStatus, owner, sortBy, sortDir],
  )

  useEffect(() => {
    load()
  }, [load])

  // Status changes here are the whole point of the page. Paused while rows
  // are selected, so a bulk action is never applied to a list that moved
  // underneath the selection.
  useAutoRefresh(() => load({ silent: true }), {
    interval: SLOW_INTERVAL,
    paused: selected.size > 0 || formOpen,
  })

  // Any filter change invalidates the current page number.
  useEffect(() => {
    setPage(1)
    setSelected(new Set())
  }, [search, environment, tag, status, sslStatus, owner, pageSize])

  const items = data?.items || []
  const allSelected = items.length > 0 && items.every((item) => selected.has(item.id))

  const toggleSelected = (id) => {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const runCheck = async (endpoint) => {
    setCheckingId(endpoint.id)
    try {
      const result = await endpointsApi.check(endpoint.id, true)
      const detail = [
        result.status.toUpperCase(),
        result.http_status_code ? `HTTP ${result.http_status_code}` : null,
        result.response_time_ms ? `${Math.round(result.response_time_ms)} ms` : null,
      ]
        .filter(Boolean)
        .join(' · ')
      if (result.status === 'up') toast.success(`${endpoint.name}: ${detail}`)
      else if (result.status === 'degraded') toast.warning(`${endpoint.name}: ${detail}`)
      else
        toast.error(
          `${endpoint.name}: ${detail}${result.error_message ? ` — ${result.error_message}` : ''}`,
        )
      load({ silent: true })
    } catch (err) {
      toast.error(`Could not check ${endpoint.name}: ${err.message}`)
    } finally {
      setCheckingId(null)
    }
  }

  const setMonitoring = async (endpoint, changes) => {
    try {
      await endpointsApi.setMonitoring(endpoint.id, changes)
      toast.success(`${endpoint.name} updated.`)
      load({ silent: true })
    } catch (err) {
      toast.error(err.message)
    }
  }

  const doDelete = async () => {
    if (!confirmDelete) return
    setDeleting(true)
    try {
      await endpointsApi.remove(confirmDelete.id)
      toast.success(`'${confirmDelete.name}' deleted.`)
      setConfirmDelete(null)
      load({ silent: true })
    } catch (err) {
      toast.error(err.message)
    } finally {
      setDeleting(false)
    }
  }

  const bulk = async (action, extra = {}) => {
    const ids = Array.from(selected)
    if (!ids.length) return
    try {
      const result = await endpointsApi.bulk({ endpoint_ids: ids, action, ...extra })
      toast.success(
        `${action}: ${result.succeeded} succeeded${result.failed ? `, ${result.failed} failed` : ''}.`,
      )
      setSelected(new Set())
      load({ silent: true })
    } catch (err) {
      toast.error(err.message)
    }
  }

  const onSort = (field, direction) => {
    setSortBy(field)
    setSortDir(direction)
  }

  const activeFilterCount = useMemo(
    () => [environment, tag, status, sslStatus, owner].filter(Boolean).length,
    [environment, tag, status, sslStatus, owner],
  )

  return (
    <>
      <PageHeader
        title="Endpoints"
        description={
          data ? `${data.meta.total.toLocaleString()} configured` : 'Monitored endpoints'
        }
        actions={
          <>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => load({ silent: true })}
              disabled={refreshing}
            >
              {refreshing ? <Spinner size={15} /> : <RefreshCw size={15} />}
              <span className="hidden sm:inline">Refresh</span>
            </button>
            {can('endpoint:export') ? (
              <Link to="/import-export" className="btn-secondary">
                <Download size={15} />
                <span className="hidden sm:inline">Import / Export</span>
              </Link>
            ) : null}
            {canWrite ? (
              <button
                type="button"
                className="btn-primary"
                onClick={() => {
                  setEditing(null)
                  setFormOpen(true)
                }}
              >
                <Plus size={16} /> Add endpoint
              </button>
            ) : null}
          </>
        }
      />

      {/* ------------------------------------------------- filter row */}
      <div className="card mb-4 p-3">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
          <div className="lg:col-span-2">
            <SearchInput
              value={search}
              onChange={setSearch}
              placeholder="Search name, URL, hostname, owner…"
            />
          </div>
          <select
            className="input"
            value={environment}
            onChange={(event) => setEnvironment(event.target.value)}
            aria-label="Filter by environment"
          >
            <option value="">All environments</option>
            {filters.environments.map((item) => (
              <option key={item.id} value={item.id}>
                {item.display_name || item.name} ({item.endpoint_count})
              </option>
            ))}
          </select>
          <select
            className="input"
            value={tag}
            onChange={(event) => setTag(event.target.value)}
            aria-label="Filter by tag"
          >
            <option value="">All tags</option>
            {filters.tags.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} ({item.endpoint_count})
              </option>
            ))}
          </select>
          <select
            className="input"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            aria-label="Filter by status"
          >
            <option value="">Any status</option>
            <option value="up">Healthy</option>
            <option value="degraded">Degraded</option>
            <option value="down">Down</option>
            <option value="unknown">Unknown</option>
            <option value="paused">Paused</option>
          </select>
          <select
            className="input"
            value={sslStatus}
            onChange={(event) => setSslStatus(event.target.value)}
            aria-label="Filter by certificate state"
          >
            <option value="">Any certificate state</option>
            <option value="valid">Valid</option>
            <option value="expiring_soon">Expiring soon</option>
            <option value="critical">Critical</option>
            <option value="expired">Expired</option>
            <option value="invalid">Invalid</option>
            <option value="unable_to_check">Unable to check</option>
          </select>
        </div>

        {activeFilterCount > 0 ? (
          <div className="mt-2.5 flex items-center gap-2 text-xs text-slate-500">
            <span>
              {activeFilterCount} filter{activeFilterCount === 1 ? '' : 's'} applied
            </span>
            <button
              type="button"
              className="btn-ghost btn-sm"
              onClick={() => {
                setEnvironment('')
                setTag('')
                setStatus('')
                setSslStatus('')
                setOwner('')
              }}
            >
              Clear all
            </button>
          </div>
        ) : null}
      </div>

      {/* -------------------------------------------- bulk action bar */}
      {selected.size > 0 && canWrite ? (
        <div className="card mb-3 flex flex-wrap items-center gap-2 border-brand-200 bg-brand-50 p-2.5 dark:border-brand-900 dark:bg-brand-900/20">
          <span className="text-sm font-medium text-brand-800 dark:text-brand-200">
            {selected.size} selected
          </span>
          <div className="ml-auto flex flex-wrap gap-1.5">
            {canCheck ? (
              <button type="button" className="btn-secondary btn-sm" onClick={() => bulk('check')}>
                <Zap size={13} /> Check now
              </button>
            ) : null}
            <button type="button" className="btn-secondary btn-sm" onClick={() => bulk('resume')}>
              <Play size={13} /> Resume
            </button>
            <button type="button" className="btn-secondary btn-sm" onClick={() => bulk('pause')}>
              <Pause size={13} /> Pause
            </button>
            {canDelete ? (
              <button
                type="button"
                className="btn-danger btn-sm"
                onClick={() => {
                  if (
                    window.confirm(
                      `Delete ${selected.size} endpoint(s) and their monitoring history? This cannot be undone.`,
                    )
                  ) {
                    bulk('delete')
                  }
                }}
              >
                <Trash2 size={13} /> Delete
              </button>
            ) : null}
            <button
              type="button"
              className="btn-ghost btn-sm"
              onClick={() => setSelected(new Set())}
            >
              Clear
            </button>
          </div>
        </div>
      ) : null}

      {/* ----------------------------------------------------- table */}
      <div className="card overflow-visible">
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
            icon={ServerCog}
            title={
              search || activeFilterCount
                ? 'No endpoints match these filters'
                : 'No endpoints yet'
            }
            description={
              search || activeFilterCount
                ? 'Try clearing a filter or widening the search.'
                : 'Add your first endpoint and the monitoring worker will pick it up within a minute.'
            }
            action={
              canWrite && !search && !activeFilterCount ? (
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() => {
                    setEditing(null)
                    setFormOpen(true)
                  }}
                >
                  <Plus size={16} /> Add endpoint
                </button>
              ) : null
            }
          />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    {canWrite ? (
                      <th className="w-8">
                        <input
                          type="checkbox"
                          className="h-4 w-4 rounded border-slate-300"
                          checked={allSelected}
                          aria-label="Select all rows on this page"
                          onChange={(event) => {
                            setSelected(
                              event.target.checked
                                ? new Set(items.map((item) => item.id))
                                : new Set(),
                            )
                          }}
                        />
                      </th>
                    ) : null}
                    <SortHeader
                      label="Endpoint"
                      field="name"
                      sortBy={sortBy}
                      sortDir={sortDir}
                      onSort={onSort}
                    />
                    <SortHeader
                      label="Status"
                      field="status"
                      sortBy={sortBy}
                      sortDir={sortDir}
                      onSort={onSort}
                    />
                    <th>Environment</th>
                    <th>Tags</th>
                    <SortHeader
                      label="Response"
                      field="response_time"
                      sortBy={sortBy}
                      sortDir={sortDir}
                      onSort={onSort}
                      align="right"
                    />
                    <th className="text-right">Uptime 24h</th>
                    <SortHeader
                      label="Certificate"
                      field="ssl_days_remaining"
                      sortBy={sortBy}
                      sortDir={sortDir}
                      onSort={onSort}
                    />
                    <SortHeader
                      label="Last check"
                      field="last_checked_at"
                      sortBy={sortBy}
                      sortDir={sortDir}
                      onSort={onSort}
                    />
                    <th className="w-10" />
                  </tr>
                </thead>
                <tbody>
                  {items.map((endpoint) => (
                    <tr key={endpoint.id}>
                      {canWrite ? (
                        <td>
                          <input
                            type="checkbox"
                            className="h-4 w-4 rounded border-slate-300"
                            checked={selected.has(endpoint.id)}
                            aria-label={`Select ${endpoint.name}`}
                            onChange={() => toggleSelected(endpoint.id)}
                          />
                        </td>
                      ) : null}

                      <td>
                        <div className="max-w-[18rem]">
                          <Clamp width="18rem" title={endpoint.name}>
                            <Link
                              to={`/endpoints/${endpoint.id}`}
                              className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                            >
                              {endpoint.name}
                            </Link>
                          </Clamp>
                          <p
                            className="truncate font-mono text-[11px] text-slate-400"
                            title={endpoint.url}
                          >
                            {endpoint.url}
                          </p>
                          {endpoint.resolved_health_path ? (
                            <p
                              className="truncate font-mono text-[11px] text-brand-600 dark:text-brand-400"
                              title={`The configured path was not found; checks use ${endpoint.resolved_health_path}`}
                            >
                              ↳ {endpoint.resolved_health_path}
                            </p>
                          ) : null}
                        </div>
                      </td>

                      <td>
                        <StatusBadge status={endpoint.current_status} pulse />
                        {endpoint.is_paused && endpoint.pause_reason ? (
                          <p
                            className="max-w-[14rem] truncate text-[11px] text-amber-600 dark:text-amber-400"
                            title={endpoint.pause_reason}
                          >
                            {endpoint.pause_reason}
                          </p>
                        ) : null}
                        {endpoint.consecutive_failures > 0 &&
                        endpoint.current_status === 'down' ? (
                          <p className="tnum mt-0.5 text-[11px] text-red-500">
                            {endpoint.consecutive_failures} consecutive
                          </p>
                        ) : null}
                        {endpoint.last_error && endpoint.current_status !== 'up' ? (
                          <p
                            className="max-w-[14rem] truncate text-[11px] text-slate-400"
                            title={endpoint.last_error}
                          >
                            {endpoint.last_error}
                          </p>
                        ) : null}
                      </td>

                      <td className="whitespace-nowrap text-slate-600 dark:text-slate-300">
                        {endpoint.environment?.display_name ||
                          endpoint.environment?.name ||
                          '—'}
                      </td>

                      <td>
                        <div className="flex max-w-[12rem] flex-wrap gap-1">
                          {(endpoint.tags || []).slice(0, 3).map((item) => (
                            <TagChip key={item.id} name={item.name} />
                          ))}
                          {endpoint.tags?.length > 3 ? (
                            <span className="chip">+{endpoint.tags.length - 3}</span>
                          ) : null}
                        </div>
                      </td>

                      <td className="tnum whitespace-nowrap text-right">
                        {formatMs(endpoint.last_response_time_ms)}
                        {endpoint.last_status_code ? (
                          <p className="text-[11px] text-slate-400">
                            HTTP {endpoint.last_status_code}
                          </p>
                        ) : null}
                      </td>

                      <td className="tnum text-right">
                        {formatPercent(endpoint.uptime_percent_24h)}
                      </td>

                      <td className="whitespace-nowrap">
                        {endpoint.ssl_monitoring_enabled ? (
                          <>
                            <SslBadge status={endpoint.ssl_status} />
                            {endpoint.ssl_days_remaining !== null ? (
                              <p className="tnum mt-0.5 text-[11px] text-slate-400">
                                {formatDaysRemaining(endpoint.ssl_days_remaining)}
                              </p>
                            ) : null}
                          </>
                        ) : (
                          <span className="text-xs text-slate-400">Not monitored</span>
                        )}
                      </td>

                      <td className="whitespace-nowrap text-slate-600 dark:text-slate-300">
                        {formatRelative(endpoint.last_checked_at)}
                        <p className="text-[11px] text-slate-400">
                          every {formatInterval(endpoint.interval_seconds)}
                        </p>
                      </td>

                      <td className="relative text-right">
                        <button
                          type="button"
                          className="btn-ghost p-1.5"
                          onClick={() =>
                            setMenuFor(menuFor === endpoint.id ? null : endpoint.id)
                          }
                          aria-label={`Actions for ${endpoint.name}`}
                          aria-haspopup="menu"
                        >
                          {checkingId === endpoint.id ? (
                            <Spinner size={15} />
                          ) : (
                            <MoreHorizontal size={16} />
                          )}
                        </button>

                        {menuFor === endpoint.id ? (
                          <>
                            <div
                              className="fixed inset-0 z-10"
                              onMouseDown={() => setMenuFor(null)}
                              aria-hidden="true"
                            />
                            <div
                              className="absolute right-2 z-20 mt-1 w-44 overflow-hidden rounded-lg border border-slate-200 bg-white text-left shadow-lg dark:border-slate-700 dark:bg-slate-800"
                              role="menu"
                            >
                              <Link
                                to={`/endpoints/${endpoint.id}`}
                                className="block px-3 py-2 text-sm hover:bg-slate-50 dark:hover:bg-slate-700"
                                role="menuitem"
                              >
                                View details
                              </Link>
                              {canCheck ? (
                                <button
                                  type="button"
                                  className="block w-full px-3 py-2 text-left text-sm hover:bg-slate-50 dark:hover:bg-slate-700"
                                  role="menuitem"
                                  onClick={() => {
                                    setMenuFor(null)
                                    runCheck(endpoint)
                                  }}
                                >
                                  Check now
                                </button>
                              ) : null}
                              {canWrite ? (
                                <>
                                  <button
                                    type="button"
                                    className="block w-full px-3 py-2 text-left text-sm hover:bg-slate-50 dark:hover:bg-slate-700"
                                    role="menuitem"
                                    onClick={() => {
                                      setMenuFor(null)
                                      setEditing(endpoint)
                                      setFormOpen(true)
                                    }}
                                  >
                                    Edit
                                  </button>
                                  <button
                                    type="button"
                                    className="block w-full px-3 py-2 text-left text-sm hover:bg-slate-50 dark:hover:bg-slate-700"
                                    role="menuitem"
                                    onClick={() => {
                                      setMenuFor(null)
                                      setMonitoring(endpoint, {
                                        is_paused: !endpoint.is_paused,
                                        monitoring_enabled: true,
                                      })
                                    }}
                                  >
                                    {endpoint.is_paused ? 'Resume monitoring' : 'Pause monitoring'}
                                  </button>
                                </>
                              ) : null}
                              {canDelete ? (
                                <button
                                  type="button"
                                  className="block w-full px-3 py-2 text-left text-sm text-red-600 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-900/30"
                                  role="menuitem"
                                  onClick={() => {
                                    setMenuFor(null)
                                    setConfirmDelete(endpoint)
                                  }}
                                >
                                  Delete
                                </button>
                              ) : null}
                            </div>
                          </>
                        ) : null}
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

      <EndpointForm
        open={formOpen}
        endpoint={editing}
        filters={filters}
        config={config}
        onClose={() => setFormOpen(false)}
        onSaved={() => {
          setFormOpen(false)
          endpointsApi.filters().then(setFilters).catch(() => {})
          load({ silent: true })
        }}
      />

      <ConfirmDialog
        open={Boolean(confirmDelete)}
        onClose={() => setConfirmDelete(null)}
        onConfirm={doDelete}
        busy={deleting}
        danger
        title="Delete this endpoint?"
        confirmLabel="Delete endpoint"
        message={
          confirmDelete
            ? `'${confirmDelete.name}' and all of its monitoring history, certificates and incidents will be permanently deleted. The audit log entry remains.`
            : ''
        }
      />
    </>
  )
}
