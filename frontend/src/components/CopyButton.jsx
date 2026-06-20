import { useState } from 'react'
import { Copy, Check } from 'lucide-react'

export default function CopyButton({ text }) {
    const [copied, setCopied] = useState(false)

    const copy = () => {
        navigator.clipboard?.writeText(text).then(() => {
            setCopied(true)
            setTimeout(() => setCopied(false), 1800)
        })
    }

    return (
        <button className={`copy-btn${copied ? ' copy-btn--done' : ''}`} onClick={copy}>
            {copied ? <Check size={14} /> : <Copy size={14} />}
        </button>
    )
}
