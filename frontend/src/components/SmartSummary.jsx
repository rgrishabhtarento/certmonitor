import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Activity,
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  ClipboardList,
  Rocket,
  ShieldCheck,
  Sparkles,
  TrendingUp,
} from 'lucide-react'
import clsx from 'clsx'

import { PRIORITY_STYLE } from './rca'
import { Spinner } from './ui'
import LiveIndicator from './LiveIndicator'
import { intelligenceApi } from '../lib/api'
import { SLOW_INTERVAL, useAutoRefresh } from '../hooks/useAutoRefresh'

/**
 * Smart DevOps summary: what needs attention, right now.
 *
 * Every figure comes from a count of real rows on this server. Nothing here
 * is sent anywhere, and nothing is inferred beyond what is labelled as a
 * correlation.
 *
 * The health score always ships with its components and its plain-English
 * reasons, because a score with no reasons is a number nobody can act on.
 */

function scoreTone(score) {
  if (score >= 95) return 'text-green-600 dark:text-green-400'
  if (score >= 85) return 'text-amber-600 dark:text-amber-400'
  return 'text-red-600 dark:text-red-400'
}

function Tile({ icon: Icon, label, value, tone, to }) {
  const body = (
    <>
      <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-slate-100 dark:bg-slate-800">
        <Icon size={17} className={tone} />
      </span>
      <span className="min-w-0">
        <span className="block truncate text-xs font-medium text-slate-500 dark:text-slate-400">
          {label}
        </span>
        <span className={clsx('block text-xl font-semibold leading-tight', tone)}>
          {value}
        </span>
      </span>
    </>
  )
  const className =
    'card flex items-center gap-3 p-3 text-left transition-shadow hover:shadow-md'
  return to ? (
    <Link to={to} className={className}>
      {body}
    </Link>
  ) : (
    <div className={className}>{body}</div>
  )
}

