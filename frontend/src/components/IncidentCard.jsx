import { Clock, Layers, Filter } from 'lucide-react'
import { timeAgo, ruleLabel } from '../utils'

export default function IncidentCard({ incident, active, onClick }) {
    const inc = incident
    const conf = inc.diagnosis?.confidence

    return (
        <div
            className={`inc-card sev-${inc.severity}${active ? ' inc-card--active' : ''}`}
            onClick={onClick}
        >
            <div className="inc-card__header">
                <span className="inc-card__name">{inc.resource}</span>
                <span className={`badge sev-${inc.severity}`}>{inc.severity}</span>
            </div>
            <div className="inc-card__rule">{ruleLabel(inc.correlation_rule)}</div>
            {inc.diagnosis ? (
                <div className="inc-card__diagnosis">
                    <div className="inc-card__cause">{inc.diagnosis.likely_cause}</div>
                    <span className={`chip chip--${conf}`}>{conf} confidence</span>
                </div>
            ) : (
                <div className="inc-card__diagnosis inc-card__diagnosis--pending">
                    Analysis pending
                </div>
            )}
            <div className="inc-card__meta">
                <span className="chip"><Layers size={12} /> {inc.total_findings} findings</span>
                <span className="chip"><Filter size={12} /> {inc.suppressed_count} suppressed</span>
                <span className="chip chip--muted"><Clock size={12} /> {timeAgo(inc.first_seen)}</span>
            </div>
        </div>
    )
}
