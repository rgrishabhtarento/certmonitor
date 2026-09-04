import { useEffect, useState } from 'react'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { Table2 } from 'lucide-react'

import { formatDateTime, formatMs, formatNumber, formatPercent } from '../lib/format'

/**
 * Chart layer.
 *
 * Colour decisions, and why:
 *
 *  - Single-series charts use ONE hue (categorical slot 1). Colouring nominal
 *    categories by magnitude would double-encode bar length as hue.
 *  - The two-series latency chart uses slots 1 and 2 (blue/orange). That pair
 *    validated all-pairs in both light and dark against this app's surfaces.
 *  - Uptime % and response time are never plotted on one chart. Two y-scales
 *    would invent a correlation that is not in the data, so they are separate
 *    charts.
 *  - Status-coloured marks (health states, certificate expiry bands) use the
 *    reserved status palette. Red/green status colours are inherently close
 *    under deuteranopia, so colour NEVER carries the meaning alone: every such
 *    mark ships with an axis label, a direct value label, a legend and a
 *    table view.
 */

// Categorical slots, stepped per mode.
export const SERIES = {
  light: { s1: '#2a78d6', s2: '#eb6834', s3: '#1baf7a' },
  dark: { s1: '#3987e5', s2: '#d95926', s3: '#199e70' },
}

// Reserved status palette - fixed, never themed.
export const STATUS = {
  good: '#0ca30c',
  warning: '#fab219',
  serious: '#ec835a',
  critical: '#d03b3b',
  neutral: '#898781',
}

// Chart chrome. Grid and axis rules are solid hairlines one shade off the
// surface - never dashed.
/**
 * Chart furniture, stepped to the surface each theme paints on.
 *
 * The light values are cool rather than warm: they sit on the blue-tinted
 * ground the rest of the light theme uses, and a beige grid against it read
 * as a mismatch. Everything here is chrome - grid, axes, labels - and stays
 * recessive so the data carries the contrast.
 */
const CHROME = {
  light: { grid: '#dde4ee', axis: '#a8b4c6', muted: '#64748b', surface: '#ffffff' },
  dark: { grid: '#2c2c2a', axis: '#383835', muted: '#898781', surface: '#0f172a' },
}

/** Tracks the `dark` class the layout toggles, so charts restep their colours. */
export function useChartMode() {
  const [mode, setMode] = useState(() =>
    document.documentElement.classList.contains('dark') ? 'dark' : 'light',
  )
  useEffect(() => {
    const observer = new MutationObserver(() => {
      setMode(document.documentElement.classList.contains('dark') ? 'dark' : 'light')
    })
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['class'],
    })
    return () => observer.disconnect()
  }, [])
  return mode
}

export function statusColor(status) {
  switch (status) {
    case 'up':
    case 'valid':
      return STATUS.good
    case 'degraded':
    case 'expiring_soon':
      return STATUS.warning
    case 'critical':
      return STATUS.serious
    case 'down':
    case 'expired':
    case 'invalid':
      return STATUS.critical
    default:
      return STATUS.neutral
  }
}

// ------------------------------------------------------------------ shared
const AXIS_FONT = 11

function axisProps(chrome) {
  return {
    stroke: chrome.axis,
    tick: { fill: chrome.muted, fontSize: AXIS_FONT },
    tickLine: false,
  }
}

