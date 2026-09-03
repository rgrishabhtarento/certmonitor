import axios from 'axios'

/**
 * Single axios instance for the whole app.
 *
 * Responsibilities kept here rather than in components:
 *  - attach the access token,
 *  - transparently refresh it once on a 401 and replay the request,
 *  - normalise error payloads into `{ message, fields, status }`,
 *  - notice the "password change required" signal from the API.
 */

const TOKEN_KEY = 'certmonitor.access_token'
const REFRESH_KEY = 'certmonitor.refresh_token'
const USER_KEY = 'certmonitor.user'

export const tokenStore = {
  get access() {
    try {
      return localStorage.getItem(TOKEN_KEY)
    } catch {
      return null
    }
  },
  get refresh() {
    try {
      return localStorage.getItem(REFRESH_KEY)
    } catch {
      return null
    }
  },
  get user() {
    try {
      const raw = localStorage.getItem(USER_KEY)
      return raw ? JSON.parse(raw) : null
    } catch {
      return null
    }
  },
  save({ access_token, refresh_token, user }) {
    try {
      if (access_token) localStorage.setItem(TOKEN_KEY, access_token)
      if (refresh_token) localStorage.setItem(REFRESH_KEY, refresh_token)
      if (user) localStorage.setItem(USER_KEY, JSON.stringify(user))
    } catch {
      /* Private browsing or blocked storage: the session simply won't persist
         across reloads, which is acceptable. */
    }
  },
  clear() {
    try {
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(REFRESH_KEY)
      localStorage.removeItem(USER_KEY)
    } catch {
      /* ignore */
    }
  },
}

export const api = axios.create({
  baseURL: '/api',
  timeout: 45000,
  headers: { 'Content-Type': 'application/json' },
})

/** Called by the auth provider when the session can no longer be recovered. */
let onSessionExpired = () => {}
export function setSessionExpiredHandler(handler) {
  onSessionExpired = handler
}

let onPasswordChangeRequired = () => {}
export function setPasswordChangeHandler(handler) {
  onPasswordChangeRequired = handler
}

api.interceptors.request.use((config) => {
  const token = tokenStore.access
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Serialise concurrent refreshes: a dashboard load fires several requests at
// once, and all of them would otherwise try to refresh independently.
let refreshPromise = null

async function refreshAccessToken() {
  const refresh_token = tokenStore.refresh
  if (!refresh_token) throw new Error('no refresh token')

  if (!refreshPromise) {
    refreshPromise = axios
      .post('/api/auth/refresh', { refresh_token })
      .then((response) => {
        tokenStore.save(response.data)
        return response.data.access_token
      })
      .finally(() => {
        refreshPromise = null
      })
  }
  return refreshPromise
}

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const { response, config } = error

    if (!response) {
      return Promise.reject(
        normaliseError(error, 'Cannot reach the server. Check your connection.'),
      )
    }

    // A forced password change is a 403 with a marker header; route there
    // rather than showing a permission error the user cannot act on.
    if (
      response.status === 403 &&
      response.headers?.['x-password-change-required'] === 'true'
    ) {
      onPasswordChangeRequired()
      return Promise.reject(normaliseError(error))
    }

    const isAuthCall = (config?.url || '').includes('/auth/')
    if (response.status === 401 && !config?._retried && !isAuthCall) {
      config._retried = true
      try {
        const token = await refreshAccessToken()
        config.headers = { ...(config.headers || {}), Authorization: `Bearer ${token}` }
        return api(config)
      } catch {
        tokenStore.clear()
        onSessionExpired()
        return Promise.reject(
          normaliseError(error, 'Your session expired. Please sign in again.'),
        )
      }
    }

    return Promise.reject(normaliseError(error))
  },
)

/**
 * Messages for responses that did NOT come from the API.
 *
 * nginx answers with its own HTML page when it cannot reach the backend, so
 * there is no JSON `detail` to show. Saying "something went wrong" in that
 * case hides the one fact worth knowing - which half of the stack is down.
 */
const TRANSPORT_MESSAGES = {
  413: 'That file is too large to upload.',
  429: 'Too many requests. Wait a moment and try again.',
  502: 'The API is not responding. The backend service may be starting up, or it may have failed - check "docker compose ps" and "docker compose logs backend".',
  503: 'The API is temporarily unavailable. It may still be starting up.',
  504: 'The API took too long to respond.',
}

