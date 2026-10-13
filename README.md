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

So the board shows why a card isn't starting, the gate comments once on each held card (ready or in review, assigned
to a paused bot) per pause, naming the plan, the window, its usage and threshold, and when the window resets. The
card starts as soon as a check finds usage back under the threshold, which can be before that reset time.

## Settings

In the default profile's `config.yaml`; every key is optional:

```yaml
usage_gate:
  enabled: true
  session_percent: 70     # pause when a session (5-hour) window reaches this
  week_percent: 80        # pause when a weekly window reaches this
  refresh_minutes: 5      # how often usage is read
  hold_minutes: 30        # when usage can't be read, keep a pause this long after the last good reading
  comment_held_cards: true
  providers:              # optional per-provider overrides
    openai-codex:
      label: ChatGPT Plus # the plan's name in held-card comments (default: the provider id)
      session_percent: 85
```

Reset times in comments use the config's `timezone` when it is set, UTC otherwise.

A window that limits only one model family counts only when one of that provider's bots runs that model. When a
provider's usage can't be read (for example the usage API rate-limits the request), its last decision holds for
`hold_minutes` after the last good reading; after that it opens, so a broken usage API never stops the board for
good.

The plugin owns `kanban.dispatch_profiles` while it is enabled.

## Use

Install and enable it in the profile that runs the gateway's dispatcher (usually `default`). Only that profile's
dispatcher drives the gate; installing it in other profiles too gives each of them the page and API below (the
Desktop sends a plugin page's requests to the selected profile).

```sh
hermes plugins install https://github.com/sergiojrdotnet/hermes-usage-gate --enable
hermes usage-gate check    # read usage now and apply
hermes usage-gate status   # settings, each plan's last reading and decision, held cards
```

## Page

A **Usage Gate** page in the Hermes Desktop (Capabilities → Plugins → Install from Git, Desktop target) and a tab
in the dashboard show each plan's session and weekly usage against its thresholds, whether the board may start its
bots' cards, when each window resets, the cards being held, and a settings form. Saving the form validates it,
replaces the `usage_gate` section and re-checks usage at once; **Check now** re-reads usage without changing
anything. In the Desktop, the status bar names any paused plan.

The page is served by `dashboard/plugin_api.py` at `/api/plugins/hermes-usage-gate/` (`GET /status`,
`POST /check`, `POST /settings`). After changing `ui/`, run `python3 build.py`.

State is kept in `~/.hermes/usage-gate/state.json`.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

## License

MIT
