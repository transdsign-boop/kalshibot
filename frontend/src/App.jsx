import { usePolling } from './hooks/usePolling'
import { fetchStatus, fetchLogs, fetchTrades } from './api'
import Header from './components/Header'
import AgentHero from './components/AgentHero'
import KeyMetrics from './components/KeyMetrics'
import MarketCard from './components/MarketCard'
import ContractTimer from './components/ContractTimer'
import TradeLog from './components/TradeLog'
import Collapsible from './components/Collapsible'
import ChatPanel from './components/ChatPanel'
import ConfigPanel from './components/ConfigPanel'
import LogPanel from './components/LogPanel'

export default function App() {
  const { data: status, refresh: refreshStatus } = usePolling(fetchStatus, 2000)
  const { data: logs } = usePolling(fetchLogs, 3000)
  const { data: tradeData } = usePolling(fetchTrades, 5000)

  if (!status) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center">
          <div className="w-6 h-6 rounded-full border-2 border-blue-500 border-t-transparent animate-spin mx-auto mb-3" />
          <p className="text-gray-500 text-sm">Connecting...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen max-w-2xl mx-auto px-4 py-6 md:py-10">
      <Header status={status} onAction={refreshStatus} />
      <AgentHero status={status} />
      <KeyMetrics status={status} />
      <MarketCard status={status} />
      <ContractTimer status={status} />
      <TradeLog tradeData={tradeData} />

      <div className="mt-6 space-y-2">
        <Collapsible title="Chat with Agent">
          <ChatPanel />
        </Collapsible>
        <Collapsible title="Configuration">
          <ConfigPanel />
        </Collapsible>
        <Collapsible title="Event Log" badge={`cycle ${status.cycle_count}`}>
          <LogPanel logs={logs} />
        </Collapsible>
      </div>

      <footer className="text-center text-xs text-gray-700 mt-8 pb-4">
        {status.trading_enabled ? 'LIVE TRADING ENABLED' : 'Trading disabled'}
      </footer>
    </div>
  )
}
