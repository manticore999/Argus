import { useState, useCallback } from 'react'

export function useChat() {
    const [messages, setMessages] = useState([])
    const [loading, setLoading] = useState(false)
    const [sessionId, setSessionId] = useState(null)

    const send = useCallback(async (text) => {
        if (!text.trim() || loading) return

        setLoading(true)

        // Append user message + empty assistant placeholder
        setMessages(prev => [
            ...prev,
            { role: 'user', content: text },
            { role: 'assistant', content: '', tools: [] },
        ])

        try {
            const res = await fetch('/api/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, message: text }),
            })

            const reader = res.body.getReader()
            const decoder = new TextDecoder()
            let buffer = ''

            while (true) {
                const { done, value } = await reader.read()
                if (done) break

                buffer += decoder.decode(value, { stream: true })
                const parts = buffer.split('\n\n')
                buffer = parts.pop()

                for (const part of parts) {
                    if (!part.startsWith('data: ')) continue
                    const event = JSON.parse(part.slice(6))

                    switch (event.type) {
                        case 'session':
                            setSessionId(event.session_id)
                            break

                        case 'tool_call':
                            setMessages(prev => {
                                const msgs = [...prev]
                                const last = { ...msgs[msgs.length - 1] }
                                last.tools = [...(last.tools || []), { name: event.name, status: 'running' }]
                                msgs[msgs.length - 1] = last
                                return msgs
                            })
                            break

                        case 'tool_result':
                            setMessages(prev => {
                                const msgs = [...prev]
                                const last = { ...msgs[msgs.length - 1] }
                                last.tools = (last.tools || []).map(t =>
                                    t.name === event.name && t.status === 'running'
                                        ? { ...t, status: 'done' }
                                        : t
                                )
                                msgs[msgs.length - 1] = last
                                return msgs
                            })
                            break

                        case 'text':
                            setMessages(prev => {
                                const msgs = [...prev]
                                const last = { ...msgs[msgs.length - 1] }
                                last.content = event.content
                                msgs[msgs.length - 1] = last
                                return msgs
                            })
                            break

                        case 'error':
                            setMessages(prev => {
                                const msgs = [...prev]
                                const last = { ...msgs[msgs.length - 1] }
                                last.content = event.content
                                last.isError = true
                                msgs[msgs.length - 1] = last
                                return msgs
                            })
                            break
                    }
                }
            }
        } catch (err) {
            if (err.name !== 'AbortError') {
                setMessages(prev => {
                    const msgs = [...prev]
                    const last = { ...msgs[msgs.length - 1] }
                    last.content = 'Connection failed. Is the backend running?'
                    last.isError = true
                    msgs[msgs.length - 1] = last
                    return msgs
                })
            }
        } finally {
            setLoading(false)
        }
    }, [loading, sessionId])

    const reset = useCallback(() => {
        setMessages([])
        setSessionId(null)
    }, [])

    return { messages, loading, send, reset, sessionId }
}
