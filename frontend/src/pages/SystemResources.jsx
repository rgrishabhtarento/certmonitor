import { useCallback, useEffect, useState } from 'react'
import {
  AlertTriangle,
  Cpu,
  Database,
  HardDrive,
  Info,
  MemoryStick,
  Server,
} from 'lucide-react'
import clsx from 'clsx'

import LiveIndicator from '../components/LiveIndicator'
import {
  Card,
  ErrorState,
  LoadingBlock,
  PageHeader,
} from '../components/ui'
import { systemApi } from '../lib/api'
import { formatDateTime, formatNumber, formatRelative } from '../lib/format'
import { LIVE_INTERVAL, useAutoRefresh } from '../hooks/useAutoRefresh'

/**
 * InfraSight watching its own services.
 *
 * A monitoring tool that runs out of disk stops monitoring, and does so
 * silently - checks simply stop being recorded. So the questions it asks of
 * everything else get asked of itself.
 *
 * What it will not do is pretend to numbers it cannot see. There is no Docker
 * socket behind this page, so nginx CPU and postgres memory are absent - and
 * the "Not measured" section says so explicitly rather than leaving a blank
 * tile that reads like a healthy zero.
 */

/**
 * Which band a percentage falls into.
 *
 * `higherIsBetter` matters more than it looks. Most meters here measure
 * consumption, where high is bad - disk, memory, connections. A cache hit
 * ratio is the opposite: 99.99% is excellent, and painting it red because the
 * number is large turns the healthiest reading on the page into an alarm.
 */
function band(percent, { warn = 75, bad = 90, higherIsBetter = false } = {}) {
  if (percent == null) return 'unknown'
  if (higherIsBetter) {
    if (percent <= bad) return 'bad'
    if (percent <= warn) return 'warn'
    return 'good'
  }
  if (percent >= bad) return 'bad'
  if (percent >= warn) return 'warn'
  return 'good'
}

const TEXT_TONE = {
  good: 'text-green-600 dark:text-green-400',
  warn: 'text-amber-600 dark:text-amber-400',
  bad: 'text-red-600 dark:text-red-400',
  unknown: 'text-slate-500 dark:text-slate-400',
}

const BAR_TONE = {
  good: 'bg-green-500',
  warn: 'bg-amber-500',
  bad: 'bg-red-500',
  unknown: 'bg-slate-300 dark:bg-slate-600',
}

/** A usage bar. Never colour alone - the figure is always beside it. */
function Meter({ label, percent, detail, thresholds }) {
  const level = band(percent, thresholds)
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <span className="text-xs font-medium text-slate-500 dark:text-slate-400">
          {label}
        </span>
        <span className={clsx('tnum text-sm font-semibold', TEXT_TONE[level])}>
          {percent == null ? 'not reported' : `${percent}%`}
        </span>
      </div>
      <div
        className="h-1.5 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"
        role="img"
        aria-label={`${label}: ${percent == null ? 'not reported' : `${percent}%`}`}
      >
        {percent != null ? (
          <div
            className={clsx('h-full rounded-full', BAR_TONE[level])}
            style={{ width: `${Math.min(100, Math.max(2, percent))}%` }}
          />
        ) : null}
      </div>
      {detail ? (
        <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{detail}</p>
      ) : null}
    </div>
  )
}

function ProcessCard({ icon: Icon, title, subtitle, stats, extra }) {
  return (
    <Card
      title={
        <span className="flex items-center gap-1.5">
          <Icon size={15} /> {title}
        </span>
      }
    >
      {subtitle ? (
        <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">{subtitle}</p>
      ) : null}
      <div className="space-y-3">
        <Meter
          label="CPU"
          percent={stats?.cpu_percent}
          detail={
            stats?.cpu_percent == null
              ? 'A rate needs two samples — this appears after the next refresh.'
              : stats?.cpu_cores
                ? `of ${stats.cpu_cores} core${stats.cpu_cores === 1 ? '' : 's'} available`
                : null
          }
        />
        <Meter
          label="Memory"
          percent={stats?.memory_percent}
          detail={
            stats?.memory_mb
              ? stats.memory_limit_mb
                ? `${formatNumber(stats.memory_mb)} MB of ${formatNumber(stats.memory_limit_mb)} MB`
                : `${formatNumber(stats.memory_mb)} MB used — no container limit set`
              : null
          }
        />
        {extra}
      </div>
    </Card>
  )
}

