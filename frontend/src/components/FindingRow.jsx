import { useState } from 'react'
import { ChevronDown } from 'lucide-react'

export default function FindingRow({ finding }) {
    const [open, setOpen] = useState(false)
    const f = finding
    const isRoot = f.role === 'ROOT_CAUSE'

    return (
        <div className="finding">
            <div className="finding__bar" onClick={() => setOpen(o => !o)}>
                <span className={`finding__dot${isRoot ? ' finding__dot--root' : ''}`} />
                <div className="finding__info">
                    <div className={`finding__name${isRoot ? '' : ' finding__name--muted'}`}>
                        {f.kind}/{f.resource} &mdash; {f.reason}
                    </div>
                    <div className="finding__sub">
                        {f.analyzer} &middot; {(f.suppressed_count || 0) + 1}x &middot; {f.last_seen?.slice(11, 19)}
                    </div>
                </div>
                <span className={`finding__role role-${f.role}`}>{f.role?.replace('_', ' ')}</span>
                <ChevronDown
                    size={16}
                    className="finding__toggle"
                    style={{
                        transform: open ? 'rotate(180deg)' : 'rotate(0deg)',
                        transition: 'transform 0.3s ease',
                    }}
                />
            </div>
            {open && (
                <div className="finding__detail">
                    <p>{f.message}</p>
                    {f.log_snippet && (
                        <div className="finding__logs">
                            <h5 className="finding__logs-title">Container Logs</h5>
                            <pre className="log-block">{f.log_snippet}</pre>
                        </div>
                    )}
                </div>
            )}
        </div>
    )
}
