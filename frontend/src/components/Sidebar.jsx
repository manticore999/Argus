import { AlertCircle } from 'lucide-react'
import { usePolling } from '../hooks/usePolling'
import IncidentCard from './IncidentCard'

export default function Sidebar({ selected, onSelect }) {
    const incidents = usePolling('/api/incidents', 3000)

    return (
        <aside className="sidebar">
            <div className="sidebar__header">
                <span className="sidebar__title">
                    <AlertCircle size={16} />
                    Incidents
                </span>
                <span className="sidebar__count">{incidents?.length ?? '--'}</span>
            </div>
            <div className="sidebar__list">
                {!incidents && <div className="sidebar__loading">Loading...</div>}
                {incidents?.length === 0 && (
                    <div className="sidebar__empty">No active incidents</div>
                )}
                {incidents?.map(inc => (
                    <IncidentCard
                        key={inc.id}
                        incident={inc}
                        active={selected?.id === inc.id}
                        onClick={() => onSelect(inc)}
                    />
                ))}
            </div>
        </aside>
    )
}