export default function SmartSummary() {
  const [summary, setSummary] = useState(null)
  const [daily, setDaily] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [showDaily, setShowDaily] = useState(false)

  const load = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true)
    setError(null)
    try {
      const [smart, day] = await Promise.all([
        intelligenceApi.summary(),
        intelligenceApi.daily(24),
      ])
      setSummary(smart)
      setDaily(day)
    } catch (err) {
      setError(err.message)
      throw err
    } finally {
      setLoading(false)
    }
  }, [])

  // The heavier of the two aggregate queries, so this polls on the slow
  // cadence rather than the conversational one.
  const { refreshing, lastRefreshedAt, refreshNow } = useAutoRefresh(
    () => load({ silent: true }),
    { interval: SLOW_INTERVAL },
  )

  useEffect(() => {
    load().catch(() => {})
  }, [load])

  if (loading && !summary) {
    return (
      <div className="card mb-4 grid place-items-center p-8">
        <Spinner size={22} />
      </div>
    )
  }
  if (error && !summary) {
    return (
      <div className="card mb-4 p-4 text-sm text-red-700 dark:text-red-300">
        Smart summary unavailable: {error}
      </div>
    )
  }
  if (!summary) return null

  // Before the first poll lands, fall back to when the server generated the
  // payload - so the counter is honest rather than blank.
  const generatedAt = summary.generated_at ? new Date(summary.generated_at) : null

  return (
    <section className="mb-5" aria-label="Smart DevOps summary">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="flex items-center gap-1.5 text-base font-semibold text-slate-900 dark:text-slate-50">
          <Sparkles size={16} className="text-brand-600 dark:text-brand-400" />
          Smart DevOps Summary
        </h2>
        <span className="text-xs text-slate-400">computed locally</span>
        <LiveIndicator
          className="ml-auto"
          refreshing={refreshing}
          lastRefreshedAt={lastRefreshedAt || generatedAt}
          onRefresh={refreshNow}
          showToggle
        />
      </div>

      {/* ------------------------------------------------- health score */}
      <div className="card mb-3 p-4">
        <div className="flex flex-wrap items-start gap-4">
          <div>
            <p className="text-xs font-medium text-slate-500 dark:text-slate-400">
              Overall infrastructure health
            </p>
            <p className={clsx('text-4xl font-bold leading-none', scoreTone(summary.health_score))}>
              {summary.health_score}
              <span className="text-lg font-medium text-slate-400">/100</span>
            </p>
          </div>
          <div className="min-w-0 flex-1">
            <p className="mb-1 text-xs font-medium text-slate-500 dark:text-slate-400">
              Why this score
            </p>
            <ul className="mb-2 space-y-0.5">
              {summary.health_reasons.map((reason, index) => (
                <li key={index} className="text-sm text-slate-700 dark:text-slate-200">
                  • {reason}
                </li>
              ))}
            </ul>
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {Object.entries(summary.health_components).map(([key, value]) => (
                <span key={key} className="text-xs text-slate-500 dark:text-slate-400">
                  <span className="capitalize">{key}</span>{' '}
                  <span className="tnum font-medium text-slate-700 dark:text-slate-200">
                    {Math.round(value)}
                  </span>
                </span>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* ------------------------------------------------------- tiles */}
      <div className="mb-3 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Tile
          icon={AlertTriangle}
          label="Critical issues"
          value={summary.critical_production_down}
          tone={
            summary.critical_production_down > 0
              ? 'text-red-600 dark:text-red-400'
              : 'text-slate-900 dark:text-slate-50'
          }
          to="/endpoints?status=down"
        />
        <Tile
          icon={Activity}
          label="Degraded"
          value={summary.degraded}
          tone={
            summary.degraded > 0
              ? 'text-amber-600 dark:text-amber-400'
              : 'text-slate-900 dark:text-slate-50'
          }
          to="/endpoints?status=degraded"
        />
        <Tile
          icon={ShieldCheck}
          label="SSL attention"
          value={summary.ssl_attention}
          tone={
            summary.ssl_attention > 0
              ? 'text-amber-600 dark:text-amber-400'
              : 'text-slate-900 dark:text-slate-50'
          }
          to="/ssl"
        />
        <Tile
          icon={Rocket}
          label="Deployments 24h"
          value={summary.recent_deployments}
          tone="text-indigo-600 dark:text-indigo-400"
          to="/changes"
        />
        <Tile
          icon={TrendingUp}
          label="Performance anomalies"
          value={summary.performance_anomalies.length}
          tone={
            summary.performance_anomalies.length > 0
              ? 'text-amber-600 dark:text-amber-400'
              : 'text-slate-900 dark:text-slate-50'
          }
        />
        <Tile
          icon={ClipboardList}
          label="RCA pending"
          value={summary.rca_pending}
          tone={
            summary.rca_pending > 0
              ? 'text-blue-600 dark:text-blue-400'
              : 'text-slate-900 dark:text-slate-50'
          }
          to="/rca"
        />
      </div>

      {/* --------------------------------- deployment/incident correlation */}
      {summary.deployment_incident_correlations.length ? (
        <div className="card mb-3 border-amber-200 bg-amber-50 p-3 dark:border-amber-900 dark:bg-amber-950/30">
          <p className="mb-1 flex items-center gap-1.5 text-sm font-semibold text-amber-900 dark:text-amber-200">
            <AlertTriangle size={15} />
            {summary.deployment_incident_correlations.length} incident
            {summary.deployment_incident_correlations.length === 1 ? '' : 's'} began
            shortly after a deployment
          </p>
          <ul className="space-y-0.5">
            {summary.deployment_incident_correlations.slice(0, 4).map((item) => (
              <li key={item.incident_id} className="text-sm text-slate-700 dark:text-slate-200">
                <Link
                  to={`/changes/${item.change_id}`}
                  className="font-medium text-brand-700 hover:underline dark:text-brand-300"
                >
                  {item.change_reference}
                </Link>{' '}
                completed {Math.round(item.minutes_before_incident)} min before{' '}
                <Link
                  to={`/endpoints/${item.endpoint_id}`}
                  className="font-medium text-brand-700 hover:underline dark:text-brand-300"
                >
                  {item.endpoint_name}
                </Link>{' '}
                went down
              </li>
            ))}
          </ul>
          <p className="mt-1.5 text-xs text-amber-900/80 dark:text-amber-200/80">
            Correlation in time only. It does not establish that the deployment
            caused the failure.
          </p>
        </div>
      ) : null}

      {/* --------------------------------------------- attention list */}
      {summary.attention.length ? (
        <div className="card mb-3">
          <div className="card-header">
            <h3 className="card-title">What needs my attention</h3>
            <span className="text-xs text-slate-400">
              prioritised by environment, failure kind and impact
            </span>
          </div>
          <ol className="divide-y divide-slate-100 dark:divide-slate-800">
            {summary.attention.map((item, index) => {
              const style = PRIORITY_STYLE[item.priority] || PRIORITY_STYLE.low
              return (
                <li key={index} className="flex items-start gap-2.5 px-4 py-2.5">
                  <span
                    className={clsx('mt-1.5 h-2 w-2 shrink-0 rounded-full', style.dot)}
                    aria-hidden="true"
                  />
                  <div className="min-w-0 flex-1">
                    <p className="flex flex-wrap items-center gap-2">
                      {item.endpoint_id ? (
                        <Link
                          to={`/endpoints/${item.endpoint_id}`}
                          className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                        >
                          {item.title}
                        </Link>
                      ) : (
                        <span className="font-medium">{item.title}</span>
                      )}
                      <span className={clsx('badge text-[10px]', style.chip)}>
                        {style.label}
                      </span>
                      {item.application ? (
                        <span className="chip">{item.application}</span>
                      ) : null}
                    </p>
                    <p className="text-sm text-slate-600 dark:text-slate-300">
                      {item.detail}
                    </p>
                  </div>
                </li>
              )
            })}
          </ol>
        </div>
      ) : (
        <div className="card mb-3 p-4 text-sm text-slate-600 dark:text-slate-300">
          Nothing needs attention right now — no failing endpoints, no expiring
          certificates, no open incidents.
        </div>
      )}

      {/* --------------------------------------------- daily summary */}
      {daily ? (
        <div className="card">
          <button
            type="button"
            className="flex w-full items-center gap-2 px-4 py-2.5 text-left"
            onClick={() => setShowDaily((value) => !value)}
            aria-expanded={showDaily}
          >
            {showDaily ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
            <span className="text-sm font-semibold text-slate-800 dark:text-slate-100">
              Daily operations summary
            </span>
            <span className="ml-auto text-xs text-slate-400">
              last {daily.window_hours}h
            </span>
          </button>
          {showDaily ? (
            <div className="border-t border-slate-100 px-4 py-3 dark:border-slate-800">
              <dl className="mb-3 grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-4">
                {[
                  ['Endpoints monitored', daily.endpoints_monitored],
                  ['Healthy throughout', daily.endpoints_healthy_throughout],
                  ['Incidents', daily.incidents],
                  ['Resolved', daily.incidents_resolved],
                  ['Deployments', daily.deployments],
                  ['Failed deployments', daily.deployments_failed],
                  ['SSL issues', daily.ssl_issues],
                  ['RCA pending', daily.rca_pending],
                ].map(([label, value]) => (
                  <div key={label}>
                    <dt className="text-xs text-slate-500 dark:text-slate-400">
                      {label}
                    </dt>
                    <dd className="tnum text-lg font-semibold text-slate-900 dark:text-slate-50">
                      {value}
                    </dd>
                  </div>
                ))}
              </dl>

              {daily.findings.length ? (
                <div className="mb-2">
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                    Findings
                  </p>
                  <ul className="mt-0.5 space-y-0.5">
                    {daily.findings.map((finding, index) => (
                      <li key={index} className="text-sm text-slate-700 dark:text-slate-200">
                        • {finding}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}

              {daily.recommendations.length ? (
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                    Recommended
                  </p>
                  <ol className="mt-0.5 space-y-0.5">
                    {daily.recommendations.map((item, index) => (
                      <li key={index} className="text-sm text-slate-700 dark:text-slate-200">
                        {index + 1}. {item}
                      </li>
                    ))}
                  </ol>
                </div>
              ) : null}

              {!daily.findings.length && !daily.recommendations.length ? (
                <p className="text-sm text-slate-500 dark:text-slate-400">
                  A quiet day — nothing stood out in the monitoring data.
                </p>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
