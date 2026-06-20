import { GitBranch, Filter, Activity } from 'lucide-react'

export default function CorrelationInfo({ incident, findings, symptoms }) {
    const inc = incident
    const total = findings.length
    const suppressed = symptoms.length

    const explanations = {
        pod_inferred_controller:
            `${total} pod(s) sharing the same controller were grouped into 1 deployment-level incident. ${suppressed} pod finding(s) suppressed as symptoms.`,
        node_dominance:
            `A node went NotReady. ${suppressed} pod failure(s) on that node are downstream symptoms.`,
        owner_dominance:
            `Deployment and pod warnings detected. Deployment takes owner precedence; pod findings suppressed.`,
        namespace_aggregation:
            `${total} resources in the same namespace shared the same failure pattern.`,
    }

    return (
        <div>
            <div className="corr-box">
                {explanations[inc.correlation_rule] || inc.correlation_rule}
            </div>
            <div className="corr-meta">
                <div className="kv">
                    <span className="kv__key">
                        <GitBranch size={14} />
                        Findings
                    </span>
                    <span className="kv__val">{inc.finding_count ?? total}</span>
                </div>
                <div className="kv">
                    <span className="kv__key">
                        <Filter size={14} />
                        Suppressed
                    </span>
                    <span className="kv__val">{inc.suppressed_symptom_count ?? suppressed}</span>
                </div>
                <div className="kv">
                    <span className="kv__key">
                        <Activity size={14} />
                        Status
                    </span>
                    <span className="kv__val">{inc.status}</span>
                </div>
            </div>
        </div>
    )
}
