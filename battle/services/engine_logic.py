"""
Battle Engine — pure Python / Pydantic domain logic.

This module is intentionally free of Django ORM calls so that it can be
unit-tested without a database.  The Django view layer is responsible for
loading data from the DB, constructing the Pydantic models defined here,
running the engine, then persisting the resulting state back to the DB.

Timeline mechanic (inspired by the Medarot series)
----------------------------------------------------
Every unit has an internal gauge that oscillates between the Base Line (0)
and the Command Line (1000).

  CHG → gauge rises from 0 to 1000
  ACT → gauge has reached 1000 and is queued to act
  CLR → gauge falls from 1000 back to 0

During CHG the unit can evade / defend normally. During CLR it cannot evade
or defend, so incoming attacks become easier to land and deal increased
damage.

  advance_rate  = leg_charge   * leg_charge_factor
  cooldown_rate = leg_cooldown * leg_cooldown_factor

Leg destruction halves both rates permanently.

Damage formula
--------------
  base_dmg  = (power + skill_level * 1.5) * uniform(0.9, 1.1)
  final_dmg = base_dmg - (target_armor / 2)

Hit-rate (%)
------------
  hit_pct = (success + skill_level) / (target_evasion + target_leg_propulsion) * 100

Part-destruction effects
------------------------
  LEG  → advance_rate and cooldown_rate halved
  RA/LA → the corresponding arm action becomes unavailable
  HEAD → unit incapacitated; if it is the leader the whole team loses
"""

from __future__ import annotations

import json
import random
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enumerations (mirror the Django model choices)
# ---------------------------------------------------------------------------

class PartSystem(str, Enum):
    """The four component systems of a Medarot."""

    HEAD = "HEAD"
    RIGHT_ARM = "RA"
    LEFT_ARM = "LA"
    LEG = "LEG"


class SkillKind(str, Enum):
    """Action kinds a Part may perform."""

    SHOOT = "SHOOT"
    MELEE = "MELEE"
    GUARD = "GUARD"
    HEAL = "HEAL"
    SUPPORT = "SUPPORT"
    NONE = "NONE"


class SpecialEffect(str, Enum):
    """Optional special effects carried by a skill."""

    PIERCE = "PIERCE"
    DOT = "DOT"
    REFLECT = "REFLECT"
    NONE = "NONE"


class Personality(str, Enum):
    """Medal personality — governs AI target selection."""

    LEADER = "LEADER"
    WEAK = "WEAK"
    RANDOM = "RANDOM"
    STRONG = "STRONG"


class TimelinePhase(str, Enum):
    """Timeline phase for a unit."""

    CHG = "CHG"
    ACT = "ACT"
    CLR = "CLR"


# ---------------------------------------------------------------------------
# Pydantic data models
# ---------------------------------------------------------------------------

