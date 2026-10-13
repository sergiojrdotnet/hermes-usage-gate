import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from usage_gate import gate, runtime
from usage_gate.gate import OPEN, PAUSED, Reading, Thresholds

NOW = datetime(2026, 10, 12, 23, 0, tzinfo=timezone.utc)
NO_CARDS = mock.patch.object(runtime, "_kanban", return_value=SimpleNamespace(returncode=0, stdout="[]", stderr=""))
T = Thresholds(session_percent=70, week_percent=80)


def snapshot(*windows, unavailable=None, plan=None):
    return SimpleNamespace(unavailable_reason=unavailable, plan=plan, windows=tuple(
        SimpleNamespace(label=label, used_percent=used, reset_at=None, scope=scope) for label, used, scope in windows))


class SettingsTest(unittest.TestCase):
    def test_defaults_when_missing(self):
        s = gate.settings_from(None)
        self.assertEqual((s.default.session_percent, s.default.week_percent), (70.0, 80.0))
        self.assertTrue(s.enabled)

    def test_per_provider_override_inherits_missing_value(self):
        s = gate.settings_from({"session_percent": 60, "providers": {"openai-codex": {"week_percent": 95}}})
        self.assertEqual(s.thresholds("openai-codex"), Thresholds(60, 95))
        self.assertEqual(s.thresholds("other"), Thresholds(60, 80))

    def test_invalid_values_fall_back(self):
        s = gate.settings_from({"session_percent": "lots", "refresh_minutes": 0, "enabled": False})
        self.assertEqual(s.default.session_percent, 70.0)
        self.assertEqual(s.refresh_minutes, 1.0)
        self.assertFalse(s.enabled)


class ReadingTest(unittest.TestCase):
    def test_claude_and_codex_labels(self):
        r = gate.reading_from_snapshot("x", snapshot(("Current session", 100, "account"), ("Current week", 23, "account"),
                                                     ("Session", 48, "account"), ("Weekly", 18, "account")), NOW)
        self.assertEqual([w["kind"] for w in r.windows], ["session", "week", "session", "week"])

    def test_model_window_counts_only_for_a_model_in_use(self):
        snap = snapshot(("Current week (Sonnet)", 99, "model"), ("Current week (Opus)", 50, "model"))
        r = gate.reading_from_snapshot("claude", snap, NOW, models=["opus"])
        self.assertEqual([w["label"] for w in r.windows], ["Current week (Opus)"])

    def test_unavailable_becomes_error(self):
        r = gate.reading_from_snapshot("claude", snapshot(unavailable="usage API returned HTTP 429"), NOW)
        self.assertEqual(r.error, "usage API returned HTTP 429")
        self.assertEqual(r.windows, ())

    def test_the_reported_plan_is_named_after_its_product(self):
        week = ("Weekly", 0, "account")
        self.assertEqual(gate.reading_from_snapshot("openai-codex", snapshot(week, plan="Prolite"), NOW).plan,
                         "ChatGPT Prolite")
        self.assertEqual(gate.reading_from_snapshot("openai-codex", snapshot(week, plan="ChatGPT Plus"), NOW).plan,
                         "ChatGPT Plus")
        self.assertEqual(gate.reading_from_snapshot("nous", snapshot(week, plan="Pro"), NOW).plan, "nous Pro")
        self.assertIsNone(gate.reading_from_snapshot("openai-codex", snapshot(week), NOW).plan)


class ClaudePlanTest(unittest.TestCase):
    def test_organization_type_and_tier(self):
        profile = {"organization": {"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_5x"}}
        self.assertEqual(gate.claude_plan(profile), "Claude Max 5x")
        self.assertEqual(gate.claude_plan({"organization": {"organization_type": "claude_pro"}}), "Claude Pro")

    def test_account_flags_when_the_organization_says_nothing(self):
        self.assertEqual(gate.claude_plan({"organization": {}, "account": {"has_claude_max": True}}), "Claude Max")
        self.assertEqual(gate.claude_plan({"account": {"has_claude_pro": True}}), "Claude Pro")
        self.assertIsNone(gate.claude_plan({"account": {}}))
        self.assertIsNone(gate.claude_plan("not a profile"))


class DecideTest(unittest.TestCase):
    def reading(self, *windows, at=NOW):
        return Reading("p", at, windows=tuple({"label": l, "kind": k, "used_percent": u} for l, k, u in windows))

    def test_session_at_threshold_pauses(self):
        state = gate.decide(self.reading(("Session", "session", 70), ("Weekly", "week", 10)), T, None, timedelta(minutes=30))
        self.assertEqual(state["decision"], PAUSED)

    def test_below_both_opens(self):
        state = gate.decide(self.reading(("Session", "session", 69), ("Weekly", "week", 79)), T, None, timedelta(minutes=30))
        self.assertEqual(state["decision"], OPEN)

    def test_week_over_pauses(self):
        state = gate.decide(self.reading(("Session", "session", 1), ("Weekly", "week", 80)), T, None, timedelta(minutes=30))
        self.assertEqual(state["decision"], PAUSED)

    def test_unknown_keeps_pause_within_hold(self):
        paused = gate.decide(self.reading(("Session", "session", 90)), T, None, timedelta(minutes=30))
        later = Reading("p", NOW + timedelta(minutes=20), error="429")
        self.assertEqual(gate.decide(later, T, paused, timedelta(minutes=30))["decision"], PAUSED)

    def test_unknown_opens_after_hold(self):
        paused = gate.decide(self.reading(("Session", "session", 90)), T, None, timedelta(minutes=30))
        later = Reading("p", NOW + timedelta(minutes=31), error="429")
        state = gate.decide(later, T, paused, timedelta(minutes=30))
        self.assertEqual(state["decision"], OPEN)
        self.assertIn("429", state["reason"])

    def test_the_plan_is_kept_until_a_reading_names_another(self):
        named = Reading("p", NOW, windows=({"label": "Weekly", "kind": "week", "used_percent": 1},), plan="ChatGPT Pro")
        state = gate.decide(named, T, None, timedelta(minutes=30))
        unnamed = Reading("p", NOW, windows=named.windows)
        self.assertEqual(gate.decide(unnamed, T, state, timedelta(minutes=30))["plan"], "ChatGPT Pro")
        self.assertEqual(gate.decide(Reading("p", NOW, error="429"), T, state, timedelta(minutes=30))["plan"],
                         "ChatGPT Pro")
        renamed = Reading("p", NOW, windows=named.windows, plan="ChatGPT Prolite")
        self.assertEqual(gate.decide(renamed, T, state, timedelta(minutes=30))["plan"], "ChatGPT Prolite")

    def test_unknown_with_no_history_is_open(self):
        self.assertEqual(gate.decide(Reading("p", NOW, error="x"), T, None, timedelta(minutes=30))["decision"], OPEN)