export default function SystemResources() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      setData(await systemApi.resources())
    } catch (err) {
      setError(err.message)
      throw err
    }
  }, [])

  const { refreshing, lastRefreshedAt, refreshNow } = useAutoRefresh(load, {
    interval: LIVE_INTERVAL,
  })

  useEffect(() => {
    load().catch(() => {})
  }, [load])

  if (!data && !error) {
    return (
      <>
        <PageHeader title="System resources" />
        <LoadingBlock rows={6} />
      </>
    )
  }
  if (error && !data) {
    return (
      <>
        <PageHeader title="System resources" />
        <ErrorState message={error} onRetry={() => load().catch(() => {})} />
      </>
    )
  }

  const { disk, database, redis, api, workers, days_until_disk_full } = data
  const diskCritical = disk.used_percent != null && disk.used_percent >= 90

  return (
    <>
      <PageHeader
        title="System resources"
        description="How InfraSight itself is doing, measured on this server."
        actions={
          <LiveIndicator
            refreshing={refreshing}
            lastRefreshedAt={lastRefreshedAt}
            onRefresh={refreshNow}
            showToggle
          />
        }
      />

      {/* -------------------------------------------------- disk headroom */}
      {diskCritical || (days_until_disk_full != null && days_until_disk_full < 30) ? (
        <div className="card mb-4 border-red-200 bg-red-50 p-3 dark:border-red-900 dark:bg-red-950/30">
          <p className="flex items-center gap-1.5 text-sm font-semibold text-red-900 dark:text-red-200">
            <AlertTriangle size={15} />
            {diskCritical
              ? `Disk is ${disk.used_percent}% full`
              : `About ${days_until_disk_full} days of disk headroom left`}
          </p>
          <p className="mt-1 text-sm text-red-800 dark:text-red-300">
            When this fills, PostgreSQL stops accepting writes and monitoring
            stops silently — checks are simply no longer recorded. Lower{' '}
            <code className="font-mono text-xs">DATA_RETENTION_DAYS</code> in
            Settings, or add disk.
          </p>
        </div>
      ) : null}

      <div className="mb-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* ------------------------------------------------------- disk */}
        <Card
          title={
            <span className="flex items-center gap-1.5">
              <HardDrive size={15} /> Disk
            </span>
          }
        >
          {disk.available ? (
            <>
              <Meter
                label={`Filesystem ${disk.path}`}
                percent={disk.used_percent}
                detail={`${disk.used_gb} GB used of ${disk.total_gb} GB — ${disk.free_gb} GB free`}
              />
              <p className="mt-3 text-xs text-slate-500 dark:text-slate-400">
                {days_until_disk_full != null ? (
                  <>
                    At the current growth rate this leaves roughly{' '}
                    <strong>{days_until_disk_full} days</strong> before the
                    disk is full.
                  </>
                ) : database.at_steady_state ? (
                  'The database has passed its retention window, so deletions now balance inserts and it should stop growing.'
                ) : (
                  'Not enough history yet to project a growth rate.'
                )}
              </p>
              <p className="mt-2 text-xs text-slate-400">
                Measured on the root filesystem of the API container, which on the
                default Compose setup shares a host device with the postgres
                volume. If you moved PostgreSQL to its own disk, this is not
                that disk.
              </p>
            </>
          ) : (
            <p className="text-sm text-slate-500">Not readable: {disk.error}</p>
          )}
        </Card>

        {/* --------------------------------------------------- database */}
        <Card
          title={
            <span className="flex items-center gap-1.5">
              <Database size={15} /> PostgreSQL
            </span>
          }
        >
          {database.available ? (
            <>
              <div className="mb-3 flex flex-wrap gap-x-6 gap-y-2">
                <div>
                  <p className="text-xs text-slate-500 dark:text-slate-400">Size</p>
                  <p className="text-xl font-semibold text-slate-900 dark:text-slate-50">
                    {database.size_pretty}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-slate-500 dark:text-slate-400">
                    Growth
                  </p>
                  <p className="text-xl font-semibold text-slate-900 dark:text-slate-50">
                    {database.growth_bytes_per_day
                      ? `${(database.growth_bytes_per_day / 1024 / 1024).toFixed(0)} MB/day`
                      : '—'}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-slate-500 dark:text-slate-400">
                    Check results
                  </p>
                  <p className="text-xl font-semibold text-slate-900 dark:text-slate-50">
                    {formatNumber(database.monitoring_results)}
                  </p>
                </div>
              </div>

              <Meter
                label="Connections"
                percent={
                  database.max_connections
                    ? Math.round(
                        (database.connections / database.max_connections) * 100,
                      )
                    : null
                }
                detail={`${database.connections} of ${database.max_connections}`}
              />

              <div className="mt-3">
                {/* Higher is better here, unlike every other meter on this
                    page. Without the flag, a perfect 99.99% renders red. */}
                <Meter
                  label="Cache hit ratio"
                  percent={database.cache_hit_percent}
                  detail="Below about 99% usually means shared_buffers is too small for the working set."
                  thresholds={{ higherIsBetter: true, warn: 99, bad: 95 }}
                />
              </div>

              {database.oldest_result_at ? (
                <p className="mt-3 text-xs text-slate-500 dark:text-slate-400">
                  History goes back to {formatDateTime(database.oldest_result_at)}{' '}
                  ({formatRelative(database.oldest_result_at)}); retention is set
                  to {database.retention_days} days.
                </p>
              ) : null}
            </>
          ) : (
            <p className="text-sm text-slate-500">
              Not readable: {database.error}
            </p>
          )}
        </Card>
      </div>

      {/* ---------------------------------------------------- largest tables */}
      {database.tables?.length ? (
        <Card
          title="Largest tables"
          className="mb-4"
          bodyClassName="p-0"
        >
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Table</th>
                  <th className="text-right">Size</th>
                  <th className="text-right">Rows</th>
                  <th className="text-right">Share</th>
                </tr>
              </thead>
              <tbody>
                {database.tables.map((table) => {
                  const share = database.size_bytes
                    ? Math.round((table.bytes / database.size_bytes) * 100)
                    : null
                  return (
                    <tr key={table.name}>
                      <td className="font-mono text-xs">{table.name}</td>
                      <td className="tnum text-right">{table.pretty}</td>
                      <td className="tnum text-right">{formatNumber(table.rows)}</td>
                      <td className="tnum text-right text-slate-500">
                        {share != null ? `${share}%` : '—'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}

      {/* --------------------------------------------------------- services */}
      <div className="mb-4 grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        <ProcessCard
          icon={Server}
          title="API"
          subtitle="The container serving this page, from its own cgroup."
          stats={api}
        />

        {workers.map((worker) => (
          <ProcessCard
            key={worker.worker_id}
            icon={Cpu}
            title={`Worker · ${worker.worker_id}`}
            subtitle={
              worker.healthy
                ? `Heartbeat ${worker.seconds_since_heartbeat}s ago · ${formatNumber(worker.checks_completed)} checks done`
                : `No heartbeat for ${worker.seconds_since_heartbeat}s`
            }
            stats={worker}
            extra={
              <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-slate-100 pt-2 text-xs text-slate-500 dark:border-slate-800 dark:text-slate-400">
                <span>In flight: {worker.in_flight}</span>
                <span>Failed: {formatNumber(worker.checks_failed)}</span>
                <span>Up {Math.round(worker.uptime_seconds / 3600)}h</span>
              </div>
            }
          />
        ))}

        <Card
          title={
            <span className="flex items-center gap-1.5">
              <MemoryStick size={15} /> Redis
            </span>
          }
        >
          {redis.available ? (
            <div className="space-y-3">
              <Meter
                label="Memory"
                percent={redis.memory_percent}
                detail={
                  redis.memory_limit_mb
                    ? `${redis.memory_mb} MB of ${redis.memory_limit_mb} MB`
                    : `${redis.memory_mb} MB used — no maxmemory set`
                }
              />
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500 dark:text-slate-400">
                <span>v{redis.version}</span>
                <span>{redis.connected_clients} clients</span>
                <span>{formatNumber(redis.keys)} keys</span>
                <span>{formatNumber(redis.cpu_seconds)}s CPU total</span>
              </div>
            </div>
          ) : (
            <p className="text-sm text-slate-500 dark:text-slate-400">
              Unavailable: {redis.reason}
              <br />
              <span className="text-xs">
                Redis is optional — rate limiting and import previews fall back
                to in-process equivalents.
              </span>
            </p>
          )}
        </Card>
      </div>

      {/* ----------------------------------------------------- not measured */}
      {data.not_measured?.length ? (
        <Card
          title={
            <span className="flex items-center gap-1.5">
              <Info size={15} /> Not measured
            </span>
          }
        >
          <p className="mb-2 text-xs text-slate-600 dark:text-slate-300">
            Listed rather than left as an empty tile, so a gap is never read as
            a healthy zero.
          </p>
          <ul className="space-y-2">
            {data.not_measured.map((item, index) => (
              <li key={index}>
                <p className="text-sm font-medium text-slate-800 dark:text-slate-100">
                  {item.service}
                </p>
                <p className="text-xs text-slate-500 dark:text-slate-400">
                  {item.reason}
                </p>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}
    </>
  )
}
