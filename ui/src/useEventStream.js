import { useEffect, useRef, useState } from 'react'

// One WebSocket to the agent service, which relays the merged stream of real
// events: the agent's own steps and the broker's checks/decisions.
export function useEventStream(onEvent) {
  const [connected, setConnected] = useState(false)
  const handler = useRef(onEvent)
  handler.current = onEvent

  useEffect(() => {
    let ws
    let closed = false
    let retry

    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${location.host}/ws`)
      ws.onopen = () => setConnected(true)
      ws.onmessage = (msg) => handler.current(JSON.parse(msg.data))
      ws.onclose = () => {
        setConnected(false)
        if (!closed) retry = setTimeout(connect, 1000)
      }
    }
    connect()
    return () => {
      closed = true
      clearTimeout(retry)
      ws && ws.close()
    }
  }, [])

  return connected
}
