import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Download, RefreshCw, ShieldCheck } from 'lucide-react'

import {
  Clamp,
  EmptyState,
  ErrorState,
  LoadingBlock,
  PageHeader,
  Pagination,
  SearchInput,
  SortHeader,
  Spinner,
  SslBadge,
  TagChip,
} from '../components/ui'
import { endpointsApi, sslApi } from '../lib/api'
import {
  formatDate,
  formatDateTime,
  formatDaysRemaining,
  formatNumber,
  formatRelative,
} from '../lib/format'
import { useToast } from '../hooks/useToast'

/** Counter chip in the header; doubles as a status filter. */
function CountChip({ label, value, tone, active, onClick }) {
  const tones = {
    good: 'border-green-200 bg-green-50 text-green-800 dark:border-green-900 dark:bg-green-950/50 dark:text-green-300',
    warn: 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/50 dark:text-amber-300',
    bad: 'border-red-200 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/50 dark:text-red-300',
    neutral:
      'border-slate-200 bg-white text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300',
  }
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-lg border px-3 py-2 text-left transition-shadow hover:shadow-sm ${tones[tone]} ${
        active ? 'ring-2 ring-brand-500' : ''
      }`}
    >
      <span className="block text-lg font-semibold leading-tight">{formatNumber(value)}</span>
      <span className="block text-[11px] font-medium">{label}</span>
    </button>
  )
}

export default function SslCertificates() {
  const toast = useToast()
  const [searchParams, setSearchParams] = useSearchParams()

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState(searchParams.get('status') || '')
  const [issuer, setIssuer] = useState('')
  const [environment, setEnvironment] = useState('')
  const [tag, setTag] = useState('')
  const [expiringWithin, setExpiringWithin] = useState('')
  const [sortBy, setSortBy] = useState('remaining')
  const [sortDir, setSortDir] = useState('asc')

  const [data, setData] = useState(null)
  const [summary, setSummary] = useState(null)
  const [issuers, setIssuers] = useState([])
  const [filters, setFilters] = useState({ environments: [], tags: [] })
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    sslApi.summary().then(setSummary).catch(() => {})
    sslApi.issuers().then(setIssuers).catch(() => {})
    endpointsApi.filters().then(setFilters).catch(() => {})
  }, [])

  useEffect(() => {
    const next = {}
    if (status) next.status = status
    setSearchParams(next, { replace: true })
  }, [status, setSearchParams])

  const load = useCallback(
    async ({ silent = false } = {}) => {
      if (silent) setRefreshing(true)
      else setLoading(true)
      setError(null)
      try {
        const payload = await sslApi.list({
          page,
          page_size: pageSize,
          search,
          status: status || undefined,
          issuer: issuer || undefined,
          environment: environment || undefined,
          tag: tag || undefined,
          expiring_within_days: expiringWithin || undefined,
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
    [page, pageSize, search, status, issuer, environment, tag, expiringWithin, sortBy, sortDir],
  )

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    setPage(1)
  }, [search, status, issuer, environment, tag, expiringWithin, pageSize])

  const refresh = () => {
    load({ silent: true })
    sslApi.summary().then(setSummary).catch(() => {})
  }

  const exportCsv = async () => {
    try {
      const { downloadFile } = await import('../lib/api')
      await downloadFile('/api/export?format=csv', 'infrasight-endpoints.csv')
    } catch (err) {
      toast.error(err.message)
    }
  }

  const items = data?.items || []

  const toggleStatus = (value) => setStatus((current) => (current === value ? '' : value))

  return (
    <>
      <PageHeader
        title="SSL certificates"
        description={
          summary
            ? `${formatNumber(summary.total)} tracked · warning at ${summary.warning_days} days, critical at ${summary.critical_days}`
            : 'Certificate inventory and expiry tracking'
        }
        actions={
          <>
            <button
              type="button"
              className="btn-secondary"
              onClick={refresh}
              disabled={refreshing}
            >
              {refreshing ? <Spinner size={15} /> : <RefreshCw size={15} />}
              <span className="hidden sm:inline">Refresh</span>
            </button>
            <button type="button" className="btn-secondary" onClick={exportCsv}>
              <Download size={15} />
              <span className="hidden sm:inline">Export</span>
            </button>
          </>
        }
      />

      {summary ? (
        <div className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
          <CountChip
            label="Valid"
            value={summary.valid}
            tone="good"
            active={status === 'valid'}
            onClick={() => toggleStatus('valid')}
          />
          <CountChip
            label="Expiring soon"
            value={summary.expiring_soon}
            tone="warn"
            active={status === 'expiring_soon'}
            onClick={() => toggleStatus('expiring_soon')}
          />
          <CountChip
            label="Critical"
            value={summary.critical}
            tone="warn"
            active={status === 'critical'}
            onClick={() => toggleStatus('critical')}
          />
          <CountChip
            label="Expired"
            value={summary.expired}
            tone="bad"
            active={status === 'expired'}
            onClick={() => toggleStatus('expired')}
          />
          <CountChip
            label="Invalid"
            value={summary.invalid}
            tone="bad"
            active={status === 'invalid'}
            onClick={() => toggleStatus('invalid')}
          />
          <CountChip
            label="Unable to check"
            value={summary.unable_to_check}
            tone="neutral"
            active={status === 'unable_to_check'}
            onClick={() => toggleStatus('unable_to_check')}
          />
        </div>
      ) : null}

      <div className="card mb-4 p-3">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <div className="lg:col-span-2">
            <SearchInput
              value={search}
              onChange={setSearch}
              placeholder="Search endpoint, hostname, common name, issuer…"
            />
          </div>
          <select
            className="input"
            value={issuer}
            onChange={(event) => setIssuer(event.target.value)}
            aria-label="Filter by issuer"
          >
            <option value="">All issuers</option>
            {issuers.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
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
            value={expiringWithin}
            onChange={(event) => setExpiringWithin(event.target.value)}
            aria-label="Filter by remaining validity"
          >
            <option value="">Any expiry</option>
            <option value="7">Within 7 days</option>
            <option value="14">Within 14 days</option>
            <option value="30">Within 30 days</option>
            <option value="60">Within 60 days</option>
            <option value="90">Within 90 days</option>
          </select>
        </div>
        <div className="mt-3">
          <select
            className="input w-auto"
            value={tag}
            onChange={(event) => setTag(event.target.value)}
            aria-label="Filter by tag"
          >
            <option value="">All tags</option>
            {filters.tags.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
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
            icon={ShieldCheck}
            title="No certificates match"
            description="Certificates appear once the worker has completed a TLS handshake with an HTTPS endpoint."
          />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <SortHeader
                      label="Endpoint"
                      field="endpoint"
                      sortBy={sortBy}
                      sortDir={sortDir}
                      onSort={(f, d) => {
                        setSortBy(f)
                        setSortDir(d)
                      }}
                    />
                    <SortHeader
                      label="Certificate"
                      field="common_name"
                      sortBy={sortBy}
                      sortDir={sortDir}
                      onSort={(f, d) => {
                        setSortBy(f)
                        setSortDir(d)
                      }}
                    />
                    <SortHeader
                      label="Issuer"
                      field="issuer"
                      sortBy={sortBy}
                      sortDir={sortDir}
                      onSort={(f, d) => {
                        setSortBy(f)
                        setSortDir(d)
                      }}
                    />
                    <th>Environment</th>
                    <SortHeader
                      label="Expiry"
                      field="expiry"
                      sortBy={sortBy}
                      sortDir={sortDir}
                      onSort={(f, d) => {
                        setSortBy(f)
                        setSortDir(d)
                      }}
                    />
                    <SortHeader
                      label="Remaining"
                      field="remaining"
                      sortBy={sortBy}
                      sortDir={sortDir}
                      onSort={(f, d) => {
                        setSortBy(f)
                        setSortDir(d)
                      }}
                      align="right"
                    />
                    <SortHeader
                      label="Status"
                      field="status"
                      sortBy={sortBy}
                      sortDir={sortDir}
                      onSort={(f, d) => {
                        setSortBy(f)
                        setSortDir(d)
                      }}
                    />
                    <th>Key</th>
                    <th>Checked</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((row) => (
                    <tr key={row.endpoint_id}>
                      <td>
                        <div className="max-w-[16rem]">
                          <Clamp width="16rem" title={row.endpoint_name}>
                            <Link
                              to={`/endpoints/${row.endpoint_id}`}
                              className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                            >
                              {row.endpoint_name}
                            </Link>
                          </Clamp>
                          <p
                            className="truncate font-mono text-[11px] text-slate-400"
                            title={row.url}
                          >
                            {row.hostname}
                          </p>
                        </div>
                        {row.tags?.length ? (
                          <div className="mt-0.5 flex flex-wrap gap-1">
                            {row.tags.slice(0, 2).map((name) => (
                              <TagChip key={name} name={name} />
                            ))}
                          </div>
                        ) : null}
                      </td>

                      <td className="max-w-[14rem]">
                        <p className="truncate font-mono text-xs" title={row.common_name || ''}>
                          {row.common_name || '—'}
                        </p>
                        <div className="mt-0.5 flex flex-wrap gap-1">
                          {row.is_wildcard ? <span className="chip">wildcard</span> : null}
                          {row.is_self_signed ? (
                            <span className="chip">self-signed</span>
                          ) : null}
                          {row.san_count > 0 ? (
                            <span className="chip">{row.san_count} SAN</span>
                          ) : null}
                        </div>
                      </td>

                      <td className="max-w-[12rem]">
                        <p className="truncate text-xs" title={row.issuer || ''}>
                          {row.issuer || '—'}
                        </p>
                        {row.issuer_organization ? (
                          <p className="truncate text-[11px] text-slate-400">
                            {row.issuer_organization}
                          </p>
                        ) : null}
                      </td>

                      <td className="whitespace-nowrap text-slate-600 dark:text-slate-300">
                        {row.environment || '—'}
                      </td>

                      <td className="whitespace-nowrap">
                        {row.expires_at ? formatDate(row.expires_at) : '—'}
                      </td>

                      <td className="tnum whitespace-nowrap text-right font-medium">
                        {formatDaysRemaining(row.days_remaining)}
                      </td>

                      <td>
                        <SslBadge status={row.status} />
                        {row.chain_verified === false ? (
                          <p className="text-[11px] text-red-500">chain not verified</p>
                        ) : null}
                        {row.hostname_matches === false ? (
                          <p className="text-[11px] text-red-500">hostname mismatch</p>
                        ) : null}
                      </td>

                      <td className="whitespace-nowrap text-xs text-slate-500">
                        {row.key_algorithm
                          ? `${row.key_algorithm}${row.key_size ? ` ${row.key_size}` : ''}`
                          : '—'}
                        {row.tls_version ? (
                          <p className="text-[11px] text-slate-400">{row.tls_version}</p>
                        ) : null}
                      </td>

                      <td className="whitespace-nowrap text-xs text-slate-500">
                        {formatRelative(row.checked_at)}
                        <p className="text-[11px] text-slate-400">
                          {formatDateTime(row.checked_at, 'dd MMM HH:mm')}
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
    </>
  )
}
