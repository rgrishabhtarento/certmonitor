import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import {
  Activity,
  Bell,
  ChevronDown,
  ClipboardList,
  FileSearch,
  FileClock,
  Globe,
  LayoutDashboard,
  LogOut,
  Menu,
  Moon,
  ServerCog,
  Settings,
  ShieldCheck,
  Sun,
  Tags,
  Upload,
  Users,
  X,
  Zap,
} from 'lucide-react'
import clsx from 'clsx'

import { alertsApi, healthApi } from '../lib/api'
import { useAuth } from '../hooks/useAuth'

const NAV = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/endpoints', label: 'Endpoints', icon: ServerCog },
  { to: '/ssl', label: 'SSL Certificates', icon: ShieldCheck },
  { to: '/incidents', label: 'Incidents', icon: Zap },
  { to: '/alerts', label: 'Alerts', icon: Bell, badge: 'alerts' },
  {
    to: '/changes',
    label: 'Change Management',
    icon: ClipboardList,
    permission: 'change:read',
  },
  {
    to: '/rca',
    label: 'RCA',
    icon: FileSearch,
    permission: 'incident:read',
  },
  { to: '/tags', label: 'Tags', icon: Tags },
  { to: '/environments', label: 'Environments', icon: Globe },
  { to: '/import-export', label: 'Import / Export', icon: Upload, permission: 'endpoint:import' },
  { to: '/users', label: 'Users', icon: Users, permission: 'user:read' },
  { to: '/audit-logs', label: 'Audit Logs', icon: FileClock, permission: 'audit:read' },
  { to: '/settings', label: 'Settings', icon: Settings, permission: 'settings:read' },
]

const THEME_KEY = 'infrasight.theme'

function useTheme() {
  const [theme, setTheme] = useState(() => {
    try {
      return (
        localStorage.getItem(THEME_KEY) ||
        // Pre-rename key, so an upgrade does not reset the chosen theme.
        localStorage.getItem('certmonitor.theme') ||
        (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
      )
    } catch {
      return 'light'
    }
  })

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    try {
      localStorage.setItem(THEME_KEY, theme)
    } catch {
      /* ignore */
    }
  }, [theme])

  return [theme, () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))]
}

