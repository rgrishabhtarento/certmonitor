import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Bell,
  ClipboardList,
  Database,
  FileSearch,
  Plus,
  RotateCcw,
  Save,
  Send,
  Server,
  ShieldCheck,
  SlidersHorizontal,
  Trash2,
} from 'lucide-react'
import clsx from 'clsx'

import {
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  Modal,
  PageHeader,
  Spinner,
  Toggle,
} from '../components/ui'
import { settingsApi } from '../lib/api'
import { formatDateTime, formatNumber, formatRelative } from '../lib/format'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'

const CATEGORY_META = {
  monitoring: { label: 'Monitoring', icon: SlidersHorizontal },
  ssl: { label: 'SSL certificates', icon: ShieldCheck },
  alerting: { label: 'Alerting', icon: Bell },
  changes: { label: 'Change management', icon: ClipboardList },
  rca: { label: 'RCA', icon: FileSearch },
  retention: { label: 'Data retention', icon: Database },
  general: { label: 'General', icon: Server },
}

/** Settings stored as a list of strings, edited as comma-separated text. */
const STRING_LIST_SETTINGS = {
  change_approval_environments: 'production, staging',
  health_path_candidates: '/health, /healthz, /ready, /actuator/health',
}

const CHANNEL_TYPES = [
  { value: 'webhook', label: 'Generic webhook' },
  { value: 'slack', label: 'Slack' },
  { value: 'teams', label: 'Microsoft Teams' },
  { value: 'pagerduty', label: 'PagerDuty' },
  { value: 'email', label: 'E-mail (SMTP)' },
]

/** Per-provider config fields; secrets are always type=password. */
const CHANNEL_FIELDS = {
  webhook: [
    { key: 'url', label: 'Webhook URL', required: true, placeholder: 'https://hooks.example.com/monitoring' },
    { key: 'secret', label: 'Signing secret', type: 'password', hint: 'Optional. Adds an X-InfraSight-Signature HMAC header.' },
  ],
  slack: [{ key: 'webhook_url', label: 'Slack webhook URL', required: true, type: 'password' }],
  teams: [{ key: 'webhook_url', label: 'Teams webhook URL', required: true, type: 'password' }],
  pagerduty: [
    { key: 'routing_key', label: 'Events API v2 routing key', required: true, type: 'password' },
  ],
  email: [
    { key: 'host', label: 'SMTP host', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '587' },
    { key: 'from_address', label: 'From address', required: true },
    { key: 'recipients', label: 'Recipients', required: true, hint: 'Comma-separated.' },
    { key: 'username', label: 'Username' },
    { key: 'password', label: 'Password', type: 'password' },
  ],
}