/** Tooltip shell: one surface, hairline ring, no drop shadow theatre. */
function TooltipShell({ title, rows }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-xs shadow-lg dark:border-slate-700 dark:bg-slate-800">
      {title ? (
        <p className="mb-1 font-medium text-slate-900 dark:text-slate-100">{title}</p>
      ) : null}
      <dl className="space-y-0.5">
        {rows.map((row) => (
          <div key={row.label} className="flex items-center gap-2">
            {row.color ? (
              <span
                className="h-2 w-2 shrink-0 rounded-sm"
                style={{ backgroundColor: row.color }}
                aria-hidden="true"
              />
            ) : null}
            <dt className="text-slate-500 dark:text-slate-400">{row.label}</dt>
            <dd className="tnum ml-auto font-medium text-slate-900 dark:text-slate-100">
              {row.value}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

/**
 * Wraps a chart with a "Show data" toggle.
 *
 * This is the table-view twin every chart needs: it is the WCAG-clean path to
 * the same values, and the documented relief for status colours that sit below
 * 3:1 against the light surface.
 */
export function ChartFrame({
  title,
  subtitle,
  legend,
  children,
  table,
  height = 240,
  // Charts whose height depends on how many rows they draw (the grouped bar
  // charts) size themselves; a fixed frame height would crop their x-axis
  // into a nested scrollbar.
  autoHeight = false,
}) {
  const [showTable, setShowTable] = useState(false)
  return (
    <section className="card flex flex-col">
      <header className="card-header">
        <div className="min-w-0">
          <h2 className="card-title">{title}</h2>
          {subtitle ? (
            <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">{subtitle}</p>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          {legend}
          {table ? (
            <button
              type="button"
              className="btn-ghost btn-sm"
              onClick={() => setShowTable((v) => !v)}
              aria-pressed={showTable}
              title={showTable ? 'Show chart' : 'Show data table'}
            >
              <Table2 size={14} />
              <span className="hidden sm:inline">{showTable ? 'Chart' : 'Data'}</span>
            </button>
          ) : null}
        </div>
      </header>
      <div className="p-3">
        {showTable && table ? (
          <div className="table-wrap max-h-[320px] overflow-y-auto">{table}</div>
        ) : (
          /* The container includes the x-axis band, so the axis labels are
             never cropped into a nested scrollbar. */
          <div style={autoHeight ? { minHeight: height } : { height }}>{children}</div>
        )}
      </div>
    </section>
  )
}

/** Legend swatches. Always rendered for >= 2 series and for status colours. */
export function Legend({ items }) {
  return (
    <ul className="flex flex-wrap items-center gap-x-3 gap-y-1">
      {items.map((item) => (
        <li
          key={item.label}
          className="flex items-center gap-1.5 text-xs text-slate-600 dark:text-slate-300"
        >
          <span
            className="h-2 w-2 rounded-sm"
            style={{ backgroundColor: item.color }}
            aria-hidden="true"
          />
          {item.label}
        </li>
      ))}
    </ul>
  )
}

// -------------------------------------------------- response time (1 series)
export function ResponseTimeChart({ data, height = 240, mode }) {
  const chrome = CHROME[mode]
  const series = SERIES[mode]

  if (!data?.length) {
    return <NoData label="No checks recorded in this window yet." />
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 4 }}>
        <defs>
          <linearGradient id="respFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={series.s1} stopOpacity={0.28} />
            <stop offset="100%" stopColor={series.s1} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={chrome.grid} vertical={false} />
        <XAxis dataKey="label" {...axisProps(chrome)} minTickGap={28} />
        <YAxis
          {...axisProps(chrome)}
          width={52}
          tickFormatter={(value) => `${Math.round(value)}`}
          label={{
            value: 'ms',
            angle: 0,
            position: 'top',
            offset: 10,
            fill: chrome.muted,
            fontSize: AXIS_FONT,
          }}
        />
        <Tooltip
          cursor={{ stroke: chrome.axis, strokeWidth: 1 }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null
            const point = payload[0].payload
            return (
              <TooltipShell
                title={formatDateTime(point.timestamp, 'dd MMM HH:mm')}
                rows={[
                  {
                    label: 'Average',
                    value: formatMs(point.avg_response_time_ms),
                    color: series.s1,
                  },
                  { label: 'Checks', value: formatNumber(point.checks) },
                  { label: 'Failed', value: formatNumber(point.failed_checks) },
                ]}
              />
            )
          }}
        />
        <Area
          type="monotone"
          dataKey="avg_response_time_ms"
          name="Average response time"
          stroke={series.s1}
          strokeWidth={2}
          fill="url(#respFill)"
          dot={false}
          isAnimationActive={false}
          activeDot={{ r: 4, strokeWidth: 2, stroke: chrome.surface }}
          connectNulls
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}

// ------------------------------------------------------- uptime (1 series)
export function UptimeChart({ data, slaTarget, height = 240, mode }) {
  const chrome = CHROME[mode]
  const series = SERIES[mode]

  if (!data?.length) {
    return <NoData label="No checks recorded in this window yet." />
  }

  // Uptime clusters near 100%; a fixed 0-100 domain would flatten every
  // meaningful dip into the top pixel row.
  const values = data.map((d) => d.uptime_percent).filter((v) => v !== null)
  const min = values.length ? Math.min(...values) : 100
  const floor = Math.max(0, Math.floor(Math.min(min, slaTarget ?? 100) - 1))

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 4 }}>
        <CartesianGrid stroke={chrome.grid} vertical={false} />
        <XAxis dataKey="label" {...axisProps(chrome)} minTickGap={28} />
        <YAxis
          {...axisProps(chrome)}
          width={52}
          domain={[floor, 100]}
          tickFormatter={(value) => `${value}%`}
        />
        {slaTarget ? (
          <ReferenceLine
            y={slaTarget}
            stroke={STATUS.warning}
            strokeWidth={1.5}
            label={{
              value: `SLA ${slaTarget}%`,
              position: 'insideBottomRight',
              fill: chrome.muted,
              fontSize: AXIS_FONT,
            }}
          />
        ) : null}
        <Tooltip
          cursor={{ stroke: chrome.axis, strokeWidth: 1 }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null
            const point = payload[0].payload
            return (
              <TooltipShell
                title={formatDateTime(point.timestamp, 'dd MMM HH:mm')}
                rows={[
                  {
                    label: 'Uptime',
                    value: formatPercent(point.uptime_percent),
                    color: series.s1,
                  },
                  { label: 'Checks', value: formatNumber(point.checks) },
                  { label: 'Failed', value: formatNumber(point.failed_checks) },
                ]}
              />
            )
          }}
        />
        <Line
          type="monotone"
          dataKey="uptime_percent"
          name="Uptime"
          stroke={series.s1}
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
          activeDot={{ r: 4, strokeWidth: 2, stroke: chrome.surface }}
          connectNulls
        />
      </LineChart>
    </ResponsiveContainer>
  )
}

