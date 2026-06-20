export function timeAgo(iso) {
    if (!iso) return ''
    const s = Math.floor((Date.now() - new Date(iso + 'Z')) / 1000)
    if (s < 60) return `${s}s ago`
    if (s < 3600) return `${Math.floor(s / 60)}m ago`
    return `${Math.floor(s / 3600)}h ago`
}

export function ruleLabel(rule) {
    const labels = {
        pod_inferred_controller: 'Pod Inferred Controller',
        owner_dominance: 'Owner Dominance',
        node_dominance: 'Node Dominance',
        namespace_aggregation: 'Namespace Aggregation',
    }
    return labels[rule] || rule
}

export function formatTimestamp(iso) {
    if (!iso) return '--'
    return iso.replace('T', ' ').slice(0, 19)
}
