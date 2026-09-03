import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  CircleSlash,
  ClipboardCheck,
  Copy,
  Globe,
  HelpCircle,
  Info,
  Lock,
  Network,
  RefreshCw,
  Rocket,
  ShieldAlert,
  ShieldCheck,
  Terminal,
  TrendingUp,
  XCircle,
} from 'lucide-react'
import clsx from 'clsx'

import { Modal, Spinner } from './ui'
import { formatDateTime, formatNumber, formatRelative } from '../lib/format'

/**
 * Diagnose: structured troubleshooting for one endpoint.
 *
 * Ordered the way an engineer actually works, not the way the data arrives:
 * what is wrong, how sure we are, the evidence, what changed, what to do,
 * and how to tell whether it worked.
 *
 * Two rules the layout enforces:
 *
 * Status is never carried by colour alone. Every stage, verdict and finding
 * shows an icon and a written state, because red/green is precisely the pair
 * that collapses under deuteranopia.
 *
 * What the platform cannot see is shown, not hidden. The "Not observable"
 * block is as prominent as the evidence, so nobody reads silence about
 * container state as a clean bill of health.
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
    label: 'Not reached',
  },
}

const SEVERITY_STYLE = {
  critical: {
    chip: 'bg-red-600 text-white',
    bar: 'border-l-red-600',
    label: 'CRITICAL',
  },
  high: {
    chip: 'bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-200',
    bar: 'border-l-red-500',
    label: 'HIGH',
  },
  medium: {
    chip: 'bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200',
    bar: 'border-l-amber-500',
    label: 'MEDIUM',
  },
  low: {
    chip: 'bg-blue-100 text-blue-800 dark:bg-blue-900/50 dark:text-blue-200',
    bar: 'border-l-blue-500',
    label: 'LOW',
  },
  info: {
    chip: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
    bar: 'border-l-slate-400',
    label: 'INFO',
  },
}

const CONFIDENCE_COPY = {
  high: {
    label: 'High confidence',
    why: 'Several independent signals point the same way.',
  },
  medium: {
    label: 'Medium confidence',
    why: 'The evidence supports this, but not decisively.',
  },
  low: {
    label: 'Low confidence',
    why: 'Thin evidence. Treat this as a lead, not a conclusion.',
  },
}

const BAND_COPY = {
  most_likely: 'Most likely',
  possible: 'Possible',
  less_likely: 'Less likely',
}

const RISK_STYLE = {
  safe: {
    chip: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300',
    label: 'Safe',
    note: 'Read-only. Changes nothing.',
  },
  disruptive: {
    chip: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
    label: 'Disruptive',
    note: 'Briefly interrupts service.',
  },
  high_risk: {
    chip: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
    label: 'High risk',
    note: 'Can cause an outage or lose data. Never run without deciding to.',
  },
}

const EVIDENCE_KIND = {
  observed: { label: 'Observed', style: 'bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200' },
  inferred: { label: 'Inferred', style: 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-300' },
  unknown: { label: 'Not checked', style: 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400' },
}

const FOCUS_OPTIONS = [
  { value: 'auto', label: 'Auto' },
  { value: 'endpoint', label: 'Endpoint' },
  { value: 'ssl', label: 'SSL' },
  { value: 'availability', label: 'Availability' },
  { value: 'performance', label: 'Performance' },
  { value: 'recent_failure', label: 'Recent failure' },
  { value: 'deployment_impact', label: 'Deployment impact' },
]

function StatusIcon({ status, size = 15 }) {
  if (status === 'ok') return <Check size={size} aria-hidden="true" />
  if (status === 'warning') return <AlertTriangle size={size} aria-hidden="true" />
  if (status === 'failed') return <XCircle size={size} aria-hidden="true" />
  return <CircleSlash size={size} aria-hidden="true" />
}

function Section({ title, icon: Icon, children, subtitle, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <section className="rounded-lg border border-slate-200 dark:border-slate-700">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        {Icon ? <Icon size={15} className="text-slate-500" /> : null}
        <span className="text-sm font-semibold text-slate-800 dark:text-slate-100">
          {title}
        </span>
        {subtitle ? (
          <span className="ml-auto truncate pl-3 text-xs text-slate-400">
            {subtitle}
          </span>
        ) : null}
      </button>
      {open ? (
        <div className="border-t border-slate-200 px-3 py-3 dark:border-slate-700">
          {children}
        </div>
      ) : null}
    </section>
  )
}

function CommandLine({ command, note, risk = 'safe' }) {
  const [copied, setCopied] = useState(false)
  const style = RISK_STYLE[risk] || RISK_STYLE.safe

  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-700">
      <div className="flex items-start gap-2 bg-slate-900 px-2.5 py-2 dark:bg-black">
        <code className="min-w-0 flex-1 overflow-x-auto whitespace-pre-wrap break-all font-mono text-[11.5px] leading-relaxed text-slate-100">
          {command}
        </code>
        <button
          type="button"
          className="shrink-0 rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100"
          title="Copy"
          aria-label={`Copy command: ${command}`}
          onClick={() => {
            navigator.clipboard?.writeText(command)
            setCopied(true)
            setTimeout(() => setCopied(false), 1500)
          }}
        >
          {copied ? <ClipboardCheck size={14} /> : <Copy size={14} />}
        </button>
      </div>
      {note ? (
        <p className="px-2.5 py-1.5 text-xs text-slate-600 dark:text-slate-300">
          <span className={clsx('badge mr-1.5 text-[10px]', style.chip)}>
            {style.label}
          </span>
          {note}
        </p>
      ) : null}
    </div>
  )
}

/** The ✓/✗ strip: the fastest way to see intermittent failure. */
function AvailabilityStrip({ checks }) {
  if (!checks?.length) return null
  return (
    <div className="flex flex-wrap gap-[3px]" role="img"
      aria-label={`Last ${checks.length} checks, oldest first`}>
      {checks.map((check, index) => {
        const down = check.status === 'down'
        const degraded = check.status === 'degraded'
        return (
          <span
            key={index}
            title={`${check.status.toUpperCase()} · ${formatDateTime(check.at, 'dd MMM HH:mm')}${
              check.code ? ` · HTTP ${check.code}` : ''
            }${check.ms ? ` · ${Math.round(check.ms)} ms` : ''}`}
            className={clsx(
              'grid h-4 w-4 place-items-center rounded-[3px] text-[9px] font-bold text-white',
              down
                ? 'bg-red-500'
                : degraded
                  ? 'bg-amber-500'
                  : 'bg-green-500',
            )}
          >
            {down ? '✕' : degraded ? '!' : '✓'}
          </span>
        )
      })}
    </div>
  )
}