// -------------------------------------- latency breakdown (2 series, detail)
export function LatencyBreakdownChart({ data, height = 240, mode }) {
  const chrome = CHROME[mode]
  const series = SERIES[mode]

  if (!data?.length) return <NoData label="No checks recorded yet." />

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 4 }}>
        <CartesianGrid stroke={chrome.grid} vertical={false} />
        <XAxis dataKey="label" {...axisProps(chrome)} minTickGap={28} />
        <YAxis {...axisProps(chrome)} width={52} />
        <Tooltip
          cursor={{ stroke: chrome.axis, strokeWidth: 1 }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null
            const point = payload[0].payload
            return (
              <TooltipShell
                title={formatDateTime(point.timestamp, 'dd MMM HH:mm')}
                rows={[
                  {
                    label: 'Average',
                    value: formatMs(point.avg_response_time_ms),
                    color: series.s1,
                  },
                  {
                    label: 'Maximum',
                    value: formatMs(point.max_response_time_ms),
                    color: series.s2,
                  },
                  { label: 'DNS', value: formatMs(point.avg_dns_time_ms, { decimals: 1 }) },
                  {
                    label: 'Connect',
                    value: formatMs(point.avg_connect_time_ms, { decimals: 1 }),
                  },
                  { label: 'TLS', value: formatMs(point.avg_tls_time_ms, { decimals: 1 }) },
                ]}
              />
            )
          }}
        />
        <Line
          type="monotone"
          dataKey="avg_response_time_ms"
          name="Average"
          stroke={series.s1}
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
          activeDot={{ r: 4, strokeWidth: 2, stroke: chrome.surface }}
          connectNulls
        />
        <Line
          type="monotone"
          dataKey="max_response_time_ms"
          name="Maximum"
          stroke={series.s2}
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
          activeDot={{ r: 4, strokeWidth: 2, stroke: chrome.surface }}
          connectNulls
        />
      </LineChart>
    </ResponsiveContainer>
  )
}

// ----------------------------------------------------- status distribution
const STATUS_ORDER = [
  { key: 'up', label: 'Healthy', color: STATUS.good },
  { key: 'degraded', label: 'Degraded', color: STATUS.warning },
  { key: 'down', label: 'Down', color: STATUS.critical },
  { key: 'unknown', label: 'Unknown', color: STATUS.neutral },
  { key: 'paused', label: 'Paused', color: '#cbd5e1' },
]

/**
 * Part-to-whole health split as a single horizontal stacked bar.
 *
 * Not a pie: the segments are frequently close in size, and a stacked bar
 * keeps them comparable. Every segment is named and counted in the legend, so
 * the status hues reinforce rather than carry the meaning.
 */
