import { usePolling } from './hooks/usePolling'
import { fetchStatus, fetchLogs, fetchTrades } from './api'
import Header from './components/Header'
import ContractCard from './components/ContractCard'
import BotStatus from './components/BotStatus'
import TradeLog from './components/TradeLog'
import ExchangeMonitor from './components/ExchangeMonitor'
import AlphaDashboard from './components/AlphaDashboard'
import Collapsible from './components/Collapsible'
import ChatPanel from './components/ChatPanel'
import ConfigPanel from './components/ConfigPanel'
import LogPanel from './components/LogPanel'
import AnalyticsPanel from './components/AnalyticsPanel'

export default function App() {
  const { data: status, refresh: refreshStatus } = usePolling(fetchStatus, 2000)
  const { data: logs } = usePolling(fetchLogs, 3000)
  const tradeMode = status?.paper_mode ? 'paper' : 'live'
  const { data: tradeData } = usePolling(() => fetchTrades(tradeMode), 5000)

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
      <ContractCard status={status} />
      <BotStatus status={status} />
      <ExchangeMonitor status={status} />
      <AlphaDashboard status={status} />
      <TradeLog tradeData={tradeData} mode={tradeMode} />

      <div className="mt-6 space-y-2">
        <Collapsible title="Trade Analytics" badge={tradeData?.summary?.total_trades ? `${tradeData.summary.total_trades} trades` : null}>
          <AnalyticsPanel mode={tradeMode} />
        </Collapsible>
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
