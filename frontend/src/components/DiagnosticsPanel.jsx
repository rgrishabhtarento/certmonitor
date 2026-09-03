import { useState } from 'react'
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  CircleSlash,
  Globe,
  Info,
  Lock,
  Network,
  ShieldCheck,
  XCircle,
} from 'lucide-react'
import clsx from 'clsx'

import { Modal, Spinner } from './ui'
import { formatDateTime, formatNumber, formatRelative } from '../lib/format'

/**
 * Failure triage for one endpoint.
 *
 * The layer chain is the point of the whole panel: a request passes through
 * DNS -> TCP -> TLS -> HTTP, and showing where it stopped localises the fault
 * before anyone reads a word of prose.
 *
 * Status is never carried by colour alone - every stage and finding shows an
 * icon and a written state, because the red/green pair here is exactly the one
 * that collapses under deuteranopia.
 */

const LAYER_META = {
  dns: { label: 'DNS', icon: Globe, blurb: 'Name resolves to an address' },
  tcp: { label: 'TCP', icon: Network, blurb: 'Port accepts a connection' },
  tls: { label: 'TLS', icon: Lock, blurb: 'Certificate handshake' },
  http: { label: 'HTTP', icon: ShieldCheck, blurb: 'Application responds' },
}

const STAGE_STYLE = {
  ok: {
    ring: 'border-green-300 bg-green-50 dark:border-green-800 dark:bg-green-950/40',
    text: 'text-green-800 dark:text-green-300',
    label: 'OK',
  },
  warning: {
    ring: 'border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/40',
    text: 'text-amber-800 dark:text-amber-300',
    label: 'Partial',
  },
  failed: {
    ring: 'border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950/40',
    text: 'text-red-800 dark:text-red-300',
    label: 'Failed',
  },
  skipped: {
    ring: 'border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900',
    text: 'text-slate-500 dark:text-slate-400',
    label: 'Skipped',
  },
}

const SEVERITY_STYLE = {
  high: {
    box: 'border-red-200 bg-red-50 dark:border-red-900 dark:bg-red-950/40',
    chip: 'bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-200',
    icon: XCircle,
  },
  medium: {
    box: 'border-amber-200 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/40',
    chip: 'bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200',
    icon: AlertTriangle,
  },
  low: {
    box: 'border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900',
    chip: 'bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200',
    icon: Info,
  },
}

function Stage({ layer }) {
  const meta = LAYER_META[layer.layer] || { label: layer.layer, icon: CircleSlash }
  const style = STAGE_STYLE[layer.status] || STAGE_STYLE.skipped
  const Icon = meta.icon

  return (
    <div className={clsx('flex-1 rounded-lg border p-3', style.ring)}>
      <div className="flex items-center gap-2">
        <Icon size={15} className={style.text} aria-hidden="true" />
        <span className="text-sm font-semibold text-slate-800 dark:text-slate-100">
          {meta.label}
        </span>
        <span className={clsx('ml-auto text-[11px] font-semibold uppercase', style.text)}>
          {style.label}
        </span>
      </div>
      <p className="mt-1 text-xs text-slate-600 dark:text-slate-300">{layer.detail}</p>
      {layer.elapsed_ms != null ? (
        <p className="tnum mt-0.5 text-[11px] text-slate-400">{layer.elapsed_ms} ms</p>
      ) : null}
    </div>
  )
}

function Section({ title, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-700">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span className="text-sm font-medium text-slate-800 dark:text-slate-200">
          {title}
        </span>
      </button>
      {open ? (
        <div className="border-t border-slate-200 px-3 py-3 dark:border-slate-700">
          {children}
        </div>
      ) : null}
    </div>
  )
}

function KeyValues({ rows }) {
  const entries = Object.entries(rows || {}).filter(
    ([, v]) => v !== null && v !== undefined && v !== '' &&
      !(Array.isArray(v) && v.length === 0) &&
      !(typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 0),
  )
  if (!entries.length) {
    return <p className="text-xs text-slate-400">Nothing recorded.</p>
  }
  return (
    <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
      {entries.map(([key, value]) => (
        <div key={key} className="flex gap-2 text-xs">
          <dt className="shrink-0 text-slate-500 dark:text-slate-400">
            {key.replace(/_/g, ' ')}
          </dt>
          <dd className="min-w-0 break-words font-mono text-slate-700 dark:text-slate-200">
            {Array.isArray(value)
              ? value.join(', ')
              : typeof value === 'object'
                ? JSON.stringify(value)
                : String(value)}
          </dd>
        </div>
      ))}
    </dl>
  )
}

