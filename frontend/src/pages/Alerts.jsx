import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { BellOff, Check, RefreshCw, Trash2 } from 'lucide-react'

import {
  Clamp,
  EmptyState,
  ErrorState,
  LoadingBlock,
  PageHeader,
  Pagination,
  SearchInput,
  SeverityBadge,
  Spinner,
} from '../components/ui'
import { alertsApi, settingsApi } from '../lib/api'
import { ALERT_TYPE_LABELS, formatDateTime, formatRelative, humanise } from '../lib/format'
import { useAuth } from '../hooks/useAuth'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import { useToast } from '../hooks/useToast'

const DELIVERY_TONE = {
  sent: 'text-green-600 dark:text-green-400',
  partial: 'text-amber-600 dark:text-amber-400',
  failed: 'text-red-600 dark:text-red-400',
  skipped: 'text-slate-400',
  pending: 'text-slate-400',
}

export default function Alerts() {
  const { can } = useAuth()
  const toast = useToast()
  const canWrite = can('alert:write')

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const [search, setSearch] = useState('')
  const [alertType, setAlertType] = useState('')
  const [severity, setSeverity] = useState('')
  const [acknowledged, setAcknowledged] = useState('false')

  const [data, setData] = useState(null)
  const [options, setOptions] = useState({ alert_types: [], severities: [] })
  const [counts, setCounts] = useState(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(() => new Set())
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    settingsApi.alertOptions().then(setOptions).catch(() => {})
  }, [])

  const loadCounts = useCallback(() => {
    alertsApi.unacknowledgedCount().then(setCounts).catch(() => {})
  }, [])

  const load = useCallback(
    async ({ silent = false } = {}) => {
      if (silent) setRefreshing(true)
      else setLoading(true)
      setError(null)
      try {
        const payload = await alertsApi.list({
          page,
          page_size: pageSize,
          search,
          alert_type: alertType || undefined,
          severity: severity || undefined,
          acknowledged: acknowledged === '' ? undefined : acknowledged === 'true',
        })
        setData(payload)
      } catch (err) {
        setError(err.message)
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [page, pageSize, search, alertType, severity, acknowledged],
  )

  useEffect(() => {
    load()
    loadCounts()
  }, [load, loadCounts])

  // Paused while rows are selected, so an acknowledge is never applied to a
  // list that shifted underneath the selection.
  useAutoRefresh(
    () => Promise.all([load({ silent: true }), loadCounts()]),
    { paused: selected.size > 0 },
  )

  useEffect(() => {
    setPage(1)
    setSelected(new Set())
  }, [search, alertType, severity, acknowledged, pageSize])

  const acknowledge = async (ids) => {
    setBusy(true)
    try {
      const result = await alertsApi.acknowledge(ids)
      toast.success(`${result.succeeded} alert(s) acknowledged.`)
      setSelected(new Set())
      load({ silent: true })
      loadCounts()
    } catch (err) {
      toast.error(err.message)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (id) => {
    try {
      await alertsApi.remove(id)
      toast.success('Alert deleted.')
      load({ silent: true })
      loadCounts()
    } catch (err) {
      toast.error(err.message)
    }
  }

  const items = data?.items || []
  const allSelected = items.length > 0 && items.every((item) => selected.has(item.id))

  return (
    <>
      <PageHeader
        title="Alerts"
        description={
          counts
            ? `${counts.total || 0} unacknowledged${counts.critical ? ` · ${counts.critical} critical` : ''}`
            : 'Generated alerts and their delivery status'
        }
        actions={
          <>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => {
                load({ silent: true })
                loadCounts()
              }}
              disabled={refreshing}
            >
              {refreshing ? <Spinner size={15} /> : <RefreshCw size={15} />}
              <span className="hidden sm:inline">Refresh</span>
            </button>
            {canWrite && (counts?.total || 0) > 0 ? (
              <button
                type="button"
                className="btn-primary"
                onClick={() => acknowledge(null)}
                disabled={busy}
              >
                {busy ? <Spinner size={15} className="text-white" /> : <Check size={15} />}
                Acknowledge all
              </button>
            ) : null}
          </>
        }
      />

      <div className="card mb-4 p-3">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <SearchInput value={search} onChange={setSearch} placeholder="Search alerts…" />
          <select
            className="input"
            value={alertType}
            onChange={(event) => setAlertType(event.target.value)}
            aria-label="Filter by alert type"
          >
            <option value="">All types</option>
            {(options.alert_types || []).map((type) => (
              <option key={type} value={type}>
                {humanise(type, ALERT_TYPE_LABELS)}
              </option>
            ))}
          </select>
          <select
            className="input"
            value={severity}
            onChange={(event) => setSeverity(event.target.value)}
            aria-label="Filter by severity"
          >
            <option value="">Any severity</option>
            {(options.severities || []).map((value) => (
              <option key={value} value={value}>
                {value.charAt(0).toUpperCase() + value.slice(1)}
              </option>
            ))}
          </select>
          <select
            className="input"
            value={acknowledged}
            onChange={(event) => setAcknowledged(event.target.value)}
            aria-label="Filter by acknowledgement"
          >
            <option value="false">Unacknowledged</option>
            <option value="true">Acknowledged</option>
            <option value="">All</option>
          </select>
        </div>
      </div>

      {selected.size > 0 && canWrite ? (
        <div className="card mb-3 flex items-center gap-2 border-brand-200 bg-brand-50 p-2.5 dark:border-brand-900 dark:bg-brand-900/20">
          <span className="text-sm font-medium text-brand-800 dark:text-brand-200">
            {selected.size} selected
          </span>
          <div className="ml-auto flex gap-1.5">
            <button
              type="button"
              className="btn-secondary btn-sm"
              onClick={() => acknowledge(Array.from(selected))}
              disabled={busy}
            >
              <Check size={13} /> Acknowledge
            </button>
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
            icon={BellOff}
            title={acknowledged === 'false' ? 'No unacknowledged alerts' : 'No alerts'}
            description="Alerts are raised when an endpoint goes down, recovers, responds slowly, or a certificate approaches expiry."
          />
        ) : (
          <>
            <div className={`table-wrap ${refreshing ? 'opacity-70' : ''}`}>
              <table className="table">
                <thead>
                  <tr>
                    {canWrite ? (
                      <th className="w-8">
                        <input
                          type="checkbox"
                          className="h-4 w-4 rounded border-slate-300"
                          checked={allSelected}
                          aria-label="Select all alerts on this page"
                          onChange={(event) =>
                            setSelected(
                              event.target.checked
                                ? new Set(items.map((item) => item.id))
                                : new Set(),
                            )
                          }
                        />
                      </th>
                    ) : null}
                    <th>Severity</th>
                    <th>Type</th>
                    <th>Alert</th>
                    <th>Endpoint</th>
                    <th>Delivery</th>
                    <th>Raised</th>
                    <th className="w-10" />
                  </tr>
                </thead>
                <tbody>
                  {items.map((alert) => (
                    <tr
                      key={alert.id}
                      className={alert.is_acknowledged ? 'opacity-60' : undefined}
                    >
                      {canWrite ? (
                        <td>
                          <input
                            type="checkbox"
                            className="h-4 w-4 rounded border-slate-300"
                            checked={selected.has(alert.id)}
                            aria-label={`Select alert ${alert.id}`}
                            onChange={() =>
                              setSelected((current) => {
                                const next = new Set(current)
                                if (next.has(alert.id)) next.delete(alert.id)
                                else next.add(alert.id)
                                return next
                              })
                            }
                          />
                        </td>
                      ) : null}
                      <td>
                        <SeverityBadge severity={alert.severity} />
                      </td>
                      <td className="whitespace-nowrap text-xs text-slate-600 dark:text-slate-300">
                        {humanise(alert.alert_type, ALERT_TYPE_LABELS)}
                      </td>
                      <td>
                        {/* Titles and messages contain long unbreakable URLs
                            and hostnames, so this cell wraps rather than
                            clamps - break-words stops the URL from pushing
                            the column wide. */}
                        <div className="max-w-[30rem] break-words">
                          <p className="font-medium text-slate-800 dark:text-slate-100">
                            {alert.title}
                          </p>
                          {alert.message ? (
                            <p className="text-xs text-slate-500 dark:text-slate-400">
                              {alert.message}
                            </p>
                          ) : null}
                          {alert.is_acknowledged ? (
                            <p className="mt-0.5 text-[11px] text-slate-400">
                              Acknowledged {formatRelative(alert.acknowledged_at)}
                            </p>
                          ) : null}
                        </div>
                      </td>
                      <td>
                        {alert.endpoint_id ? (
                          <Clamp width="13rem" title={alert.endpoint_name}>
                            <Link
                              to={`/endpoints/${alert.endpoint_id}`}
                              className="text-brand-600 hover:underline dark:text-brand-400"
                            >
                              {alert.endpoint_name}
                            </Link>
                          </Clamp>
                        ) : (
                          <span className="text-slate-400">—</span>
                        )}
                      </td>
                      <td className="whitespace-nowrap text-xs">
                        <span className={DELIVERY_TONE[alert.notification_status] || ''}>
                          {alert.notification_status}
                        </span>
                        {alert.notification_error ? (
                          <p
                            className="max-w-[12rem] truncate text-[11px] text-red-500"
                            title={alert.notification_error}
                          >
                            {alert.notification_error}
                          </p>
                        ) : null}
                      </td>
                      <td className="whitespace-nowrap">
                        {formatRelative(alert.created_at)}
                        <p className="text-[11px] text-slate-400">
                          {formatDateTime(alert.created_at, 'dd MMM HH:mm')}
                        </p>
                      </td>
                      <td className="text-right">
                        {canWrite ? (
                          <div className="flex justify-end gap-1">
                            {!alert.is_acknowledged ? (
                              <button
                                type="button"
                                className="btn-ghost p-1.5"
                                title="Acknowledge"
                                onClick={() => acknowledge([alert.id])}
                              >
                                <Check size={15} />
                              </button>
                            ) : null}
                            <button
                              type="button"
                              className="btn-ghost p-1.5 text-red-500"
                              title="Delete"
                              onClick={() => remove(alert.id)}
                            >
                              <Trash2 size={15} />
                            </button>
                          </div>
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
    </>
  )
}
