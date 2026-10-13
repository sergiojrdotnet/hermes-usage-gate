import { host, PALETTE_AREA, ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS } from '@hermes/plugin-sdk'
import * as React from 'react'

const ID = 'hermes-usage-gate'
const ROUTE = '/usage-gate'

/* @app */

function PausedPlans({ ctx }) {
  const [paused, setPaused] = React.useState([])
  React.useEffect(() => {
    let alive = true
    const load = () => ctx.rest('/status')
      .then(r => alive && setPaused(r.plans.filter(p => p.decision === 'paused').map(p => p.label)))
      .catch(() => {})
    load()
    const timer = setInterval(load, 60000)
    return () => { alive = false; clearInterval(timer) }
  }, [])
  if (!paused.length) return null
  return React.createElement('button', {
    type: 'button', onClick: () => host.navigate(ROUTE), title: 'New board cards for these plans’ bots are waiting',
    style: { display: 'inline-flex', alignItems: 'center', height: '100%', padding: '0 6px', fontSize: 11, border: 'none',
      background: 'none', cursor: 'pointer', color: 'var(--ui-red, #d64545)' }
  }, `⏸ ${paused.join(', ')} paused`)
}

export default {
  id: ID,
  name: 'Usage Gate',
  description: 'Each plan\'s usage, whether the board may start its bots\' cards, the cards being held, and the gate\'s settings.',
  register(ctx) {
    const api = {
      get: path => ctx.rest(path),
      post: (path, body) => ctx.rest(path, { method: 'POST', body: body || {} })
    }
    const App = createUsageGate(React, api)
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => React.createElement(App) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: ROUTE, label: 'Usage Gate', codicon: 'pulse' } },
      { id: 'open', area: PALETTE_AREA, data: { id: `${ID}.open`, label: 'Open Usage Gate', run: () => host.navigate(ROUTE) } },
      { id: 'paused', area: STATUSBAR_AREAS.right, order: 121, render: () => React.createElement(PausedPlans, { ctx }) }
    ])
  }
}
