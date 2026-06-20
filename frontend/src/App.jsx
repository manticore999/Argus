import { useState } from 'react'
import TopBar from './components/TopBar'
import StatsBar from './components/StatsBar'
import Sidebar from './components/Sidebar'
import IncidentDetail from './components/IncidentDetail'
import EventFeed from './components/EventFeed'
import ChatPanel from './components/ChatPanel'

export default function App() {
    const [selected, setSelected] = useState(null)
    const [chatOpen, setChatOpen] = useState(false)

    return (
        <div className="shell">
            <TopBar onChatToggle={() => setChatOpen(o => !o)} chatOpen={chatOpen} />
            <StatsBar />
            <div className="workspace">
                <Sidebar selected={selected} onSelect={setSelected} />
                <div className="main">
                    <IncidentDetail incident={selected} />
                    <EventFeed />
                </div>
                {chatOpen && <ChatPanel onClose={() => setChatOpen(false)} />}
            </div>
        </div>
    )
}