export default function SettingsPage() {
  const { can } = useAuth()
  const toast = useToast()
  const canWrite = can('settings:write')
  const canManageChannels = can('notification:write')

  const [payload, setPayload] = useState(null)
  const [draft, setDraft] = useState({})
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)

  const [channels, setChannels] = useState(null)
  const [workers, setWorkers] = useState([])
  const [channelOpen, setChannelOpen] = useState(false)
  const [channelType, setChannelType] = useState('webhook')
  const [channelForm, setChannelForm] = useState({ name: '', min_severity: 'warning' })
  const [channelConfig, setChannelConfig] = useState({})
  const [channelError, setChannelError] = useState(null)
  const [channelBusy, setChannelBusy] = useState(false)
  const [testingId, setTestingId] = useState(null)
  const [confirmDelete, setConfirmDelete] = useState(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const data = await settingsApi.get()
      setPayload(data)
      setDraft({})
    } catch (err) {
      setError(err.message)
    }
  }, [])

  const loadChannels = useCallback(() => {
    settingsApi.channels().then(setChannels).catch(() => setChannels([]))
  }, [])

  useEffect(() => {
    load()
    loadChannels()
    settingsApi.workers().then(setWorkers).catch(() => {})
  }, [load, loadChannels])

  const grouped = useMemo(() => {
    const map = {}
    for (const setting of payload?.settings || []) {
      map[setting.category] = map[setting.category] || []
      map[setting.category].push(setting)
    }
    return map
  }, [payload])

  const dirtyKeys = Object.keys(draft)

  const setValue = (key, value) =>
    setDraft((current) => ({ ...current, [key]: value }))

  const save = async () => {
    if (!dirtyKeys.length) return
    setSaving(true)
    try {
      const data = await settingsApi.update(draft)
      setPayload(data)
      setDraft({})
      toast.success('Configuration saved. The worker picks it up within seconds.')
    } catch (err) {
      toast.error(err.message)
    } finally {
      setSaving(false)
    }
  }

  const renderControl = (setting) => {
    const value = draft[setting.key] !== undefined ? draft[setting.key] : setting.value
    const dirty = draft[setting.key] !== undefined

    if (setting.value_type === 'bool') {
      return (
        <Toggle
          checked={Boolean(value)}
          onChange={(next) => setValue(setting.key, next)}
          label={setting.label}
          description={setting.description}
          disabled={!canWrite || !setting.is_editable}
        />
      )
    }

    if (setting.key === 'allowed_intervals') {
      return (
        <Field label={setting.label} hint={setting.description}>
          <input
            className={clsx('input font-mono text-xs', dirty && 'ring-1 ring-brand-400')}
            value={Array.isArray(value) ? value.join(', ') : String(value)}
            disabled={!canWrite}
            onChange={(event) =>
              setValue(
                setting.key,
                event.target.value
                  .split(',')
                  .map((part) => parseInt(part.trim(), 10))
                  .filter((n) => Number.isFinite(n)),
              )
            }
          />
        </Field>
      )
    }

    // Lists of strings, edited as comma-separated text but stored as a list -
    // the API rejects a bare string here.
    if (STRING_LIST_SETTINGS[setting.key]) {
      return (
        <Field label={setting.label} hint={setting.description}>
          <input
            className={clsx('input font-mono text-xs', dirty && 'ring-1 ring-brand-400')}
            placeholder={STRING_LIST_SETTINGS[setting.key]}
            value={Array.isArray(value) ? value.join(', ') : String(value ?? '')}
            disabled={!canWrite || !setting.is_editable}
            onChange={(event) =>
              setValue(
                setting.key,
                event.target.value
                  .split(',')
                  .map((part) => part.trim())
                  .filter(Boolean),
              )
            }
          />
        </Field>
      )
    }

    if (setting.allowed_values?.length) {
      return (
        <Field label={setting.label} hint={setting.description}>
          <select
            className={clsx('input', dirty && 'ring-1 ring-brand-400')}
            value={value}
            disabled={!canWrite || !setting.is_editable}
            onChange={(event) => setValue(setting.key, Number(event.target.value))}
          >
            {setting.allowed_values.map((option) => (
              <option key={option} value={option}>
                {setting.category === 'retention'
                  ? `${option} days`
                  : option < 60
                    ? `${option} seconds`
                    : option < 3600
                      ? `${option / 60} minutes`
                      : `${option / 3600} hour${option === 3600 ? '' : 's'}`}
              </option>
            ))}
          </select>
        </Field>
      )
    }

    return (
      <Field
        label={setting.label}
        hint={setting.description}
        error={
          setting.min_value !== null &&
          value !== '' &&
          Number(value) < setting.min_value
            ? `Must be at least ${setting.min_value}`
            : undefined
        }
      >
        <input
          type={setting.value_type === 'string' ? 'text' : 'number'}
          className={clsx('input', dirty && 'ring-1 ring-brand-400')}
          value={value ?? ''}
          min={setting.min_value ?? undefined}
          max={setting.max_value ?? undefined}
          step={setting.value_type === 'float' ? '0.01' : '1'}
          disabled={!canWrite || !setting.is_editable}
          onChange={(event) =>
            setValue(
              setting.key,
              setting.value_type === 'string'
                ? event.target.value
                : event.target.value === ''
                  ? ''
                  : Number(event.target.value),
            )
          }
        />
      </Field>
    )
  }

  // ------------------------------------------------------- channels
  const openChannelForm = () => {
    setChannelType('webhook')
    setChannelForm({ name: '', min_severity: 'warning' })
    setChannelConfig({})
    setChannelError(null)
    setChannelOpen(true)
  }

  const saveChannel = async (event) => {
    event.preventDefault()
    setChannelError(null)
    setChannelBusy(true)
    try {
      await settingsApi.createChannel({
        name: channelForm.name,
        channel_type: channelType,
        is_enabled: true,
        min_severity: channelForm.min_severity,
        config: channelConfig,
      })
      toast.success(`Channel '${channelForm.name}' created.`)
      setChannelOpen(false)
      loadChannels()
    } catch (err) {
      setChannelError(err.message)
    } finally {
      setChannelBusy(false)
    }
  }

  const testChannel = async (channel) => {
    setTestingId(channel.id)
    try {
      await settingsApi.testChannel(channel.id)
      toast.success(`Test notification sent via '${channel.name}'.`)
      loadChannels()
    } catch (err) {
      toast.error(err.message)
      loadChannels()
    } finally {
      setTestingId(null)
    }
  }

  const toggleChannel = async (channel) => {
    try {
      await settingsApi.updateChannel(channel.id, { is_enabled: !channel.is_enabled })
      loadChannels()
    } catch (err) {
      toast.error(err.message)
    }
  }

  const deleteChannel = async () => {
    if (!confirmDelete) return
    try {
      await settingsApi.removeChannel(confirmDelete.id)
      toast.success('Channel deleted.')
      setConfirmDelete(null)
      loadChannels()
    } catch (err) {
      toast.error(err.message)
    }
  }

  if (!payload && !error) {
    return (
      <>
        <PageHeader title="Settings" />
        <LoadingBlock rows={8} />
      </>
    )
  }

  if (error && !payload) {
    return (
      <>
        <PageHeader title="Settings" />
        <ErrorState message={error} onRetry={load} />
      </>
    )
  }

  return (
    <>
      <PageHeader
        title="Settings"
        description="Runtime configuration. Environment variables provide the boot defaults; these values override them without a redeploy."
        actions={
          canWrite ? (
            <>
              {dirtyKeys.length ? (
                <button type="button" className="btn-secondary" onClick={() => setDraft({})}>
                  <RotateCcw size={15} /> Discard
                </button>
              ) : null}
              <button
                type="button"
                className="btn-primary"
                onClick={save}
                disabled={saving || !dirtyKeys.length}
              >
                {saving ? <Spinner size={15} className="text-white" /> : <Save size={15} />}
                Save {dirtyKeys.length ? `(${dirtyKeys.length})` : ''}
              </button>
            </>
          ) : null
        }
      />

      {!canWrite ? (
        <p className="mb-4 rounded-lg bg-slate-100 px-3 py-2 text-sm text-slate-600 dark:bg-slate-800 dark:text-slate-300">
          You have read-only access to configuration.
        </p>
      ) : null}

      <div className="space-y-4">
        {Object.entries(CATEGORY_META).map(([category, meta]) => {
          const settings = grouped[category]
          if (!settings?.length) return null
          const Icon = meta.icon
          return (
            <Card
              key={category}
              title={
                <span className="flex items-center gap-2">
                  <Icon size={15} /> {meta.label}
                </span>
              }
            >
              <div className="grid gap-4 sm:grid-cols-2">
                {settings.map((setting) => (
                  <div key={setting.key}>
                    {renderControl(setting)}
                    {setting.updated_at ? (
                      <p className="mt-1 text-[11px] text-slate-400">
                        changed {formatRelative(setting.updated_at)}
                      </p>
                    ) : null}
                  </div>
                ))}
              </div>
              {category === 'ssl' ? (
                <p className="mt-3 rounded-lg bg-brand-50 px-2.5 py-2 text-xs text-brand-800 dark:bg-brand-900/25 dark:text-brand-200">
                  Changing an SSL threshold re-grades every stored certificate
                  immediately, rather than waiting for each endpoint&apos;s next check.
                </p>
              ) : null}
            </Card>
          )
        })}

        {/* -------------------------------------------------- channels */}
        <Card
          title={
            <span className="flex items-center gap-2">
              <Bell size={15} /> Notification channels
            </span>
          }
          actions={
            canManageChannels ? (
              <button type="button" className="btn-primary btn-sm" onClick={openChannelForm}>
                <Plus size={14} /> Add channel
              </button>
            ) : null
          }
          bodyClassName="p-0"
        >
          {!channels ? (
            <div className="p-4">
              <LoadingBlock rows={3} />
            </div>
          ) : channels.length === 0 ? (
            <EmptyState
              icon={Bell}
              title="No notification channels"
              description="Alerts are still recorded and shown in the UI. Add a channel to have them delivered to Slack, Teams, PagerDuty, e-mail or a generic webhook."
              action={
                canManageChannels ? (
                  <button type="button" className="btn-primary" onClick={openChannelForm}>
                    <Plus size={15} /> Add channel
                  </button>
                ) : null
              }
            />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Channel</th>
                    <th>Type</th>
                    <th>Target</th>
                    <th>Min severity</th>
                    <th className="text-right">Delivered</th>
                    <th>Last used</th>
                    <th>Enabled</th>
                    <th className="w-20" />
                  </tr>
                </thead>
                <tbody>
                  {channels.map((channel) => (
                    <tr key={channel.id}>
                      <td className="font-medium">{channel.name}</td>
                      <td className="capitalize">{channel.channel_type}</td>
                      <td className="max-w-[14rem] truncate font-mono text-[11px] text-slate-500">
                        {/* Only the non-sensitive part of the config is ever
                            returned by the API. */}
                        {channel.config_public?.target_host ||
                          channel.config_public?.host ||
                          (channel.config_public?.routing_key_configured
                            ? 'routing key configured'
                            : '—')}
                      </td>
                      <td className="capitalize">{channel.min_severity}</td>
                      <td className="tnum text-right">
                        {formatNumber(channel.success_count)}
                        {channel.failure_count ? (
                          <span className="text-red-500"> / {channel.failure_count} failed</span>
                        ) : null}
                      </td>
                      <td className="whitespace-nowrap text-xs text-slate-500">
                        {channel.last_used_at ? formatRelative(channel.last_used_at) : 'Never'}
                        {channel.last_error ? (
                          <p
                            className="max-w-[12rem] truncate text-[11px] text-red-500"
                            title={channel.last_error}
                          >
                            {channel.last_error}
                          </p>
                        ) : null}
                      </td>
                      <td>
                        <input
                          type="checkbox"
                          className="h-4 w-4 rounded border-slate-300"
                          checked={channel.is_enabled}
                          disabled={!canManageChannels}
                          aria-label={`Toggle ${channel.name}`}
                          onChange={() => toggleChannel(channel)}
                        />
                      </td>
                      <td className="text-right">
                        {canManageChannels ? (
                          <div className="flex justify-end gap-1">
                            <button
                              type="button"
                              className="btn-ghost p-1.5"
                              title="Send a test notification"
                              onClick={() => testChannel(channel)}
                              disabled={testingId === channel.id}
                            >
                              {testingId === channel.id ? (
                                <Spinner size={15} />
                              ) : (
                                <Send size={15} />
                              )}
                            </button>
                            <button
                              type="button"
                              className="btn-ghost p-1.5 text-red-500"
                              title="Delete"
                              onClick={() => setConfirmDelete(channel)}
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
          )}
        </Card>

        {/* --------------------------------------------------- workers */}
        <Card
          title={
            <span className="flex items-center gap-2">
              <Server size={15} /> Monitoring workers
            </span>
          }
          bodyClassName="p-0"
        >
          {workers.length === 0 ? (
            <EmptyState
              icon={Server}
              title="No worker has reported in"
              description="The worker writes a heartbeat on every cycle. If this stays empty, check `docker compose logs worker`."
            />
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Worker</th>
                    <th>Host</th>
                    <th>Version</th>
                    <th>State</th>
                    <th className="text-right">Checks done</th>
                    <th className="text-right">Failed</th>
                    <th className="text-right">In flight</th>
                    <th>Started</th>
                    <th>Last heartbeat</th>
                  </tr>
                </thead>
                <tbody>
                  {workers.map((worker) => (
                    <tr key={worker.worker_id}>
                      <td className="font-mono text-[11px]">{worker.worker_id}</td>
                      <td>{worker.hostname || '—'}</td>
                      <td>{worker.version || '—'}</td>
                      <td>
                        <span
                          className={
                            worker.is_healthy
                              ? 'badge bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300'
                              : 'badge bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300'
                          }
                        >
                          {worker.is_healthy ? 'Healthy' : 'Stale'}
                        </span>
                      </td>
                      <td className="tnum text-right">
                        {formatNumber(worker.checks_completed)}
                      </td>
                      <td className="tnum text-right">{formatNumber(worker.checks_failed)}</td>
                      <td className="tnum text-right">{worker.in_flight}</td>
                      <td className="whitespace-nowrap text-xs text-slate-500">
                        {formatDateTime(worker.started_at, 'dd MMM HH:mm')}
                      </td>
                      <td className="whitespace-nowrap text-xs text-slate-500">
                        {worker.seconds_since_heartbeat.toFixed(0)}s ago
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {/* ---------------------------------------------------- storage */}
        {payload?.storage ? (
          <Card
            title={
              <span className="flex items-center gap-2">
                <Database size={15} /> Stored data
              </span>
            }
          >
            <div className="grid gap-4 sm:grid-cols-3 lg:grid-cols-6">
              {Object.entries(payload.storage).map(([key, value]) => (
                <div key={key}>
                  <p className="text-lg font-semibold">{formatNumber(value)}</p>
                  <p className="text-[11px] text-slate-500">{key.replace(/_/g, ' ')}</p>
                </div>
              ))}
            </div>
            <p className="mt-3 text-xs text-slate-500 dark:text-slate-400">
              The retention sweep runs hourly in the worker and deletes rows in bounded
              batches, so it never holds a long transaction while checks are being
              written.
            </p>
          </Card>
        ) : null}
      </div>

      {/* ----------------------------------------------- channel dialog */}
      <Modal
        open={channelOpen}
        onClose={() => setChannelOpen(false)}
        title="Add a notification channel"
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setChannelOpen(false)}>
              Cancel
            </button>
            <button
              type="submit"
              form="channel-form"
              className="btn-primary"
              disabled={channelBusy}
            >
              {channelBusy ? <Spinner size={15} className="text-white" /> : null}
              Create channel
            </button>
          </>
        }
      >
        <form id="channel-form" onSubmit={saveChannel} className="space-y-3">
          {channelError ? (
            <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/50 dark:text-red-200">
              {channelError}
            </p>
          ) : null}
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Name" required>
              <input
                className="input"
                value={channelForm.name}
                onChange={(event) =>
                  setChannelForm({ ...channelForm, name: event.target.value })
                }
                required
                maxLength={96}
                autoFocus
              />
            </Field>
            <Field label="Type" required>
              <select
                className="input"
                value={channelType}
                onChange={(event) => {
                  setChannelType(event.target.value)
                  setChannelConfig({})
                }}
              >
                {CHANNEL_TYPES.map((type) => (
                  <option key={type.value} value={type.value}>
                    {type.label}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          <Field
            label="Minimum severity"
            hint="Alerts below this severity are not delivered through this channel."
          >
            <select
              className="input"
              value={channelForm.min_severity}
              onChange={(event) =>
                setChannelForm({ ...channelForm, min_severity: event.target.value })
              }
            >
              <option value="info">Info and above</option>
              <option value="warning">Warning and above</option>
              <option value="critical">Critical only</option>
            </select>
          </Field>

          {(CHANNEL_FIELDS[channelType] || []).map((field) => (
            <Field
              key={field.key}
              label={field.label}
              required={field.required}
              hint={field.hint}
            >
              <input
                type={field.type || 'text'}
                className={clsx('input', field.type === 'password' && 'font-mono text-xs')}
                value={channelConfig[field.key] ?? ''}
                placeholder={field.placeholder}
                required={field.required}
                autoComplete={field.type === 'password' ? 'new-password' : 'off'}
                onChange={(event) =>
                  setChannelConfig({ ...channelConfig, [field.key]: event.target.value })
                }
              />
            </Field>
          ))}

          <p className="rounded-lg bg-slate-50 px-2.5 py-2 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
            The whole configuration is stored encrypted. Once saved, secrets are never
            returned by the API - only the host name, port or recipient count is shown
            back. Use the test button afterwards to confirm delivery works.
          </p>
        </form>
      </Modal>

      <ConfirmDialog
        open={Boolean(confirmDelete)}
        onClose={() => setConfirmDelete(null)}
        onConfirm={deleteChannel}
        danger
        title={`Delete '${confirmDelete?.name}'?`}
        confirmLabel="Delete channel"
        message="Alerts will still be recorded, but no longer delivered through this channel."
      />
    </>
  )
}
