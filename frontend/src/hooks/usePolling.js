import { useState, useEffect, useCallback } from 'react'

export function usePolling(url, interval = 4000) {
    const [data, setData] = useState(null)

    const load = useCallback(() => {
        fetch(url).then(r => r.json()).then(setData).catch(() => { })
    }, [url])

    useEffect(() => {
        load()
        const t = setInterval(load, interval)
        return () => clearInterval(t)
    }, [load, interval])

    return data
}
