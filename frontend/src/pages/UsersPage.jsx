import { useCallback, useEffect, useState } from 'react'
import { KeyRound, Plus, ShieldCheck, Trash2, Unlock, UserCog, Users } from 'lucide-react'

import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  Modal,
  PageHeader,
  Pagination,
  SearchInput,
  Spinner,
  Toggle,
} from '../components/ui'
import { usersApi } from '../lib/api'
import { formatDateTime, formatRelative } from '../lib/format'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'

const ROLE_DESCRIPTIONS = {
  admin: 'Full access: endpoints, users, configuration and alerts.',
  viewer: 'Read-only: dashboards, endpoint health, SSL and history.',
}

export default function UsersPage() {
  const { user: me, can } = useAuth()
  const toast = useToast()
  const canManage = can('user:write')

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [search, setSearch] = useState('')
  const [roleFilter, setRoleFilter] = useState('')
  const [activeFilter, setActiveFilter] = useState('')

  const [data, setData] = useState(null)
  const [roles, setRoles] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  const [createOpen, setCreateOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [resetting, setResetting] = useState(null)
  const [confirmDelete, setConfirmDelete] = useState(null)
  const [busy, setBusy] = useState(false)

  const [form, setForm] = useState({
    username: '',
    password: '',
    role: 'viewer',
    email: '',
    full_name: '',
    team: '',
    is_active: true,
    must_change_password: true,
  })
  const [formError, setFormError] = useState(null)
  const [newPassword, setNewPassword] = useState('')
  const [forceChange, setForceChange] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const payload = await usersApi.list({
        page,
        page_size: pageSize,
        search,
        role: roleFilter || undefined,
        is_active: activeFilter === '' ? undefined : activeFilter === 'true',
      })
      setData(payload)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [page, pageSize, search, roleFilter, activeFilter])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    usersApi.roles().then(setRoles).catch(() => {})
  }, [])

  useEffect(() => {
    setPage(1)
  }, [search, roleFilter, activeFilter, pageSize])

  const createUser = async (event) => {
    event.preventDefault()
    setFormError(null)
    setBusy(true)
    try {
      await usersApi.create({
        ...form,
        email: form.email || null,
        full_name: form.full_name || null,
        team: form.team || null,
      })
      toast.success(`User '${form.username}' created.`)
      setCreateOpen(false)
      setForm({
        username: '',
        password: '',
        role: 'viewer',
        email: '',
        full_name: '',
    team: '',
        is_active: true,
        must_change_password: true,
      })
      load()
    } catch (err) {
      setFormError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const updateUser = async (payload) => {
    if (!editing) return
    setBusy(true)
    setFormError(null)
    try {
      await usersApi.update(editing.id, payload)
      toast.success('User updated.')
      setEditing(null)
      load()
    } catch (err) {
      setFormError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const resetPassword = async (event) => {
    event.preventDefault()
    if (!resetting) return
    setBusy(true)
    setFormError(null)
    try {
      await usersApi.resetPassword(resetting.id, {
        new_password: newPassword,
        force_change: forceChange,
      })
      toast.success(
        `Password reset for '${resetting.username}'. Their existing sessions were signed out.`,
      )
      setResetting(null)
      setNewPassword('')
      load()
    } catch (err) {
      setFormError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const deleteUser = async () => {
    if (!confirmDelete) return
    setBusy(true)
    try {
      await usersApi.remove(confirmDelete.id)
      toast.success(`User '${confirmDelete.username}' deleted.`)
      setConfirmDelete(null)
      load()
    } catch (err) {
      toast.error(err.message)
    } finally {
      setBusy(false)
    }
  }

  const quickToggle = async (target, changes, label) => {
    try {
      await usersApi.update(target.id, changes)
      toast.success(label)
      load()
    } catch (err) {
      toast.error(err.message)
    }
  }

  /**
   * Let someone sign in again immediately.
   *
   * Clears both throttles at once - the account lockout on the user row and
   * the login rate limiter keyed on their username and recent addresses. The
   * confirmation names which one was actually in the way, because "unlocked"
   * is unhelpfully vague when there were two things it could have been.
   */
  const resetSignInLimits = async (target) => {
    try {
      const result = await usersApi.resetSignInLimits(target.id)
      const cleared = [
        result.was_locked ? 'lockout' : null,
        result.failed_attempts_cleared
          ? `${result.failed_attempts_cleared} failed attempt(s)`
          : null,
        result.addresses_cleared
          ? `rate limit on ${result.addresses_cleared} address(es)`
          : 'login rate limit',
      ].filter(Boolean)
      toast.success(`${result.detail} Cleared: ${cleared.join(', ')}.`)
      load()
    } catch (err) {
      toast.error(err.message)
    }
  }

  const items = data?.items || []

  return (
    <>
      <PageHeader
        title="Users"
        description={
          data ? `${data.meta.total} account(s)` : 'Accounts, roles and access'
        }
        actions={
          canManage ? (
            <button
              type="button"
              className="btn-primary"
              onClick={() => {
                setFormError(null)
                setCreateOpen(true)
              }}
            >
              <Plus size={16} /> New user
            </button>
          ) : null
        }
      />

      {/* Role reference so an admin can see what a role actually grants. */}
      {roles.length ? (
        <div className="mb-4 grid gap-3 sm:grid-cols-2">
          {roles.map((role) => (
            <div key={role.id} className="card p-3">
              <div className="flex items-center gap-2">
                <ShieldCheck size={16} className="text-brand-600 dark:text-brand-400" />
                <p className="font-medium capitalize">{role.name}</p>
                <span className="tnum ml-auto text-xs text-slate-500">
                  {role.user_count} user(s)
                </span>
              </div>
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                {ROLE_DESCRIPTIONS[role.name] || role.description}
              </p>
              <p className="mt-1.5 text-[11px] text-slate-400">
                {role.permissions.length} permission(s)
              </p>
            </div>
          ))}
        </div>
      ) : null}

      <div className="card mb-4 p-3">
        <div className="grid gap-3 sm:grid-cols-3">
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search username, e-mail or name…"
          />
          <select
            className="input"
            value={roleFilter}
            onChange={(event) => setRoleFilter(event.target.value)}
            aria-label="Filter by role"
          >
            <option value="">All roles</option>
            {roles.map((role) => (
              <option key={role.id} value={role.name}>
                {role.name}
              </option>
            ))}
          </select>
          <select
            className="input"
            value={activeFilter}
            onChange={(event) => setActiveFilter(event.target.value)}
            aria-label="Filter by account status"
          >
            <option value="">Any status</option>
            <option value="true">Enabled</option>
            <option value="false">Disabled</option>
          </select>
        </div>
      </div>

      <div className="card">
        {loading && !data ? (
          <div className="p-4">
            <LoadingBlock rows={5} />
          </div>
        ) : error && !data ? (
          <div className="p-4">
            <ErrorState message={error} onRetry={load} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState icon={Users} title="No users match" />
        ) : (
          <>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>User</th>
                    <th>Role</th>
                    <th>Status</th>
                    <th>Last login</th>
                    <th>Created</th>
                    <th className="w-28" />
                  </tr>
                </thead>
                <tbody>
                  {items.map((row) => (
                    <tr key={row.id}>
                      <td>
                        <div className="flex items-center gap-2">
                          <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-slate-200 text-[11px] font-semibold uppercase text-slate-700 dark:bg-slate-700 dark:text-slate-200">
                            {row.username.slice(0, 2)}
                          </span>
                          <div className="min-w-0">
                            <p className="font-medium">
                              {row.username}
                              {row.id === me?.id ? (
                                <span className="chip ml-1">you</span>
                              ) : null}
                            </p>
                            <p className="truncate text-[11px] text-slate-400">
                              {row.full_name || row.email || '—'}
                            </p>
                          </div>
                        </div>
                      </td>
                      <td>
                        <span className="badge bg-slate-100 capitalize text-slate-700 dark:bg-slate-800 dark:text-slate-300">
                          {row.role}
                        </span>
                      </td>
                      <td>
                        <div className="flex flex-wrap gap-1">
                          {row.is_active ? (
                            <span className="badge bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300">
                              Enabled
                            </span>
                          ) : (
                            <span className="badge bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400">
                              Disabled
                            </span>
                          )}
                          {row.is_locked ? (
                            <span className="badge bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300">
                              Locked
                            </span>
                          ) : null}
                          {row.must_change_password ? (
                            <span className="badge bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
                              Must change password
                            </span>
                          ) : null}
                        </div>
                        {row.failed_login_attempts > 0 ? (
                          <p className="tnum mt-0.5 text-[11px] text-slate-400">
                            {row.failed_login_attempts} failed attempt(s)
                          </p>
                        ) : null}
                      </td>
                      <td className="whitespace-nowrap">
                        {row.last_login_at ? (
                          <>
                            {formatRelative(row.last_login_at)}
                            <p className="text-[11px] text-slate-400">
                              {row.last_login_ip || ''}
                            </p>
                          </>
                        ) : (
                          <span className="text-slate-400">Never</span>
                        )}
                      </td>
                      <td className="whitespace-nowrap text-slate-500">
                        {formatDateTime(row.created_at, 'dd MMM yyyy')}
                      </td>
                      <td className="text-right">
                        {canManage ? (
                          <div className="flex justify-end gap-1">
                            {/* Shown as soon as anything is blocking them,
                                not only once they are fully locked out. The
                                rate limiter trips well before the lockout
                                does, and waiting for the lockout meant an
                                administrator could watch someone be refused
                                and have nothing to press. */}
                            {row.is_locked || (row.failed_login_attempts || 0) > 0 ? (
                              <button
                                type="button"
                                className="btn-ghost p-1.5 text-amber-600 dark:text-amber-400"
                                title={
                                  row.is_locked
                                    ? 'Clear the lockout and login rate limit'
                                    : `Clear ${row.failed_login_attempts} failed attempt(s) and the login rate limit`
                                }
                                onClick={() => resetSignInLimits(row)}
                              >
                                <Unlock size={15} />
                              </button>
                            ) : null}
                            <button
                              type="button"
                              className="btn-ghost p-1.5"
                              title="Edit"
                              onClick={() => {
                                setFormError(null)
                                setEditing(row)
                              }}
                            >
                              <UserCog size={15} />
                            </button>
                            <button
                              type="button"
                              className="btn-ghost p-1.5"
                              title="Reset password"
                              onClick={() => {
                                setFormError(null)
                                setNewPassword('')
                                setForceChange(true)
                                setResetting(row)
                              }}
                            >
                              <KeyRound size={15} />
                            </button>
                            <button
                              type="button"
                              className="btn-ghost p-1.5 text-red-500 disabled:opacity-30"
                              title={
                                row.id === me?.id
                                  ? 'You cannot delete your own account'
                                  : 'Delete'
                              }
                              disabled={row.id === me?.id}
                              onClick={() => setConfirmDelete(row)}
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
            <Pagination
              meta={data.meta}
              onPageChange={setPage}
              onPageSizeChange={setPageSize}
            />
          </>
        )}
      </div>

      {/* ------------------------------------------------- create user */}
      <Modal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="New user"
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setCreateOpen(false)}>
              Cancel
            </button>
            <button type="submit" form="user-form" className="btn-primary" disabled={busy}>
              {busy ? <Spinner size={15} className="text-white" /> : null}
              Create user
            </button>
          </>
        }
      >
        <form id="user-form" onSubmit={createUser} className="space-y-3">
          {formError ? (
            <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/50 dark:text-red-200">
              {formError}
            </p>
          ) : null}
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Username" required hint="Letters, digits, dots, underscores, hyphens.">
              <input
                className="input"
                value={form.username}
                onChange={(event) => setForm({ ...form, username: event.target.value })}
                required
                minLength={3}
                maxLength={64}
                autoFocus
              />
            </Field>
            <Field label="Role" required>
              <select
                className="input"
                value={form.role}
                onChange={(event) => setForm({ ...form, role: event.target.value })}
              >
                {roles.map((role) => (
                  <option key={role.id} value={role.name}>
                    {role.name}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Full name">
              <input
                className="input"
                value={form.full_name}
                onChange={(event) => setForm({ ...form, full_name: event.target.value })}
                maxLength={128}
              />
            </Field>
            <Field label="Team" hint="Free text, e.g. DevOps. Used for RCA ownership.">
              <input
                className="input"
                value={form.team}
                onChange={(event) => setForm({ ...form, team: event.target.value })}
                maxLength={64}
                placeholder="DevOps"
              />
            </Field>
            <Field label="Team" hint="Free text, e.g. DevOps. Used for RCA ownership.">
              <input
                className="input"
                value={form.team}
                onChange={(event) => setForm({ ...form, team: event.target.value })}
                maxLength={64}
                placeholder="DevOps"
              />
            </Field>
            <Field label="E-mail">
              <input
                type="email"
                className="input"
                value={form.email}
                onChange={(event) => setForm({ ...form, email: event.target.value })}
              />
            </Field>
          </div>
          <Field
            label="Temporary password"
            required
            hint="At least 10 characters with upper case, lower case, a digit and a symbol."
          >
            <input
              type="password"
              className="input"
              value={form.password}
              onChange={(event) => setForm({ ...form, password: event.target.value })}
              required
              autoComplete="new-password"
            />
          </Field>
          <Toggle
            checked={form.must_change_password}
            onChange={(value) => setForm({ ...form, must_change_password: value })}
            label="Require a password change at first sign-in"
            description="Recommended: the password you set here is known to you, not just to them."
          />
          <Toggle
            checked={form.is_active}
            onChange={(value) => setForm({ ...form, is_active: value })}
            label="Account enabled"
          />
        </form>
      </Modal>

      {/* --------------------------------------------------- edit user */}
      <Modal
        open={Boolean(editing)}
        onClose={() => setEditing(null)}
        title={editing ? `Edit ${editing.username}` : ''}
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setEditing(null)}>
              Cancel
            </button>
            <button
              type="submit"
              form="edit-user-form"
              className="btn-primary"
              disabled={busy}
            >
              {busy ? <Spinner size={15} className="text-white" /> : null}
              Save changes
            </button>
          </>
        }
      >
        {editing ? (
          <form
            id="edit-user-form"
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault()
              const formData = new FormData(event.currentTarget)
              updateUser({
                role: formData.get('role'),
                email: formData.get('email') || null,
                full_name: formData.get('full_name') || null,
                team: formData.get('team') || null,
                is_active: formData.get('is_active') === 'on',
                must_change_password: formData.get('must_change_password') === 'on',
              })
            }}
          >
            {formError ? (
              <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/50 dark:text-red-200">
                {formError}
              </p>
            ) : null}
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Full name">
                <input
                  name="full_name"
                  className="input"
                  defaultValue={editing.full_name || ''}
                  maxLength={128}
                />
              </Field>
              <Field label="Team" hint="Free text, e.g. DevOps. Used for RCA ownership.">
                <input
                  name="team"
                  className="input"
                  defaultValue={editing.team || ''}
                  maxLength={64}
                  placeholder="DevOps"
                />
              </Field>
              <Field label="E-mail">
                <input
                  name="email"
                  type="email"
                  className="input"
                  defaultValue={editing.email || ''}
                />
              </Field>
            </div>
            <Field
              label="Role"
              hint={
                editing.id === me?.id
                  ? 'You cannot remove your own administrator role.'
                  : 'A role change invalidates that user’s current sessions.'
              }
            >
              <select
                name="role"
                className="input"
                defaultValue={editing.role}
                disabled={editing.id === me?.id}
              >
                {roles.map((role) => (
                  <option key={role.id} value={role.name}>
                    {role.name}
                  </option>
                ))}
              </select>
            </Field>
            <label className="flex items-center gap-2 text-sm">
              <input
                name="is_active"
                type="checkbox"
                className="h-4 w-4 rounded border-slate-300"
                defaultChecked={editing.is_active}
                disabled={editing.id === me?.id}
              />
              Account enabled
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                name="must_change_password"
                type="checkbox"
                className="h-4 w-4 rounded border-slate-300"
                defaultChecked={editing.must_change_password}
              />
              Require a password change at next sign-in
            </label>
          </form>
        ) : null}
      </Modal>

      {/* ---------------------------------------------- reset password */}
      <Modal
        open={Boolean(resetting)}
        onClose={() => setResetting(null)}
        title={resetting ? `Reset password for ${resetting.username}` : ''}
        size="sm"
        footer={
          <>
            <button type="button" className="btn-secondary" onClick={() => setResetting(null)}>
              Cancel
            </button>
            <button type="submit" form="reset-form" className="btn-primary" disabled={busy}>
              {busy ? <Spinner size={15} className="text-white" /> : null}
              Reset password
            </button>
          </>
        }
      >
        <form id="reset-form" onSubmit={resetPassword} className="space-y-3">
          {formError ? (
            <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/50 dark:text-red-200">
              {formError}
            </p>
          ) : null}
          <Field
            label="New password"
            required
            hint="At least 10 characters with upper case, lower case, a digit and a symbol."
          >
            <input
              type="password"
              className="input"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              required
              autoComplete="new-password"
              autoFocus
            />
          </Field>
          <Toggle
            checked={forceChange}
            onChange={setForceChange}
            label="Require a change at next sign-in"
          />
          <p className="rounded-lg bg-amber-50 px-2.5 py-2 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
            Every session this user currently holds will be signed out immediately.
          </p>
        </form>
      </Modal>

      <ConfirmDialog
        open={Boolean(confirmDelete)}
        onClose={() => setConfirmDelete(null)}
        onConfirm={deleteUser}
        busy={busy}
        danger
        title={`Delete ${confirmDelete?.username}?`}
        confirmLabel="Delete user"
        message="The account is removed. Their audit log entries are kept, attributed to the username."
      />
    </>
  )
}
