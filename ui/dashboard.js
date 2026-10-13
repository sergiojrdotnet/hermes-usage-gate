(function () {
  const SDK = window.__HERMES_PLUGIN_SDK__
  const React = SDK.React
  const BASE = '/api/plugins/hermes-usage-gate'
  const withProfile = path => {
    const profile = new URLSearchParams(window.location.search).get('profile')
    return profile ? `${path}${path.includes('?') ? '&' : '?'}profile=${encodeURIComponent(profile)}` : path
  }
  const api = {
    get: path => SDK.fetchJSON(withProfile(BASE + path)),
    post: (path, body) => SDK.fetchJSON(withProfile(BASE + path), {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
    })
  }

  /* @app */

  window.__HERMES_PLUGINS__.register('hermes-usage-gate', createUsageGate(React, api))
})()
