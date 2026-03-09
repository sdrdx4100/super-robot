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
        charge=55,
        cooldown=50,
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
    a1 = _create_demo_medarot("メタビー", "メタビーのメダル", Personality.LEADER, "赤-")
    a2 = _create_demo_medarot("ロクショウ", "ロクショウのメダル", Personality.WEAK, "緑-")
    a3 = _create_demo_medarot("イカロス", "イカロスのメダル", Personality.RANDOM, "青-")

    team_a = Team.objects.create(
        name="チームA",
        medarot_1=a1,
        medarot_2=a2,
        medarot_3=a3,
        leader_index=0,
    )

    # Team B (slightly varied personalities)
    b1 = _create_demo_medarot("スパルタン", "スパルタンのメダル", Personality.STRONG, "金-")
    b2 = _create_demo_medarot("ティグリス", "ティグリスのメダル", Personality.RANDOM, "黒-")
    b3 = _create_demo_medarot("クロスファイア", "クロスファイアのメダル", Personality.LEADER, "白-")

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
        return {
            "id": u.medarot_id,
            "name": u.name,
            "personality": u.personality.value,
            "gauge": round(u.gauge, 1),
            "cooling_down": u.cooling_down,
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
        "is_finished": state.is_finished,
        "winner": state.winner,
    }


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
        "initial_state": json.dumps(_state_to_dict(state)),
    }
    return render(request, "battle/battle_field.html", context)


@require_http_methods(["GET"])
def battle_state(request: HttpRequest, session_id: int) -> JsonResponse:
    """Return the current battle state as JSON without advancing the battle."""
    session = get_object_or_404(BattleSession, pk=session_id)
    state = state_from_json(session.state_json)
    return JsonResponse(_state_to_dict(state))


@require_http_methods(["GET"])
def battle_step(request: HttpRequest, session_id: int) -> JsonResponse:
    """Advance the battle by one action and return the updated state.

    The JavaScript client calls this endpoint on a short interval to drive the
    battle animation without a page reload.
    """
    session = get_object_or_404(BattleSession, pk=session_id)

    if session.is_finished:
        state = state_from_json(session.state_json)
        return JsonResponse({**_state_to_dict(state), "already_finished": True})

    state = state_from_json(session.state_json)
    engine = BattleEngine(state)
    updated_state, new_events = engine.advance()

    # Persist the updated state
    session.state_json = state_to_json(updated_state)
    if updated_state.is_finished:
        session.is_finished = True
        session.winner = updated_state.winner
    session.save()

    response_data = _state_to_dict(updated_state)
    response_data["new_events"] = [e.model_dump() for e in new_events]
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
