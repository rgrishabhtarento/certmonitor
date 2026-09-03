import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Pencil, Plus, Tags, Trash2 } from 'lucide-react'

import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  Modal,
  PageHeader,
  Spinner,
  TagChip,
} from '../components/ui'
import { taxonomyApi } from '../lib/api'
import { formatDate } from '../lib/format'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'

export default function TagsPage() {
  const { can } = useAuth()
  const toast = useToast()
  const canWrite = can('tag:write')

  const [tags, setTags] = useState(null)
  const [error, setError] = useState(null)
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [fieldError, setFieldError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(null)
  const [deleting, setDeleting] = useState(false)

  const load = useCallback(async () => {
    setError(null)
    try {
      setTags(await taxonomyApi.tags())
    } catch (err) {
      setError(err.message)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const openForm = (tag) => {
    setEditing(tag)
    setName(tag?.name || '')
    setDescription(tag?.description || '')
    setFieldError(null)
    setFormOpen(true)
  }

  const save = async (event) => {
    event.preventDefault()
    const cleaned = name.trim().toLowerCase()
    if (!cleaned) {
      setFieldError('A name is required.')
      return
    }
    setBusy(true)
    try {
      const payload = { name: cleaned, description: description || null }
      if (editing) await taxonomyApi.updateTag(editing.id, payload)
      else await taxonomyApi.createTag(payload)
      toast.success(editing ? 'Tag updated.' : `Tag '${cleaned}' created.`)
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
      await taxonomyApi.removeTag(confirmDelete.id, force)
      toast.success(`Tag '${confirmDelete.name}' deleted.`)
      setConfirmDelete(null)
      load()
    } catch (err) {
      // A tag still applied to endpoints is protected; offer the forced path
      // rather than making the user guess.
      if (err.status === 409 && !force) {
        setConfirmDelete({ ...confirmDelete, conflict: err.message })
      } else {
        toast.error(err.message)
      }
    } finally {
      setDeleting(false)
    }
  }

  return (
    <>
      <PageHeader
        title="Tags"
        description="Free-form labels for grouping endpoints. New tags can also be created inline when editing an endpoint."
        actions={
          canWrite ? (
            <button type="button" className="btn-primary" onClick={() => openForm(null)}>
              <Plus size={16} /> New tag
            </button>
          ) : null
        }
      />

      <div className="card">
        {!tags && !error ? (
          <div className="p-4">
            <LoadingBlock rows={5} />
          </div>
        ) : error ? (
          <div className="p-4">
            <ErrorState message={error} onRetry={load} />
          </div>
        ) : tags.length === 0 ? (
          <EmptyState
            icon={Tags}
            title="No tags yet"
            description="Tags such as production, backend, critical or translation make it easy to slice the dashboard."
            action={
              canWrite ? (
                <button type="button" className="btn-primary" onClick={() => openForm(null)}>
                  <Plus size={16} /> New tag
                </button>
              ) : null
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Tag</th>
                  <th>Description</th>
                  <th className="text-right">Endpoints</th>
                  <th>Created</th>
                  <th className="w-20" />
                </tr>
              </thead>
              <tbody>
                {tags.map((tag) => (
                  <tr key={tag.id}>
                    <td>
                      <TagChip name={tag.name} />
                    </td>
                    <td className="text-slate-600 dark:text-slate-300">
                      {tag.description || '—'}
                    </td>
                    <td className="tnum text-right">
                      {tag.endpoint_count > 0 ? (
                        <Link
                          to={`/endpoints?tag=${tag.id}`}
                          className="text-brand-600 hover:underline dark:text-brand-400"
                        >
                          {tag.endpoint_count}
                        </Link>
                      ) : (
                        0
                      )}
                    </td>
                    <td className="whitespace-nowrap text-slate-500">
                      {formatDate(tag.created_at)}
                    </td>
                    <td className="text-right">
                      {canWrite ? (
                        <div className="flex justify-end gap-1">
                          <button
                            type="button"
                            className="btn-ghost p-1.5"
                            title="Edit"
                            onClick={() => openForm(tag)}
                          >
                            <Pencil size={15} />
                          </button>
                          <button
                            type="button"
                            className="btn-ghost p-1.5 text-red-500"
                            title="Delete"
                            onClick={() => setConfirmDelete(tag)}
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
        title={editing ? `Edit '${editing.name}'` : 'New tag'}
        size="sm"
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setFormOpen(false)}>
              Cancel
            </button>
            <button type="submit" form="tag-form" className="btn-primary" disabled={busy}>
              {busy ? <Spinner size={15} className="text-white" /> : null}
              {editing ? 'Save' : 'Create'}
            </button>
          </>
        }
      >
        <form id="tag-form" onSubmit={save} className="space-y-3">
          <Field
            label="Name"
            required
            error={fieldError}
            hint="Lower-cased automatically. Commas, semicolons and pipes are not allowed."
          >
            <input
              className="input"
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={64}
              autoFocus
            />
          </Field>
          <Field label="Description">
            <input
              className="input"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              maxLength={255}
            />
          </Field>
        </form>
      </Modal>

      <ConfirmDialog
        open={Boolean(confirmDelete)}
        onClose={() => setConfirmDelete(null)}
        onConfirm={() => doDelete(Boolean(confirmDelete?.conflict))}
        busy={deleting}
        danger
        title={`Delete '${confirmDelete?.name}'?`}
        confirmLabel={confirmDelete?.conflict ? 'Delete anyway' : 'Delete tag'}
        message={
          confirmDelete?.conflict
            ? `${confirmDelete.conflict} Deleting it will remove the tag from those endpoints.`
            : 'The tag will be removed. Endpoints keep all of their other tags.'
        }
      />
    </>
  )
}
