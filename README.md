# Hermes Usage Gate

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that stops the kanban dispatcher from
starting new cards for bots whose subscription plan is running out, and lets them start again once the plan's
window resets.

Every few minutes it reads each provider's account limits (the same windows `hermes usage` shows: Codex
session/weekly, Claude session/week) and compares them with your thresholds. While a provider is over a
threshold, its bots are left out of `kanban.dispatch_profiles`, the dispatcher's claim allowlist, so their ready
cards wait. When every provider is under its thresholds, the allowlist is removed again.

Only board cards are gated, in both the work and the review lanes. Chats, scheduled jobs, webhooks and anything
else that doesn't go through the dispatcher keep running, and a card that is already running is never stopped.

## Settings

In the default profile's `config.yaml`; every key is optional:

```yaml
usage_gate:
  enabled: true
  session_percent: 70     # pause when a session (5-hour) window reaches this
  week_percent: 80        # pause when a weekly window reaches this
  refresh_minutes: 5      # how often usage is read
  hold_minutes: 30        # when usage can't be read, keep a pause this long after the last good reading
  providers:              # optional per-provider overrides
    openai-codex:
      session_percent: 85
```

A window that limits only one model family counts only when one of that provider's bots runs that model. When a
provider's usage can't be read (for example the usage API rate-limits the request), its last decision holds for
`hold_minutes` after the last good reading; after that it opens, so a broken usage API never stops the board for
good.

The plugin owns `kanban.dispatch_profiles` while it is enabled.

## Use

Install and enable it in the profile that runs the gateway's dispatcher (usually `default`), then restart the
gateway:

```sh
hermes plugins install https://github.com/sergiojrdotnet/hermes-usage-gate --enable
hermes usage-gate check    # read usage now and apply
hermes usage-gate status   # last readings, decisions and the allowlist in force
```

State is kept in `~/.hermes/usage-gate/state.json`.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

## License

MIT
