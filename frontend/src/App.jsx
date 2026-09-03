import { Suspense, lazy } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'

import AppLayout from './layouts/AppLayout'
import { Spinner } from './components/ui'
import { useAuth } from './hooks/useAuth'
import ChangePassword from './pages/ChangePassword'
import Dashboard from './pages/Dashboard'
import Endpoints from './pages/Endpoints'
import Login from './pages/Login'

// Screens behind a click are loaded on demand: the dashboard and endpoint
// list are what an operator opens first, so those stay in the main bundle.
const EndpointDetail = lazy(() => import('./pages/EndpointDetail'))
const SslCertificates = lazy(() => import('./pages/SslCertificates'))
const Incidents = lazy(() => import('./pages/Incidents'))
const Alerts = lazy(() => import('./pages/Alerts'))
const Changes = lazy(() => import('./pages/Changes'))
const ChangeDetail = lazy(() => import('./pages/ChangeDetail'))
const TagsPage = lazy(() => import('./pages/TagsPage'))
const EnvironmentsPage = lazy(() => import('./pages/EnvironmentsPage'))
const ImportExport = lazy(() => import('./pages/ImportExport'))
const UsersPage = lazy(() => import('./pages/UsersPage'))
const AuditLogs = lazy(() => import('./pages/AuditLogs'))
const SettingsPage = lazy(() => import('./pages/SettingsPage'))
const NotFound = lazy(() => import('./pages/NotFound'))

function FullPageSpinner() {
  return (
    <div className="grid min-h-screen place-items-center bg-slate-50 dark:bg-slate-950">
      <Spinner size={26} />
    </div>
  )
}

function RouteFallback() {
  return (
    <div className="grid place-items-center py-20">
      <Spinner size={22} />
    </div>
  )
}

/**
 * Gate for authenticated routes.
 *
 * A user with `must_change_password` is redirected to the password screen from
 * anywhere else - the API refuses every other route in that state, so letting
 * them navigate would only produce 403s.
 */
function RequireAuth({ children }) {
  const { isAuthenticated, loading, mustChangePassword } = useAuth()
  const location = useLocation()

  if (loading) return <FullPageSpinner />
  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  if (mustChangePassword && location.pathname !== '/change-password') {
    return <Navigate to="/change-password" replace />
  }
  return children
}

/** Hides a route whose permission the signed-in role does not hold. */
function RequirePermission({ permission, children }) {
  const { can } = useAuth()
  if (!can(permission)) return <Navigate to="/" replace />
  return children
}

export default function App() {
  const { isAuthenticated, loading } = useAuth()

  if (loading) return <FullPageSpinner />

  return (
    <Routes>
      <Route
        path="/login"
        element={isAuthenticated ? <Navigate to="/" replace /> : <Login />}
      />

      <Route
        path="/change-password"
        element={
          <RequireAuth>
            <ChangePassword />
          </RequireAuth>
        }
      />

      <Route
        element={
          <RequireAuth>
            <AppLayout />
          </RequireAuth>
        }
      >
        <Route
          index
          element={
            <Suspense fallback={<RouteFallback />}>
              <Dashboard />
            </Suspense>
          }
        />
        <Route path="endpoints" element={<Endpoints />} />
        <Route
          path="endpoints/:endpointId"
          element={
            <Suspense fallback={<RouteFallback />}>
              <EndpointDetail />
            </Suspense>
          }
        />
        <Route
          path="ssl"
          element={
            <Suspense fallback={<RouteFallback />}>
              <SslCertificates />
            </Suspense>
          }
        />
        <Route
          path="incidents"
          element={
            <Suspense fallback={<RouteFallback />}>
              <Incidents />
            </Suspense>
          }
        />
        <Route
          path="alerts"
          element={
            <Suspense fallback={<RouteFallback />}>
              <Alerts />
            </Suspense>
          }
        />
        <Route
          path="changes"
          element={
            <RequirePermission permission="change:read">
              <Suspense fallback={<RouteFallback />}>
                <Changes />
              </Suspense>
            </RequirePermission>
          }
        />
        <Route
          path="changes/:changeId"
          element={
            <RequirePermission permission="change:read">
              <Suspense fallback={<RouteFallback />}>
                <ChangeDetail />
              </Suspense>
            </RequirePermission>
          }
        />
        <Route
          path="tags"
          element={
            <Suspense fallback={<RouteFallback />}>
              <TagsPage />
            </Suspense>
          }
        />
        <Route
          path="environments"
          element={
            <Suspense fallback={<RouteFallback />}>
              <EnvironmentsPage />
            </Suspense>
          }
        />
        <Route
          path="import-export"
          element={
            <RequirePermission permission="endpoint:export">
              <Suspense fallback={<RouteFallback />}>
                <ImportExport />
              </Suspense>
            </RequirePermission>
          }
        />
        <Route
          path="users"
          element={
            <RequirePermission permission="user:read">
              <Suspense fallback={<RouteFallback />}>
                <UsersPage />
              </Suspense>
            </RequirePermission>
          }
        />
        <Route
          path="audit-logs"
          element={
            <RequirePermission permission="audit:read">
              <Suspense fallback={<RouteFallback />}>
                <AuditLogs />
              </Suspense>
            </RequirePermission>
          }
        />
        <Route
          path="settings"
          element={
            <RequirePermission permission="settings:read">
              <Suspense fallback={<RouteFallback />}>
                <SettingsPage />
              </Suspense>
            </RequirePermission>
          }
        />
        <Route
          path="*"
          element={
            <Suspense fallback={<RouteFallback />}>
              <NotFound />
            </Suspense>
          }
        />
      </Route>
    </Routes>
  )
}