class AllowlistTest(unittest.TestCase):
    profiles = {"default": ("codex", "gpt-6.1-sol"), "ceo": ("codex", "gpt-6-astra"), "cto": ("claude", "fable"),
                "web": ("claude", "opus")}

    def test_none_when_nothing_paused(self):
        self.assertIsNone(gate.allowlist(self.profiles, {"codex": {"decision": OPEN}, "claude": {"decision": OPEN}}))

    def test_paused_provider_bots_removed(self):
        self.assertEqual(gate.allowlist(self.profiles, {"codex": {"decision": OPEN}, "claude": {"decision": PAUSED}}),
                         ["ceo", "default"])

    def test_everything_paused_is_empty_list(self):
        self.assertEqual(gate.allowlist(self.profiles, {"codex": {"decision": PAUSED}, "claude": {"decision": PAUSED}}), [])


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "profiles" / "cto").mkdir(parents=True)
        (root / "profiles" / "ceo").mkdir(parents=True)
        (root / "profiles" / ".deleted").mkdir(parents=True)
        (root / "config.yaml").write_text("model:\n  provider: openai-codex\n  default: gpt-6.1-sol\n"
                                          "usage_gate:\n  session_percent: 70\n  week_percent: 80\n")
        (root / "profiles" / "cto" / "config.yaml").write_text("model:\n  provider: claude\n  default: opus\n")
        (root / "profiles" / "ceo" / "config.yaml").write_text("model:\n  provider: openai-codex\n  default: gpt-6-astra\n")
        self.env = mock.patch.dict(os.environ, {"USAGE_GATE_HERMES_ROOT": str(root)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_profile_models_skips_tombstones(self):
        self.assertEqual(runtime.profile_models(), {"default": ("openai-codex", "gpt-6.1-sol"),
                                                    "ceo": ("openai-codex", "gpt-6-astra"), "cto": ("claude", "opus")})

    def test_refresh_pauses_claude_and_writes_allowlist_once(self):
        readings = {"claude": Reading("claude", NOW, windows=({"label": "Current session", "kind": "session", "used_percent": 100},)),
                    "openai-codex": Reading("openai-codex", NOW, windows=({"label": "Session", "kind": "session", "used_percent": 40},))}
        calls = []
        with mock.patch.object(runtime, "fetch", side_effect=lambda p, m: readings[p]), NO_CARDS, \
             mock.patch.object(runtime, "_hermes", side_effect=lambda *a: calls.append(a) or SimpleNamespace(returncode=0, stderr="")):
            state = runtime.refresh(force=True)
            runtime.refresh(force=True)
        self.assertEqual(state["applied"], ["ceo", "default"])
        self.assertEqual(calls, [("set", "kanban.dispatch_profiles", json.dumps(["ceo", "default"]))])

    def test_refresh_removes_allowlist_when_reopened(self):
        runtime.save_state({"applied": ["ceo", "default"], "providers": {}})
        open_reading = lambda p, m: Reading(p, NOW, windows=({"label": "Session", "kind": "session", "used_percent": 5},))
        calls = []
        with mock.patch.object(runtime, "fetch", side_effect=open_reading), \
             mock.patch.object(runtime, "_hermes", side_effect=lambda *a: calls.append(a) or SimpleNamespace(returncode=0, stderr="")):
            state = runtime.refresh(force=True)
        self.assertIsNone(state["applied"])
        self.assertEqual(calls, [("unset", "kanban.dispatch_profiles")])

    def test_failed_write_keeps_previous_allowlist(self):
        reading = lambda p, m: Reading(p, NOW, windows=({"label": "Session", "kind": "session", "used_percent": 99},))
        with mock.patch.object(runtime, "fetch", side_effect=reading), NO_CARDS, \
             mock.patch.object(runtime, "_hermes", return_value=SimpleNamespace(returncode=1, stderr="boom")):
            state = runtime.refresh(force=True)
        self.assertIsNone(state["applied"])

    def test_a_low_openrouter_balance_holds_every_bot(self):
        open_reading = lambda p, m: Reading(p, NOW, windows=({"label": "Session", "kind": "session", "used_percent": 5},))
        low = {"checked_at": NOW.isoformat(), "last_reading_at": NOW.isoformat(), "balance": 1.5, "holding": True,
               "held_since": NOW.isoformat(), "balance_hold_usd": 2.0}
        calls = []
        with mock.patch.object(runtime, "fetch", side_effect=open_reading), NO_CARDS, \
             mock.patch.object(runtime, "refresh_openrouter", return_value=low), \
             mock.patch.object(runtime, "_hermes", side_effect=lambda *a: calls.append(a) or SimpleNamespace(returncode=0, stderr="")):
            state = runtime.refresh(force=True)
        self.assertEqual(state["applied"], [])
        self.assertEqual(calls, [("set", "kanban.dispatch_profiles", "[]")])

    def test_refresh_skips_until_due(self):
        runtime.save_state({"checked_at": NOW.isoformat(), "applied": None})
        with mock.patch.object(runtime, "utcnow", return_value=NOW + timedelta(minutes=2)), \
             mock.patch.object(runtime, "fetch") as fetch:
            runtime.refresh()
        fetch.assert_not_called()


class EpisodeTest(unittest.TestCase):
    def reading(self, used, at):
        return Reading("p", at, windows=({"label": "Session", "kind": "session", "used_percent": used, "resets_at": None},))

    def test_paused_since_is_stable_while_paused(self):
        first = gate.decide(self.reading(90, NOW), T, None, timedelta(minutes=30))
        second = gate.decide(self.reading(95, NOW + timedelta(minutes=10)), T, first, timedelta(minutes=30))
        self.assertEqual(second["paused_since"], first["paused_since"])
        self.assertEqual(second["over"][0]["threshold"], 70)

    def test_reopening_clears_the_episode(self):
        paused = gate.decide(self.reading(90, NOW), T, None, timedelta(minutes=30))
        opened = gate.decide(self.reading(10, NOW + timedelta(minutes=10)), T, paused, timedelta(minutes=30))
        self.assertNotIn("paused_since", opened)
        again = gate.decide(self.reading(90, NOW + timedelta(minutes=20)), T, opened, timedelta(minutes=30))
        self.assertNotEqual(again["paused_since"], paused["paused_since"])


class CommentTextTest(unittest.TestCase):
    def test_names_plan_window_threshold_and_local_reset(self):
        from zoneinfo import ZoneInfo
        over = [{"label": "Current session", "used_percent": 100, "threshold": 70,
                 "resets_at": "2026-10-13T01:40:00+00:00"}]
        text = gate.held_comment("Claude Pro", over, ZoneInfo("America/Sao_Paulo"))
        self.assertIn("Claude Pro current session at 100% (threshold 70%)", text)
        self.assertIn("2026-10-12 22:40 America/Sao_Paulo", text)
        self.assertIn("starts on its own", text)

    def test_settings_label_and_toggle(self):
        s = gate.settings_from({"comment_held_cards": False, "providers": {"claude": {"label": "Claude Pro"}}})
        self.assertFalse(s.comment_held_cards)
        self.assertEqual((s.label("claude"), s.label("other")), ("Claude Pro", "other"))

    def test_a_detected_plan_wins_over_the_configured_label(self):
        s = gate.settings_from({"providers": {"claude": {"label": "Claude Pro"}}})
        self.assertEqual((s.label("claude", "Claude Max 5x"), s.label("claude", None)), ("Claude Max 5x", "Claude Pro"))


class HeldCardsTest(unittest.TestCase):
    profiles = {"ceo": ("codex", "astra"), "cto": ("claude", "opus"), "web": ("claude", "opus")}
    paused = {"claude": {"decision": PAUSED, "paused_since": "E1", "over": [{"label": "Session", "used_percent": 90,
                                                                            "threshold": 70, "resets_at": None}]},
              "codex": {"decision": OPEN}}

    def run_sweep(self, notes, providers=None):
        cards = {"ready": [{"id": "t_1", "assignee": "cto"}, {"id": "t_2", "assignee": "ceo"}],
                 "review": [{"id": "t_3", "assignee": "web"}]}
        comments = []

        def kanban(*args):
            if args[0] == "list":
                return SimpleNamespace(returncode=0, stdout=json.dumps(cards[args[2]]), stderr="")
            comments.append(args[1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(runtime, "_kanban", side_effect=kanban), mock.patch.object(runtime, "_timezone", return_value=None):
            kept = runtime.comment_held_cards(gate.settings_from(None), self.profiles, providers or self.paused, notes)
        return kept, comments

    def test_comments_only_cards_of_paused_bots(self):
        kept, comments = self.run_sweep({})
        self.assertEqual(sorted(comments), ["t_1", "t_3"])
        self.assertEqual(kept, {"t_1": "E1", "t_3": "E1"})

    def test_the_comment_names_the_detected_plan(self):
        texts = []

        def kanban(*args):
            if args[0] == "list":
                return SimpleNamespace(returncode=0, stderr="",
                                       stdout=json.dumps([{"id": "t_1", "assignee": "cto"}] if args[2] == "ready" else []))
            texts.append(args[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        providers = {**self.paused, "claude": {**self.paused["claude"], "plan": "Claude Max 5x"}}
        with mock.patch.object(runtime, "_kanban", side_effect=kanban), mock.patch.object(runtime, "_timezone", return_value=None):
            runtime.comment_held_cards(gate.settings_from(None), self.profiles, providers, {})
        self.assertIn("Claude Max 5x session at 90%", texts[0])

    def test_no_second_comment_in_the_same_pause(self):
        _, comments = self.run_sweep({"t_1": "E1", "t_3": "E1"})
        self.assertEqual(comments, [])

    def test_new_pause_comments_again(self):
        providers = {**self.paused, "claude": {**self.paused["claude"], "paused_since": "E2"}}
        _, comments = self.run_sweep({"t_1": "E1", "t_3": "E1"}, providers)
        self.assertEqual(sorted(comments), ["t_1", "t_3"])

    def test_an_openrouter_hold_comments_every_bots_cards_once(self):
        holding = {"holding": True, "held_since": "H1", "balance": 1.5, "balance_hold_usd": 2.0}
        cards = {"ready": [{"id": "t_1", "assignee": "cto"}, {"id": "t_2", "assignee": "ceo"}],
                 "review": [{"id": "t_3", "assignee": "web"}]}
        comments = []

        def kanban(*args):
            if args[0] == "list":
                return SimpleNamespace(returncode=0, stdout=json.dumps(cards[args[2]]), stderr="")
            comments.append((args[1], args[-1]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        open_plans = {"claude": {"decision": OPEN}, "codex": {"decision": OPEN}}
        with mock.patch.object(runtime, "_kanban", side_effect=kanban), mock.patch.object(runtime, "_timezone", return_value=None):
            kept = runtime.comment_held_cards(gate.settings_from(None), self.profiles, open_plans, {}, holding)
            again = runtime.comment_held_cards(gate.settings_from(None), self.profiles, open_plans, kept, holding)
        self.assertEqual(sorted(c for c, _ in comments), ["t_1", "t_2", "t_3"])
        self.assertIn("balance is $1.50, under $2.00", comments[0][1])
        self.assertEqual(again, kept)

    def test_nothing_paused_lists_nothing(self):
        kept, comments = self.run_sweep({"t_1": "E1"}, {"claude": {"decision": OPEN}})
        self.assertEqual((kept, comments), ({}, []))


class PlanOverrideTest(unittest.TestCase):
    def test_label_only_plan_inherits_later_threshold_changes(self):
        s = gate.settings_from({"session_percent": 60, "providers": {"claude": {"label": "Claude Pro"}}})
        self.assertEqual(s.thresholds("claude"), Thresholds(60, 80))
        self.assertNotIn("claude", s.per_provider)


class StatusViewTest(RuntimeTest):
    def test_plans_bots_and_held_cards(self):
        runtime.save_state({"checked_at": NOW.isoformat(), "applied": ["ceo", "default"],
                            "providers": {"claude": {"decision": PAUSED, "reason": "Current session 100% ≥ 70%"},
                                          "openai-codex": {"decision": OPEN}}})
        cards = lambda *a: SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            [{"id": "t_1", "title": "Fix CI", "assignee": "cto", "status": a[2], "body": "x"}] if a[2] == "ready" else []))
        with mock.patch.object(runtime, "_kanban", side_effect=cards):
            view = runtime.status_view()
        plans = {p["provider"]: p for p in view["plans"]}
        self.assertEqual(plans["claude"]["bots"], ["cto"])
        self.assertEqual(plans["openai-codex"]["bots"], ["ceo"])
        self.assertEqual(plans["claude"]["thresholds"], {"session_percent": 70, "week_percent": 80})
        self.assertEqual(view["held"], [{"id": "t_1", "title": "Fix CI", "assignee": "cto", "status": "ready"}])
        self.assertEqual(view["timezone"], None)

    def test_plans_are_labelled_with_the_detected_plan(self):
        (Path(os.environ["USAGE_GATE_HERMES_ROOT"]) / "config.yaml").write_text(
            "usage_gate:\n  providers:\n    claude:\n      label: Claude Pro\n    openai-codex:\n      label: ChatGPT Plus\n")
        runtime.save_state({"checked_at": NOW.isoformat(), "applied": None,
                            "providers": {"claude": {"decision": OPEN, "plan": "Claude Max 5x"},
                                          "openai-codex": {"decision": OPEN}}})
        with NO_CARDS:
            plans = {p["provider"]: p["label"] for p in runtime.status_view()["plans"]}
        self.assertEqual(plans, {"claude": "Claude Max 5x", "openai-codex": "ChatGPT Plus"})


class FetchPlanTest(RuntimeTest):
    CLAUDE = "claude-subscription-directsdk-experimental"

    def fetch(self, provider, snap, profile_plan):
        with mock.patch("agent.account_usage.fetch_account_usage", return_value=snap), \
             mock.patch.object(runtime, "claude_profile_plan", return_value=profile_plan) as lookup:
            return runtime.fetch(provider, ["opus"]), lookup

    def test_claude_asks_its_profile_for_the_plan(self):
        reading, lookup = self.fetch(self.CLAUDE, snapshot(("Current week", 3, "account")), "Claude Max 5x")
        self.assertEqual(reading.plan, "Claude Max 5x")
        lookup.assert_called_once_with(self.CLAUDE)

    def test_a_plan_in_the_usage_reply_needs_no_lookup(self):
        reading, lookup = self.fetch("openai-codex", snapshot(("Weekly", 0, "account"), plan="Prolite"), "unused")
        self.assertEqual(reading.plan, "ChatGPT Prolite")
        lookup.assert_not_called()

    def test_other_providers_are_not_looked_up(self):
        reading, lookup = self.fetch("nous", snapshot(("Weekly", 0, "account")), "unused")
        self.assertIsNone(reading.plan)
        lookup.assert_not_called()

    def test_profile_lookup_uses_the_plugins_own_token(self):
        plugin = types.ModuleType("fake_claude_plugin")
        plugin._subscription_token = lambda: ("tok", None)
        profile = type("Profile", (), {"__module__": "fake_claude_plugin"})()
        reply = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            "organization": {"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_20x"}})
        with mock.patch.dict(sys.modules, {"fake_claude_plugin": plugin}), \
             mock.patch("providers.get_provider_profile", return_value=profile), \
             mock.patch("httpx.get", return_value=reply) as get:
            self.assertEqual(runtime.claude_profile_plan(self.CLAUDE), "Claude Max 20x")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer tok")

    def test_a_failed_profile_lookup_names_no_plan(self):
        with mock.patch("providers.get_provider_profile", return_value=None):
            self.assertIsNone(runtime.claude_profile_plan(self.CLAUDE))
        with mock.patch("providers.get_provider_profile", side_effect=RuntimeError("no registry")):
            self.assertIsNone(runtime.claude_profile_plan(self.CLAUDE))


class ConcurrentRefreshTest(RuntimeTest):
    def test_overlapping_refreshes_read_usage_once(self):
        import threading
        import time
        runtime.save_state({"checked_at": (NOW - timedelta(hours=1)).isoformat(), "applied": None})
        seen = []

        def slow_fetch(provider, models):
            seen.append(provider)
            time.sleep(0.2)
            return Reading(provider, NOW, windows=({"label": "Session", "kind": "session", "used_percent": 5},))

        with mock.patch.object(runtime, "fetch", side_effect=slow_fetch), NO_CARDS, \
             mock.patch.object(runtime, "utcnow", return_value=NOW), \
             mock.patch.object(runtime, "_hermes", return_value=SimpleNamespace(returncode=0, stderr="")):
            threads = [threading.Thread(target=runtime.refresh) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(sorted(seen), ["claude", "openai-codex"])
        self.assertTrue((Path(os.environ["USAGE_GATE_HERMES_ROOT"]) / "usage-gate" / "refresh.lock").exists())


class HookGuardTest(unittest.TestCase):
    def test_only_the_default_profile_drives_the_gate(self):
        with mock.patch.object(runtime.threading, "Thread") as thread:
            runtime.on_dispatch_tick(profile_name="cto")
            thread.assert_not_called()
            runtime.on_dispatch_tick(profile_name="default")
            thread.assert_called_once()
        runtime._running.clear()


class LimitErrorTest(unittest.TestCase):
    def test_plan_limits_count(self):
        self.assertTrue(gate.is_limit_error(429, None, None))
        self.assertTrue(gate.is_limit_error(None, "rate_limit", {"type": "ClaudeAPIError", "message": "x"}))
        self.assertTrue(gate.is_limit_error(None, None, {"type": "E", "message": "You've hit your usage limit"}))

    def test_other_failures_do_not(self):
        self.assertFalse(gate.is_limit_error(500, "server_error", {"type": "APIError", "message": "Internal error"}))
        self.assertFalse(gate.is_limit_error(None, "timeout", "read timed out"))


class ResumeActionTest(unittest.TestCase):
    ENTRY = {"interrupted_at": (NOW - timedelta(hours=2)).isoformat(), "resumes": 0}
    WITHIN = timedelta(hours=12)

    def action(self, entry=None, decision=OPEN, user=False, answered=False, now=NOW):
        return gate.resume_action(entry or self.ENTRY, {"decision": decision}, now, self.WITHIN, user, answered)

    def test_resumes_once_the_plan_is_open(self):
        self.assertEqual(self.action(), gate.RESUME)

    def test_waits_while_the_plan_is_paused_or_unread(self):
        self.assertEqual(self.action(decision=PAUSED), gate.WAIT)
        self.assertEqual(gate.resume_action(self.ENTRY, None, NOW, self.WITHIN, False, False), gate.WAIT)

    def test_expiry_counts_from_the_first_stop(self):
        entry = {**self.ENTRY, "first_interrupted_at": (NOW - timedelta(hours=13)).isoformat()}
        self.assertEqual(self.action(entry), gate.EXPIRED)

    def test_you_or_the_bot_moved_on(self):
        self.assertEqual(self.action(user=True), gate.SKIPPED_USER)
        self.assertEqual(self.action(answered=True), gate.SKIPPED_RECOVERED)

    def test_gives_up_after_the_last_resume(self):
        self.assertEqual(self.action({**self.ENTRY, "resumes": gate.MAX_RESUMES}), gate.GAVE_UP)

    def test_settings(self):
        s = gate.settings_from({"resume_within_hours": 0, "resume_message": "  Go on.  "})
        self.assertTrue(s.resume_interrupted_chats)
        self.assertEqual((s.resume_within_hours, s.resume_message), (0.5, "Go on."))
        self.assertFalse(gate.settings_from({"resume_interrupted_chats": False}).resume_interrupted_chats)


class InterruptedChatTest(RuntimeTest):
    SID = "20261012_224043_e574c8"
    LIMIT = {"status_code": 429, "reason": "rate_limit",
             "error": {"type": "ClaudeAPIError", "message": "HTTP 429: You've hit your session limit"}}

    def setUp(self):
        super().setUp()
        import sqlite3
        self.root = Path(os.environ["USAGE_GATE_HERMES_ROOT"])
        self.db = sqlite3.connect(self.root / "profiles" / "cto" / "state.db")
        self.db.executescript("create table sessions (id text primary key, title text);"
                              "create table messages (id integer primary key, session_id text, role text, content text,"
                              " tool_calls text, display_kind text, timestamp real);")
        self.db.execute("insert into sessions values (?, ?)", (self.SID, "First 1:1"))
        self.db.commit()
        self.kanban_env = mock.patch.dict(os.environ, {}, clear=False)
        self.kanban_env.start()
        os.environ.pop("HERMES_KANBAN_TASK", None)

    def tearDown(self):
        self.kanban_env.stop()
        self.db.close()
        super().tearDown()

    def message(self, role, at, content="text", tool_calls=None, display_kind=None):
        self.db.execute("insert into messages (session_id, role, content, tool_calls, display_kind, timestamp)"
                        " values (?, ?, ?, ?, ?, ?)", (self.SID, role, content, tool_calls, display_kind, at.timestamp()))
        self.db.commit()

    def stop(self, at=NOW, platform="desktop", **overrides):
        with mock.patch.object(runtime, "utcnow", return_value=at), \
             mock.patch.object(runtime, "_current_profile", return_value="default"):
            runtime.on_api_request_error(session_id=self.SID, platform=platform, provider="claude",
                                         **{**self.LIMIT, **overrides})

    def refresh(self, at, claude=OPEN):
        used = 5 if claude == OPEN else 100
        reading = lambda provider, models: Reading(provider, at, windows=(
            {"label": "Current session", "kind": "session", "used_percent": used if provider == "claude" else 5},))
        with mock.patch.object(runtime, "fetch", side_effect=reading), NO_CARDS, \
             mock.patch.object(runtime, "utcnow", return_value=at), \
             mock.patch.object(runtime, "_hermes", return_value=SimpleNamespace(returncode=0, stderr="")), \
             mock.patch.object(runtime, "start_resume") as start:
            runtime.refresh(force=True)
        return start

    def entry(self):
        return runtime.load_interruptions()[f"cto:{self.SID}"]

    def test_desktop_chat_is_kept_under_the_profile_that_holds_it(self):
        self.stop()
        entry = self.entry()
        self.assertEqual((entry["profile"], entry["status"], entry["resumes"]), ("cto", "waiting", 0))
        self.assertEqual(entry["first_interrupted_at"], NOW.isoformat())

    def test_board_workers_schedules_and_other_failures_are_ignored(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_1"}):
            self.stop()
        self.stop(platform="cron")
        self.stop(status_code=500, reason="server_error", error={"type": "APIError", "message": "Internal error"})
        self.assertEqual(runtime.load_interruptions(), {})

    def test_waits_while_paused_then_resumes_once(self):
        self.stop()
        self.assertFalse(self.refresh(NOW + timedelta(minutes=10), claude=PAUSED).called)
        self.assertEqual(self.entry()["status"], "waiting")
        start = self.refresh(NOW + timedelta(hours=2))
        start.assert_called_once_with("cto", self.SID, gate.DEFAULTS["resume_message"])
        self.assertEqual((self.entry()["status"], self.entry()["resumes"]), ("resumed", 1))
        self.assertFalse(self.refresh(NOW + timedelta(hours=2, minutes=10)).called)

    def test_the_failed_turn_note_is_not_an_answer(self):
        self.stop()
        self.message("assistant", NOW + timedelta(seconds=1), "Your request was not processed.", display_kind="failed_turn")
        self.message("assistant", NOW + timedelta(seconds=2), "", tool_calls='[{"id": "x"}]')
        self.assertTrue(self.refresh(NOW + timedelta(hours=2)).called)

    def test_an_answer_or_your_message_drops_it(self):
        self.stop()
        self.message("assistant", NOW + timedelta(minutes=1), "Done.")
        self.assertFalse(self.refresh(NOW + timedelta(hours=2)).called)
        self.assertEqual(self.entry()["status"], gate.SKIPPED_RECOVERED)
        self.stop(at=NOW + timedelta(hours=3))
        self.message("user", NOW + timedelta(hours=3, minutes=1), "Never mind")
        self.assertFalse(self.refresh(NOW + timedelta(hours=4)).called)
        self.assertEqual(self.entry()["status"], gate.SKIPPED_USER)

    def test_a_resume_that_hits_the_limit_again_continues_the_same_stop(self):
        self.stop()
        self.refresh(NOW + timedelta(hours=2))
        self.stop(at=NOW + timedelta(hours=2, minutes=1), platform="cli")
        entry = self.entry()
        self.assertEqual((entry["status"], entry["resumes"], entry["platform"]), ("waiting", 1, "desktop"))
        self.assertEqual(entry["first_interrupted_at"], NOW.isoformat())

    def test_a_later_stop_after_a_resume_starts_over(self):
        self.stop()
        self.refresh(NOW + timedelta(hours=2))
        self.stop(at=NOW + timedelta(days=2))
        self.assertEqual((self.entry()["resumes"], self.entry()["first_interrupted_at"]),
                         (0, (NOW + timedelta(days=2)).isoformat()))

    def test_a_group_chat_turn_is_left_to_the_room(self):
        self.db.execute("insert into sessions values (?, ?)", ("20261013_120000_aaaaaa", "Group: r1 · main"))
        self.db.commit()
        with mock.patch.object(runtime, "utcnow", return_value=NOW), \
             mock.patch.object(runtime, "_current_profile", return_value="cto"):
            runtime.on_api_request_error(session_id="20261013_120000_aaaaaa", platform="desktop", provider="claude",
                                         **self.LIMIT)
        self.assertEqual(runtime.load_interruptions(), {})

    def test_status_view_lists_it_with_its_title(self):
        self.stop()
        runtime.save_state({"providers": {"claude": {"decision": OPEN, "plan": "Claude Max 5x"}}})
        with NO_CARDS:
            view = runtime.status_view()
        self.assertEqual([(c["profile"], c["title"], c["status"], c["label"]) for c in view["interrupted"]],
                         [("cto", "First 1:1", "waiting", "Claude Max 5x")])


class TokenUsageTest(RuntimeTest):
    def store(self, profile, sessions, usage):
        import sqlite3
        home = Path(os.environ["USAGE_GATE_HERMES_ROOT"]) / "profiles" / profile
        home.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(home / "state.db")
        db.executescript("create table sessions (id text primary key, title text, started_at real);"
                         "create table session_model_usage (session_id text, model text, billing_provider text default '',"
                         " task text default '', api_call_count int, input_tokens int, output_tokens int,"
                         " cache_read_tokens int, cache_write_tokens int, reasoning_tokens int);")
        db.executemany("insert into sessions values (?, '', ?)", [(sid, at.timestamp()) for sid, at in sessions])
        db.executemany("insert into session_model_usage values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", usage)
        db.commit()
        db.close()

    def setUp(self):
        super().setUp()
        self.store("cto", [("s1", NOW - timedelta(days=1)), ("s2", NOW - timedelta(days=2)), ("old", NOW - timedelta(days=10))], [
            ("s1", "opus", "claude", "", 10, 100, 50, 1000, 20, 5),
            ("s2", "opus", "claude", "", 5, 10, 5, 100, 2, 1),
            ("s1", "haiku", "claude", "title_generation", 1, 500, 20, 0, 900, 0),
            ("old", "opus", "claude", "", 99, 9999, 9999, 9999, 9999, 9999)])
        (Path(os.environ["USAGE_GATE_HERMES_ROOT"]) / "profiles" / "ceo" / "state.db").write_text("not a database")

    def test_per_bot_task_and_model_within_the_period(self):
        rows = runtime.token_usage(7, now=NOW)["rows"]
        self.assertEqual([(r["bot"], r["task"], r["model"], r["calls"], r["input"], r["cache_read"]) for r in rows],
                         [("cto", "main", "opus", 15, 110, 1100), ("cto", "title_generation", "haiku", 1, 500, 0)])

    def test_rows_carry_the_plan_label(self):
        (Path(os.environ["USAGE_GATE_HERMES_ROOT"]) / "config.yaml").write_text(
            "usage_gate:\n  providers:\n    claude:\n      label: Claude Pro\n")
        self.assertEqual({r["label"] for r in runtime.token_usage(7, now=NOW)["rows"]}, {"Claude Pro"})
        runtime.save_state({"providers": {"claude": {"decision": OPEN, "plan": "Claude Max 5x"}}})
        self.assertEqual({r["label"] for r in runtime.token_usage(7, now=NOW)["rows"]}, {"Claude Max 5x"})

    def test_all_time_includes_old_sessions(self):
        main = runtime.token_usage(None, now=NOW)["rows"][0]
        self.assertEqual((main["calls"], main["output"]), (114, 10054))


class FableWeekTest(unittest.TestCase):
    """Max carries a weekly cap on Fable alone next to the account's own windows."""

    def snap(self, fable_week, session=10, week=10):
        return snapshot(("Current session", session, "account"), ("Current week", week, "account"),
                        ("Fable week", fable_week, "account"))

    def test_family_names(self):
        self.assertEqual([gate.model_family(t) for t in ("fable", "claude-fable-5-1", "Fable week", "gpt-6.1-sol",
                                                         "Current week (Opus)", "Console week", "Current session")],
                         ["fable", "fable", "fable", "sol", "opus", None, None])

    def test_fable_week_counts_only_when_a_bot_runs_fable(self):
        without = gate.reading_from_snapshot("claude", self.snap(95), NOW, models=["opus"])
        self.assertEqual([w["label"] for w in without.windows], ["Current session", "Current week"])
        with_fable = gate.reading_from_snapshot("claude", self.snap(95), NOW, models=["opus", "fable"])
        self.assertEqual([(w["label"], w["model"]) for w in with_fable.windows],
                         [("Current session", None), ("Current week", None), ("Fable week", "fable")])

    def test_full_fable_week_pauses_only_fable(self):
        state = gate.decide(gate.reading_from_snapshot("claude", self.snap(85), NOW, models=["opus", "fable"]), T, None,
                            timedelta(minutes=30))
        self.assertEqual((state["decision"], state["paused_models"]), (OPEN, ["fable"]))
        self.assertIn("only fable paused", state["reason"])
        profiles = {"cto": ("claude", "fable"), "backend": ("claude", "opus"), "qa": ("codex", "gpt-6.1-sol")}
        self.assertEqual(gate.allowlist(profiles, {"claude": state, "codex": {"decision": OPEN}}), ["backend", "qa"])
        self.assertEqual([w["label"] for w in gate.held_windows(state, "fable")], ["Fable week"])
        self.assertEqual(gate.held_windows(state, "opus"), [])

    def test_a_full_account_window_still_pauses_everyone(self):
        state = gate.decide(gate.reading_from_snapshot("claude", self.snap(85, session=90), NOW, models=["opus", "fable"]),
                            T, None, timedelta(minutes=30))
        self.assertEqual((state["decision"], state["paused_models"]), (PAUSED, ["fable"]))
        self.assertEqual(gate.allowlist({"cto": ("claude", "fable"), "backend": ("claude", "opus")}, {"claude": state}), [])
        self.assertEqual([w["label"] for w in gate.held_windows(state, "opus")], ["Current session"])

    def test_model_pause_episode_is_stable_and_kept_when_unreadable(self):
        first = gate.decide(gate.reading_from_snapshot("claude", self.snap(85), NOW, models=["fable"]), T, None,
                            timedelta(minutes=30))
        later = gate.decide(gate.reading_from_snapshot("claude", self.snap(90), NOW + timedelta(minutes=10), models=["fable"]),
                            T, first, timedelta(minutes=30))
        self.assertEqual(gate.held_episode(later, "fable"), gate.held_episode(first, "fable"))
        kept = gate.decide(Reading("claude", NOW + timedelta(minutes=20), error="HTTP 429"), T, later, timedelta(minutes=30))
        self.assertEqual(kept["paused_models"], ["fable"])
        expired = gate.decide(Reading("claude", NOW + timedelta(hours=2), error="HTTP 429"), T, later, timedelta(minutes=30))
        self.assertNotIn("paused_models", expired)

    def test_an_interrupted_fable_chat_waits_while_fable_is_paused(self):
        state = {"decision": OPEN, "paused_models": ["fable"]}
        entry = {"interrupted_at": NOW.isoformat(), "resumes": 0}
        wait = gate.resume_action({**entry, "model": "fable"}, state, NOW, timedelta(hours=12), False, False)
        go = gate.resume_action({**entry, "model": "opus"}, state, NOW, timedelta(hours=12), False, False)
        self.assertEqual((wait, go), (gate.WAIT, gate.RESUME))

    def test_held_cards_of_fable_bots_only(self):
        state = gate.decide(gate.reading_from_snapshot("claude", self.snap(85), NOW, models=["opus", "fable"]), T, None,
                            timedelta(minutes=30))
        cards = {"ready": [{"id": "t_1", "assignee": "cto"}, {"id": "t_2", "assignee": "backend"}], "review": []}
        comments = []

        def kanban(*args):
            if args[0] == "list":
                return SimpleNamespace(returncode=0, stdout=json.dumps(cards[args[2]]), stderr="")
            comments.append((args[1], args[-1]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(runtime, "_kanban", side_effect=kanban), mock.patch.object(runtime, "_timezone", return_value=None):
            runtime.comment_held_cards(gate.settings_from({"providers": {"claude": {"label": "Claude Max"}}}),
                                       {"cto": ("claude", "fable"), "backend": ("claude", "opus")}, {"claude": state}, {})
        self.assertEqual([c for c, _ in comments], ["t_1"])
        self.assertIn("Claude Max fable week at 85%", comments[0][1])


class OpenRouterTest(unittest.TestCase):
    SETTINGS = gate.settings_from({"openrouter": {"workspace_id": "ws1", "label": "OpenRouter"}}).openrouter
    KEY = {"name": "Hermes", "limit": 20, "limit_reset": "monthly", "limit_remaining": 19.84, "usage": 0.16,
           "usage_daily": 0.05, "usage_weekly": 0.16, "usage_monthly": 0.16}

    def test_healthy_numbers_have_no_warning(self):
        state = gate.openrouter_state({"total_credits": 10, "total_usage": 0.16}, [self.KEY], self.SETTINGS, NOW)
        self.assertAlmostEqual(state["balance"], 9.84)
        self.assertEqual(state["warnings"], [])
        self.assertAlmostEqual(state["keys"][0]["spent"], 0.16)

    def test_low_balance_and_a_key_near_its_limit_warn(self):
        key = {**self.KEY, "limit_remaining": 3.0}
        state = gate.openrouter_state({"total_credits": 10, "total_usage": 8.5}, [key], self.SETTINGS, NOW)
        self.assertEqual(state["warnings"], ["Balance $1.50 is under $2.00", "Key Hermes at 85% of its $20.00 monthly limit"])

    def test_disabled_keys_are_left_out(self):
        state = gate.openrouter_state({"total_credits": 10, "total_usage": 0}, [{**self.KEY, "disabled": True}], self.SETTINGS, NOW)
        self.assertEqual(state["keys"], [])

    def test_an_unreadable_api_keeps_the_last_numbers(self):
        good = gate.openrouter_state({"total_credits": 10, "total_usage": 0.16}, [self.KEY], self.SETTINGS, NOW)
        bad = gate.openrouter_state(None, None, self.SETTINGS, NOW + timedelta(minutes=10), error="HTTP 503", previous=good)
        self.assertEqual((bad["balance"], bad["error"], bad["last_reading_at"]), (good["balance"], "HTTP 503", NOW.isoformat()))

    def test_a_balance_under_the_hold_limit_holds_until_it_is_back(self):
        low = gate.openrouter_state({"total_credits": 10, "total_usage": 8.5}, [], self.SETTINGS, NOW)
        lower = gate.openrouter_state({"total_credits": 10, "total_usage": 9}, [], self.SETTINGS,
                                      NOW + timedelta(minutes=10), previous=low)
        topped = gate.openrouter_state({"total_credits": 20, "total_usage": 9}, [], self.SETTINGS,
                                       NOW + timedelta(minutes=20), previous=lower)
        self.assertTrue(low["holding"] and lower["holding"])
        self.assertEqual(lower["held_since"], low["held_since"])
        self.assertFalse(topped["holding"])
        self.assertNotIn("held_since", topped)

    def test_an_unreadable_api_keeps_a_hold_only_for_the_hold_window(self):
        low = gate.openrouter_state({"total_credits": 10, "total_usage": 8.5}, [], self.SETTINGS, NOW)
        kept = gate.openrouter_state(None, None, self.SETTINGS, NOW + timedelta(minutes=10), error="HTTP 503",
                                     previous=low, hold=timedelta(minutes=30))
        released = gate.openrouter_state(None, None, self.SETTINGS, NOW + timedelta(minutes=40), error="HTTP 503",
                                         previous=kept, hold=timedelta(minutes=30))
        self.assertTrue(kept["holding"])
        self.assertFalse(released["holding"])

    def test_a_zero_hold_limit_only_watches(self):
        settings = gate.settings_from({"openrouter": {"balance_hold_usd": 0}}).openrouter
        state = gate.openrouter_state({"total_credits": 10, "total_usage": 10}, [], settings, NOW)
        self.assertFalse(state["holding"])
        self.assertEqual(gate.allowlist({"cto": ("claude", "opus")}, {}, hold_all=True), [])

    def test_settings_defaults_and_bounds(self):
        o = gate.settings_from({"openrouter": {"balance_warn_usd": -5, "key_warn_percent": 0}}).openrouter
        self.assertEqual((o["enabled"], o["key_env"], o["balance_warn_usd"], o["key_warn_percent"]),
                         (True, "OPENROUTER_MANAGEMENT_KEY", 0.0, 1.0))

    def test_refresh_reads_credits_and_the_workspace_keys(self):
        settings = gate.settings_from({"openrouter": {"workspace_id": "ws1"}})
        calls = []

        def get(path, key):
            calls.append((path, key))
            return {"total_credits": 10, "total_usage": 1} if path == "/credits" else [self.KEY]

        with mock.patch.dict(os.environ, {"OPENROUTER_MANAGEMENT_KEY": "sk-test"}), \
             mock.patch.object(runtime, "_openrouter_get", side_effect=get):
            state = runtime.refresh_openrouter(settings, None)
        self.assertEqual(calls, [("/credits", "sk-test"), ("/keys?workspace_id=ws1", "sk-test")])
        self.assertEqual(state["balance"], 9.0)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(runtime.refresh_openrouter(settings, None))


if __name__ == "__main__":
    unittest.main()
