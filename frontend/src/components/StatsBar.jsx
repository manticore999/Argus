import { AlertTriangle, Search, EyeOff, TrendingDown } from 'lucide-react'
import { usePolling } from '../hooks/usePolling'

export default function StatsBar() {
    const data = usePolling('/api/stats', 3000)

    const stats = [
        { label: 'Active Incidents', value: data?.active_incidents ?? '--', cls: 'stat--warning', icon: AlertTriangle },
        { label: 'Total Findings', value: data?.total_findings ?? '--', cls: 'stat--critical', icon: Search },
        { label: 'Suppressed', value: data?.suppressed_findings ?? '--', cls: 'stat--muted', icon: EyeOff },
        { label: 'Noise Reduction', value: data ? `${data.noise_reduction}%` : '--', cls: 'stat--ok', icon: TrendingDown },
    ]

    return (
        <div className="statsbar">
            {stats.map(s => {
                const Icon = s.icon
                return (
                    <div key={s.label} className={`stat ${s.cls}`}>
                        <div className="stat__indicator">
                            <Icon size={16} />
                        </div>
                        <div>
                            <div className="stat__value">{s.value}</div>
                            <div className="stat__label">{s.label}</div>
                        </div>
                    </div>
                )
            })}
        </div>
    )
}
