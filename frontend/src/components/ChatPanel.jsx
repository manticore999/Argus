import { useState, useRef, useEffect } from 'react'
import { useChat } from '../hooks/useChat'
import { MessageCircle, Plus, X, Send, ArrowUp, CheckCircle, Loader } from 'lucide-react'

export default function ChatPanel({ onClose }) {
    const { messages, loading, send, reset } = useChat()
    const [input, setInput] = useState('')
    const endRef = useRef(null)
    const inputRef = useRef(null)

    useEffect(() => {
        endRef.current?.scrollIntoView({ behavior: 'smooth' })
    }, [messages])

    useEffect(() => {
        inputRef.current?.focus()
    }, [])

    const handleSubmit = (e) => {
        e.preventDefault()
        if (!input.trim() || loading) return
        send(input.trim())
        setInput('')
    }

    const handleKeyDown = (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            handleSubmit(e)
        }
    }

    return (
        <div className="chat">
            <div className="chat__header">
                <div className="chat__title">
                    <MessageCircle className="chat__icon" size={16} />
                    Chat
                </div>
                <div className="chat__actions">
                    <button className="chat__action-btn" onClick={reset} title="New conversation">
                        <Plus size={14} />
                    </button>
                    <button className="chat__action-btn" onClick={onClose} title="Close">
                        <X size={14} />
                    </button>
                </div>
            </div>

            <div className="chat__messages">
                {messages.length === 0 && (
                    <div className="chat__empty">
                        <MessageCircle className="chat__empty-icon" size={40} />
                        <h3>Ask about your cluster</h3>
                        <p>Investigate incidents, check pod health, read logs, query metrics, and more.</p>
                    </div>
                )}

                {messages.map((msg, i) => (
                    <div key={i} className={`chat__msg chat__msg--${msg.role}`}>
                        {msg.role === 'user' && (
                            <div className="chat__bubble chat__bubble--user">
                                {msg.content}
                            </div>
                        )}
                        {msg.role === 'assistant' && (
                            <div className="chat__bubble chat__bubble--assistant">
                                {msg.tools?.length > 0 && (
                                    <div className="chat__tools">
                                        {msg.tools.map((t, j) => (
                                            <span key={j} className={`chat__tool chat__tool--${t.status}`}>
                                                <span className="chat__tool-icon">
                                                    {t.status === 'running'
                                                        ? <Loader size={12} />
                                                        : <CheckCircle size={12} />
                                                    }
                                                </span>
                                                {t.name.replace(/_/g, ' ')}
                                            </span>
                                        ))}
                                    </div>
                                )}
                                {msg.content ? (
                                    <div className={`chat__text${msg.isError ? ' chat__text--error' : ''}`}>
                                        {msg.content}
                                    </div>
                                ) : loading && i === messages.length - 1 ? (
                                    <div className="chat__thinking">
                                        <span className="chat__thinking-dot" />
                                        <span className="chat__thinking-dot" />
                                        <span className="chat__thinking-dot" />
                                    </div>
                                ) : null}
                            </div>
                        )}
                    </div>
                ))}
                <div ref={endRef} />
            </div>

            <form className="chat__input-area" onSubmit={handleSubmit}>
                <textarea
                    ref={inputRef}
                    className="chat__input"
                    value={input}
                    onChange={e => setInput(e.target.value)}
                    onKeyDown={handleKeyDown}
                    placeholder="Ask about your cluster..."
                    rows={1}
                    disabled={loading}
                />
                <button
                    className="chat__send"
                    type="submit"
                    disabled={loading || !input.trim()}
                >
                    <ArrowUp size={16} />
                </button>
            </form>
        </div>
    )
}
