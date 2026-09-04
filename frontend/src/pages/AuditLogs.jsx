import { useCallback, useEffect, useState } from 'react'
import { FileClock, RefreshCw } from 'lucide-react'

import {
  EmptyState,
  ErrorState,
  LoadingBlock,
  Modal,
  PageHeader,
  Pagination,
  SearchInput,
  Spinner,
} from '../components/ui'
import { settingsApi } from '../lib/api'
import { formatDateTime, formatRelative, humanise } from '../lib/format'

const ACTION_TONE = (action) => {
  if (action.includes('deleted') || action.includes('failed')) {
    return 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300'
  }
  if (action.includes('created') || action.includes('imported')) {
    return 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300'
  }
  if (action.includes('password') || action.includes('role') || action.includes('settings')) {
    return 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300'
  }
  return 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300'
}

export default function AuditLogs() {
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [search, setSearch] = useState('')
  const [action, setAction] = useState('')
  const [status, setStatus] = useState('')
  const [since, setSince] = useState('')

  const [data, setData] = useState(null)
  const [actions, setActions] = useState([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)

  useEffect(() => {
    settingsApi.auditActions().then(setActions).catch(() => {})
  }, [])

  const load = useCallback(
    async ({ silent = false } = {}) => {
      if (silent) setRefreshing(true)
      else setLoading(true)
      setError(null)
      try {
        const payload = await settingsApi.auditLogs({
          page,
          page_size: pageSize,
          search,
          action: action || undefined,
          status: status || undefined,
          since: since ? new Date(since).toISOString() : undefined,
        })
        setData(payload)
      } catch (err) {
        setError(err.message)
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [page, pageSize, search, action, status, since],
  )

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    setPage(1)
  }, [search, action, status, since, pageSize])

  const items = data?.items || []

  return (
    <>
      <PageHeader
        title="Audit logs"
        description={
          data
            ? `${data.meta.total.toLocaleString()} entries · append-only`
            : 'Administrative action trail'
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
            placeholder="Search user, resource or IP…"
          />
          <select
            className="input"
            value={action}
            onChange={(event) => setAction(event.target.value)}
            aria-label="Filter by action"
          >
            <option value="">All actions</option>
            {actions.map((value) => (
              <option key={value} value={value}>
                {humanise(value)}
              </option>
            ))}
          </select>
          <select
            className="input"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            aria-label="Filter by outcome"
          >
            <option value="">Any outcome</option>
            <option value="success">Success</option>
            <option value="failure">Failure</option>
            <option value="locked">Locked</option>
            <option value="rate_limited">Rate limited</option>
          </select>
          <input
            type="datetime-local"
            className="input"
            value={since}
            onChange={(event) => setSince(event.target.value)}
            aria-label="Only entries after"
          />
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
            icon={FileClock}
            title="No audit entries match"
            description="Sign-ins, endpoint changes, user management and configuration changes are all recorded here."
          />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>User</th>
                    <th>Action</th>
                    <th>Resource</th>
                    <th>Outcome</th>
                    <th>IP address</th>
                    <th className="w-10" />
                  </tr>
                </thead>
                <tbody>
                  {items.map((entry) => (
                    <tr key={entry.id}>
                      <td className="whitespace-nowrap">
                        {formatDateTime(entry.created_at, 'dd MMM HH:mm:ss')}
                        <p className="text-[11px] text-slate-400">
                          {formatRelative(entry.created_at)}
                        </p>
                      </td>
                      <td className="whitespace-nowrap font-medium">
                        {entry.username || <span className="text-slate-400">system</span>}
                      </td>
                      <td>
                        <span className={`badge ${ACTION_TONE(entry.action)}`}>
                          {humanise(entry.action)}
                        </span>
                      </td>
                      <td className="max-w-[16rem]">
                        {entry.resource_name ? (
                          <p className="truncate" title={entry.resource_name}>
                            {entry.resource_name}
                          </p>
                        ) : (
                          <span className="text-slate-400">—</span>
                        )}
                        {entry.resource_type ? (
                          <p className="text-[11px] text-slate-400">{entry.resource_type}</p>
                        ) : null}
                      </td>
                      <td>
                        <span
                          className={
                            entry.status === 'success'
                              ? 'text-xs text-green-600 dark:text-green-400'
                              : 'text-xs text-red-600 dark:text-red-400'
                          }
                        >
                          {entry.status}
                        </span>
                      </td>
                      <td className="whitespace-nowrap font-mono text-[11px] text-slate-500">
                        {entry.ip_address || '—'}
                      </td>
                      <td className="text-right">
                        {entry.details && Object.keys(entry.details).length ? (
                          <button
                            type="button"
                            className="btn-ghost btn-sm"
                            onClick={() => setSelected(entry)}
                          >
                            Details
                          </button>
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

      <Modal
        open={Boolean(selected)}
        onClose={() => setSelected(null)}
        title={selected ? `${humanise(selected.action)} · ${selected.username || 'system'}` : ''}
        footer={
          <button type="button" className="btn-secondary" onClick={() => setSelected(null)}>
            Close
          </button>
        }
      >
        {selected ? (
          <div className="space-y-3 text-sm">
            <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
              <div>
                <dt className="text-xs text-slate-500">Timestamp</dt>
                <dd className="font-medium">{formatDateTime(selected.created_at)}</dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">Outcome</dt>
                <dd className="font-medium">{selected.status}</dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">Resource</dt>
                <dd className="font-medium">
                  {selected.resource_name || selected.resource_id || '—'}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">Request</dt>
                <dd className="font-mono text-xs">
                  {selected.request_method} {selected.request_path}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">IP address</dt>
                <dd className="font-mono text-xs">{selected.ip_address || '—'}</dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-xs text-slate-500">User agent</dt>
                <dd className="break-all text-xs">{selected.user_agent || '—'}</dd>
              </div>
            </dl>
            <div>
              <p className="label">Details</p>
              {/* Credential-shaped values are scrubbed server-side before the
                  entry is written, so this is always safe to display. */}
              <pre className="max-h-72 overflow-auto rounded-lg bg-slate-50 p-3 font-mono text-[11px] text-slate-700 dark:bg-slate-800 dark:text-slate-200">
                {JSON.stringify(selected.details, null, 2)}
              </pre>
            </div>
          </div>
        ) : null}
      </Modal>
    </>
  )
}
