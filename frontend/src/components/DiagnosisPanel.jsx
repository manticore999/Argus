import { useState } from 'react'
import { Terminal, ListOrdered, Loader, Play, CheckCircle, XCircle } from 'lucide-react'

function ActionExecutor({ action }) {
    const [status, setStatus] = useState('idle') // idle, loading, success, error
    const [resultMsg, setResultMsg] = useState('')

    const handleExecute = async () => {
        setStatus('loading')
        try {
            const res = await fetch('/api/actions/execute', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    tool_name: action.action_tool_name,
                    parameters: action.parameters
                })
            })
            const data = await res.json()
            if (data.success) {
                setStatus('success')
                setResultMsg('Action executed successfully.')
            } else {
                setStatus('error')
                setResultMsg(data.error || 'Execution failed.')
            }
        } catch (e) {
            setStatus('error')
            setResultMsg('Network or server error.')
        }
    }

    return (
        <div className="cmd" style={{ flexDirection: 'column', alignItems: 'stretch', gap: '12px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                    <div style={{ fontSize: '14px', fontWeight: '700', color: '#fff', marginBottom: '4px' }}>
                        {action.action_tool_name.replace(/_/g, ' ')}
                    </div>
                    <div style={{ fontSize: '13px', color: '#94a3b8' }}>
                        {action.explanation}
                    </div>
                </div>
                
                {status === 'idle' && (
                    <button className="action-btn action-btn--primary" onClick={handleExecute}>
                        <Play size={14} /> Execute
                    </button>
                )}
                {status === 'loading' && (
                    <button className="action-btn" disabled style={{ opacity: 0.7 }}>
                        <Loader size={14} className="spin" style={{ animation: 'spin 1s linear infinite' }} /> Executing...
                    </button>
                )}
                {status === 'success' && (
                    <div className="status-pill">
                        <CheckCircle size={14} /> Done
                    </div>
                )}
                {status === 'error' && (
                    <div style={{ color: 'var(--critical)', display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', fontWeight: '600' }}>
                        <XCircle size={14} /> Failed
                    </div>
                )}
            </div>
            
            <div style={{ background: 'rgba(0,0,0,0.3)', padding: '8px 12px', borderRadius: '6px', fontFamily: 'var(--mono)', fontSize: '12px', color: '#86efac' }}>
                Parameters: {JSON.stringify(action.parameters)}
            </div>
            
            {resultMsg && status === 'error' && (
                <div style={{ color: 'var(--critical)', fontSize: '13px', marginTop: '4px' }}>
                    {resultMsg}
                </div>
            )}
        </div>
    )
}

export default function DiagnosisPanel({ diagnosis }) {
    if (!diagnosis) {
        return (
            <div className="diag-pending">
                <Loader size={16} style={{ animation: 'spin 1s linear infinite' }} />
                Analysis in progress. Auto-refreshes on completion.
            </div>
        )
    }

    const d = diagnosis

    return (
        <div>
            <div className={`diag__confidence conf-${d.confidence?.toLowerCase() || 'medium'}`}>
                {d.confidence} confidence
            </div>

            <div className="diag__cause">{d.root_cause || d.likely_cause}</div>
            {d.explanation && <p className="diag__explain">{d.explanation}</p>}

            {d.suggested_actions?.length > 0 && (
                <div className="diag__section">
                    <h4 className="diag__heading">
                        <Terminal size={14} />
                        Recommended Actions (Requires Approval)
                    </h4>
                    <div className="cmd-list">
                        {d.suggested_actions.map((action, i) => (
                            <ActionExecutor key={i} action={action} />
                        ))}
                    </div>
                </div>
            )}
        </div>
    )
}
