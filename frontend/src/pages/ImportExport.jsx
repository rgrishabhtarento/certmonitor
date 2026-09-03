import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  FileSpreadsheet,
  Upload,
  XCircle,
} from 'lucide-react'
import clsx from 'clsx'

import { Card, EmptyState, PageHeader, Spinner } from '../components/ui'
import { downloadFile, importExportApi } from '../lib/api'
import { formatDateTime } from '../lib/format'
import { useAuth } from '../hooks/useAuth'
import { useToast } from '../hooks/useToast'

const STEPS = ['Choose a file', 'Review the preview', 'Import']

export default function ImportExport() {
  const { can } = useAuth()
  const toast = useToast()
  const canImport = can('endpoint:import')

  const fileInput = useRef(null)
  const [file, setFile] = useState(null)
  const [preview, setPreview] = useState(null)
  const [result, setResult] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [importing, setImporting] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const [showOnlyProblems, setShowOnlyProblems] = useState(false)
  const [skipRows, setSkipRows] = useState(() => new Set())

  const step = result ? 2 : preview ? 1 : 0

  const reset = () => {
    setFile(null)
    setPreview(null)
    setResult(null)
    setSkipRows(new Set())
    setShowOnlyProblems(false)
    if (fileInput.current) fileInput.current.value = ''
  }

  const upload = async (chosen) => {
    if (!chosen) return
    setFile(chosen)
    setPreview(null)
    setResult(null)
    setUploading(true)
    try {
      const payload = await importExportApi.preview(chosen)
      setPreview(payload)
      if (payload.file_errors?.length) {
        toast.error(payload.file_errors[0])
      } else if (payload.invalid_count > 0) {
        toast.warning(
          `${payload.valid_count} row(s) are ready to import; ${payload.invalid_count} need attention.`,
        )
      } else {
        toast.success(`${payload.valid_count} row(s) validated and ready to import.`)
      }
    } catch (err) {
      toast.error(err.message)
      setFile(null)
    } finally {
      setUploading(false)
    }
  }

  const confirm = async () => {
    if (!preview) return
    const rows = preview.rows
      .filter((row) => row.valid && !skipRows.has(row.row_number))
      .map((row) => row.row_number)

    if (!rows.length) {
      toast.warning('No rows are selected for import.')
      return
    }

    setImporting(true)
    try {
      const payload = await importExportApi.confirm(preview.token, rows)
      setResult(payload)
      if (payload.created_count) {
        toast.success(
          `${payload.created_count} endpoint(s) created and queued for their first check.`,
        )
      }
      if (payload.failed_count) {
        toast.error(`${payload.failed_count} row(s) could not be created.`)
      }
    } catch (err) {
      toast.error(err.message)
    } finally {
      setImporting(false)
    }
  }

  const exportFile = async (format) => {
    setExporting(true)
    try {
      const stamp = new Date().toISOString().slice(0, 10)
      await downloadFile(
        `/api/export?format=${format}`,
        `certmonitor-endpoints-${stamp}.${format}`,
      )
      toast.success('Export downloaded.')
    } catch (err) {
      toast.error(err.message)
    } finally {
      setExporting(false)
    }
  }

  const downloadTemplate = async () => {
    try {
      await downloadFile('/api/import/template', 'certmonitor-import-template.csv')
    } catch (err) {
      toast.error(err.message)
    }
  }

  const visibleRows = (preview?.rows || []).filter((row) =>
    showOnlyProblems ? !row.valid || row.warnings.length : true,
  )
  const selectedCount = (preview?.rows || []).filter(
    (row) => row.valid && !skipRows.has(row.row_number),
  ).length

  return (
    <>
      <PageHeader
        title="Import / Export"
        description="Bulk-load endpoints from CSV or Excel, and export the monitoring configuration."
      />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* ------------------------------------------------- import flow */}
        <div className="lg:col-span-2">
          <Card
            title="Import endpoints"
            actions={
              preview || result ? (
                <button type="button" className="btn-ghost btn-sm" onClick={reset}>
                  Start over
                </button>
              ) : null
            }
          >
            {!canImport ? (
              <EmptyState
                icon={Upload}
                title="You do not have permission to import"
                description="Ask an administrator to import endpoints, or to grant you the endpoint:import permission."
              />
            ) : (
              <>
                {/* Step indicator */}
                <ol className="mb-4 flex items-center gap-2 text-xs">
                  {STEPS.map((label, index) => (
                    <li key={label} className="flex items-center gap-2">
                      <span
                        className={clsx(
                          'grid h-5 w-5 place-items-center rounded-full text-[11px] font-semibold',
                          index < step
                            ? 'bg-green-600 text-white'
                            : index === step
                              ? 'bg-brand-600 text-white'
                              : 'bg-slate-200 text-slate-500 dark:bg-slate-700 dark:text-slate-400',
                        )}
                      >
                        {index < step ? '✓' : index + 1}
                      </span>
                      <span
                        className={clsx(
                          index === step
                            ? 'font-medium text-slate-900 dark:text-slate-100'
                            : 'text-slate-500',
                        )}
                      >
                        {label}
                      </span>
                      {index < STEPS.length - 1 ? (
                        <span className="text-slate-300">→</span>
                      ) : null}
                    </li>
                  ))}
                </ol>

                {/* ------------------------------------------ step 0 */}
                {step === 0 ? (
                  <div>
                    <div
                      className={clsx(
                        'rounded-xl border-2 border-dashed px-4 py-10 text-center transition-colors',
                        dragOver
                          ? 'border-brand-500 bg-brand-50 dark:bg-brand-900/20'
                          : 'border-slate-300 dark:border-slate-700',
                      )}
                      onDragOver={(event) => {
                        event.preventDefault()
                        setDragOver(true)
                      }}
                      onDragLeave={() => setDragOver(false)}
                      onDrop={(event) => {
                        event.preventDefault()
                        setDragOver(false)
                        upload(event.dataTransfer.files?.[0])
                      }}
                    >
                      <FileSpreadsheet
                        size={30}
                        className="mx-auto mb-2 text-slate-300 dark:text-slate-600"
                      />
                      <p className="text-sm font-medium text-slate-700 dark:text-slate-200">
                        Drop a CSV or .xlsx file here
                      </p>
                      <p className="mt-0.5 text-xs text-slate-500">
                        Nothing is created until you review the preview and confirm.
                      </p>
                      <div className="mt-3 flex flex-wrap justify-center gap-2">
                        <button
                          type="button"
                          className="btn-primary"
                          onClick={() => fileInput.current?.click()}
                          disabled={uploading}
                        >
                          {uploading ? (
                            <Spinner size={15} className="text-white" />
                          ) : (
                            <Upload size={15} />
                          )}
                          Choose a file
                        </button>
                        <button
                          type="button"
                          className="btn-secondary"
                          onClick={downloadTemplate}
                        >
                          <Download size={15} /> Template
                        </button>
                      </div>
                      <input
                        ref={fileInput}
                        type="file"
                        accept=".csv,.tsv,.txt,.xlsx,.xlsm"
                        className="hidden"
                        onChange={(event) => upload(event.target.files?.[0])}
                      />
                    </div>

                    <div className="mt-4 rounded-lg bg-slate-50 p-3 text-xs text-slate-600 dark:bg-slate-800/60 dark:text-slate-300">
                      <p className="mb-1 font-medium">Expected columns</p>
                      <p className="font-mono text-[11px] leading-relaxed">
                        name, url, environment, tags, interval, timeout, description,
                        owner, team, application, method, expected_status, check_type,
                        monitoring_enabled, ssl_monitoring, verify_ssl, follow_redirects,
                        failure_threshold, response_time_threshold_ms
                      </p>
                      <p className="mt-2">
                        Only <span className="font-mono">url</span> is required.
                        Common alternative spellings (endpoint_name, env, host,
                        interval_seconds…) are recognised automatically, and unknown
                        columns are reported but ignored.
                      </p>
                    </div>
                  </div>
                ) : null}

                {/* ------------------------------------------ step 1 */}
                {step === 1 && preview ? (
                  <div>
                    <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
                      <div className="rounded-lg border border-slate-200 p-2.5 dark:border-slate-700">
                        <p className="text-lg font-semibold">{preview.total_rows}</p>
                        <p className="text-[11px] text-slate-500">Rows in file</p>
                      </div>
                      <div className="rounded-lg border border-green-200 bg-green-50 p-2.5 dark:border-green-900 dark:bg-green-950/40">
                        <p className="text-lg font-semibold text-green-700 dark:text-green-300">
                          {preview.valid_count}
                        </p>
                        <p className="text-[11px] text-green-700 dark:text-green-300">Valid</p>
                      </div>
                      <div className="rounded-lg border border-red-200 bg-red-50 p-2.5 dark:border-red-900 dark:bg-red-950/40">
                        <p className="text-lg font-semibold text-red-700 dark:text-red-300">
                          {preview.invalid_count}
                        </p>
                        <p className="text-[11px] text-red-700 dark:text-red-300">
                          With errors
                        </p>
                      </div>
                      <div className="rounded-lg border border-amber-200 bg-amber-50 p-2.5 dark:border-amber-900 dark:bg-amber-950/40">
                        <p className="text-lg font-semibold text-amber-700 dark:text-amber-300">
                          {preview.duplicate_count}
                        </p>
                        <p className="text-[11px] text-amber-700 dark:text-amber-300">
                          Duplicates
                        </p>
                      </div>
                    </div>

                    {preview.file_errors?.length ? (
                      <div
                        role="alert"
                        className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/50 dark:text-red-200"
                      >
                        {preview.file_errors.map((message) => (
                          <p key={message}>{message}</p>
                        ))}
                      </div>
                    ) : null}

                    {preview.unknown_columns?.length ? (
                      <p className="mb-3 text-xs text-amber-600 dark:text-amber-400">
                        Ignored unknown column(s): {preview.unknown_columns.join(', ')}
                      </p>
                    ) : null}

                    <div className="mb-2 flex flex-wrap items-center gap-3">
                      <label className="flex items-center gap-1.5 text-xs">
                        <input
                          type="checkbox"
                          className="h-3.5 w-3.5 rounded border-slate-300"
                          checked={showOnlyProblems}
                          onChange={(event) => setShowOnlyProblems(event.target.checked)}
                        />
                        Show only rows needing attention
                      </label>
                      <span className="ml-auto text-xs text-slate-500">
                        Preview expires {formatDateTime(preview.expires_at, 'HH:mm')}
                      </span>
                    </div>

                    <div className="table-wrap max-h-[420px] overflow-y-auto rounded-lg border border-slate-200 dark:border-slate-700">
                      <table className="table">
                        <thead>
                          <tr>
                            <th className="w-8">Skip</th>
                            <th className="w-12">Row</th>
                            <th>Name</th>
                            <th>URL</th>
                            <th>Environment</th>
                            <th>Tags</th>
                            <th>Interval</th>
                            <th>Status</th>
                          </tr>
                        </thead>
                        <tbody>
                          {visibleRows.map((row) => (
                            <tr
                              key={row.row_number}
                              className={
                                !row.valid
                                  ? 'bg-red-50/60 dark:bg-red-950/20'
                                  : skipRows.has(row.row_number)
                                    ? 'opacity-50'
                                    : undefined
                              }
                            >
                              <td>
                                <input
                                  type="checkbox"
                                  className="h-3.5 w-3.5 rounded border-slate-300"
                                  disabled={!row.valid}
                                  checked={skipRows.has(row.row_number)}
                                  aria-label={`Skip row ${row.row_number}`}
                                  onChange={() =>
                                    setSkipRows((current) => {
                                      const next = new Set(current)
                                      if (next.has(row.row_number))
                                        next.delete(row.row_number)
                                      else next.add(row.row_number)
                                      return next
                                    })
                                  }
                                />
                              </td>
                              <td className="tnum text-slate-400">{row.row_number}</td>
                              <td className="max-w-[10rem] truncate">{row.name || '—'}</td>
                              <td className="max-w-[14rem] truncate font-mono text-[11px]">
                                {row.url || '—'}
                              </td>
                              <td>{row.environment || '—'}</td>
                              <td>
                                <div className="flex flex-wrap gap-1">
                                  {(row.tags || []).slice(0, 3).map((tag) => (
                                    <span key={tag} className="chip">
                                      {tag}
                                    </span>
                                  ))}
                                </div>
                              </td>
                              <td className="tnum">
                                {row.interval_seconds ? `${row.interval_seconds}s` : '—'}
                              </td>
                              <td className="max-w-[16rem]">
                                {row.valid ? (
                                  <span className="badge bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300">
                                    <CheckCircle2 size={11} /> Ready
                                  </span>
                                ) : (
                                  <span className="badge bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300">
                                    <XCircle size={11} /> Error
                                  </span>
                                )}
                                {row.errors?.map((message) => (
                                  <p
                                    key={message}
                                    className="mt-0.5 text-[11px] text-red-600 dark:text-red-400"
                                  >
                                    {message}
                                  </p>
                                ))}
                                {row.warnings?.map((message) => (
                                  <p
                                    key={message}
                                    className="mt-0.5 text-[11px] text-amber-600 dark:text-amber-400"
                                  >
                                    {message}
                                  </p>
                                ))}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>

                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <p className="text-sm text-slate-600 dark:text-slate-300">
                        <span className="font-semibold">{selectedCount}</span> row(s) will be
                        imported.
                      </p>
                      <div className="ml-auto flex gap-2">
                        <button type="button" className="btn-secondary" onClick={reset}>
                          Cancel
                        </button>
                        <button
                          type="button"
                          className="btn-primary"
                          onClick={confirm}
                          disabled={importing || selectedCount === 0}
                        >
                          {importing ? (
                            <Spinner size={15} className="text-white" />
                          ) : (
                            <Upload size={15} />
                          )}
                          Import {selectedCount} endpoint(s)
                        </button>
                      </div>
                    </div>
                  </div>
                ) : null}

                {/* ------------------------------------------ step 2 */}
                {step === 2 && result ? (
                  <div>
                    <div className="mb-3 grid grid-cols-3 gap-2">
                      <div className="rounded-lg border border-green-200 bg-green-50 p-3 dark:border-green-900 dark:bg-green-950/40">
                        <p className="text-xl font-semibold text-green-700 dark:text-green-300">
                          {result.created_count}
                        </p>
                        <p className="text-xs text-green-700 dark:text-green-300">Created</p>
                      </div>
                      <div className="rounded-lg border border-red-200 bg-red-50 p-3 dark:border-red-900 dark:bg-red-950/40">
                        <p className="text-xl font-semibold text-red-700 dark:text-red-300">
                          {result.failed_count}
                        </p>
                        <p className="text-xs text-red-700 dark:text-red-300">Failed</p>
                      </div>
                      <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-700">
                        <p className="text-xl font-semibold">{result.skipped_count}</p>
                        <p className="text-xs text-slate-500">Skipped</p>
                      </div>
                    </div>

                    {result.failed?.length ? (
                      <div className="mb-3">
                        <p className="label flex items-center gap-1">
                          <AlertTriangle size={13} className="text-red-500" /> Failed rows
                        </p>
                        <ul className="space-y-1 text-xs">
                          {result.failed.map((row) => (
                            <li key={row.row_number} className="text-red-600 dark:text-red-400">
                              Row {row.row_number} ({row.name || row.url}): {row.error}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}

                    {result.created?.length ? (
                      <div className="table-wrap max-h-64 overflow-y-auto rounded-lg border border-slate-200 dark:border-slate-700">
                        <table className="table">
                          <thead>
                            <tr>
                              <th>Row</th>
                              <th>Name</th>
                              <th>URL</th>
                            </tr>
                          </thead>
                          <tbody>
                            {result.created.map((row) => (
                              <tr key={row.id}>
                                <td className="tnum text-slate-400">{row.row_number}</td>
                                <td>
                                  <Link
                                    to={`/endpoints/${row.id}`}
                                    className="text-brand-600 hover:underline dark:text-brand-400"
                                  >
                                    {row.name}
                                  </Link>
                                </td>
                                <td className="font-mono text-[11px]">{row.url}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ) : null}

                    <div className="mt-3 flex gap-2">
                      <Link to="/endpoints" className="btn-primary">
                        View endpoints
                      </Link>
                      <button type="button" className="btn-secondary" onClick={reset}>
                        Import another file
                      </button>
                    </div>
                  </div>
                ) : null}
              </>
            )}
          </Card>
        </div>

        {/* ----------------------------------------------------- export */}
        <div className="space-y-4">
          <Card title="Export configuration">
            <p className="mb-3 text-sm text-slate-600 dark:text-slate-300">
              Download every endpoint with its monitoring configuration and current
              state.
            </p>
            <div className="flex flex-col gap-2">
              <button
                type="button"
                className="btn-secondary justify-start"
                onClick={() => exportFile('csv')}
                disabled={exporting}
              >
                {exporting ? <Spinner size={15} /> : <Download size={15} />}
                Export as CSV
              </button>
              <button
                type="button"
                className="btn-secondary justify-start"
                onClick={() => exportFile('xlsx')}
                disabled={exporting}
              >
                {exporting ? <Spinner size={15} /> : <Download size={15} />}
                Export as Excel
              </button>
            </div>
            <p className="mt-3 rounded-lg bg-amber-50 px-2.5 py-2 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
              Credentials are never exported. Only the authentication <em>type</em>
              appears in the file, so an export can be shared without leaking a token.
            </p>
          </Card>

          <Card title="Round-trip safe">
            <p className="text-sm text-slate-600 dark:text-slate-300">
              An export can be edited and re-imported. Rows whose URL or name already
              exists are flagged as duplicates in the preview rather than creating a
              second copy, so re-importing the same file changes nothing.
            </p>
          </Card>
        </div>
      </div>
    </>
  )
}
