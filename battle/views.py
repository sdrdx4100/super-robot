"""
Views for the 3vs3 Medarot-style Tactical Simulator.

Two endpoints are exposed:

1. ``GET /``   → renders the battle field HTML page.
   Creates a demo BattleSession if none exists.

2. ``GET /api/battle/<session_id>/step/``
   → advances the battle by one action and returns the updated state as JSON.
   The JavaScript client calls this endpoint repeatedly (polling) to animate
   the battle without page reloads.

3. ``GET /api/battle/<session_id>/state/``
   → returns the current state without advancing the battle.

4. ``POST /api/battle/new/``
   → creates a new demo battle session and returns its ID.
"""

from __future__ import annotations

import json
from typing import Any

from django.http import JsonResponse, HttpRequest, HttpResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt

from .models import (
    Attribute,
    Part,
    Medal,
    Medarot,
    Team,
    BattleSession,
    PartSystem,
    SkillKind,
    SpecialEffect,
    Personality,
)
from .services.engine_logic import (
    BattleEngine,
    BattleState,
    TimelinePhase,
    VALID_ACTION_PART_KEYS,
    build_state_from_db,
    state_from_json,
    state_to_json,
)


# ---------------------------------------------------------------------------
# Demo data factory
# ---------------------------------------------------------------------------

def _create_demo_part(
    name: str,
    system: str,
    skill_kind: str,
    armor: int = 60,
    success: int = 60,
    power: int = 40,
    charge: int = 50,
    cooldown: int = 50,
    special_effect: str = SpecialEffect.NONE,
) -> Part:
    """Create and persist a Part with a fresh Attribute for demo battles."""
    attr = Attribute.objects.create(
        armor=armor,
        success=success,
        power=power,
        charge=charge,
        cooldown=cooldown,
    )
    return Part.objects.create(
        name=name,
        system=system,
        skill_kind=skill_kind,
        special_effect=special_effect,
        attribute=attr,
    )


def _create_demo_medarot(
    name: str,
    medal_name: str,
    personality: str,
    color_prefix: str = "",
    *,
    leg_charge: int = 55,
    leg_cooldown: int = 50,
) -> Medarot:
    """Build a demo Medarot with balanced stats for ~10–20 action rounds."""
    medal = Medal.objects.create(
        name=medal_name,
        personality=personality,
        skill_head=5,
        skill_ra=6,
        skill_la=5,
        skill_leg=5,
    )
    head = _create_demo_part(
        f"{color_prefix}ヘッド",
        PartSystem.HEAD,
        SkillKind.SHOOT,
        armor=100,
        power=65,
        success=70,
    )
    ra = _create_demo_part(
        f"{color_prefix}右腕",
        PartSystem.RIGHT_ARM,
        SkillKind.SHOOT,
        armor=70,
        power=72,
        success=65,
    )
    la = _create_demo_part(
        f"{color_prefix}左腕",
        PartSystem.LEFT_ARM,
        SkillKind.MELEE,
        armor=70,
        power=78,
        success=60,
    )
    leg = _create_demo_part(
        f"{color_prefix}脚部",
        PartSystem.LEG,
        SkillKind.NONE,
        armor=80,
        charge=leg_charge,
        cooldown=leg_cooldown,
    )
    return Medarot.objects.create(
        name=name,
        medal=medal,
        part_head=head,
        part_ra=ra,
        part_la=la,
        part_leg=leg,
    )


def _build_demo_teams() -> tuple[Team, Team]:
    """Create two balanced demo teams for a sample battle."""
    # Team A
    a1 = _create_demo_medarot(
        "メタビー", "メタビーのメダル", Personality.LEADER, "赤-", leg_charge=42, leg_cooldown=60
    )
    a2 = _create_demo_medarot(
        "ロクショウ", "ロクショウのメダル", Personality.WEAK, "緑-", leg_charge=58, leg_cooldown=44
    )
    a3 = _create_demo_medarot(
        "イカロス", "イカロスのメダル", Personality.RANDOM, "青-", leg_charge=49, leg_cooldown=54
    )

    team_a = Team.objects.create(
        name="チームA",
        medarot_1=a1,
        medarot_2=a2,
        medarot_3=a3,
        leader_index=0,
    )

    # Team B (slightly varied personalities)
    b1 = _create_demo_medarot(
        "スパルタン", "スパルタンのメダル", Personality.STRONG, "金-", leg_charge=40, leg_cooldown=63
    )
    b2 = _create_demo_medarot(
        "ティグリス", "ティグリスのメダル", Personality.RANDOM, "黒-", leg_charge=56, leg_cooldown=46
    )
    b3 = _create_demo_medarot(
        "クロスファイア", "クロスファイアのメダル", Personality.LEADER, "白-", leg_charge=47, leg_cooldown=57
    )

    team_b = Team.objects.create(
        name="チームB",
        medarot_1=b1,
        medarot_2=b2,
        medarot_3=b3,
        leader_index=0,
    )

    return team_a, team_b


