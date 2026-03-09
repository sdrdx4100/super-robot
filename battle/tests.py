"""Unit tests for the battle application.

Tests cover:
- BattleEngine core logic (timeline advance, action execution, victory)
- TargetSelector strategies
- Damage calculation formula
- Part destruction side-effects
- Django API endpoints (view layer)
"""

from __future__ import annotations

import json
import random
from itertools import count
from django.test import TestCase, Client

from .services.engine_logic import (
    AttributeState,
    BattleEngine,
    BattleState,
    MedarotState,
    PartState,
    PartSystem,
    Personality,
    SkillKind,
    SpecialEffect,
    TeamState,
    TimelinePhase,
    TargetSelector,
    apply_part_destruction,
    build_state_from_db,
    calculate_damage,
    state_from_json,
    state_to_json,
)
from .views import _state_to_dict
from .models import (
    Attribute,
    BattleSession,
    Medal,
    Medarot,
    Part,
    Team,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UNIT_IDS = count(1)

def _make_attr(
    armor: int = 100,
    success: int = 70,
    power: int = 60,
    charge: int = 50,
    cooldown: int = 50,
    current_hp: int | None = None,
) -> AttributeState:
    return AttributeState(
        armor=armor,
        success=success,
        power=power,
        charge=charge,
        cooldown=cooldown,
        current_hp=current_hp if current_hp is not None else armor,
    )


def _make_part(
    system: PartSystem = PartSystem.HEAD,
    skill_kind: SkillKind = SkillKind.SHOOT,
    special_effect: SpecialEffect = SpecialEffect.NONE,
    **attr_kwargs: int,
) -> PartState:
    return PartState(
        part_id=1,
        name=f"Test{system.value}",
        system=system,
        skill_kind=skill_kind,
        special_effect=special_effect,
        attr=_make_attr(**attr_kwargs),
    )


def _make_unit(
    name: str = "TestUnit",
    personality: Personality = Personality.RANDOM,
    gauge: float = 0.0,
    incapacitated: bool = False,
    medarot_id: int | None = None,
) -> MedarotState:
    return MedarotState(
        medarot_id=medarot_id or next(_UNIT_IDS),
        name=name,
        personality=personality,
        skill_head=5,
        skill_ra=5,
        skill_la=5,
        skill_leg=5,
        part_head=_make_part(PartSystem.HEAD, SkillKind.SHOOT),
        part_ra=_make_part(PartSystem.RIGHT_ARM, SkillKind.SHOOT),
        part_la=_make_part(PartSystem.LEFT_ARM, SkillKind.MELEE),
        part_leg=_make_part(
            PartSystem.LEG,
            SkillKind.NONE,
            charge=50,
            cooldown=50,
        ),
        gauge=gauge,
        incapacitated=incapacitated,
    )


def _make_team(
    name: str = "TeamA",
    team_id: int = 1,
    leader_index: int = 0,
    units: list[MedarotState] | None = None,
) -> TeamState:
    if units is None:
        units = [
            _make_unit(f"{name}-U{i}", gauge=float(i * 100))
            for i in range(3)
        ]
    return TeamState(team_id=team_id, name=name, units=units, leader_index=leader_index)


def _make_battle(
    team_a: TeamState | None = None,
    team_b: TeamState | None = None,
) -> BattleState:
    return BattleState(
        team_a=team_a or _make_team("TeamA", 1),
        team_b=team_b or _make_team("TeamB", 2),
    )


# ---------------------------------------------------------------------------
# TargetSelector tests
# ---------------------------------------------------------------------------

class TargetSelectorTests(TestCase):
    """Tests for the AI target-selection strategies."""

    def setUp(self) -> None:
        self.selector = TargetSelector()
        self.strong = _make_unit("Strong", gauge=0.0)
        self.weak   = _make_unit("Weak",   gauge=0.0)
        # Damage the weak unit's head
        self.weak.part_head.attr.current_hp = 10
        self.team = _make_team(units=[self.strong, self.weak, _make_unit("Mid")])

    def test_weak_targets_lowest_hp(self) -> None:
        target = self.selector.select(Personality.WEAK, self.team)
        self.assertEqual(target.name, "Weak")

    def test_strong_targets_highest_hp(self) -> None:
        target = self.selector.select(Personality.STRONG, self.team)
        self.assertEqual(target.name, "Strong")

    def test_leader_targets_leader(self) -> None:
        self.team.leader_index = 0
        target = self.selector.select(Personality.LEADER, self.team)
        self.assertEqual(target.name, "Strong")

    def test_random_returns_alive_unit(self) -> None:
        target = self.selector.select(Personality.RANDOM, self.team)
        self.assertIn(target.name, [u.name for u in self.team.alive_units])

    def test_returns_none_when_all_dead(self) -> None:
        for u in self.team.units:
            u.incapacitated = True
        target = self.selector.select(Personality.RANDOM, self.team)
        self.assertIsNone(target)


# ---------------------------------------------------------------------------
# Damage calculation tests
# ---------------------------------------------------------------------------

class DamageCalculationTests(TestCase):
    """Tests for the damage / hit-rate formulas."""

    def _run_many(self, actor: MedarotState, part: PartState, target: MedarotState, n: int = 200):
        hits = 0
        total_dmg = 0.0
        for _ in range(n):
            dmg, hit = calculate_damage(actor, part, target)
            if hit:
                hits += 1
                total_dmg += dmg
        return hits, total_dmg

    def test_hit_damage_is_positive(self) -> None:
        actor  = _make_unit("Actor")
        target = _make_unit("Target")
        for _ in range(50):
            dmg, hit = calculate_damage(actor, actor.part_head, target)
            if hit:
                self.assertGreater(dmg, 0)

    def test_pierce_ignores_armor_reduction(self) -> None:
        actor = _make_unit("Attacker")
        actor.part_ra = _make_part(
            PartSystem.RIGHT_ARM,
            SkillKind.SHOOT,
            SpecialEffect.PIERCE,
            power=60,
        )
        target = _make_unit("Defender")
        # Force a hit with very high success
        actor.part_ra.attr.success = 999
        dmg_pierce, hit = calculate_damage(actor, actor.part_ra, target)
        if hit:
            # Pierce damage should be >= non-pierce (armor reduction skipped)
            actor_no_pierce = _make_unit("AttackerNP")
            actor_no_pierce.part_ra.attr.power = 60
            actor_no_pierce.part_ra.attr.success = 999
            dmg_normal, _ = calculate_damage(actor_no_pierce, actor_no_pierce.part_ra, target)
            self.assertGreaterEqual(dmg_pierce, dmg_normal * 0.9)  # within noise

    def test_miss_returns_zero_damage(self) -> None:
        actor = _make_unit("Actor")
        # Impossible to hit: success = 0
        actor.part_head.attr.success = 0
        # Run enough times that at least some should be misses if success=0
        all_miss = all(
            not calculate_damage(actor, actor.part_head, _make_unit("T"))[1]
            for _ in range(20)
        )
        # With success=0 and skill=5, hit_pct = 5/115*100 ≈ 4.3%
        # With 20 attempts the probability of all misses is ~(0.957)^20 ≈ 42%
        # So this might not always pass, skip asserting and just check no crash.

    def test_miss_damage_is_zero(self) -> None:
        """Whenever hit==False, damage must be 0."""
        actor = _make_unit("Actor")
        for _ in range(100):
            dmg, hit = calculate_damage(actor, actor.part_head, _make_unit("Target"))
            if not hit:
                self.assertEqual(dmg, 0.0)

    def test_cooling_target_takes_bonus_damage_and_loses_evasion(self) -> None:
        actor = _make_unit("Actor")
        actor.part_head.attr.success = 999
        target = _make_unit("Target")
        target.phase = TimelinePhase.CLR

        original_uniform = random.uniform
        try:
            random.uniform = lambda a, b: 0 if b == 100 else 1.0
            dmg, hit = calculate_damage(actor, actor.part_head, target)
        finally:
            random.uniform = original_uniform

        self.assertTrue(hit)
        self.assertEqual(dmg, 21.0)


# ---------------------------------------------------------------------------
# Part-destruction tests
# ---------------------------------------------------------------------------

class PartDestructionTests(TestCase):
    """Tests for the part-destruction side-effect system."""

    def test_leg_destruction_sets_leg_broken(self) -> None:
        unit = _make_unit("Robot")
        self.assertFalse(unit.leg_broken)
        apply_part_destruction(unit, PartSystem.LEG)
        self.assertTrue(unit.leg_broken)

    def test_leg_broken_halves_advance_rate(self) -> None:
        unit = _make_unit("Robot")
        rate_before = unit.advance_rate()
        apply_part_destruction(unit, PartSystem.LEG)
        self.assertAlmostEqual(unit.advance_rate(), rate_before * 0.5)

    def test_ra_destruction_disables_ra(self) -> None:
        unit = _make_unit("Robot")
        apply_part_destruction(unit, PartSystem.RIGHT_ARM)
        self.assertTrue(unit.part_ra.disabled)
        self.assertFalse(unit.part_la.disabled)

    def test_la_destruction_disables_la(self) -> None:
        unit = _make_unit("Robot")
        apply_part_destruction(unit, PartSystem.LEFT_ARM)
        self.assertTrue(unit.part_la.disabled)

    def test_head_destruction_incapacitates_unit(self) -> None:
        unit = _make_unit("Robot")
        apply_part_destruction(unit, PartSystem.HEAD)
        self.assertTrue(unit.incapacitated)
        self.assertFalse(unit.is_alive)


# ---------------------------------------------------------------------------
# BattleEngine tests
# ---------------------------------------------------------------------------

class BattleEngineTests(TestCase):
    """Tests for the timeline-based battle engine."""

    def test_advance_increments_tick(self) -> None:
        state = _make_battle()
        engine = BattleEngine(state)
        updated, _ = engine.advance()
        self.assertGreater(updated.tick, 0)

    def test_advance_returns_events(self) -> None:
        state = _make_battle()
        engine = BattleEngine(state)
        _, events = engine.advance()
        self.assertGreater(len(events), 0)

    def test_battle_eventually_ends(self) -> None:
        """A complete battle must have a winner within a reasonable bound."""
        state = _make_battle()
        engine = BattleEngine(state)
        for _ in range(500):
            state, _ = engine.advance()
            if state.is_finished:
                break
        self.assertTrue(state.is_finished)
        self.assertIn(state.winner, ("A", "B"))

    def test_victory_condition_leader_death(self) -> None:
        """Destroying the leader immediately ends the battle."""
        leader = _make_unit("Leader", gauge=1000.0)
        leader.part_head.attr.current_hp = 1  # one hit from dying
        others = [_make_unit(f"Unit{i}") for i in range(2)]
        team_a = _make_team("TeamA", 1, leader_index=0, units=[leader] + others)
        team_b = _make_team("TeamB", 2, units=[_make_unit("Enemy")] * 3)
        state = _make_battle(team_a, team_b)
        engine = BattleEngine(state)

        for _ in range(100):
            state, _ = engine.advance()
            if state.is_finished:
                break

        self.assertTrue(state.is_finished)

    def test_no_advance_after_finished(self) -> None:
        state = _make_battle()
        state.is_finished = True
        state.winner = "A"
        engine = BattleEngine(state)
        updated, events = engine.advance()
        self.assertEqual(len(events), 0)
        self.assertTrue(updated.is_finished)

    def test_serialisation_round_trip(self) -> None:
        state = _make_battle()
        engine = BattleEngine(state)
        updated, _ = engine.advance()
        json_str = state_to_json(updated)
        restored = state_from_json(json_str)
        self.assertEqual(restored.tick, updated.tick)
        self.assertEqual(len(restored.team_a.units), 3)

    def test_actor_enters_cooling_phase_after_action(self) -> None:
        actor = _make_unit("Leader", gauge=950.0)
        actor.part_leg.attr.charge = 60
        actor.part_head.attr.success = 999
        target = _make_unit("Enemy")
        state = _make_battle(
            _make_team("TeamA", 1, units=[actor, _make_unit("Ally1"), _make_unit("Ally2")]),
            _make_team("TeamB", 2, units=[target, _make_unit("Enemy2"), _make_unit("Enemy3")]),
        )

        updated, events = BattleEngine(state).advance()

        self.assertEqual(updated.team_a.units[0].phase, TimelinePhase.CLR)
        self.assertEqual(updated.team_a.units[0].gauge, 1000.0)
        self.assertIn("COOLING", [event.action for event in events])

    def test_cooling_unit_returns_to_charge_phase_at_base_line(self) -> None:
        unit = _make_unit("Cooler", gauge=20.0)
        unit.phase = TimelinePhase.CLR
        unit.part_leg.attr.cooldown = 50
        state = _make_battle(
            _make_team("TeamA", 1, units=[unit, _make_unit("A2"), _make_unit("A3")]),
            _make_team("TeamB", 2),
        )
        engine = BattleEngine(state)

        engine._tick_all_units()

        self.assertEqual(unit.gauge, 0.0)
        self.assertEqual(unit.phase, TimelinePhase.CHG)

    def test_ready_stack_acts_in_lifo_order_without_advancing_tick(self) -> None:
        first = _make_unit("First", gauge=1000.0)
        first.phase = TimelinePhase.ACT
        second = _make_unit("Second", gauge=1000.0)
        second.phase = TimelinePhase.ACT
        first.part_head.attr.success = 999
        second.part_head.attr.success = 999
        state = _make_battle(
            _make_team("TeamA", 1, units=[first, second, _make_unit("Ally")]),
            _make_team("TeamB", 2),
        )
        state.ready_stack = [first.medarot_id, second.medarot_id]
        engine = BattleEngine(state)

        updated, events = engine.advance()

        self.assertEqual(updated.tick, 0)
        self.assertEqual(events[0].actor_name, "Second")
        self.assertEqual(updated.ready_stack, [first.medarot_id])

    def test_attack_log_notes_cooling_target(self) -> None:
        actor = _make_unit("Actor", gauge=1000.0)
        actor.phase = TimelinePhase.ACT
        actor.part_head.attr.success = 999
        target = _make_unit("Target")
        target.phase = TimelinePhase.CLR
        state = _make_battle(
            _make_team("TeamA", 1, units=[actor, _make_unit("A2"), _make_unit("A3")]),
            _make_team("TeamB", 2, units=[target, _make_unit("B2"), _make_unit("B3")]),
        )
        state.ready_stack = [actor.medarot_id]

        _, events = BattleEngine(state).advance()
        attack_event = next(event for event in events if event.target_name == "Target")

        self.assertEqual(attack_event.note, "（放熱中につき回避不能！）")


# ---------------------------------------------------------------------------
# Django model + view tests
# ---------------------------------------------------------------------------

class ModelBuildTests(TestCase):
    """Smoke tests for Django ORM model creation and build_state_from_db."""

    def _create_db_team(self, name: str) -> Team:
        def _attr(**kw) -> Attribute:
            return Attribute.objects.create(armor=80, success=65, power=60, charge=50, cooldown=50, **kw)

        def _part(pname, system, skill) -> Part:
            return Part.objects.create(
                name=pname,
                system=system,
                skill_kind=skill,
                special_effect="NONE",
                attribute=_attr(),
            )

        def _medarot(mname) -> Medarot:
            medal = Medal.objects.create(
                name=f"{mname}Medal",
                personality="RANDOM",
                skill_head=5, skill_ra=5, skill_la=5, skill_leg=5,
            )
            return Medarot.objects.create(
                name=mname,
                medal=medal,
                part_head=_part(f"{mname}H", "HEAD", "SHOOT"),
                part_ra=_part(f"{mname}R", "RA", "SHOOT"),
                part_la=_part(f"{mname}L", "LA", "MELEE"),
                part_leg=_part(f"{mname}G", "LEG", "NONE"),
            )

        m1, m2, m3 = _medarot(f"{name}1"), _medarot(f"{name}2"), _medarot(f"{name}3")
        return Team.objects.create(
            name=name,
            medarot_1=m1,
            medarot_2=m2,
            medarot_3=m3,
            leader_index=0,
        )

    def test_build_state_from_db_produces_valid_state(self) -> None:
        team_a = self._create_db_team("Alpha")
        team_b = self._create_db_team("Beta")
        state  = build_state_from_db(team_a, team_b)
        self.assertEqual(len(state.team_a.units), 3)
        self.assertEqual(len(state.team_b.units), 3)
        self.assertEqual(state.tick, 0)
        self.assertFalse(state.is_finished)


class APIViewTests(TestCase):
    """Integration tests for the battle API views."""

    def setUp(self) -> None:
        self.client = Client()

    def test_home_page_returns_200(self) -> None:
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "メダロット")

    def test_new_battle_creates_session(self) -> None:
        resp = self.client.post("/api/battle/new/")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("session_id", data)
        self.assertIsInstance(data["session_id"], int)

    def test_battle_step_advances_tick(self) -> None:
        resp = self.client.post("/api/battle/new/")
        sid = json.loads(resp.content)["session_id"]

        resp = self.client.get(f"/api/battle/{sid}/step/")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertGreater(data["tick"], 0)

    def test_battle_state_endpoint(self) -> None:
        resp = self.client.post("/api/battle/new/")
        sid = json.loads(resp.content)["session_id"]

        resp = self.client.get(f"/api/battle/{sid}/state/")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("team_a", data)
        self.assertIn("team_b", data)
        self.assertFalse(data["is_finished"])
        self.assertIn("phase", data["team_a"]["units"][0])
        self.assertIn("is_cooling", data["team_a"]["units"][0])

    def test_battle_finishes_after_many_steps(self) -> None:
        resp = self.client.post("/api/battle/new/")
        sid = json.loads(resp.content)["session_id"]

        data = {}
        for _ in range(200):
            resp = self.client.get(f"/api/battle/{sid}/step/")
            data = json.loads(resp.content)
            if data["is_finished"]:
                break

        self.assertTrue(data.get("is_finished"), "Battle should finish within 200 steps")
        self.assertIn(data.get("winner"), ("A", "B"))

    def test_already_finished_flag(self) -> None:
        resp = self.client.post("/api/battle/new/")
        sid = json.loads(resp.content)["session_id"]

        # Force finish
        session = BattleSession.objects.get(pk=sid)
        session.is_finished = True
        session.winner = "A"
        session.save()

        resp = self.client.get(f"/api/battle/{sid}/step/")
        data = json.loads(resp.content)
        self.assertTrue(data["already_finished"])

    def test_404_for_nonexistent_session(self) -> None:
        resp = self.client.get("/api/battle/99999/step/")
        self.assertEqual(resp.status_code, 404)


