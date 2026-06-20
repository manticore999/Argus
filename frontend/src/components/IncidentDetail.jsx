import { useState, useEffect } from 'react'
import { Search, Clock, GitBranch, Stethoscope, Network, FileSearch } from 'lucide-react'
import { timeAgo, ruleLabel, formatTimestamp } from '../utils'
import Section from './Section'
import DiagnosisPanel from './DiagnosisPanel'
import CorrelationInfo from './CorrelationInfo'
import FindingRow from './FindingRow'

export default function IncidentDetail({ incident }) {
    const [detail, setDetail] = useState(null)

    useEffect(() => {
        if (!incident) { setDetail(null); return }
        const load = () =>
            fetch(`/api/incidents/${incident.id}`)
                .then(r => r.json())
                .then(setDetail)
                .catch(() => { })
        load()
        const t = setInterval(load, 4000)
        return () => clearInterval(t)
    }, [incident?.id])

    if (!incident) {
        return (
            <div className="placeholder">
                <Search size={32} />
                <h2>Select an incident</h2>
                <p>Choose an incident from the sidebar to view analysis details and evidence.</p>
            </div>
        )
    }

    if (!detail) {
        return (
            <div className="placeholder">
                <h2>Loading...</h2>
            </div>
        )
    }

    const { incident: inc, findings } = detail
    const roots = findings.filter(f => f.role === 'ROOT_CAUSE')
    const symptoms = findings.filter(f => f.role === 'SYMPTOM')
    const all = [...roots, ...symptoms]

    return (
        <div className="detail">
            <div className="detail__header">
                <div className="detail__info">
                    <span className="detail__scope">{inc.scope}</span>
                    <h2 className="detail__title">{inc.resource}</h2>
                    <div className="detail__meta">
                        <span className="meta-item">
                            Rule: <strong>{ruleLabel(inc.correlation_rule)}</strong>
                        </span>
                        <span className="meta-sep" />
                        <span className="meta-item">
                            <Clock size={14} /> First seen: <strong>{formatTimestamp(inc.first_seen)}</strong>
                        </span>
                        <span className="meta-sep" />
                        <span className="meta-item">
                            <Clock size={14} /> Last: <strong>{formatTimestamp(inc.last_seen)}</strong>
                        </span>
                        <span className="meta-sep" />
                        <span className="meta-item meta-item--muted">{timeAgo(inc.first_seen)}</span>
                    </div>
                </div>
                <div className="detail__badges">
                    <span className={`badge badge--lg sev-${inc.severity}`}>{inc.severity}</span>
                    <span className="detail__id">{inc.id?.slice(0, 8)}</span>
                </div>
            </div>

            <Section title="Diagnosis" icon={<Stethoscope size={16} />} defaultOpen={true}>
                <DiagnosisPanel diagnosis={inc.diagnosis} />
            </Section>

            <Section title="Correlation" icon={<Network size={16} />} defaultOpen={false}>
                <CorrelationInfo incident={inc} findings={findings} symptoms={symptoms} />
            </Section>

            <Section title="Evidence" icon={<FileSearch size={16} />} badge={`${all.length} finding${all.length !== 1 ? 's' : ''}`} defaultOpen={false}>
                <div className="findings-grid">
                    {all.length === 0 && <div className="text-muted">No findings linked.</div>}
                    {all.map(f => <FindingRow key={f.id} finding={f} />)}
                </div>
            </Section>
        </div>
    )
}
