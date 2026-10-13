# Hermes Usage Gate

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that stops the kanban dispatcher from
starting new cards for bots whose subscription plan is running out, lets them start again once the plan's
window resets, and resumes the chats a plan limit cut off. It also shows how many tokens each bot used, split by
side task and model.

Every few minutes it reads each provider's account limits (the same windows `hermes usage` shows: Codex
session/weekly, Claude session/week) and compares them with your thresholds. While a provider is over a
threshold, its bots are left out of `kanban.dispatch_profiles`, the dispatcher's claim allowlist, so their ready
cards wait. When every provider is under its thresholds, the allowlist is removed again.

Only board cards are gated, in both the work and the review lanes. Chats, scheduled jobs, webhooks and anything
else that doesn't go through the dispatcher keep running, and a card that is already running is never stopped.

So the board shows why a card isn't starting, the gate comments once on each held card (ready or in review, assigned
to a paused bot) per pause, naming the plan, the window, its usage and threshold, and when the window resets. The
card starts as soon as a check finds usage back under the threshold, which can be before that reset time.

## Resuming chats

When a chat (Desktop, TUI or CLI) stops because its model call hit the plan's limit, the gate notes it. Once a
check finds that plan open again, it sends the chat one message, by default *"The plan limit has reset; continue
where you stopped."*, so the bot picks up where it was. It runs as a one-shot `hermes chat --resume` in the
background; its output goes to `~/.hermes/usage-gate/resume-logs/`.

A chat is left alone when you wrote in it after it stopped, when the bot already answered (the note Hermes leaves
for a failed turn doesn't count), or when `resume_within_hours` have passed since it first stopped. A resume that
hits the limit again is retried at the next check, up to 3 times. Board cards, scheduled jobs, webhooks and email
runs are never resumed this way: the dispatcher and their schedules already run them again.

## Tokens

Hermes records every model call in each profile's session store, with the side task that made it: the bot's own
work, title generation, compression, vision, web extraction, session search, the background memory review. The
gate adds them up per bot, task and model (calls, input, cache read and write, output, reasoning). A session counts
in the period it started, the way `hermes insights` counts it.

Cache reads dwarf output, and that is expected: every model call sends the whole conversation again, and the part the
provider already holds is billed as a cache read. Reasoning tokens are part of output.

Hermes doesn't record the curator's runs, calls that failed, or anything a memory provider such as Honcho spends
on its own models.

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
  resume_interrupted_chats: true
  resume_message: "The plan limit has reset; continue where you stopped."
  resume_within_hours: 12 # give up on a chat this long after it first stopped
  providers:              # optional per-provider overrides
    openai-codex:
      label: ChatGPT Plus # the plan's name in held-card comments (default: the provider id)
      session_percent: 85
```

Reset times in comments use the config's `timezone` when it is set, UTC otherwise.

A window that limits only one model family (Claude Max's "Fable week", say) counts only when one of that provider's
bots runs that model, and when it crosses its threshold it holds only the bots on that family: the plan's other bots keep
starting their cards. A window that limits the whole account holds every bot on the plan. A chat stopped by a model's
own limit is resumed once that limit is back, not just the account's. When a
provider's usage can't be read (for example the usage API rate-limits the request), its last decision holds for
`hold_minutes` after the last good reading; after that it opens, so a broken usage API never stops the board for
good.

The plugin owns `kanban.dispatch_profiles` while it is enabled.

## Use

Install and enable it in the profile that runs the gateway's dispatcher (usually `default`). Only that profile's
dispatcher drives the gate. Install it in every other profile too: that is what notes their chats when a limit stops
them, and gives each the page and API below (the Desktop sends a plugin page's requests to the selected profile).

```sh
hermes plugins install https://github.com/sergiojrdotnet/hermes-usage-gate --enable
hermes usage-gate check    # read usage now and apply
hermes usage-gate status   # settings, each plan's last reading and decision, held cards, interrupted chats
hermes usage-gate tokens --days 7   # tokens per bot, side task and model (omit --days for all time)
```

## Page

A **Usage Gate** page in the Hermes Desktop (Capabilities → Plugins → Install from Git, Desktop target) and a tab
in the dashboard show each plan's session and weekly usage against its thresholds, whether the board may start its
bots' cards, when each window resets, the cards being held, the chats a plan limit stopped this week and
what became of each, a chart of the tokens each model used (last 24 h, 7 days, 30 days or all time, for every bot or one),
and the settings in force. The split per side task is in `hermes usage-gate tokens` and `GET /tokens/{days|all}`. The page never changes
the settings; edit them in `config.yaml`. **Check now** re-reads usage and applies the result. In the Desktop,
the status bar names any paused plan.

The page is served by `dashboard/plugin_api.py` at `/api/plugins/hermes-usage-gate/` (`GET /status`,
`POST /check`, `GET /tokens/{days|all}`). After changing `ui/`, run `python3 build.py`.

State is kept in `~/.hermes/usage-gate/state.json`, interrupted chats in `interrupted.json` next to it.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

## License

MIT
