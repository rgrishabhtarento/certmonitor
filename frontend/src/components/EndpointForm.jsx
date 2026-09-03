import { useEffect, useMemo, useState } from 'react'
import { AlertCircle, ChevronDown, ChevronRight, Lock } from 'lucide-react'

import { Field, Modal, Spinner, TagInput, Toggle } from './ui'
import { endpointsApi } from '../lib/api'
import { useToast } from '../hooks/useToast'

const METHODS = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']
const CHECK_TYPES = [
  { value: 'http', label: 'HTTP(S) request' },
  { value: 'tls', label: 'TLS handshake only' },
  { value: 'tcp', label: 'TCP connect only' },
]
const AUTH_TYPES = [
  { value: 'none', label: 'None' },
  { value: 'bearer', label: 'Bearer token' },
  { value: 'basic', label: 'Basic authentication' },
  { value: 'header', label: 'Custom header' },
]

const EMPTY = {
  name: '',
  url: '',
  check_type: 'http',
  http_method: 'GET',
  environment: '',
  tags: [],
  description: '',
  owner: '',
  team: '',
  application: '',
  monitoring_enabled: true,
  is_paused: false,
  interval_seconds: '',
  timeout_seconds: '',
  expected_status_codes: '',
  expected_body_substring: '',
  follow_redirects: true,
  verify_ssl: true,
  ssl_monitoring_enabled: true,
  request_body: '',
  custom_headers: '',
  auth_type: 'none',
  auth_username: '',
  auth_header_name: '',
  auth_secret: '',
  failure_threshold: '',
  response_time_threshold_ms: '',
  ssl_warning_days: '',
  ssl_critical_days: '',
  alerts_enabled: true,
}

function Section({ title, description, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-700">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        <span className="text-sm font-medium text-slate-800 dark:text-slate-200">{title}</span>
        {description ? (
          <span className="ml-auto hidden text-xs text-slate-400 sm:block">{description}</span>
        ) : null}
      </button>
      {open ? (
        <div className="space-y-3 border-t border-slate-200 px-3 py-3 dark:border-slate-700">
          {children}
        </div>
      ) : null}
    </div>
  )
}

/**
 * Create/edit dialog for an endpoint.
 *
 * The credential field behaves the way the API does: it is write-only. An
 * existing endpoint shows only a masked hint, and leaving the field blank
 * keeps whatever is stored - the plaintext is never sent back to the browser,
 * so there is nothing to prefill.
 */