export function StatusDistribution({ counts, total }) {
  const segments = STATUS_ORDER.map((entry) => ({
    ...entry,
    count: counts?.[entry.key] || 0,
  })).filter((entry) => entry.count > 0)

  if (!total) return <NoData label="No endpoints configured yet." />

  return (
    <div>
      <div
        className="flex h-7 w-full gap-[2px] overflow-hidden rounded-md"
        role="img"
        aria-label={segments
          .map((s) => `${s.label}: ${s.count} of ${total}`)
          .join(', ')}
      >
        {segments.map((segment) => {
          const share = (segment.count / total) * 100
          return (
            <div
              key={segment.key}
              className="grid place-items-center text-[11px] font-semibold text-white"
              style={{ width: `${share}%`, backgroundColor: segment.color }}
              title={`${segment.label}: ${segment.count} (${share.toFixed(1)}%)`}
            >
              {/* Only label a segment wide enough to hold the text; the rest
                  read their value from the legend below. */}
              {share >= 9 ? segment.count : null}
            </div>
          )
        })}
      </div>
      <ul className="mt-3 grid grid-cols-2 gap-1.5 sm:grid-cols-3">
        {segments.map((segment) => (
          <li key={segment.key} className="flex items-center gap-1.5 text-xs">
            <span
              className="h-2.5 w-2.5 shrink-0 rounded-sm"
              style={{ backgroundColor: segment.color }}
              aria-hidden="true"
            />
            <span className="text-slate-600 dark:text-slate-300">{segment.label}</span>
            <span className="tnum ml-auto font-semibold text-slate-900 dark:text-slate-100">
              {segment.count}
            </span>
            <span className="tnum w-12 text-right text-slate-400">
              {((segment.count / total) * 100).toFixed(1)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

// ----------------------------------------------- availability by dimension
/**
 * Horizontal bars comparing availability across nominal groups.
 *
 * All bars share ONE hue: the bar length already encodes the magnitude, so
 * shading by value would spend the colour channel on information the chart
 * shows twice.
 */
export function AvailabilityBars({ groups, height = 240, mode, metric = 'uptime_percent' }) {
  const chrome = CHROME[mode]
  const series = SERIES[mode]

  const data = (groups || [])
    .filter((group) => group[metric] !== null && group[metric] !== undefined)
    .slice(0, 12)

  if (!data.length) {
    return <NoData label="Not enough data to compare these groups yet." />
  }

  const values = data.map((d) => d[metric])
  const min = Math.min(...values)
  const domainFloor = metric === 'uptime_percent' ? Math.max(0, Math.floor(min - 2)) : 0

  return (
    <ResponsiveContainer width="100%" height={Math.max(height, data.length * 30 + 40)}>
      <BarChart
        data={data}
        layout="vertical"
        margin={{ top: 4, right: 48, left: 4, bottom: 4 }}
        barCategoryGap={4}
      >
        <CartesianGrid stroke={chrome.grid} horizontal={false} />
        <XAxis
          type="number"
          domain={[domainFloor, 100]}
          {...axisProps(chrome)}
          tickFormatter={(value) => `${value}%`}
        />
        <YAxis
          type="category"
          dataKey="name"
          {...axisProps(chrome)}
          width={110}
          interval={0}
        />
        <Tooltip
          cursor={{ fill: chrome.grid, fillOpacity: 0.4 }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null
            const point = payload[0].payload
            return (
              <TooltipShell
                title={point.name}
                rows={[
                  {
                    label: 'Uptime',
                    value: formatPercent(point.uptime_percent),
                    color: series.s1,
                  },
                  { label: 'Endpoints', value: formatNumber(point.total) },
                  { label: 'Healthy', value: formatNumber(point.healthy) },
                  { label: 'Down', value: formatNumber(point.down) },
                ]}
              />
            )
          }}
        />
        <Bar
          dataKey={metric}
          name="Uptime"
          fill={series.s1}
          radius={[0, 4, 4, 0]}
          isAnimationActive={false}
        >
          {/* Direct value labels outside the bar end: never clipped, and the
              value stays readable without a hover. */}
          <LabelList
            dataKey={metric}
            position="right"
            formatter={(value) => `${Number(value).toFixed(2)}%`}
            fill={chrome.muted}
            fontSize={AXIS_FONT}
          />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

// ------------------------------------------------ failures over time (detail)
/**
 * When the failures happened, rather than how many there were in total.
 *
 * A count of 47 failed checks says nothing useful; 47 failures inside two
 * adjacent buckets is an outage with a start and an end, and 47 spread evenly
 * across three days is a flapping endpoint. Those are completely different
 * problems and the total cannot tell them apart.
 *
 * Failed and degraded are stacked because they are the same axis - checks that
 * did not fully succeed - and stacking keeps the total height readable as
 * "how bad was that moment".
 */
export function FailuresOverTimeChart({ data, height = 200, mode }) {
  const chrome = CHROME[mode]

  if (!data?.length) return <NoData label="No checks recorded yet." />

  const anyFailures = data.some(
    (point) => (point.failed_checks || 0) + (point.degraded_checks || 0) > 0,
  )
  if (!anyFailures) {
    return <NoData label="No failed or degraded checks in this window." />
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 4 }}>
        <CartesianGrid stroke={chrome.grid} vertical={false} />
        <XAxis dataKey="label" {...axisProps(chrome)} minTickGap={28} />
        <YAxis {...axisProps(chrome)} width={40} allowDecimals={false} />
        <Tooltip
          cursor={{ fill: chrome.grid, opacity: 0.4 }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null
            const point = payload[0].payload
            return (
              <TooltipShell
                title={formatDateTime(point.timestamp, 'dd MMM HH:mm')}
                rows={[
                  {
                    label: 'Failed',
                    value: formatNumber(point.failed_checks || 0),
                    color: STATUS.critical,
                  },
                  {
                    label: 'Degraded',
                    value: formatNumber(point.degraded_checks || 0),
                    color: STATUS.warning,
                  },
                  { label: 'Checks run', value: formatNumber(point.checks || 0) },
                ]}
              />
            )
          }}
        />
        <Bar
          dataKey="failed_checks"
          name="Failed"
          stackId="outcome"
          fill={STATUS.critical}
          isAnimationActive={false}
        />
        <Bar
          dataKey="degraded_checks"
          name="Degraded"
          stackId="outcome"
          fill={STATUS.warning}
          radius={[3, 3, 0, 0]}
          isAnimationActive={false}
        />
      </BarChart>
    </ResponsiveContainer>
  )
}


// ------------------------------------------------------ SSL expiry buckets
const BUCKET_TONE = (bucket) => {
  const label = String(bucket).toLowerCase()
  if (label.includes('expired')) return STATUS.critical
  if (label.startsWith('0-7')) return STATUS.critical
  if (label.startsWith('8-14') || label.startsWith('15-30')) return STATUS.warning
  return STATUS.good
}

/**
 * Certificates grouped by how soon they expire.
 *
 * Buckets are ordered, so the x-axis carries the meaning; the status tones
 * reinforce urgency. Counts are labelled directly above each bar, which is the
 * documented relief for status hues that fall below 3:1 on a light surface.
 */
export function SslExpiryChart({ buckets, height = 220, mode }) {
  const chrome = CHROME[mode]
  const data = (buckets || []).filter((bucket) => bucket.count > 0)

  if (!data.length) {
    return <NoData label="No certificates are being tracked yet." />
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ top: 20, right: 8, left: 4, bottom: 4 }}>
        <CartesianGrid stroke={chrome.grid} vertical={false} />
        <XAxis
          dataKey="bucket"
          {...axisProps(chrome)}
          interval={0}
          angle={-20}
          textAnchor="end"
          height={48}
        />
        <YAxis {...axisProps(chrome)} width={36} allowDecimals={false} />
        <Tooltip
          cursor={{ fill: chrome.grid, fillOpacity: 0.4 }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null
            const point = payload[0].payload
            return (
              <TooltipShell
                title={point.bucket}
                rows={[
                  {
                    label: 'Certificates',
                    value: formatNumber(point.count),
                    color: BUCKET_TONE(point.bucket),
                  },
                ]}
              />
            )
          }}
        />
        <Bar
          dataKey="count"
          name="Certificates"
          radius={[4, 4, 0, 0]}
          isAnimationActive={false}
        >
          {data.map((bucket) => (
            <Cell key={bucket.bucket} fill={BUCKET_TONE(bucket.bucket)} />
          ))}
          <LabelList
            dataKey="count"
            position="top"
            fill={chrome.muted}
            fontSize={AXIS_FONT}
          />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

// ---------------------------------------------------------- failure counts
export function FailureBars({ endpoints, height = 220, mode }) {
  const chrome = CHROME[mode]
  const data = (endpoints || []).slice(0, 8)

  if (!data.length) {
    return <NoData label="No failed checks in this window." />
  }

  return (
    <ResponsiveContainer width="100%" height={Math.max(height, data.length * 30 + 30)}>
      <BarChart
        data={data}
        layout="vertical"
        margin={{ top: 4, right: 40, left: 4, bottom: 4 }}
        barCategoryGap={4}
      >
        <CartesianGrid stroke={chrome.grid} horizontal={false} />
        <XAxis type="number" {...axisProps(chrome)} allowDecimals={false} />
        <YAxis
          type="category"
          dataKey="name"
          {...axisProps(chrome)}
          width={130}
          interval={0}
          tickFormatter={(value) =>
            String(value).length > 20 ? `${String(value).slice(0, 19)}…` : value
          }
        />
        <Tooltip
          cursor={{ fill: chrome.grid, fillOpacity: 0.4 }}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null
            const point = payload[0].payload
            return (
              <TooltipShell
                title={point.name}
                rows={[
                  {
                    label: 'Failed checks',
                    value: formatNumber(point.failed_checks),
                    color: STATUS.critical,
                  },
                  { label: 'URL', value: point.url },
                ]}
              />
            )
          }}
        />
        <Bar
          dataKey="failed_checks"
          name="Failed checks"
          fill={STATUS.critical}
          radius={[0, 4, 4, 0]}
          isAnimationActive={false}
        >
          <LabelList
            dataKey="failed_checks"
            position="right"
            fill={chrome.muted}
            fontSize={AXIS_FONT}
          />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

function NoData({ label }) {
  return (
    <div className="grid h-full min-h-[120px] place-items-center px-4 text-center">
      <p className="text-sm text-slate-400 dark:text-slate-500">{label}</p>
    </div>
  )
}

/** Adds the short axis label the time-series charts render on the x-axis. */
export function withSeriesLabels(series, bucketSeconds) {
  const pattern = bucketSeconds >= 86400 ? 'dd MMM' : 'dd MMM HH:mm'
  return (series || []).map((point) => ({
    ...point,
    label: formatDateTime(point.timestamp, bucketSeconds >= 3600 ? pattern : 'HH:mm'),
  }))
}

/** Table twin for a time series. */
export function SeriesTable({ series, valueLabel = 'Avg response', unit = 'ms' }) {
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Time</th>
          <th className="text-right">Checks</th>
          <th className="text-right">Failed</th>
          <th className="text-right">Uptime</th>
          <th className="text-right">{valueLabel}</th>
        </tr>
      </thead>
      <tbody>
        {series.map((point) => (
          <tr key={point.timestamp}>
            <td className="whitespace-nowrap">
              {formatDateTime(point.timestamp, 'dd MMM yyyy HH:mm')}
            </td>
            <td className="tnum text-right">{formatNumber(point.checks)}</td>
            <td className="tnum text-right">{formatNumber(point.failed_checks)}</td>
            <td className="tnum text-right">{formatPercent(point.uptime_percent)}</td>
            <td className="tnum text-right">
              {unit === 'ms'
                ? formatMs(point.avg_response_time_ms)
                : formatNumber(point.avg_response_time_ms)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** Table twin for a grouped-availability chart. */
export function GroupTable({ groups, groupLabel = 'Group' }) {
  return (
    <table className="table">
      <thead>
        <tr>
          <th>{groupLabel}</th>
          <th className="text-right">Endpoints</th>
          <th className="text-right">Healthy</th>
          <th className="text-right">Down</th>
          <th className="text-right">Uptime</th>
        </tr>
      </thead>
      <tbody>
        {groups.map((group) => (
          <tr key={group.name}>
            <td>{group.name}</td>
            <td className="tnum text-right">{formatNumber(group.total)}</td>
            <td className="tnum text-right">{formatNumber(group.healthy)}</td>
            <td className="tnum text-right">{formatNumber(group.down)}</td>
            <td className="tnum text-right">{formatPercent(group.uptime_percent)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