/** Turn any axios failure into a predictable shape for the UI. */
export function normaliseError(error, fallback) {
  const status = error?.response?.status ?? 0
  const data = error?.response?.data
  let message =
    fallback ||
    TRANSPORT_MESSAGES[status] ||
    (status ? `Unexpected response from the server (HTTP ${status}).` : 'Something went wrong.')
  let fields = null

  // An HTML body means a proxy answered, not the API - keep our own message.
  const isHtml = typeof data === 'string' && data.trimStart().startsWith('<')

  if (data && !isHtml) {
    if (typeof data.detail === 'string') {
      message = data.detail
    } else if (Array.isArray(data.detail)) {
      message = data.detail.map((d) => d.msg || String(d)).join('; ')
    }
    if (data.fields && typeof data.fields === 'object') {
      fields = data.fields
      const first = Object.entries(fields)[0]
      if (first && (!data.detail || data.detail === 'The request could not be validated.')) {
        message = `${first[0]}: ${first[1]}`
      }
    }
  }

  const normalised = new Error(message)
  normalised.status = status
  normalised.fields = fields
  normalised.requestId = error?.response?.headers?.['x-request-id']
  return normalised
}

/** Drop empty values so we never send `?search=&page=1`. */
export function cleanParams(params = {}) {
  const output = {}
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) {
      if (value.length === 0) continue
      output[key] = value
    } else {
      output[key] = value
    }
  }
  return output
}

// ------------------------------------------------------------------ calls
export const authApi = {
  login: (username, password) =>
    api.post('/auth/login', { username, password }).then((r) => r.data),
  me: () => api.get('/auth/me').then((r) => r.data),
  logout: () => api.post('/auth/logout').then((r) => r.data),
  changePassword: (current_password, new_password) =>
    api
      .post('/auth/change-password', { current_password, new_password })
      .then((r) => r.data),
  passwordPolicy: () => api.get('/auth/password-policy').then((r) => r.data),
}

export const endpointsApi = {
  list: (params) =>
    api.get('/endpoints', { params: cleanParams(params) }).then((r) => r.data),
  filters: () => api.get('/endpoints/filters').then((r) => r.data),
  get: (id) => api.get(`/endpoints/${id}`).then((r) => r.data),
  create: (payload) => api.post('/endpoints', payload).then((r) => r.data),
  update: (id, payload) => api.put(`/endpoints/${id}`, payload).then((r) => r.data),
  remove: (id) => api.delete(`/endpoints/${id}`).then((r) => r.data),
  setMonitoring: (id, payload) =>
    api.patch(`/endpoints/${id}/monitoring`, payload).then((r) => r.data),
  check: (id, persist = true) =>
    api
      .post(`/endpoints/${id}/check`, null, { params: { persist } })
      .then((r) => r.data),
  // Runs several live probes back to back, so it needs a longer ceiling than
  // the client default.
  diagnose: (id) =>
    api
      .post(`/endpoints/${id}/diagnose`, null, { timeout: 90000 })
      .then((r) => r.data),
  history: (id, params) =>
    api.get(`/endpoints/${id}/history`, { params: cleanParams(params) }).then((r) => r.data),
  stats: (id, window) =>
    api.get(`/endpoints/${id}/stats`, { params: { window } }).then((r) => r.data),
  ssl: (id) => api.get(`/endpoints/${id}/ssl`).then((r) => r.data),
  sslHistory: (id) => api.get(`/endpoints/${id}/ssl/history`).then((r) => r.data),
  bulk: (payload) => api.post('/endpoints/bulk', payload).then((r) => r.data),
}

export const dashboardApi = {
  get: (params) => api.get('/dashboard', { params: cleanParams(params) }).then((r) => r.data),
  summary: (params) =>
    api.get('/dashboard/summary', { params: cleanParams(params) }).then((r) => r.data),
  availability: (params) =>
    api.get('/dashboard/availability', { params: cleanParams(params) }).then((r) => r.data),
}

export const sslApi = {
  list: (params) => api.get('/ssl', { params: cleanParams(params) }).then((r) => r.data),
  summary: () => api.get('/ssl/summary').then((r) => r.data),
  issuers: () => api.get('/ssl/issuers').then((r) => r.data),
}

