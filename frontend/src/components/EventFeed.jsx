import { useState } from 'react'
import { ChevronUp, Radio } from 'lucide-react'
import { useEventStream } from '../hooks/useEventStream'

export default function EventFeed() {
    const [open, setOpen] = useState(false)
    const events = useEventStream()

    return (
        <div className={`feed${open ? ' feed--open' : ' feed--collapsed'}`}>
            <div className="feed__toggle" onClick={() => setOpen(o => !o)}>
                <div className="feed__label">
                    <Radio size={14} className="feed__dot" />
                    Event Stream
                    <span className="feed__count">{events.length} events</span>
                </div>
                <ChevronUp
                    size={16}
                    className="feed__arrow"
                    style={{
                        transform: open ? 'rotate(180deg)' : 'rotate(0deg)',
                        transition: 'transform 0.3s ease',
                    }}
                />
            </div>
            {open && (
                <div className="feed__list">
                    {events.length === 0 && <span className="text-muted">Waiting for events...</span>}
                    {events.map(ev => (
                        <div key={ev.id} className={`ev ev--${ev.type}`}>
                            <span className="ev__time">{ev.time}</span>
                            <span className="ev__dot" />
                            <span className="ev__msg">{ev.message}</span>
                        </div>
                    ))}
                </div>
            )}
        </div>
    )
}