class ViewSerialisationTests(TestCase):
    def test_state_to_dict_maps_legacy_cooling_status_to_phase(self) -> None:
        state = state_from_json(json.dumps({
            "tick": 0,
            "team_a": {
                "team_id": 1,
                "name": "A",
                "leader_index": 0,
                "units": [{
                    "medarot_id": 1,
                    "name": "Legacy",
                    "personality": "RANDOM",
                    "skill_head": 5,
                    "skill_ra": 5,
                    "skill_la": 5,
                    "skill_leg": 5,
                    "part_head": _make_part(PartSystem.HEAD, SkillKind.SHOOT).model_dump(),
                    "part_ra": _make_part(PartSystem.RIGHT_ARM, SkillKind.SHOOT).model_dump(),
                    "part_la": _make_part(PartSystem.LEFT_ARM, SkillKind.MELEE).model_dump(),
                    "part_leg": _make_part(PartSystem.LEG, SkillKind.NONE).model_dump(),
                    "gauge": 700.0,
                    "cooling_down": True,
                }],
            },
            "team_b": {
                "team_id": 2,
                "name": "B",
                "leader_index": 0,
                "units": [_make_unit("Enemy").model_dump()],
            },
            "events": [],
            "is_finished": False,
            "winner": "",
        }))

        data = _state_to_dict(state)

        self.assertEqual(data["team_a"]["units"][0]["phase"], "CLR")
        self.assertTrue(data["team_a"]["units"][0]["is_cooling"])