def _get_or_create_demo_session() -> BattleSession:
    """Return the latest unfinished session or create a fresh one."""
    session = BattleSession.objects.filter(is_finished=False).first()
    if session is None:
        team_a, team_b = _build_demo_teams()
        state = build_state_from_db(team_a, team_b)
        session = BattleSession.objects.create(
            team_a=team_a,
            team_b=team_b,
            state_json=state_to_json(state),
        )
    return session


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------

def _state_to_dict(state: BattleState) -> dict[str, Any]:
    """Convert BattleState to a plain dict suitable for JSON serialisation."""
    def part_dict(p: Any) -> dict[str, Any]:
        return {
            "name": p.name,
            "system": p.system.value,
            "skill_kind": p.skill_kind.value,
            "disabled": p.disabled,
            "current_hp": p.attr.current_hp,
            "max_hp": p.attr.armor,
        }

    def unit_dict(u: Any) -> dict[str, Any]:
        is_cooling = u.phase == TimelinePhase.CLR
        return {
            "id": u.medarot_id,
            "name": u.name,
            "personality": u.personality.value,
            "gauge": round(u.gauge, 1),
            "phase": u.phase.value,
            "is_cooling": is_cooling,
            "cooling_down": is_cooling,
            "incapacitated": u.incapacitated,
            "leg_broken": u.leg_broken,
            "is_alive": u.is_alive,
            "parts": {
                "head": part_dict(u.part_head),
                "ra": part_dict(u.part_ra),
                "la": part_dict(u.part_la),
                "leg": part_dict(u.part_leg),
            },
        }

    def team_dict(t: Any) -> dict[str, Any]:
        return {
            "id": t.team_id,
            "name": t.name,
            "leader_index": t.leader_index,
            "units": [unit_dict(u) for u in t.units],
        }

    events = [e.model_dump() for e in state.events[-20:]]  # last 20 events

    return {
        "tick": state.tick,
        "team_a": team_dict(state.team_a),
        "team_b": team_dict(state.team_b),
        "events": events,
        "event_stack": [],
        "is_finished": state.is_finished,
        "winner": state.winner,
    }


def _player_command_payload(state: BattleState, awaiting_player_action: bool = False) -> dict[str, Any]:
    """Describe the current player-controlled command window for the UI."""
    active_unit = next(
        (unit for unit in state.team_a.units if unit.phase == TimelinePhase.ACT and unit.is_alive),
        None,
    )
    if active_unit is None:
        return {
            "awaiting_player_action": False,
            "player_command": None,
        }

    actions = []
    for key, label in (
        ("head", "頭部"),
        ("ra", "右腕"),
        ("la", "左腕"),
        ("leg", "脚部"),
    ):
        part = getattr(active_unit, f"part_{key}")
        actions.append(
            {
                "key": key,
                "label": label,
                "part_name": part.name,
                "skill_kind": part.skill_kind.value,
                "enabled": part.is_usable and part.skill_kind != SkillKind.NONE,
            }
        )

    return {
        "awaiting_player_action": awaiting_player_action,
        "player_command": {
            "unit_id": active_unit.medarot_id,
            "unit_name": active_unit.name,
            "actions": actions,
        },
    }


def _state_response_payload(
    state: BattleState,
    *,
    awaiting_player_action: bool = False,
) -> dict[str, Any]:
    """Build the full JSON payload returned to the front-end."""
    return {
        **_state_to_dict(state),
        **_player_command_payload(state, awaiting_player_action=awaiting_player_action),
    }


def _iter_units(state: BattleState) -> list[tuple[str, Any]]:
    """Return every unit in the state alongside its team label."""
    units: list[tuple[str, Any]] = []
    for team_label, team in (("A", state.team_a), ("B", state.team_b)):
        for unit in team.units:
            units.append((team_label, unit))
    return units


def _find_unit_context(state: BattleState, unit_name: str | None) -> dict[str, Any] | None:
    """Resolve a unit name to team/unit metadata for animation serialisation."""
    if not unit_name:
        return None
    for team_label, unit in _iter_units(state):
        if unit.name == unit_name:
            return {
                "team": team_label,
                "id": unit.medarot_id,
                "name": unit.name,
                "unit": unit,
            }
    return None


def _find_part_context(unit: Any | None, part_name: str | None) -> tuple[str | None, Any | None]:
    """Resolve an event part name back to a part slot on the unit."""
    if unit is None or not part_name:
        return None, None
    for key in ("head", "ra", "la", "leg"):
        part = getattr(unit, f"part_{key}")
        if part.name == part_name:
            return key, part
    return None, None


def _target_part_key_for_event(action: str) -> str | None:
    """Map an engine action to the affected target part slot in the current ruleset."""
    return "head" if action in {"SHOOT", "MELEE", "HEAL", "DOT"} else None


