import { useState } from 'react'
import { Link } from 'react-router-dom'
import { HelpCircle, Search } from 'lucide-react'
import clsx from 'clsx'

import { Spinner } from './ui'
import { intelligenceApi } from '../lib/api'

/**
 * Ask the infrastructure a question, answered from the local database.
 *
 * The parser is deterministic and server-side: it recognises a fixed
 * vocabulary and executes an ordinary query. Nothing typed here leaves the
 * server, and a question it does not recognise says so rather than guessing -
 * which is why `understood` and the interpretation are shown above the rows.
 */

const EXAMPLES = [
  'production services that are down',
  'SSL certificates expiring in 30 days',
  'endpoints with latency above 1 second',
  'failed deployments this week',
  'incidents without RCA',
  'currently paused endpoints',
  'RCA pending for more than 7 days',
  'applications with recurring incidents',
]

export default function InfraSearch() {
  const [query, setQuery] = useState('')
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const run = async (text) => {
    const value = (text ?? query).trim()
    if (!value) return
    setQuery(value)
    setLoading(true)
    setError(null)
    try {
      setResult(await intelligenceApi.search(value))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="card mb-5" aria-label="Infrastructure search">
      <div className="card-header">
        <h2 className="card-title flex items-center gap-1.5">
          <Search size={15} /> Search infrastructure
        </h2>
        <span className="text-xs text-slate-400">
          answered from this server, offline
        </span>
      </div>

      <div className="p-4">
        <form
          className="flex flex-col gap-2 sm:flex-row"
          onSubmit={(event) => {
            event.preventDefault()
            run()
          }}
        >
          <input
            className="input flex-1"
            placeholder="production services that are down"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            maxLength={300}
            aria-label="Ask a question about your infrastructure"
          />
          <button type="submit" className="btn-primary" disabled={loading || !query.trim()}>
            {loading ? <Spinner size={15} className="text-white" /> : <Search size={15} />}
            Search
          </button>
        </form>

        <div className="mt-2 flex flex-wrap gap-1.5">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              className="chip hover:bg-slate-200 dark:hover:bg-slate-700"
              onClick={() => run(example)}
            >
              {example}
            </button>
          ))}
        </div>

        {error ? (
          <p role="alert" className="mt-3 text-sm text-red-700 dark:text-red-300">
            {error}
          </p>
        ) : null}

        {result ? (
          <div className="mt-4">
            {/* Showing what the parser understood is what makes a misread
                question obvious instead of silently wrong. */}
            <p
              className={clsx(
                'mb-2 flex items-start gap-1.5 rounded-lg px-3 py-2 text-sm',
                result.understood
                  ? 'bg-slate-50 text-slate-700 dark:bg-slate-800 dark:text-slate-200'
                  : 'bg-amber-50 text-amber-900 dark:bg-amber-950/40 dark:text-amber-200',
              )}
            >
              <HelpCircle size={15} className="mt-0.5 shrink-0" />
              <span>
                {result.understood ? (
                  <>
                    Interpreted as: <strong>{result.description}</strong> —{' '}
                    {result.count} result{result.count === 1 ? '' : 's'}
                  </>
                ) : (
                  result.description
                )}
              </span>
            </p>

            {result.rows.length ? (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      {result.columns.map((column) => (
                        <th key={column}>{column}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {result.rows.map((row, index) => (
                      <tr key={row.id || index}>
                        {result.columns.map((column, columnIndex) => (
                          <td key={column} className="whitespace-nowrap">
                            {columnIndex === 0 && row.link ? (
                              <Link
                                to={row.link}
                                className="font-medium text-brand-600 hover:underline dark:text-brand-400"
                              >
                                {row[column]}
                              </Link>
                            ) : (
                              row[column]
                            )}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : result.understood ? (
              <p className="px-1 py-3 text-sm text-slate-500 dark:text-slate-400">
                Nothing matched — which in this case is good news.
              </p>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  )
}