export const incidentsApi = {
  list: (params) => api.get('/incidents', { params: cleanParams(params) }).then((r) => r.data),
  get: (id) => api.get(`/incidents/${id}`).then((r) => r.data),
  update: (id, payload) => api.patch(`/incidents/${id}`, payload).then((r) => r.data),
}

export const alertsApi = {
  list: (params) => api.get('/alerts', { params: cleanParams(params) }).then((r) => r.data),
  unacknowledgedCount: () => api.get('/alerts/unacknowledged/count').then((r) => r.data),
  acknowledge: (alert_ids) =>
    api.post('/alerts/acknowledge', { alert_ids }).then((r) => r.data),
  remove: (id) => api.delete(`/alerts/${id}`).then((r) => r.data),
}

export const taxonomyApi = {
  tags: () => api.get('/tags').then((r) => r.data),
  createTag: (payload) => api.post('/tags', payload).then((r) => r.data),
  updateTag: (id, payload) => api.put(`/tags/${id}`, payload).then((r) => r.data),
  removeTag: (id, force) =>
    api.delete(`/tags/${id}`, { params: { force } }).then((r) => r.data),
  environments: () => api.get('/environments').then((r) => r.data),
  createEnvironment: (payload) => api.post('/environments', payload).then((r) => r.data),
  updateEnvironment: (id, payload) =>
    api.put(`/environments/${id}`, payload).then((r) => r.data),
  removeEnvironment: (id, force) =>
    api.delete(`/environments/${id}`, { params: { force } }).then((r) => r.data),
}

export const usersApi = {
  list: (params) => api.get('/users', { params: cleanParams(params) }).then((r) => r.data),
  roles: () => api.get('/users/roles').then((r) => r.data),
  get: (id) => api.get(`/users/${id}`).then((r) => r.data),
  create: (payload) => api.post('/users', payload).then((r) => r.data),
  update: (id, payload) => api.put(`/users/${id}`, payload).then((r) => r.data),
  resetPassword: (id, payload) =>
    api.post(`/users/${id}/reset-password`, payload).then((r) => r.data),
  remove: (id) => api.delete(`/users/${id}`).then((r) => r.data),
}

export const settingsApi = {
  get: () => api.get('/settings').then((r) => r.data),
  update: (updates) => api.put('/settings', { updates }).then((r) => r.data),
  alertOptions: () => api.get('/settings/alert-options').then((r) => r.data),
  auditLogs: (params) =>
    api.get('/audit-logs', { params: cleanParams(params) }).then((r) => r.data),
  auditActions: () => api.get('/audit-logs/actions').then((r) => r.data),
  channels: () => api.get('/notification-channels').then((r) => r.data),
  createChannel: (payload) => api.post('/notification-channels', payload).then((r) => r.data),
  updateChannel: (id, payload) =>
    api.put(`/notification-channels/${id}`, payload).then((r) => r.data),
  testChannel: (id) => api.post(`/notification-channels/${id}/test`).then((r) => r.data),
  removeChannel: (id) => api.delete(`/notification-channels/${id}`).then((r) => r.data),
  workers: () => api.get('/workers').then((r) => r.data),
}

export const importExportApi = {
  preview: (file) => {
    const form = new FormData()
    form.append('file', file)
    return api
      .post('/import', form, { headers: { 'Content-Type': 'multipart/form-data' } })
      .then((r) => r.data)
  },
  confirm: (token, row_numbers) =>
    api.post('/import/confirm', { token, row_numbers }).then((r) => r.data),
  templateUrl: '/api/import/template',
  exportUrl: (params) => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(cleanParams(params))) {
      if (Array.isArray(value)) value.forEach((v) => query.append(key, v))
      else query.append(key, value)
    }
    return `/api/export?${query.toString()}`
  },
}

/**
 * Download a file through axios so the Authorization header is sent.
 * A plain <a href> would omit the bearer token and get a 401.
 */
export async function downloadFile(url, filename) {
  const response = await api.get(url.replace(/^\/api/, ''), { responseType: 'blob' })
  const blobUrl = URL.createObjectURL(response.data)
  const link = document.createElement('a')
  link.href = blobUrl
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(blobUrl)
}

export const healthApi = {
  health: () => axios.get('/health').then((r) => r.data),
}