export default function DiagnosticsPanel({
  open,
  onClose,
  report,
  previous,
  loading,
  error,
  onRerun,
}) {
  const [focus, setFocus] = useState('auto')
  const [showLayerData, setShowLayerData] = useState(false)

  useEffect(() => {
    if (open) setShowLayerData(false)
  }, [open])

  const severity = SEVERITY_STYLE[report?.severity] || SEVERITY_STYLE.info
  const confidence = CONFIDENCE_COPY[report?.confidence] || CONFIDENCE_COPY.low

  // Before/after comparison after a re-run: the point of a verification loop
  // is seeing the delta, not re-reading an absolute state.
  const delta = useMemo(() => {
    if (!report || !previous) return null
    const before = previous
    const changed =
      before.verdict !== report.verdict ||
      before.severity !== report.severity ||
      before.deepest_layer_ok !== report.deepest_layer_ok
    return { before, changed }
  }, [report, previous])

  const httpLayer = report?.layers?.find((layer) => layer.layer === 'http')
  const resolved =
    report && ['healthy', 'recovered_since_last_check'].includes(report.verdict)

  return (
    <Modal open={open} onClose={onClose} title="Diagnose" size="xl">
      {loading ? (
        <div className="flex flex-col items-center gap-3 py-12 text-center">
          <Spinner size={26} />
          <div>
            <p className="text-sm font-medium text-slate-700 dark:text-slate-200">
              Investigating…
            </p>
            <p className="mt-1 text-xs text-slate-500">
              Probing DNS, TCP per address, TLS and HTTP, then correlating with
              history, incidents and recent deployments.
            </p>
          </div>
        </div>
      ) : error ? (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2.5 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/50 dark:text-red-200"
        >
          <XCircle size={16} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      ) : !report ? null : (
        <div className="space-y-3">
          {/* ============================================ 1. THE VERDICT */}
          <div className={clsx('rounded-lg border border-l-4 p-3.5', severity.bar,
            'border-slate-200 dark:border-slate-700')}>
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <span className={clsx('badge', severity.chip)}>
                <ShieldAlert size={12} /> {severity.label}
              </span>
              <span className="badge bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300">
                {confidence.label}
              </span>
              {report.deepest_layer_ok ? (
                <span className="badge bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300">
                  Reached {LAYER_META[report.deepest_layer_ok]?.label} OK
                </span>
              ) : null}
              <span className="ml-auto text-xs text-slate-400">
                {formatNumber(report.elapsed_ms)} ms · {formatRelative(report.generated_at)}
              </span>
            </div>

            <p className="text-sm font-medium text-slate-900 dark:text-slate-50">
              {report.summary}
            </p>

            {report.root_cause ? (
              <div className="mt-2.5 rounded-lg bg-slate-50 px-3 py-2 dark:bg-slate-800/60">
                <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  Most likely cause
                </p>
                <p className="mt-0.5 text-sm text-slate-800 dark:text-slate-100">
                  {report.root_cause}
                </p>
                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                  {confidence.why}
                </p>
              </div>
            ) : null}

            {report.failure_started_at ? (
              <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                Failing since {formatDateTime(report.failure_started_at)} (
                {formatRelative(report.failure_started_at)})
              </p>
            ) : null}
          </div>

          {/* ------------------------------- re-run before/after comparison */}
          {delta ? (
            <div
              className={clsx(
                'rounded-lg border p-3 text-sm',
                resolved
                  ? 'border-green-300 bg-green-50 dark:border-green-800 dark:bg-green-950/30'
                  : 'border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-800/50',
              )}
            >
              <p className="mb-1.5 font-semibold text-slate-800 dark:text-slate-100">
                {resolved
                  ? 'Compared with the previous diagnosis — this looks resolved'
                  : 'Compared with the previous diagnosis'}
              </p>
              <div className="grid gap-2 sm:grid-cols-2">
                <div>
                  <p className="text-[11px] font-semibold uppercase text-slate-500">Before</p>
                  <p className="text-slate-700 dark:text-slate-200">
                    {delta.before.summary}
                  </p>
                </div>
                <div>
                  <p className="text-[11px] font-semibold uppercase text-slate-500">Now</p>
                  <p className="text-slate-700 dark:text-slate-200">{report.summary}</p>
                </div>
              </div>
              {!delta.changed ? (
                <p className="mt-2 text-xs text-slate-600 dark:text-slate-300">
                  Nothing changed between the two runs.
                </p>
              ) : null}
            </div>
          ) : null}

          {/* ================================== 2. RANKED PROBABLE CAUSES */}
          {report.candidates?.length ? (
            <Section
              title="Probable causes"
              icon={HelpCircle}
              subtitle={`${report.candidates.length} considered`}
            >
              <ol className="space-y-2.5">
                {report.candidates.map((candidate, index) => (
                  <li
                    key={candidate.cause}
                    className={clsx(
                      'rounded-lg border p-2.5',
                      index === 0
                        ? 'border-slate-300 bg-slate-50 dark:border-slate-600 dark:bg-slate-800/60'
                        : 'border-slate-200 dark:border-slate-700',
                    )}
                  >
                    <div className="mb-1 flex flex-wrap items-center gap-2">
                      <span
                        className={clsx(
                          'badge',
                          index === 0
                            ? 'bg-slate-800 text-white dark:bg-slate-200 dark:text-slate-900'
                            : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300',
                        )}
                      >
                        {BAND_COPY[candidate.band] || candidate.band}
                      </span>
                      <span className="text-sm font-semibold text-slate-900 dark:text-slate-50">
                        {candidate.label}
                      </span>
                      <span
                        className="ml-auto tnum text-xs text-slate-400"
                        title="Share of the total evidence weight behind all candidates. A weighting, not a probability."
                      >
                        {Math.round(candidate.share * 100)}% of evidence
                      </span>
                    </div>
                    <p className="text-sm text-slate-700 dark:text-slate-200">
                      {candidate.explanation}
                    </p>
                    {candidate.why?.length ? (
                      <ul className="mt-1.5 space-y-0.5">
                        {candidate.why.map((reason, i) => (
                          <li
                            key={i}
                            className="flex gap-1.5 text-xs text-slate-600 dark:text-slate-300"
                          >
                            <span className="text-slate-400">•</span>
                            <span>{reason}</span>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </li>
                ))}
              </ol>
            </Section>
          ) : null}

          {/* ============================================== 3. THE EVIDENCE */}
          <Section title="Evidence" icon={ClipboardCheck}>
            {/* layer chain */}
            <div className="mb-3 grid gap-1.5 sm:grid-cols-2 lg:grid-cols-4">
              {report.layers?.map((layer) => {
                const meta = LAYER_META[layer.layer] || {}
                const style = STAGE_STYLE[layer.status] || STAGE_STYLE.skipped
                const Icon = meta.icon || Info
                return (
                  <div
                    key={layer.layer}
                    className={clsx('rounded-lg border p-2', style.ring)}
                  >
                    <div className={clsx('flex items-center gap-1.5 text-xs font-semibold', style.text)}>
                      <Icon size={13} aria-hidden="true" />
                      {meta.label || layer.layer}
                      <span className="ml-auto flex items-center gap-1">
                        <StatusIcon status={layer.status} size={13} />
                        {style.label}
                      </span>
                    </div>
                    <p className="mt-1 break-words text-[11px] text-slate-600 dark:text-slate-300">
                      {layer.detail}
                    </p>
                    {layer.elapsed_ms != null ? (
                      <p className="tnum mt-0.5 text-[11px] text-slate-400">
                        {formatNumber(layer.elapsed_ms)} ms
                      </p>
                    ) : null}
                  </div>
                )
              })}
            </div>

            {/* itemised evidence */}
            <ul className="divide-y divide-slate-100 dark:divide-slate-800">
              {report.evidence?.map((item, index) => (
                <li key={index} className="flex flex-wrap items-start gap-x-2 gap-y-1 py-1.5">
                  <span className="w-44 shrink-0 text-xs font-medium text-slate-500 dark:text-slate-400">
                    {item.label}
                  </span>
                  <span className="flex items-center gap-1.5 text-sm text-slate-800 dark:text-slate-100">
                    {item.status ? (
                      <span
                        className={clsx(
                          item.status === 'ok'
                            ? 'text-green-600 dark:text-green-400'
                            : item.status === 'warning'
                              ? 'text-amber-600 dark:text-amber-400'
                              : item.status === 'failed'
                                ? 'text-red-600 dark:text-red-400'
                                : 'text-slate-400',
                        )}
                      >
                        <StatusIcon status={item.status} size={13} />
                      </span>
                    ) : null}
                    {item.value}
                  </span>
                  {item.detail ? (
                    <span className="w-full pl-44 text-xs text-slate-500 dark:text-slate-400">
                      {item.detail}
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>

            {/* availability strip */}
            {report.history?.recent_checks?.length ? (
              <div className="mt-3">
                <p className="mb-1 text-xs font-medium text-slate-500 dark:text-slate-400">
                  Last {report.history.recent_checks.length} checks (oldest first) —{' '}
                  {report.history.recent_availability_pct}% passed
                </p>
                <AvailabilityStrip checks={report.history.recent_checks} />
              </div>
            ) : null}

            <button
              type="button"
              className="mt-3 text-xs text-brand-600 hover:underline dark:text-brand-400"
              onClick={() => setShowLayerData((value) => !value)}
            >
              {showLayerData ? 'Hide' : 'Show'} raw probe data
            </button>
            {showLayerData ? (
              <pre className="mt-2 max-h-72 overflow-auto rounded-lg bg-slate-900 p-2.5 font-mono text-[11px] leading-relaxed text-slate-100">
                {JSON.stringify(
                  { layers: report.layers, comparisons: report.comparisons },
                  null,
                  2,
                )}
              </pre>
            ) : null}
          </Section>

          {/* ============================== 4. WHAT CHANGED / CORRELATIONS */}
          {report.changes?.active_deployment ||
          report.changes?.closest ||
          report.incidents?.open_incident ||
          report.recurrence?.most_common_verdict_count >= 2 ? (
            <Section title="What changed" icon={Rocket}>
              {report.changes?.active_deployment ? (
                <div className="mb-2 rounded-lg border border-indigo-200 bg-indigo-50 p-2.5 text-sm dark:border-indigo-900 dark:bg-indigo-950/30">
                  <p className="font-semibold text-indigo-900 dark:text-indigo-200">
                    Deployment in progress — {report.changes.active_deployment.reference}
                  </p>
                  <p className="text-slate-700 dark:text-slate-200">
                    {report.changes.active_deployment.application} deployed by{' '}
                    {report.changes.active_deployment.deployer_name}, started{' '}
                    {formatRelative(report.changes.active_deployment.started_at)}.
                    Monitoring is paused, so the current state of this endpoint
                    is expected and no incident will be raised.
                  </p>
                </div>
              ) : null}

              {report.changes?.closest ? (
                <div className="mb-2 rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-sm dark:border-amber-900 dark:bg-amber-950/30">
                  <p className="font-semibold text-amber-900 dark:text-amber-200">
                    {report.changes.closest.reference} completed{' '}
                    {Math.round(report.changes.closest.minutes_before_failure)} minutes
                    before the failure started
                  </p>
                  <p className="text-slate-700 dark:text-slate-200">
                    {report.changes.closest.title} — {report.changes.closest.application}
                    {report.changes.closest.environment
                      ? ` / ${report.changes.closest.environment}`
                      : ''}
                    , deployed by {report.changes.closest.deployer_name || 'unknown'}.
                  </p>
                  <p className="mt-1 text-xs text-amber-900/80 dark:text-amber-200/80">
                    This is a correlation in time. It does not prove the deployment
                    caused the failure — but it is the cheapest hypothesis to test,
                    because it can be rolled back.
                  </p>
                </div>
              ) : null}

              {report.incidents?.open_incident ? (
                <p className="mb-2 text-sm text-slate-700 dark:text-slate-200">
                  <span className="font-medium">Open incident</span> since{' '}
                  {formatDateTime(report.incidents.open_incident.started_at)} —{' '}
                  {report.incidents.open_incident.failed_check_count} failed checks.
                  {report.incidents.open_incident.acknowledged_at
                    ? ' Acknowledged.'
                    : ' Not yet acknowledged.'}{' '}
                  This diagnosis relates to that incident; no duplicate is created.
                </p>
              ) : null}

              {report.recurrence?.most_common_verdict_count >= 2 ? (
                <div className="rounded-lg bg-slate-50 p-2.5 text-sm dark:bg-slate-800/60">
                  <p className="font-medium text-slate-800 dark:text-slate-100">
                    Diagnosed as “{report.recurrence.most_common_verdict}”{' '}
                    {report.recurrence.most_common_verdict_count} times in the last{' '}
                    {report.recurrence.window_days} days
                  </p>
                  {report.recurrence.past_resolutions?.length ? (
                    <ul className="mt-1 space-y-0.5">
                      {report.recurrence.past_resolutions.map((entry, index) => (
                        <li key={index} className="text-xs text-slate-600 dark:text-slate-300">
                          <span className="text-slate-400">
                            {formatDateTime(entry.at, 'dd MMM')}:
                          </span>{' '}
                          {entry.resolution}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                      No resolution was recorded for the previous occurrences, so
                      there is nothing to learn from them. Record one this time.
                    </p>
                  )}
                </div>
              ) : null}
            </Section>
          ) : null}

          {/* ============================================ 5. WHAT TO DO NOW */}
          {report.actions?.length ? (
            <Section
              title="What to do now"
              icon={TrendingUp}
              subtitle="Safest first"
            >
              <ol className="space-y-2">
                {report.actions.map((action) => {
                  const style = RISK_STYLE[action.risk] || RISK_STYLE.safe
                  return (
                    <li
                      key={action.step}
                      className="rounded-lg border border-slate-200 p-2.5 dark:border-slate-700"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-slate-800 text-[11px] font-bold text-white dark:bg-slate-200 dark:text-slate-900">
                          {action.step}
                        </span>
                        <span className="text-sm font-semibold text-slate-900 dark:text-slate-50">
                          {action.title}
                        </span>
                        <span className={clsx('badge ml-auto text-[10px]', style.chip)}>
                          {style.label}
                        </span>
                      </div>
                      <p className="mt-1 pl-7 text-sm text-slate-700 dark:text-slate-200">
                        {action.detail}
                      </p>
                      {action.risk === 'high_risk' ? (
                        <p className="mt-1 pl-7 text-xs font-medium text-red-700 dark:text-red-300">
                          {style.note} CertMonitor will never run this for you.
                        </p>
                      ) : null}
                      {action.command ? (
                        <div className="mt-1.5 pl-7">
                          <CommandLine
                            command={action.command}
                            note={action.command_note}
                            risk={action.risk}
                          />
                        </div>
                      ) : action.command_note ? (
                        <p className="mt-1 pl-7 text-xs text-slate-500 dark:text-slate-400">
                          {action.command_note}
                        </p>
                      ) : null}
                    </li>
                  )
                })}
              </ol>
            </Section>
          ) : null}

          {/* ================================================= 6. COMMANDS */}
          {report.commands?.length ? (
            <Section
              title="Commands"
              icon={Terminal}
              subtitle="All read-only"
              defaultOpen={false}
            >
              <div className="space-y-2">
                {report.commands.map((entry, index) => (
                  <CommandLine
                    key={index}
                    command={entry.command}
                    note={entry.note}
                    risk={entry.risk}
                  />
                ))}
              </div>
            </Section>
          ) : null}

          {/* ================================================== 7. FINDINGS */}
          {report.findings?.length ? (
            <Section
              title="Findings"
              icon={AlertTriangle}
              subtitle={`${report.findings.length} noted`}
              defaultOpen={false}
            >
              <ul className="space-y-2">
                {report.findings.map((finding, index) => (
                  <li
                    key={index}
                    className="rounded-lg border border-slate-200 p-2.5 dark:border-slate-700"
                  >
                    <p className="flex items-center gap-1.5 text-sm font-semibold text-slate-900 dark:text-slate-50">
                      <span
                        className={clsx(
                          finding.severity === 'high'
                            ? 'text-red-600 dark:text-red-400'
                            : finding.severity === 'medium'
                              ? 'text-amber-600 dark:text-amber-400'
                              : 'text-slate-400',
                        )}
                      >
                        <AlertTriangle size={13} aria-hidden="true" />
                      </span>
                      {finding.title}
                    </p>
                    <p className="mt-0.5 text-sm text-slate-700 dark:text-slate-200">
                      {finding.detail}
                    </p>
                    <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                      <span className="font-medium">Next:</span> {finding.action}
                    </p>
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}

          {/* ============================================== 8. VERIFICATION */}
          {report.verification?.length ? (
            <Section title="How to verify the fix" icon={ShieldCheck}>
              <ul className="space-y-1">
                {report.verification.map((item, index) => (
                  <li
                    key={index}
                    className="flex items-start gap-2 text-sm text-slate-700 dark:text-slate-200"
                  >
                    <span className="mt-0.5 shrink-0 text-slate-400">
                      <Check size={14} aria-hidden="true" />
                    </span>
                    {item}
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}

          {/* ========================================== 9. WHAT WE CANNOT SEE */}
          {report.not_observable?.length ? (
            <Section
              title="Not observable from CertMonitor"
              icon={HelpCircle}
              subtitle="Check these yourself"
              defaultOpen={false}
            >
              <p className="mb-2 text-xs text-slate-600 dark:text-slate-300">
                CertMonitor watches this endpoint from the outside. Nothing below
                was measured, so nothing below is claimed — it is listed so that
                silence is not mistaken for a clean result.
              </p>
              <ul className="space-y-1.5">
                {report.not_observable.map((item, index) => (
                  <li key={index}>
                    <p className="flex items-center gap-1.5 text-sm text-slate-800 dark:text-slate-100">
                      <span
                        className={clsx(
                          'badge text-[10px]',
                          EVIDENCE_KIND[item.kind]?.style || EVIDENCE_KIND.unknown.style,
                        )}
                      >
                        {EVIDENCE_KIND[item.kind]?.label || 'Not checked'}
                      </span>
                      <span className="font-medium">{item.label}</span>
                    </p>
                    {item.detail ? (
                      <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
                        {item.detail}
                      </p>
                    ) : null}
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}

          {/* =============================================== 10. RE-DIAGNOSE */}
          {onRerun ? (
            <div className="flex flex-wrap items-center gap-2 rounded-lg border border-slate-200 p-2.5 dark:border-slate-700">
              <label
                htmlFor="diagnose-focus"
                className="text-xs font-medium text-slate-500 dark:text-slate-400"
              >
                Focus
              </label>
              <select
                id="diagnose-focus"
                className="input w-auto py-1 text-xs"
                value={focus}
                onChange={(event) => setFocus(event.target.value)}
              >
                {FOCUS_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn-primary ml-auto"
                onClick={() => onRerun(focus)}
                disabled={loading}
              >
                <RefreshCw size={15} /> Re-diagnose
              </button>
            </div>
          ) : null}

          {httpLayer?.data?.http_status ? (
            <p className="text-center text-[11px] text-slate-400">
              Diagnosis #{report.diagnosis_id} · HTTP {httpLayer.data.http_status} ·
              nothing here was written to the monitoring history
            </p>
          ) : null}
        </div>
      )}
    </Modal>
  )
}
