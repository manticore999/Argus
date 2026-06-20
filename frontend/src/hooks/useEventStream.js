import { useState, useEffect } from 'react'

export function useEventStream() {
    const [events, setEvents] = useState([])

    useEffect(() => {
        fetch('/api/events/recent')
            .then(r => r.json())
            .then(h => setEvents([...h].reverse().slice(0, 120)))
            .catch(() => { })

        const es = new EventSource('/api/stream')
        es.onmessage = e => {
            const ev = JSON.parse(e.data)
            if (ev.type === 'ping') return
            setEvents(prev => [ev, ...prev].slice(0, 300))
        }
        return () => es.close()
    }, [])

    return events
}