export default function AppLayout() {
  const { user, logout, can } = useAuth()
  const location = useLocation()
  const [theme, toggleTheme] = useTheme()
  const [mobileOpen, setMobileOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [alertCount, setAlertCount] = useState(0)
  const [health, setHealth] = useState(null)

  // Close the mobile drawer whenever the route changes.
  useEffect(() => setMobileOpen(false), [location.pathname])

  // Poll the badge and the worker health indicator. Both are cheap endpoints;
  // the dashboard itself is not polled, so an idle tab stays quiet.
  useEffect(() => {
    let cancelled = false
    const refresh = () => {
      alertsApi
        .unacknowledgedCount()
        .then((data) => !cancelled && setAlertCount(data.total || 0))
        .catch(() => {})
      healthApi
        .health()
        .then((data) => !cancelled && setHealth(data))
        .catch(() => !cancelled && setHealth({ status: 'unhealthy' }))
    }
    refresh()
    const timer = setInterval(refresh, 60000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  const visibleNav = NAV.filter((item) => !item.permission || can(item.permission))

  const workerState = health?.monitoring_worker
  const workerTone =
    workerState === 'healthy'
      ? 'bg-green-500'
      : workerState === 'degraded'
        ? 'bg-amber-500'
        : 'bg-red-500'

  const sidebar = (
    <nav className="flex h-full flex-col gap-1 p-3" aria-label="Main navigation">
      {visibleNav.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end}
          className={({ isActive }) => clsx('nav-link', isActive && 'nav-link-active')}
        >
          <item.icon size={17} aria-hidden="true" />
          <span className="flex-1 truncate">{item.label}</span>
          {item.badge === 'alerts' && alertCount > 0 ? (
            <span className="tnum rounded-full bg-red-100 px-1.5 py-0.5 text-[11px] font-semibold text-red-700 dark:bg-red-900/50 dark:text-red-300">
              {alertCount > 99 ? '99+' : alertCount}
            </span>
          ) : null}
        </NavLink>
      ))}

      <div className="mt-auto rounded-lg bg-slate-50 p-2.5 text-xs dark:bg-slate-800/60">
        <p className="mb-1 font-medium text-slate-600 dark:text-slate-300">System</p>
        <div className="flex items-center gap-1.5 text-slate-500 dark:text-slate-400">
          <span className={clsx('h-1.5 w-1.5 rounded-full', workerTone)} aria-hidden="true" />
          <span>
            Worker: <span className="font-medium">{workerState || 'unknown'}</span>
          </span>
        </div>
        <div className="mt-0.5 flex items-center gap-1.5 text-slate-500 dark:text-slate-400">
          <span
            className={clsx(
              'h-1.5 w-1.5 rounded-full',
              health?.database === 'healthy' ? 'bg-green-500' : 'bg-red-500',
            )}
            aria-hidden="true"
          />
          <span>
            Database: <span className="font-medium">{health?.database || 'unknown'}</span>
          </span>
        </div>
      </div>
    </nav>
  )

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-950">
      {/* ------------------------------------------------------ top bar */}
      <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/95 backdrop-blur dark:border-slate-800 dark:bg-slate-900/95">
        <div className="flex h-14 items-center gap-3 px-3 sm:px-4">
          <button
            type="button"
            className="btn-ghost p-2 lg:hidden"
            onClick={() => setMobileOpen((open) => !open)}
            aria-label={mobileOpen ? 'Close navigation' : 'Open navigation'}
            aria-expanded={mobileOpen}
          >
            {mobileOpen ? <X size={19} /> : <Menu size={19} />}
          </button>

          <NavLink to="/" className="flex items-center gap-2">
            <span className="grid h-8 w-8 place-items-center rounded-lg bg-brand-600 text-white">
              <Activity size={17} />
            </span>
            <span className="text-base font-semibold text-slate-900 dark:text-slate-50">
              InfraSight
            </span>
          </NavLink>

          <div className="ml-auto flex items-center gap-1.5">
            <button
              type="button"
              className="btn-ghost p-2"
              onClick={toggleTheme}
              aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
            >
              {theme === 'dark' ? <Sun size={17} /> : <Moon size={17} />}
            </button>

            <div className="relative">
              <button
                type="button"
                className="btn-ghost gap-2 px-2"
                onClick={() => setMenuOpen((open) => !open)}
                aria-haspopup="menu"
                aria-expanded={menuOpen}
              >
                <span className="grid h-7 w-7 place-items-center rounded-full bg-slate-200 text-xs font-semibold uppercase text-slate-700 dark:bg-slate-700 dark:text-slate-200">
                  {(user?.username || '?').slice(0, 2)}
                </span>
                <span className="hidden text-left sm:block">
                  <span className="block text-xs font-medium leading-tight text-slate-800 dark:text-slate-100">
                    {user?.username}
                  </span>
                  <span className="block text-[11px] capitalize leading-tight text-slate-500 dark:text-slate-400">
                    {user?.role}
                  </span>
                </span>
                <ChevronDown size={14} />
              </button>

              {menuOpen ? (
                <>
                  <div
                    className="fixed inset-0 z-10"
                    onMouseDown={() => setMenuOpen(false)}
                    aria-hidden="true"
                  />
                  <div
                    className="absolute right-0 z-20 mt-1 w-56 overflow-hidden rounded-lg border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-800"
                    role="menu"
                  >
                    <div className="border-b border-slate-100 px-3 py-2 dark:border-slate-700">
                      <p className="truncate text-sm font-medium text-slate-800 dark:text-slate-100">
                        {user?.full_name || user?.username}
                      </p>
                      <p className="truncate text-xs text-slate-500 dark:text-slate-400">
                        {user?.email || 'No e-mail set'}
                      </p>
                    </div>
                    <NavLink
                      to="/change-password"
                      className="block px-3 py-2 text-sm text-slate-700 hover:bg-slate-50 dark:text-slate-200 dark:hover:bg-slate-700"
                      role="menuitem"
                      onClick={() => setMenuOpen(false)}
                    >
                      Change password
                    </NavLink>
                    <button
                      type="button"
                      className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-red-600 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-900/30"
                      role="menuitem"
                      onClick={logout}
                    >
                      <LogOut size={15} /> Sign out
                    </button>
                  </div>
                </>
              ) : null}
            </div>
          </div>
        </div>
      </header>

      <div className="flex">
        {/* --------------------------------------------------- sidebar */}
        <aside className="sticky top-14 hidden h-[calc(100vh-3.5rem)] w-60 shrink-0 border-r border-slate-200 bg-white lg:block dark:border-slate-800 dark:bg-slate-900">
          {sidebar}
        </aside>

        {mobileOpen ? (
          <div className="fixed inset-0 top-14 z-20 lg:hidden">
            <div
              className="absolute inset-0 bg-slate-900/40"
              onMouseDown={() => setMobileOpen(false)}
              aria-hidden="true"
            />
            <aside className="relative h-full w-64 border-r border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
              {sidebar}
            </aside>
          </div>
        ) : null}

        {/* ------------------------------------------------------- main */}
        <main className="min-w-0 flex-1 px-3 py-4 sm:px-5 sm:py-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