def _build_event_stack(
    previous_state: BattleState,
    updated_state: BattleState,
    events: list[dict[str, Any]] | list[Any],
) -> list[dict[str, Any]]:
    """Build UI-facing cinematic metadata from raw engine events."""
    stack: list[dict[str, Any]] = []

    for raw_event in events:
        event = raw_event if isinstance(raw_event, dict) else raw_event.model_dump()
        actor_before = _find_unit_context(previous_state, event.get("actor_name"))
        actor_after = _find_unit_context(updated_state, event.get("actor_name"))
        target_before = _find_unit_context(previous_state, event.get("target_name"))
        target_after = _find_unit_context(updated_state, event.get("target_name"))
        actor_ctx = actor_after or actor_before
        target_ctx = target_after or target_before
        actor_part_key = actor_part_system = None
        event_part_name = event.get("part_name")
        if actor_ctx:
            actor_part_key, actor_part = _find_part_context(actor_ctx["unit"], event_part_name)
            actor_part_system = actor_part.system.value if actor_part else None
        else:
            actor_part = None

        target_part_key = _target_part_key_for_event(event.get("action", ""))
        target_part_system = target_part_name = None
        hp_before = hp_after = hp_max = None
        if target_ctx and target_part_key:
            previous_part = getattr(target_before["unit"], f"part_{target_part_key}", None) if target_before else None
            updated_part = getattr(target_after["unit"], f"part_{target_part_key}", None) if target_after else None
            reference_part = updated_part or previous_part
            if reference_part is not None:
                target_part_system = reference_part.system.value
                target_part_name = reference_part.name
                hp_max = reference_part.attr.armor
            if previous_part is not None:
                hp_before = previous_part.attr.current_hp
            if updated_part is not None:
                hp_after = updated_part.attr.current_hp

        stack.append(
            {
                **event,
                "actor_id": actor_ctx["id"] if actor_ctx else None,
                "actor_team": actor_ctx["team"] if actor_ctx else event.get("actor_team"),
                "actor_part_key": actor_part_key,
                "actor_part_system": actor_part_system,
                "target_id": target_ctx["id"] if target_ctx else None,
                "target_team": target_ctx["team"] if target_ctx else None,
                "target_part_key": target_part_key,
                "target_part_system": target_part_system,
                "target_part_name": target_part_name,
                "hp_before": hp_before,
                "hp_after": hp_after,
                "hp_max": hp_max,
                "show_parts_reveal": bool(target_ctx and target_part_key and hp_after is not None),
                "camera_mode": "duel" if target_ctx else "timeline",
            }
        )
    return stack


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def battle_field(request: HttpRequest) -> HttpResponse:
    """Render the main battle field HTML page.

    Ensures a demo BattleSession exists and passes its ID to the template so
    the JavaScript polling client knows which session to call.
    """
    session = _get_or_create_demo_session()
    state = state_from_json(session.state_json)
    context = {
        "session_id": session.pk,
        "initial_state": json.dumps(_state_response_payload(state)),
    }
    return render(request, "battle/battle_field.html", context)


@require_http_methods(["GET"])
def battle_state(request: HttpRequest, session_id: int) -> JsonResponse:
    """Return the current battle state as JSON without advancing the battle."""
    session = get_object_or_404(BattleSession, pk=session_id)
    state = state_from_json(session.state_json)
    return JsonResponse(_state_response_payload(state))


@require_http_methods(["GET"])
def battle_step(request: HttpRequest, session_id: int) -> JsonResponse:
    """Advance the battle by one action and return the updated state.

    The JavaScript client calls this endpoint on a short interval to drive the
    battle animation without a page reload.
    """
    session = get_object_or_404(BattleSession, pk=session_id)

    if session.is_finished:
        state = state_from_json(session.state_json)
        return JsonResponse({**_state_response_payload(state), "already_finished": True})

    action_part = request.GET.get("action_part")
    if action_part is not None and action_part not in VALID_ACTION_PART_KEYS:
        return JsonResponse({"error": "invalid action_part"}, status=400)

    previous_state = state_from_json(session.state_json)
    state = state_from_json(session.state_json)
    engine = BattleEngine(state)
    updated_state, new_events = engine.advance(
        player_team="A",
        action_part_key=action_part,
    )

    # Persist the updated state
    session.state_json = state_to_json(updated_state)
    if updated_state.is_finished:
        session.is_finished = True
        session.winner = updated_state.winner
    session.save()

    response_data = _state_response_payload(
        updated_state,
        awaiting_player_action=engine.awaiting_player_action,
    )
    response_data["new_events"] = [e.model_dump() for e in new_events]
    response_data["event_stack"] = _build_event_stack(previous_state, updated_state, new_events)
    return JsonResponse(response_data)


@csrf_exempt
@require_http_methods(["POST"])
def new_battle(request: HttpRequest) -> JsonResponse:
    """Create a fresh demo battle session and return its ID."""
    team_a, team_b = _build_demo_teams()
    state = build_state_from_db(team_a, team_b)
    session = BattleSession.objects.create(
        team_a=team_a,
        team_b=team_b,
        state_json=state_to_json(state),
    )
    return JsonResponse({"session_id": session.pk})
