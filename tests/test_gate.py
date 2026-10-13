import json
import os
import tempfile
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


def snapshot(*windows, unavailable=None):
    return SimpleNamespace(unavailable_reason=unavailable, windows=tuple(
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

    def test_unknown_with_no_history_is_open(self):
        self.assertEqual(gate.decide(Reading("p", NOW, error="x"), T, None, timedelta(minutes=30))["decision"], OPEN)


class AllowlistTest(unittest.TestCase):
    profiles = {"default": "codex", "ceo": "codex", "cto": "claude", "web": "claude"}

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

    def test_no_second_comment_in_the_same_pause(self):
        _, comments = self.run_sweep({"t_1": "E1", "t_3": "E1"})
        self.assertEqual(comments, [])

    def test_new_pause_comments_again(self):
        providers = {**self.paused, "claude": {**self.paused["claude"], "paused_since": "E2"}}
        _, comments = self.run_sweep({"t_1": "E1", "t_3": "E1"}, providers)
        self.assertEqual(sorted(comments), ["t_1", "t_3"])

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


if __name__ == "__main__":
    unittest.main()