export default function EndpointForm({ open, onClose, onSaved, endpoint, filters, config }) {
  const toast = useToast()
  const isEdit = Boolean(endpoint?.id)

  const [form, setForm] = useState(EMPTY)
  const [errors, setErrors] = useState({})
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState(null)

  useEffect(() => {
    if (!open) return
    setErrors({})
    setError(null)
    setTestResult(null)
    if (endpoint) {
      setForm({
        ...EMPTY,
        ...endpoint,
        environment: endpoint.environment?.id || '',
        tags: (endpoint.tags || []).map((tag) => tag.name),
        interval_seconds: endpoint.interval_seconds ?? '',
        timeout_seconds: endpoint.timeout_seconds ?? '',
        expected_status_codes: endpoint.expected_status_codes ?? '',
        expected_body_substring: endpoint.expected_body_substring ?? '',
        description: endpoint.description ?? '',
        owner: endpoint.owner ?? '',
        team: endpoint.team ?? '',
        application: endpoint.application ?? '',
        request_body: endpoint.request_body ?? '',
        custom_headers: endpoint.custom_headers
          ? JSON.stringify(endpoint.custom_headers, null, 2)
          : '',
        auth_secret: '',
        failure_threshold: endpoint.failure_threshold ?? '',
        response_time_threshold_ms: endpoint.response_time_threshold_ms ?? '',
        ssl_warning_days: endpoint.ssl_warning_days ?? '',
        ssl_critical_days: endpoint.ssl_critical_days ?? '',
      })
    } else {
      setForm({
        ...EMPTY,
        interval_seconds: config?.default_monitor_interval ?? '',
        timeout_seconds: config?.default_timeout ?? '',
      })
    }
  }, [open, endpoint, config])

  const tagSuggestions = useMemo(
    () => (filters?.tags || []).map((tag) => tag.name),
    [filters],
  )
  const intervals = config?.allowed_intervals || [30, 60, 300, 600, 1800, 3600]

  const set = (key) => (value) => setForm((current) => ({ ...current, [key]: value }))
  const setInput = (key) => (event) =>
    setForm((current) => ({ ...current, [key]: event.target.value }))

  /** Convert the form into the API payload, dropping untouched optionals. */
  const buildPayload = () => {
    const payload = {
      name: form.name.trim(),
      url: form.url.trim(),
      check_type: form.check_type,
      http_method: form.http_method,
      environment: form.environment || null,
      tags: form.tags,
      description: form.description || null,
      owner: form.owner || null,
      team: form.team || null,
      application: form.application || null,
      monitoring_enabled: form.monitoring_enabled,
      is_paused: form.is_paused,
      follow_redirects: form.follow_redirects,
      verify_ssl: form.verify_ssl,
      ssl_monitoring_enabled: form.ssl_monitoring_enabled,
      alerts_enabled: form.alerts_enabled,
      expected_status_codes: form.expected_status_codes || null,
      expected_body_substring: form.expected_body_substring || null,
      request_body: form.request_body || null,
      auth_type: form.auth_type,
      auth_username: form.auth_username || null,
      auth_header_name: form.auth_header_name || null,
    }

    for (const key of [
      'interval_seconds',
      'timeout_seconds',
      'failure_threshold',
      'response_time_threshold_ms',
      'ssl_warning_days',
      'ssl_critical_days',
    ]) {
      payload[key] = form[key] === '' || form[key] === null ? null : Number(form[key])
    }

    if (form.custom_headers.trim()) {
      payload.custom_headers = JSON.parse(form.custom_headers)
    } else {
      payload.custom_headers = null
    }

    // Blank means "leave the stored credential alone" on edit, and "no
    // credential" on create.
    if (form.auth_secret) payload.auth_secret = form.auth_secret

    return payload
  }

  const validate = () => {
    const next = {}
    if (!form.name.trim()) next.name = 'A name is required.'
    if (!form.url.trim()) next.url = 'A URL or hostname is required.'
    if (form.custom_headers.trim()) {
      try {
        const parsed = JSON.parse(form.custom_headers)
        if (typeof parsed !== 'object' || Array.isArray(parsed)) {
          next.custom_headers = 'Headers must be a JSON object.'
        }
      } catch {
        next.custom_headers = 'Headers must be valid JSON, e.g. {"X-Api-Version": "2"}.'
      }
    }
    if (
      form.auth_type !== 'none' &&
      !form.auth_secret &&
      !(isEdit && endpoint?.has_auth_secret)
    ) {
      next.auth_secret = 'A credential is required for this authentication type.'
    }
    if (form.auth_type === 'basic' && !form.auth_username) {
      next.auth_username = 'Basic authentication needs a username.'
    }
    if (form.auth_type === 'header' && !form.auth_header_name) {
      next.auth_header_name = 'Give the header a name.'
    }
    if (
      form.ssl_warning_days &&
      form.ssl_critical_days &&
      Number(form.ssl_critical_days) > Number(form.ssl_warning_days)
    ) {
      next.ssl_critical_days = 'The critical threshold must not exceed the warning threshold.'
    }
    setErrors(next)
    return Object.keys(next).length === 0
  }

  const submit = async (event) => {
    event.preventDefault()
    setError(null)
    if (!validate()) return

    setBusy(true)
    try {
      const payload = buildPayload()
      const saved = isEdit
        ? await endpointsApi.update(endpoint.id, payload)
        : await endpointsApi.create(payload)
      toast.success(
        isEdit ? `'${saved.name}' updated.` : `'${saved.name}' added and queued for a check.`,
      )
      onSaved(saved)
    } catch (err) {
      setError(err.message)
      if (err.fields) setErrors(err.fields)
    } finally {
      setBusy(false)
    }
  }

  /** Dry-run the check without saving or recording anything. */
  const runTest = async () => {
    if (!isEdit) {
      toast.info('Save the endpoint first, then run a test check against it.')
      return
    }
    setTesting(true)
    setTestResult(null)
    try {
      const result = await endpointsApi.check(endpoint.id, false)
      setTestResult(result)
    } catch (err) {
      setTestResult({ status: 'down', error_message: err.message })
    } finally {
      setTesting(false)
    }
  }

  const isHttp = form.check_type === 'http'
  const isHttps = /^https:/i.test(form.url.trim()) || !/^\w+:/.test(form.url.trim())

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={isEdit ? `Edit ${endpoint?.name}` : 'Add an endpoint'}
      size="lg"
      footer={
        <>
          {isEdit ? (
            <button
              type="button"
              className="btn-secondary mr-auto"
              onClick={runTest}
              disabled={testing}
            >
              {testing ? <Spinner size={15} /> : null}
              Test now
            </button>
          ) : null}
          <button type="button" className="btn-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" form="endpoint-form" className="btn-primary" disabled={busy}>
            {busy ? <Spinner size={15} className="text-white" /> : null}
            {isEdit ? 'Save changes' : 'Add endpoint'}
          </button>
        </>
      }
    >
      <form id="endpoint-form" onSubmit={submit} className="space-y-4">
        {error ? (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/50 dark:text-red-200"
          >
            <AlertCircle size={16} className="mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        ) : null}

        {testResult ? (
          <div
            className={`rounded-lg border px-3 py-2 text-sm ${
              testResult.status === 'up'
                ? 'border-green-200 bg-green-50 text-green-900 dark:border-green-900 dark:bg-green-950/50 dark:text-green-200'
                : testResult.status === 'degraded'
                  ? 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/50 dark:text-amber-200'
                  : 'border-red-200 bg-red-50 text-red-900 dark:border-red-900 dark:bg-red-950/50 dark:text-red-200'
            }`}
          >
            <p className="font-medium">
              Test result: {testResult.status?.toUpperCase()}
              {testResult.http_status_code ? ` · HTTP ${testResult.http_status_code}` : ''}
              {testResult.response_time_ms
                ? ` · ${Math.round(testResult.response_time_ms)} ms`
                : ''}
            </p>
            {testResult.error_message ? <p className="mt-0.5">{testResult.error_message}</p> : null}
            {testResult.resolved_ip ? (
              <p className="mt-0.5 text-xs opacity-80">Resolved to {testResult.resolved_ip}</p>
            ) : null}
            <p className="mt-0.5 text-xs opacity-70">
              Nothing was recorded - this was a dry run.
            </p>
          </div>
        ) : null}

        {/* ------------------------------------------------- essentials */}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Name" required error={errors.name}>
            <input
              className="input"
              value={form.name}
              onChange={setInput('name')}
              placeholder="Translation API"
              maxLength={160}
              autoFocus
            />
          </Field>
          <Field
            label="URL or hostname"
            required
            error={errors.url}
            hint="https:// is assumed when no scheme is given."
          >
            <input
              className="input font-mono text-xs"
              value={form.url}
              onChange={setInput('url')}
              placeholder="https://api.example.com/health"
            />
          </Field>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Check type">
            <select className="input" value={form.check_type} onChange={setInput('check_type')}>
              {CHECK_TYPES.map((type) => (
                <option key={type.value} value={type.value}>
                  {type.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="HTTP method">
            <select
              className="input"
              value={form.http_method}
              onChange={setInput('http_method')}
              disabled={!isHttp}
            >
              {METHODS.map((method) => (
                <option key={method} value={method}>
                  {method}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Environment">
            <select className="input" value={form.environment} onChange={setInput('environment')}>
              <option value="">Unassigned</option>
              {(filters?.environments || []).map((environment) => (
                <option key={environment.id} value={environment.id}>
                  {environment.display_name || environment.name}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Monitoring interval" error={errors.interval_seconds}>
            <select
              className="input"
              value={form.interval_seconds}
              onChange={setInput('interval_seconds')}
            >
              <option value="">Use the default</option>
              {intervals.map((seconds) => (
                <option key={seconds} value={seconds}>
                  {seconds < 60
                    ? `${seconds} seconds`
                    : seconds < 3600
                      ? `${seconds / 60} minutes`
                      : `${seconds / 3600} hour${seconds === 3600 ? '' : 's'}`}
                </option>
              ))}
            </select>
          </Field>
          <Field
            label="Timeout (seconds)"
            error={errors.timeout_seconds}
            hint="Must not exceed the interval."
          >
            <input
              type="number"
              min={1}
              max={120}
              className="input"
              value={form.timeout_seconds}
              onChange={setInput('timeout_seconds')}
              placeholder="10"
            />
          </Field>
          <Field
            label="Expected HTTP status"
            error={errors.expected_status_codes}
            hint="200, or 200,204, or 2xx"
          >
            <input
              className="input"
              value={form.expected_status_codes}
              onChange={setInput('expected_status_codes')}
              placeholder="200"
              disabled={!isHttp}
            />
          </Field>
        </div>

        <Field label="Tags" hint="Type a name and press Enter. New tags are created as you go.">
          <TagInput value={form.tags} onChange={set('tags')} suggestions={tagSuggestions} />
        </Field>

        <Field label="Description">
          <textarea
            className="input"
            rows={2}
            value={form.description}
            onChange={setInput('description')}
            placeholder="What this endpoint is and who depends on it"
          />
        </Field>

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Owner">
            <input
              className="input"
              value={form.owner}
              onChange={setInput('owner')}
              placeholder="platform@example.com"
            />
          </Field>
          <Field label="Team">
            <input className="input" value={form.team} onChange={setInput('team')} />
          </Field>
          <Field label="Application">
            <input
              className="input"
              value={form.application}
              onChange={setInput('application')}
            />
          </Field>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <Toggle
            checked={form.monitoring_enabled}
            onChange={set('monitoring_enabled')}
            label="Monitoring enabled"
            description="Turn off to stop checking without deleting the endpoint."
          />
          <Toggle
            checked={form.is_paused}
            onChange={set('is_paused')}
            label="Paused"
            description="Temporarily suspend checks, for example during a planned migration."
          />
        </div>

        {/* ------------------------------------------------ TLS section */}
        <Section
          title="TLS and certificates"
          description={isHttps ? 'Monitored' : 'Not applicable'}
          defaultOpen={false}
        >
          <div className="grid gap-3 sm:grid-cols-2">
            <Toggle
              checked={form.ssl_monitoring_enabled}
              onChange={set('ssl_monitoring_enabled')}
              label="Inspect the certificate"
              description="Only applies to https:// endpoints."
              disabled={!isHttps}
            />
            <Toggle
              checked={form.verify_ssl}
              onChange={set('verify_ssl')}
              label="Verify the certificate chain"
              description="Turn off for internal CAs or self-signed certificates. The certificate is still inspected and reported."
            />
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field
              label="Warning threshold (days)"
              error={errors.ssl_warning_days}
              hint="Leave blank to use the global setting."
            >
              <input
                type="number"
                min={1}
                max={365}
                className="input"
                value={form.ssl_warning_days}
                onChange={setInput('ssl_warning_days')}
              />
            </Field>
            <Field label="Critical threshold (days)" error={errors.ssl_critical_days}>
              <input
                type="number"
                min={1}
                max={180}
                className="input"
                value={form.ssl_critical_days}
                onChange={setInput('ssl_critical_days')}
              />
            </Field>
          </div>
        </Section>

        {/* ---------------------------------------- request/auth section */}
        <Section title="Request and authentication">
          <Toggle
            checked={form.follow_redirects}
            onChange={set('follow_redirects')}
            label="Follow redirects"
            disabled={!isHttp}
          />

          <Field
            label="Custom headers (JSON)"
            error={errors.custom_headers}
            hint='Non-sensitive headers only, e.g. {"X-Api-Version": "2"}. Authorization belongs below.'
          >
            <textarea
              className="input font-mono text-xs"
              rows={3}
              value={form.custom_headers}
              onChange={setInput('custom_headers')}
              placeholder="{}"
              disabled={!isHttp}
            />
          </Field>

          {['POST', 'PUT', 'PATCH'].includes(form.http_method) && isHttp ? (
            <Field label="Request body">
              <textarea
                className="input font-mono text-xs"
                rows={3}
                value={form.request_body}
                onChange={setInput('request_body')}
              />
            </Field>
          ) : null}

          <Field
            label="Expected content"
            hint="Optional. The check fails if the response body does not contain this text."
          >
            <input
              className="input"
              value={form.expected_body_substring}
              onChange={setInput('expected_body_substring')}
              placeholder='"status":"ok"'
              disabled={!isHttp}
            />
          </Field>

          <Field label="Authentication">
            <select className="input" value={form.auth_type} onChange={setInput('auth_type')}>
              {AUTH_TYPES.map((type) => (
                <option key={type.value} value={type.value}>
                  {type.label}
                </option>
              ))}
            </select>
          </Field>

          {form.auth_type !== 'none' ? (
            <div className="grid gap-3 sm:grid-cols-2">
              {form.auth_type === 'basic' ? (
                <Field label="Username" required error={errors.auth_username}>
                  <input
                    className="input"
                    value={form.auth_username}
                    onChange={setInput('auth_username')}
                    autoComplete="off"
                  />
                </Field>
              ) : null}
              {form.auth_type === 'header' ? (
                <Field label="Header name" required error={errors.auth_header_name}>
                  <input
                    className="input"
                    value={form.auth_header_name}
                    onChange={setInput('auth_header_name')}
                    placeholder="X-Api-Key"
                    autoComplete="off"
                  />
                </Field>
              ) : null}
              <Field
                label={
                  form.auth_type === 'basic'
                    ? 'Password'
                    : form.auth_type === 'bearer'
                      ? 'Token'
                      : 'Header value'
                }
                required={!isEdit || !endpoint?.has_auth_secret}
                error={errors.auth_secret}
                hint={
                  isEdit && endpoint?.has_auth_secret
                    ? `A credential is stored (${endpoint.auth_secret_hint || 'hidden'}). Leave blank to keep it.`
                    : 'Stored encrypted. It is never shown again after saving.'
                }
                className={form.auth_type === 'bearer' ? 'sm:col-span-2' : undefined}
              >
                <div className="relative">
                  <Lock
                    size={14}
                    className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400"
                  />
                  <input
                    type="password"
                    className="input pl-8 font-mono text-xs"
                    value={form.auth_secret}
                    onChange={setInput('auth_secret')}
                    placeholder={
                      isEdit && endpoint?.has_auth_secret ? '•••••• (unchanged)' : ''
                    }
                    autoComplete="new-password"
                  />
                </div>
              </Field>
            </div>
          ) : null}
        </Section>

        {/* -------------------------------------------- alerts section */}
        <Section title="Alerting thresholds">
          <Toggle
            checked={form.alerts_enabled}
            onChange={set('alerts_enabled')}
            label="Generate alerts for this endpoint"
          />
          <div className="grid gap-3 sm:grid-cols-2">
            <Field
              label="Consecutive failures before an incident"
              error={errors.failure_threshold}
              hint="Leave blank to use the global setting."
            >
              <input
                type="number"
                min={1}
                max={20}
                className="input"
                value={form.failure_threshold}
                onChange={setInput('failure_threshold')}
              />
            </Field>
            <Field
              label="Response time threshold (ms)"
              error={errors.response_time_threshold_ms}
              hint="Slower successful responses are reported as degraded."
            >
              <input
                type="number"
                min={1}
                className="input"
                value={form.response_time_threshold_ms}
                onChange={setInput('response_time_threshold_ms')}
                placeholder="2000"
              />
            </Field>
          </div>
        </Section>
      </form>
    </Modal>
  )
}