export default function DiagnosticsPanel({ open, onClose, report, loading, error }) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={report ? `Diagnostics — ${report.endpoint_name}` : 'Diagnostics'}
      size="lg"
      footer={
        <button type="button" className="btn-secondary" onClick={onClose}>
          Close
        </button>
      }
    >
      {loading ? (
        <div className="flex flex-col items-center gap-3 py-10">
          <Spinner size={24} />
          <p className="text-sm text-slate-500">
            Probing DNS, TCP, TLS and HTTP…
          </p>
          <p className="text-xs text-slate-400">
            Runs live requests against the endpoint. Nothing is written to its history.
          </p>
        </div>
      ) : error ? (
        <div
          role="alert"
          className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/50 dark:text-red-200"
        >
          {error}
        </div>
      ) : report ? (
        <div className="space-y-4">
          {/* ------------------------------------------------- verdict */}
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 dark:border-slate-700 dark:bg-slate-800/60">
            <div className="flex flex-wrap items-center gap-2">
              <span className="badge bg-slate-200 font-mono text-[11px] text-slate-700 dark:bg-slate-700 dark:text-slate-200">
                {report.verdict}
              </span>
              {report.deepest_layer_ok ? (
                <span className="text-xs text-slate-500 dark:text-slate-400">
                  deepest layer reached:{' '}
                  <span className="font-semibold uppercase">
                    {report.deepest_layer_ok}
                  </span>
                </span>
              ) : null}
              <span className="tnum ml-auto text-[11px] text-slate-400">
                {report.elapsed_ms} ms · {formatRelative(report.generated_at)}
              </span>
            </div>
            <p className="mt-2 text-sm text-slate-800 dark:text-slate-100">
              {report.summary}
            </p>
          </div>

          {/* -------------------------------------------- layer chain */}
          <div>
            <p className="label">Request path</p>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-stretch">
              {report.layers.map((layer, index) => (
                <div key={layer.layer} className="flex flex-1 items-stretch gap-2">
                  <Stage layer={layer} />
                  {index < report.layers.length - 1 ? (
                    <span
                      className="hidden self-center text-slate-300 sm:block dark:text-slate-600"
                      aria-hidden="true"
                    >
                      ›
                    </span>
                  ) : null}
                </div>
              ))}
            </div>
          </div>

          {/* ----------------------------------------------- findings */}
          {report.findings?.length ? (
            <div>
              <p className="label">What to do</p>
              <div className="space-y-2">
                {report.findings.map((finding, index) => {
                  const style = SEVERITY_STYLE[finding.severity] || SEVERITY_STYLE.low
                  const Icon = style.icon
                  return (
                    <div
                      key={index}
                      className={clsx('rounded-lg border p-3', style.box)}
                    >
                      <div className="flex items-start gap-2">
                        <Icon size={15} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <p className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                              {finding.title}
                            </p>
                            <span
                              className={clsx(
                                'badge text-[10px] uppercase',
                                style.chip,
                              )}
                            >
                              {finding.severity}
                            </span>
                          </div>
                          <p className="mt-1 text-xs text-slate-700 dark:text-slate-300">
                            {finding.detail}
                          </p>
                          <p className="mt-1.5 break-words text-xs font-medium text-slate-900 dark:text-slate-100">
                            → {finding.action}
                          </p>
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          ) : (
            <p className="rounded-lg bg-green-50 px-3 py-2 text-sm text-green-800 dark:bg-green-950/40 dark:text-green-200">
              No problems found. Every layer responded as configured.
            </p>
          )}

          {/* ----------------------------------------------- evidence */}
          <div className="space-y-2">
            {report.layers.map((layer) =>
              Object.keys(layer.data || {}).length ? (
                <Section key={layer.layer} title={`${LAYER_META[layer.layer]?.label || layer.layer} details`}>
                  <KeyValues rows={layer.data} />
                </Section>
              ) : null,
            )}

            {Object.keys(report.comparisons || {}).length ? (
              <Section title="Comparison probes" defaultOpen>
                <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">
                  Extra requests run to narrow the cause — the site root, and the
                  same request with certificate verification disabled.
                </p>
                {Object.entries(report.comparisons).map(([name, value]) => (
                  <div key={name} className="mb-2">
                    <p className="text-xs font-semibold capitalize text-slate-700 dark:text-slate-200">
                      {name}
                    </p>
                    <KeyValues rows={value} />
                  </div>
                ))}
              </Section>
            ) : null}

            <Section title="Recent history">
              <KeyValues
                rows={{
                  window: `${report.history.window_hours}h`,
                  checks_analysed: formatNumber(report.history.checks_analysed),
                  total_checks_recorded: formatNumber(
                    report.history.total_checks_recorded,
                  ),
                  ever_succeeded: report.history.ever_succeeded ? 'yes' : 'NO',
                  last_success_at: report.history.last_success_at
                    ? formatDateTime(report.history.last_success_at)
                    : 'never',
                  state_transitions: report.history.state_transitions,
                  avg_response_time_ms: report.history.avg_response_time_ms,
                  failure_reasons: report.history.failure_reasons,
                  http_status_codes: report.history.http_status_codes,
                  resolved_ips: report.history.resolved_ips,
                }}
              />
            </Section>

            <Section title="What else is failing">
              <KeyValues
                rows={{
                  fleet: `${report.correlation.fleet_down} of ${report.correlation.fleet_total} endpoints down`,
                  environment: `${report.correlation.environment_down} of ${report.correlation.environment_total} down in this environment`,
                  same_hostname_down: report.correlation.same_hostname_down,
                }}
              />
              {report.correlation.same_hostname?.length ? (
                <div className="mt-2">
                  <p className="mb-1 text-xs font-semibold text-slate-700 dark:text-slate-200">
                    Other endpoints on this hostname
                  </p>
                  <ul className="space-y-0.5">
                    {report.correlation.same_hostname.map((sibling) => (
                      <li key={sibling.url} className="text-xs">
                        <span
                          className={
                            sibling.status === 'down'
                              ? 'font-semibold text-red-600 dark:text-red-400'
                              : 'text-slate-600 dark:text-slate-300'
                          }
                        >
                          {sibling.status}
                        </span>{' '}
                        — {sibling.name}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </Section>
          </div>
        </div>
      ) : null}
    </Modal>
  )
}
