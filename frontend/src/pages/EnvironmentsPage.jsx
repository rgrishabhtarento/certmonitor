import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Globe, Pencil, Plus, Trash2 } from 'lucide-react'

import {
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
import { taxonomyApi } from '../lib/api'
import { formatDate } from '../lib/format'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'

const EMPTY = {
  name: '',
  display_name: '',
  description: '',
  color: '#2563eb',
  sort_order: 100,
  is_active: true,
}

export default function EnvironmentsPage() {
  const { can } = useAuth()
  const toast = useToast()
  const canWrite = can('environment:write')

  const [environments, setEnvironments] = useState(null)
  const [error, setError] = useState(null)
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [form, setForm] = useState(EMPTY)
  const [fieldError, setFieldError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(null)
  const [deleting, setDeleting] = useState(false)

  const load = useCallback(async () => {
    setError(null)
    try {
      setEnvironments(await taxonomyApi.environments())
    } catch (err) {
      setError(err.message)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const openForm = (environment) => {
    setEditing(environment)
    setForm(
      environment
        ? {
            name: environment.name,
            display_name: environment.display_name || '',
            description: environment.description || '',
            color: environment.color || '#2563eb',
            sort_order: environment.sort_order ?? 100,
            is_active: environment.is_active,
          }
        : EMPTY,
    )
    setFieldError(null)
    setFormOpen(true)
  }

  const save = async (event) => {
    event.preventDefault()
    const cleaned = form.name.trim().toLowerCase()
    if (!cleaned) {
      setFieldError('A name is required.')
      return
    }
    setBusy(true)
    try {
      const payload = {
        name: cleaned,
        display_name: form.display_name || null,
        description: form.description || null,
        color: form.color || null,
        sort_order: Number(form.sort_order) || 100,
        is_active: form.is_active,
      }
      if (editing) await taxonomyApi.updateEnvironment(editing.id, payload)
      else await taxonomyApi.createEnvironment(payload)
      toast.success(editing ? 'Environment updated.' : `Environment '${cleaned}' created.`)
      setFormOpen(false)
      load()
    } catch (err) {
      setFieldError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const doDelete = async (force = false) => {
    if (!confirmDelete) return
    setDeleting(true)
    try {
      await taxonomyApi.removeEnvironment(confirmDelete.id, force)
      toast.success(`Environment '${confirmDelete.name}' deleted.`)
      setConfirmDelete(null)
      load()
    } catch (err) {
      if (err.status === 409 && !force) {
        setConfirmDelete({ ...confirmDelete, conflict: err.message })
      } else {
        toast.error(err.message)
      }
    } finally {
      setDeleting(false)
    }
  }

  const set = (key) => (event) =>
    setForm((current) => ({ ...current, [key]: event.target.value }))

  return (
    <>
      <PageHeader
        title="Environments"
        description="Environments are rows, not a fixed list - add whatever your infrastructure actually uses."
        actions={
          canWrite ? (
            <button type="button" className="btn-primary" onClick={() => openForm(null)}>
              <Plus size={16} /> New environment
            </button>
          ) : null
        }
      />

      <div className="card">
        {!environments && !error ? (
          <div className="p-4">
            <LoadingBlock rows={5} />
          </div>
        ) : error ? (
          <div className="p-4">
            <ErrorState message={error} onRetry={load} />
          </div>
        ) : environments.length === 0 ? (
          <EmptyState
            icon={Globe}
            title="No environments defined"
            description="Development, Testing, Staging and Production are seeded on first start; add your own as needed."
            action={
              canWrite ? (
                <button type="button" className="btn-primary" onClick={() => openForm(null)}>
                  <Plus size={16} /> New environment
                </button>
              ) : null
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Environment</th>
                  <th>Description</th>
                  <th className="text-right">Endpoints</th>
                  <th className="text-right">Order</th>
                  <th>Active</th>
                  <th>Created</th>
                  <th className="w-20" />
                </tr>
              </thead>
              <tbody>
                {environments.map((environment) => (
                  <tr key={environment.id}>
                    <td>
                      <div className="flex items-center gap-2">
                        <span
                          className="h-2.5 w-2.5 shrink-0 rounded-full"
                          style={{ backgroundColor: environment.color || '#94a3b8' }}
                          aria-hidden="true"
                        />
                        <div>
                          <p className="font-medium">
                            {environment.display_name || environment.name}
                          </p>
                          <p className="font-mono text-[11px] text-slate-400">
                            {environment.name}
                          </p>
                        </div>
                      </div>
                    </td>
                    <td className="text-slate-600 dark:text-slate-300">
                      {environment.description || '—'}
                    </td>
                    <td className="tnum text-right">
                      {environment.endpoint_count > 0 ? (
                        <Link
                          to={`/endpoints?environment=${environment.id}`}
                          className="text-brand-600 hover:underline dark:text-brand-400"
                        >
                          {environment.endpoint_count}
                        </Link>
                      ) : (
                        0
                      )}
                    </td>
                    <td className="tnum text-right text-slate-500">
                      {environment.sort_order}
                    </td>
                    <td>
                      {environment.is_active ? (
                        <span className="badge bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300">
                          Active
                        </span>
                      ) : (
                        <span className="badge bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400">
                          Inactive
                        </span>
                      )}
                    </td>
                    <td className="whitespace-nowrap text-slate-500">
                      {formatDate(environment.created_at)}
                    </td>
                    <td className="text-right">
                      {canWrite ? (
                        <div className="flex justify-end gap-1">
                          <button
                            type="button"
                            className="btn-ghost p-1.5"
                            title="Edit"
                            onClick={() => openForm(environment)}
                          >
                            <Pencil size={15} />
                          </button>
                          <button
                            type="button"
                            className="btn-ghost p-1.5 text-red-500"
                            title="Delete"
                            onClick={() => setConfirmDelete(environment)}
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
      </div>

      <Modal
        open={formOpen}
        onClose={() => setFormOpen(false)}
        title={editing ? `Edit '${editing.name}'` : 'New environment'}
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setFormOpen(false)}>
              Cancel
            </button>
            <button type="submit" form="env-form" className="btn-primary" disabled={busy}>
              {busy ? <Spinner size={15} className="text-white" /> : null}
              {editing ? 'Save' : 'Create'}
            </button>
          </>
        }
      >
        <form id="env-form" onSubmit={save} className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field
              label="Name"
              required
              error={fieldError}
              hint="Lower-cased identifier, e.g. production."
            >
              <input
                className="input"
                value={form.name}
                onChange={set('name')}
                maxLength={64}
                autoFocus
              />
            </Field>
            <Field label="Display name" hint="Shown in the UI.">
              <input
                className="input"
                value={form.display_name}
                onChange={set('display_name')}
                maxLength={64}
                placeholder="Production"
              />
            </Field>
          </div>
          <Field label="Description">
            <input
              className="input"
              value={form.description}
              onChange={set('description')}
              maxLength={255}
            />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Colour">
              <input
                type="color"
                className="input h-10 p-1"
                value={form.color}
                onChange={set('color')}
              />
            </Field>
            <Field label="Sort order" hint="Lower values appear first.">
              <input
                type="number"
                min={0}
                max={10000}
                className="input"
                value={form.sort_order}
                onChange={set('sort_order')}
              />
            </Field>
          </div>
          <Toggle
            checked={form.is_active}
            onChange={(value) => setForm((current) => ({ ...current, is_active: value }))}
            label="Active"
            description="Inactive environments stay assignable but are de-emphasised in filters."
          />
        </form>
      </Modal>

      <ConfirmDialog
        open={Boolean(confirmDelete)}
        onClose={() => setConfirmDelete(null)}
        onConfirm={() => doDelete(Boolean(confirmDelete?.conflict))}
        busy={deleting}
        danger
        title={`Delete '${confirmDelete?.name}'?`}
        confirmLabel={confirmDelete?.conflict ? 'Delete anyway' : 'Delete environment'}
        message={
          confirmDelete?.conflict
            ? `${confirmDelete.conflict} The endpoints themselves are not deleted.`
            : 'The environment will be removed. Endpoints assigned to it become unassigned.'
        }
      />
    </>
  )
}
