import { useState } from 'react'
import { ChevronDown } from 'lucide-react'

export default function Section({ title, badge, defaultOpen = true, children }) {
    const [open, setOpen] = useState(defaultOpen)

    return (
        <div className="section">
            <div className="section__head" onClick={() => setOpen(o => !o)}>
                <div className="section__title">
                    {title}
                    {badge && <span className="section__badge">{badge}</span>}
                </div>
                <ChevronDown
                    size={18}
                    className={`section__arrow${open ? ' section__arrow--open' : ''}`}
                    style={{
                        transform: open ? 'rotate(180deg)' : 'rotate(0deg)',
                        transition: 'transform 0.3s ease',
                    }}
                />
            </div>
            {open && <div className="section__body">{children}</div>}
        </div>
    )
}
