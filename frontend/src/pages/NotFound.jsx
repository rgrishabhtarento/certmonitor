import { Link } from 'react-router-dom'
import { Compass } from 'lucide-react'

import { EmptyState } from '../components/ui'

export default function NotFound() {
  return (
    <div className="card">
      <EmptyState
        icon={Compass}
        title="Page not found"
        description="That route does not exist. It may have been renamed, or the link may be out of date."
        action={
          <Link to="/" className="btn-primary">
            Back to the dashboard
          </Link>
        }
      />
    </div>
  )
}
