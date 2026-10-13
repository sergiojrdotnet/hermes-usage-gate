import { host, PALETTE_AREA, ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS } from '@hermes/plugin-sdk'
import * as React from 'react'

const ID = 'hermes-usage-gate'
const ROUTE = '/usage-gate'

/* @app */

function pauses(status) {
  const found = {}
  for (const plan of status.plans || []) {
    if (plan.decision === 'paused') {
      found[`plan:${plan.provider}:${plan.paused_since || ''}`] = {
        title: `Usage Gate: ${plan.label} paused`,
        body: `${plan.reason || 'Usage is over its threshold'}. Its bots' cards wait until usage is back under it.`,
        reopened: `${plan.label} is open again; its held cards start on their own.`
      }
    }
    for (const family of plan.paused_models || []) {
      found[`model:${plan.provider}:${family}:${(plan.model_paused_since || {})[family] || ''}`] = {
        title: `Usage Gate: ${plan.label} ${family} paused`,
        body: `Bots on ${family} wait for its own limit; the rest of ${plan.label} keeps working.`,
        reopened: `${family} on ${plan.label} is open again; its held cards start on their own.`
      }
    }
  }
  const openrouter = status.openrouter || {}
  if (openrouter.holding) {
    const label = (status.settings.openrouter || {}).label || 'OpenRouter'
    found[`openrouter:${openrouter.held_since || ''}`] = {
      title: 'Usage Gate: every card held',
      body: `The ${label} balance is $${Number(openrouter.balance || 0).toFixed(2)}, under $${Number(openrouter.balance_hold_usd || 0).toFixed(2)}. Cards start again once credit is added.`,
      reopened: `The ${label} balance is back over its limit; held cards start on their own.`
    }
  }
  return found
}

function PausedPlans({ ctx }) {
  const [paused, setPaused] = React.useState([])
  React.useEffect(() => {
    let alive = true
    const load = () => ctx.rest('/status')
      .then(r => alive && setPaused([
        ...r.plans.filter(p => p.decision === 'paused' || (p.paused_models || []).length)
          .map(p => `⏸ ${p.decision === 'paused' ? p.label : `${p.label} (${p.paused_models.join(', ')})`} paused`),
        ...(r.openrouter && r.openrouter.holding ? [`⏸ All cards held: ${(r.settings.openrouter || {}).label || 'OpenRouter'} low`]
          : (r.openrouter && (r.openrouter.warnings || []).length) ? [`⚠ ${(r.settings.openrouter || {}).label || 'OpenRouter'} low`] : [])]))
      .catch(() => {})
    load()
    const timer = setInterval(load, 60000)
    return () => { alive = false; clearInterval(timer) }
  }, [])
  if (!paused.length) return null
  return React.createElement('button', {
    type: 'button', onClick: () => host.navigate(ROUTE), title: 'Open the Usage Gate page',
    style: { display: 'inline-flex', alignItems: 'center', height: '100%', padding: '0 6px', fontSize: 11, border: 'none',
      background: 'none', cursor: 'pointer', color: 'var(--ui-red, #d64545)' }
  }, paused.join(' · '))
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

    let known = null
    const poll = () => ctx.rest('/status').then(status => {
      const now = pauses(status)
      if (known) {
        for (const [key, pause] of Object.entries(now)) {
          if (!known[key]) ctx.os.notify({ title: pause.title, body: pause.body, activate: ROUTE })
        }
        for (const [key, pause] of Object.entries(known)) {
          if (!now[key]) ctx.os.notify({ title: 'Usage Gate: open again', body: pause.reopened, activate: ROUTE })
        }
      }
      known = now
    }).catch(() => {})
    poll()
    ctx.setInterval(poll, 60000)
  }
}
