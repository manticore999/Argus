import { MessageCircle, Wifi } from 'lucide-react'

export default function TopBar({ onChatToggle, chatOpen }) {
    return (
        <header className="topbar">
            <div className="topbar__brand">
                <img src="/logo.jfif" alt="Argus Logo" style={{ width: '26px', height: '26px', borderRadius: '6px', marginRight: '8px', objectFit: 'cover' }} />
                <h1>Argus</h1>
                <span className="topbar__sep">Kubernetes Incident Monitor</span>
            </div>
            <div className="topbar__actions">
                <button
                    className={`topbar__chat-btn${chatOpen ? ' topbar__chat-btn--active' : ''}`}
                    onClick={onChatToggle}
                >
                    <MessageCircle size={14} />
                    Chat
                </button>
                <div className="status-pill">
                    <Wifi size={12} />
                    <span className="status-dot" />
                    Connected
                </div>
            </div>
        </header>
    )
}