class AttributeState(BaseModel):
    """Snapshot of a single Part's mutable runtime state (HP + base stats).

    All base stats are read-only after construction; only ``current_hp``
    changes during a battle.
    """

    armor: int = Field(ge=0, description="最大HP (装甲)")
    success: int = Field(ge=0, description="命中基礎値 (成功)")
    power: int = Field(ge=0, description="ダメージ基礎値 (威力)")
    charge: int = Field(ge=0, description="充填速度 (充填)")
    cooldown: int = Field(ge=0, description="放熱速度 (放熱)")
    current_hp: int = Field(ge=0, description="現在HP")

    @model_validator(mode="before")
    @classmethod
    def set_current_hp(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Default current_hp to armor when not explicitly provided."""
        if "current_hp" not in data or data["current_hp"] is None:
            data["current_hp"] = data.get("armor", 0)
        return data

    @property
    def is_destroyed(self) -> bool:
        """Return True if this part's HP has reached zero."""
        return self.current_hp <= 0


class PartState(BaseModel):
    """Runtime state of a single Part slot."""

    part_id: int
    name: str
    system: PartSystem
    skill_kind: SkillKind
    special_effect: SpecialEffect
    attr: AttributeState
    disabled: bool = False  # True when the part is destroyed

    @property
    def is_usable(self) -> bool:
        """Return True when this part can still act."""
        return not self.disabled and not self.attr.is_destroyed


class MedarotState(BaseModel):
    """Runtime state of a single Medarot unit on the battlefield."""

    medarot_id: int
    name: str
    personality: Personality
    skill_head: int
    skill_ra: int
    skill_la: int
    skill_leg: int

    part_head: PartState
    part_ra: PartState
    part_la: PartState
    part_leg: PartState

    # Timeline gauge (0 = Base Line, 1000 = Command Line)
    gauge: float = 0.0
    # Current timeline phase
    phase: TimelinePhase = TimelinePhase.CHG
    # Leg-destruction flag — halves both advance and cooldown rates
    leg_broken: bool = False
    # Head-destruction / incapacitation flag
    incapacitated: bool = False
    # Pending special effect ticks (DoT)
    dot_turns_remaining: int = 0
    dot_damage_per_turn: float = 0.0

    @model_validator(mode="before")
    @classmethod
    def infer_phase_from_legacy_data(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Map legacy cooling_down state into the phase-based timeline model."""
        if "phase" not in data:
            if data.get("cooling_down"):
                data["phase"] = TimelinePhase.CLR
            elif float(data.get("gauge", 0.0)) >= 1000.0:
                data["phase"] = TimelinePhase.ACT
            else:
                data["phase"] = TimelinePhase.CHG
        return data

    @property
    def is_alive(self) -> bool:
        """Return True while the unit can still act."""
        return not self.incapacitated and not self.part_head.attr.is_destroyed

    @property
    def cooling_down(self) -> bool:
        """Compatibility helper for code paths that still inspect cooldown state."""
        return self.phase == TimelinePhase.CLR

    def skill_for(self, system: PartSystem) -> int:
        """Return the Medal skill level for the given part system."""
        return {
            PartSystem.HEAD: self.skill_head,
            PartSystem.RIGHT_ARM: self.skill_ra,
            PartSystem.LEFT_ARM: self.skill_la,
            PartSystem.LEG: self.skill_leg,
        }[system]

    def _leg_factor(self) -> float:
        """Return the movement multiplier (0.5 if leg is broken)."""
        return 0.5 if self.leg_broken else 1.0

    def advance_rate(self) -> float:
        """Ticks added to gauge per engine tick during charge phase."""
        base = self.part_leg.attr.charge if not self.part_leg.attr.is_destroyed else 10
        return base * self._leg_factor()

    def cooldown_rate(self) -> float:
        """Ticks removed from gauge per engine tick during cooldown phase."""
        base = self.part_leg.attr.cooldown if not self.part_leg.attr.is_destroyed else 10
        return base * self._leg_factor()

    def usable_arm_parts(self) -> list[PartState]:
        """Return RA / LA parts that are currently usable."""
        arms = [self.part_ra, self.part_la]
        return [p for p in arms if p.is_usable and p.skill_kind != SkillKind.NONE]

    def action_part_for_slot(self, slot_key: str | None) -> PartState | None:
        """Return the action part mapped to a UI slot key if it is usable."""
        if slot_key not in {"head", "ra", "la", "leg"}:
            return None
        part = getattr(self, f"part_{slot_key}")
        if not part.is_usable or part.skill_kind == SkillKind.NONE:
            return None
        return part

    def choose_action_part(self, preferred_slot: str | None = None) -> PartState | None:
        """Pick which part acts this turn (HEAD > arms > None).

        Priority: HEAD if usable, then a random usable arm.
        """
        preferred = self.action_part_for_slot(preferred_slot)
        if preferred is not None:
            return preferred
        if self.part_head.is_usable and self.part_head.skill_kind != SkillKind.NONE:
            return self.part_head
        arms = self.usable_arm_parts()
        return random.choice(arms) if arms else None


class TeamState(BaseModel):
    """Runtime state for one side in the battle."""

    team_id: int
    name: str
    units: list[MedarotState]
    leader_index: int = 0

    @property
    def alive_units(self) -> list[MedarotState]:
        """Return units that are still in fighting condition."""
        return [u for u in self.units if u.is_alive]

    @property
    def is_defeated(self) -> bool:
        """A team is defeated when ALL units are incapacitated OR the leader is down."""
        if not self.alive_units:
            return True
        leader = self.units[self.leader_index]
        return not leader.is_alive


class BattleEvent(BaseModel):
    """A single narrated event that occurred during one engine tick."""

    tick: int
    actor_team: str  # "A" or "B"
    actor_name: str
    part_name: str
    target_name: str | None = None
    action: str
    damage: float = 0.0
    hit: bool = True
    is_critical: bool = False  # True when the variance multiplier was ≥ 1.05
    special: str = ""
    part_destroyed: str = ""  # name of destroyed part, if any
    note: str = ""


class BattleState(BaseModel):
    """Complete serialisable state of a battle at a single point in time."""

    tick: int = 0
    team_a: TeamState
    team_b: TeamState
    events: list[BattleEvent] = Field(default_factory=list)
    ready_stack: list[int] = Field(default_factory=list)
    is_finished: bool = False
    winner: str = ""  # "A" or "B"


# ---------------------------------------------------------------------------
# Target Selector
# ---------------------------------------------------------------------------

class TargetSelector:
    """Selects an enemy target based on the acting Medarot's Medal personality.

    Strategies
    ----------
    LEADER  → always aim at the opponent's designated leader (if alive).
    WEAK    → target the enemy with the least total remaining HP across all parts.
    STRONG  → target the enemy with the most total remaining HP.
    RANDOM  → pick any alive enemy at random.
    """

    def select(
        self,
        personality: Personality,
        enemy_team: TeamState,
    ) -> MedarotState | None:
        """Return the chosen target or None if the enemy team has no alive units.

        Parameters
        ----------
        personality:  the Medal personality that governs targeting.
        enemy_team:   the opposing TeamState.
        """
        alive = enemy_team.alive_units
        if not alive:
            return None

        if personality == Personality.LEADER:
            leader = enemy_team.units[enemy_team.leader_index]
            return leader if leader.is_alive else random.choice(alive)

        if personality == Personality.WEAK:
            return min(alive, key=self._total_hp)

        if personality == Personality.STRONG:
            return max(alive, key=self._total_hp)

        # RANDOM
        return random.choice(alive)

    @staticmethod
    def _total_hp(unit: MedarotState) -> int:
        """Sum current HP across all four parts."""
        return sum(
            p.attr.current_hp
            for p in [unit.part_head, unit.part_ra, unit.part_la, unit.part_leg]
        )


# ---------------------------------------------------------------------------
# Damage calculator
# ---------------------------------------------------------------------------

def calculate_damage(
    actor: MedarotState,
    acting_part: PartState,
    target: MedarotState,
) -> tuple[float, bool, bool]:
    """Compute (final_damage, did_hit, is_critical) for an attack action.

    Formulae
    --------
    base_dmg  = (威力 + 熟練度 * 1.5) * uniform(0.9, 1.1)
    final_dmg = base_dmg - (target_armor / 2)
    hit_pct   = (成功 + 熟練度) / (target_evasion + target_leg_propulsion) * 100

    A critical hit occurs when the random multiplier is ≥ 1.05.  Critical hits
    deal 1.5× final damage.

    ``target_evasion`` is derived from the target's LEG part success stat.
    ``target_leg_propulsion`` is the target's LEG charge stat (leg speed).

    Parameters
    ----------
    actor       : the unit performing the attack
    acting_part : the specific part being used
    target      : the unit being attacked

    Returns
    -------
    (damage, hit, is_critical) — damage is 0 when the attack misses.
    """
    skill_level = actor.skill_for(acting_part.system)
    target_is_cooling = target.phase == TimelinePhase.CLR

    # Hit determination
    if target_is_cooling:
        target_evasion = 0
        target_propulsion = 0
    else:
        target_evasion = target.part_leg.attr.success if not target.part_leg.attr.is_destroyed else 0
        target_propulsion = target.part_leg.attr.charge if not target.part_leg.attr.is_destroyed else 0
    divisor = max(1, target_evasion + target_propulsion)
    hit_pct = (acting_part.attr.success + skill_level) / divisor * 100
    hit_pct = min(hit_pct, 95.0)  # cap at 95 % for balance

    did_hit = random.uniform(0, 100) < hit_pct

    if not did_hit:
        return 0.0, False, False

    # Damage calculation
    variance = random.uniform(0.9, 1.1)
    is_critical = variance >= 1.05
    base_dmg = (acting_part.attr.power + skill_level * 1.5) * variance
    target_armor = target.part_head.attr.armor  # attacks land on the body / head for now
    final_dmg = max(1.0, base_dmg - (target_armor / 2))

    # Pierce ignores armour reduction
    if acting_part.special_effect == SpecialEffect.PIERCE:
        final_dmg = max(1.0, base_dmg)

    if target_is_cooling:
        final_dmg *= 1.2

    # Critical hits deal 1.5× damage
    if is_critical:
        final_dmg *= 1.5

    return round(final_dmg, 1), True, is_critical


# ---------------------------------------------------------------------------
# Part-destruction handler
# ---------------------------------------------------------------------------

def apply_part_destruction(unit: MedarotState, destroyed_system: PartSystem) -> str:
    """Apply the side-effects of destroying a part and return a description.

    Effects
    -------
    LEG  → leg_broken = True  (speed halved)
    RA   → part_ra.disabled = True
    LA   → part_la.disabled = True
    HEAD → unit.incapacitated = True
    """
    part_map = {
        PartSystem.HEAD: unit.part_head,
        PartSystem.RIGHT_ARM: unit.part_ra,
        PartSystem.LEFT_ARM: unit.part_la,
        PartSystem.LEG: unit.part_leg,
    }
    part = part_map[destroyed_system]

    if destroyed_system == PartSystem.LEG:
        unit.leg_broken = True
        return f"{unit.name} の脚部が破壊された！移動速度が半減。"
    if destroyed_system in (PartSystem.RIGHT_ARM, PartSystem.LEFT_ARM):
        part.disabled = True
        return f"{unit.name} の{'右腕' if destroyed_system == PartSystem.RIGHT_ARM else '左腕'}が破壊された！"
    if destroyed_system == PartSystem.HEAD:
        unit.incapacitated = True
        return f"{unit.name} の頭部が破壊された！機能停止。"

    return ""


# ---------------------------------------------------------------------------
# Battle Engine
# ---------------------------------------------------------------------------

class BattleEngine:
    """Drives the battle forward one action at a time.

    Usage::

        engine = BattleEngine(state)
        state, events = engine.advance()   # advance until the next action fires

    The engine advances the timeline gauge of all living units simultaneously.
    The first unit whose gauge crosses 1000 (Command Line) executes its action
    and immediately begins cooling down.  The method returns after each action.
    """

    COMMAND_LINE: float = 1000.0
    BASE_LINE: float = 0.0

    def __init__(self, state: BattleState) -> None:
        self.state = state
        self._selector = TargetSelector()
        self.awaiting_player_action = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def advance(
        self,
        *,
        player_team: str | None = None,
        action_part_key: str | None = None,
    ) -> tuple[BattleState, list[BattleEvent]]:
        """Advance the battle until one unit takes an action.

        Returns
        -------
        (updated_state, new_events)
        """
        new_events: list[BattleEvent] = []
        self.awaiting_player_action = False

        if self.state.is_finished:
            return self.state, new_events

        actor, actor_team_label = self._pop_ready_unit()
        if actor is not None:
            if self._should_wait_for_player(actor, actor_team_label, player_team, action_part_key):
                self.awaiting_player_action = True
                self._push_ready_unit(actor)
                return self.state, new_events
            new_events.extend(
                self._execute_action(
                    actor,
                    actor_team_label,
                    action_part_key=action_part_key if actor_team_label == player_team else None,
                )
            )
            result = self._check_victory()
            if result:
                self.state.is_finished = True
                self.state.winner = result
            self.state.events.extend(new_events)
            return self.state, new_events

        # Advance gauges until a unit hits the Command Line
        max_iterations = 10_000
        for _ in range(max_iterations):
            self.state.tick += 1

            self._tick_all_units()

            actor, actor_team_label = self._pop_ready_unit()
            if actor is not None:
                if self._should_wait_for_player(actor, actor_team_label, player_team, action_part_key):
                    self.awaiting_player_action = True
                    self._push_ready_unit(actor)
                    break
                events = self._execute_action(
                    actor,
                    actor_team_label,
                    action_part_key=action_part_key if actor_team_label == player_team else None,
                )
                new_events.extend(events)

                # Check for battle-over conditions
                result = self._check_victory()
                if result:
                    self.state.is_finished = True
                    self.state.winner = result
                break
        else:
            # Safety valve — should not happen in a balanced battle
            self.state.is_finished = True
            self.state.winner = ""

        self.state.events.extend(new_events)
        return self.state, new_events

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _tick_all_units(self) -> None:
        """Advance or retract gauge for every living unit by one engine tick."""
        for team in (self.state.team_a, self.state.team_b):
            for unit in team.units:
                if not unit.is_alive:
                    continue
                if unit.phase == TimelinePhase.CLR:
                    unit.gauge -= unit.cooldown_rate()
                    if unit.gauge <= self.BASE_LINE:
                        unit.gauge = self.BASE_LINE
                        unit.phase = TimelinePhase.CHG
                elif unit.phase == TimelinePhase.CHG:
                    unit.gauge += unit.advance_rate()
                    if unit.gauge >= self.COMMAND_LINE:
                        unit.gauge = self.COMMAND_LINE
                        unit.phase = TimelinePhase.ACT
                        self._push_ready_unit(unit)

    def _push_ready_unit(self, unit: MedarotState) -> None:
        """Remember that a unit reached the Command Line this tick."""
        if unit.medarot_id not in self.state.ready_stack:
            self.state.ready_stack.append(unit.medarot_id)

    def _pop_ready_unit(self) -> tuple[MedarotState | None, str]:
        """Pop the most recent Command Line arrival from the wait stack."""
        while self.state.ready_stack:
            medarot_id = self.state.ready_stack.pop()
            actor, team_label = self._find_unit(medarot_id)
            if actor is None or not actor.is_alive:
                continue
            if actor.phase == TimelinePhase.ACT and actor.gauge >= self.COMMAND_LINE:
                return actor, team_label
        return None, ""

    def _find_unit(self, medarot_id: int) -> tuple[MedarotState | None, str]:
        """Resolve a Medarot id back to the in-memory unit and team label."""
        for label, team in (("A", self.state.team_a), ("B", self.state.team_b)):
            for unit in team.units:
                if unit.medarot_id == medarot_id:
                    return unit, label
        return None, ""

    def _execute_action(
        self,
        actor: MedarotState,
        actor_team_label: str,
        action_part_key: str | None = None,
    ) -> list[BattleEvent]:
        """Resolve the actor's chosen action and return narration events."""
        events: list[BattleEvent] = []

        # Mark as cooling down immediately
        actor.gauge = self.COMMAND_LINE
        actor.phase = TimelinePhase.CLR

        acting_part = actor.choose_action_part(action_part_key)
        if acting_part is None:
            events.append(
                BattleEvent(
                    tick=self.state.tick,
                    actor_team=actor_team_label,
                    actor_name=actor.name,
                    part_name="(なし)",
                    action="待機",
                )
            )
            events.append(self._cooling_start_event(actor, actor_team_label))
            return events

        enemy_team = (
            self.state.team_b if actor_team_label == "A" else self.state.team_a
        )
        ally_team = (
            self.state.team_a if actor_team_label == "A" else self.state.team_b
        )

        skill = acting_part.skill_kind

        # ------ Offensive actions ------
        if skill in (SkillKind.SHOOT, SkillKind.MELEE):
            target = self._selector.select(actor.personality, enemy_team)
            if target is None:
                return events

            dmg, hit, is_critical = calculate_damage(actor, acting_part, target)
            part_destroyed_msg = ""
            note = "（放熱中につき回避不能！）" if hit and target.phase == TimelinePhase.CLR else ""
            if is_critical and hit:
                note = "★クリティカルヒット！★" + (f" {note}" if note else "")

            if hit and dmg > 0:
                # Damage the target's head by default (simplified model)
                target.part_head.attr.current_hp = max(
                    0, target.part_head.attr.current_hp - int(dmg)
                )
                # DoT special effect
                if acting_part.special_effect == SpecialEffect.DOT:
                    target.dot_turns_remaining = 3
                    target.dot_damage_per_turn = dmg * 0.3

                # Check part destruction
                if target.part_head.attr.is_destroyed:
                    part_destroyed_msg = apply_part_destruction(target, PartSystem.HEAD)

            events.append(
                BattleEvent(
                    tick=self.state.tick,
                    actor_team=actor_team_label,
                    actor_name=actor.name,
                    part_name=acting_part.name,
                    target_name=target.name,
                    action=skill.value,
                    damage=dmg,
                    hit=hit,
                    is_critical=is_critical,
                    special=acting_part.special_effect.value,
                    part_destroyed=part_destroyed_msg,
                    note=note,
                )
            )
            events.append(self._cooling_start_event(actor, actor_team_label))

        # ------ Heal ------
        elif skill == SkillKind.HEAL:
            # Heals the weakest ally
            wounded = sorted(
                ally_team.alive_units,
                key=lambda u: u.part_head.attr.current_hp,
            )
            if wounded:
                target_ally = wounded[0]
                skill_level = actor.skill_for(acting_part.system)
                heal_amount = int(
                    (acting_part.attr.power + skill_level * 1.5) * random.uniform(0.9, 1.1)
                )
                target_ally.part_head.attr.current_hp = min(
                    target_ally.part_head.attr.armor,
                    target_ally.part_head.attr.current_hp + heal_amount,
                )
                events.append(
                    BattleEvent(
                        tick=self.state.tick,
                        actor_team=actor_team_label,
                        actor_name=actor.name,
                        part_name=acting_part.name,
                        target_name=target_ally.name,
                        action="HEAL",
                        damage=-heal_amount,
                        hit=True,
                    )
                )
            events.append(self._cooling_start_event(actor, actor_team_label))

        # ------ Guard ------
        elif skill == SkillKind.GUARD:
            # Actor temporarily boosts own armour (represented by a short cooldown)
            events.append(
                BattleEvent(
                    tick=self.state.tick,
                    actor_team=actor_team_label,
                    actor_name=actor.name,
                    part_name=acting_part.name,
                    action="GUARD",
                    hit=True,
                )
            )
            events.append(self._cooling_start_event(actor, actor_team_label))

        # ------ Support ------
        elif skill == SkillKind.SUPPORT:
            # Boosts the gauge of the weakest ally
            weakest = sorted(
                [u for u in ally_team.alive_units if u.phase != TimelinePhase.CLR],
                key=lambda u: u.gauge,
            )
            if weakest:
                boost_target = weakest[0]
                boost_target.gauge = min(
                    self.COMMAND_LINE, boost_target.gauge + 200
                )
                if boost_target.gauge >= self.COMMAND_LINE:
                    boost_target.gauge = self.COMMAND_LINE
                    boost_target.phase = TimelinePhase.ACT
                    self._push_ready_unit(boost_target)
                events.append(
                    BattleEvent(
                        tick=self.state.tick,
                        actor_team=actor_team_label,
                        actor_name=actor.name,
                        part_name=acting_part.name,
                        target_name=boost_target.name,
                        action="SUPPORT",
                        hit=True,
                    )
                )
            events.append(self._cooling_start_event(actor, actor_team_label))

        # DoT ticks for all units
        dot_events = self._apply_dot_effects()
        events.extend(dot_events)

        return events

    @staticmethod
    def _should_wait_for_player(
        actor: MedarotState,
        actor_team_label: str,
        player_team: str | None,
        action_part_key: str | None,
    ) -> bool:
        """Return True when a player-controlled unit is ready but has not chosen a command."""
        if player_team is None or actor_team_label != player_team:
            return False
        if action_part_key is None:
            return True
        return actor.action_part_for_slot(action_part_key) is None

    def _cooling_start_event(
        self,
        actor: MedarotState,
        actor_team_label: str,
    ) -> BattleEvent:
        """Describe a unit beginning its return trip to the Base Line."""
        return BattleEvent(
            tick=self.state.tick,
            actor_team=actor_team_label,
            actor_name=actor.name,
            part_name="放熱",
            action="COOLING",
            note=f"{actor.name} がベースラインへ帰還開始",
        )

    def _apply_dot_effects(self) -> list[BattleEvent]:
        """Apply damage-over-time effects to all affected units."""
        events: list[BattleEvent] = []
        for team_label, team in (("A", self.state.team_a), ("B", self.state.team_b)):
            for unit in team.units:
                if unit.dot_turns_remaining > 0 and unit.is_alive:
                    dot = unit.dot_damage_per_turn
                    unit.part_head.attr.current_hp = max(
                        0, unit.part_head.attr.current_hp - int(dot)
                    )
                    unit.dot_turns_remaining -= 1
                    events.append(
                        BattleEvent(
                            tick=self.state.tick,
                            actor_team="",
                            actor_name="DoT",
                            part_name="継続ダメージ",
                            target_name=unit.name,
                            action="DOT",
                            damage=dot,
                            hit=True,
                        )
                    )
                    if unit.part_head.attr.is_destroyed:
                        apply_part_destruction(unit, PartSystem.HEAD)
        return events

    def _check_victory(self) -> str:
        """Return "A", "B", or "" (battle continues)."""
        if self.state.team_b.is_defeated:
            return "A"
        if self.state.team_a.is_defeated:
            return "B"
        return ""


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def state_to_json(state: BattleState) -> str:
    """Serialise a BattleState to a JSON string."""
    return state.model_dump_json()


def state_from_json(data: str) -> BattleState:
    """Deserialise a BattleState from a JSON string."""
    return BattleState.model_validate_json(data)


# ---------------------------------------------------------------------------
# Factory: build a BattleState from Django ORM objects
# ---------------------------------------------------------------------------

def build_state_from_db(team_a_db: Any, team_b_db: Any) -> BattleState:
    """Construct a fresh BattleState from two Django Team instances.

    Parameters
    ----------
    team_a_db : battle.models.Team  (Django ORM object)
    team_b_db : battle.models.Team  (Django ORM object)

    Returns
    -------
    BattleState ready to be fed into BattleEngine.
    """

    def _attr(attr_db: Any) -> AttributeState:
        return AttributeState(
            armor=attr_db.armor,
            success=attr_db.success,
            power=attr_db.power,
            charge=attr_db.charge,
            cooldown=attr_db.cooldown,
        )

    def _part(part_db: Any) -> PartState:
        return PartState(
            part_id=part_db.pk,
            name=part_db.name,
            system=PartSystem(part_db.system),
            skill_kind=SkillKind(part_db.skill_kind),
            special_effect=SpecialEffect(part_db.special_effect),
            attr=_attr(part_db.attribute),
        )

    def _medarot(m_db: Any) -> MedarotState:
        return MedarotState(
            medarot_id=m_db.pk,
            name=m_db.name,
            personality=Personality(m_db.medal.personality),
            skill_head=m_db.medal.skill_head,
            skill_ra=m_db.medal.skill_ra,
            skill_la=m_db.medal.skill_la,
            skill_leg=m_db.medal.skill_leg,
            part_head=_part(m_db.part_head),
            part_ra=_part(m_db.part_ra),
            part_la=_part(m_db.part_la),
            part_leg=_part(m_db.part_leg),
            gauge=0.0,
        )

    def _team(team_db: Any) -> TeamState:
        return TeamState(
            team_id=team_db.pk,
            name=team_db.name,
            units=[_medarot(m) for m in team_db.medarots],
            leader_index=team_db.leader_index,
        )

    return BattleState(
        tick=0,
        team_a=_team(team_a_db),
        team_b=_team(team_b_db),
    )
