"""
Django models for the 3vs3 Medarot-style Tactical Simulator.

Architecture:
  - Attribute  : raw integer stats for a Part (HP, accuracy, power, charge, cooldown)
  - Part       : a robot component with system (HEAD/RA/LA/LEG), skill kind, and effects
  - Medal      : the AI soul of a Medarot, holding personality and skill levels
  - Medarot    : a robot consisting of 4 Parts + 1 Medal
  - Team       : 3 Medarots with a designated leader
  - BattleSession : persists the JSON state of an ongoing battle
"""

from __future__ import annotations

from django.db import models


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class PartSystem(models.TextChoices):
    """The four component systems of a Medarot."""

    HEAD = "HEAD", "頭部 (Head)"
    RIGHT_ARM = "RA", "右腕 (Right Arm)"
    LEFT_ARM = "LA", "左腕 (Left Arm)"
    LEG = "LEG", "脚部 (Leg)"


class SkillKind(models.TextChoices):
    """Possible action types a Part can perform."""

    SHOOT = "SHOOT", "射撃 (Shoot)"
    MELEE = "MELEE", "格闘 (Melee)"
    GUARD = "GUARD", "守る (Guard)"
    HEAL = "HEAL", "治す (Heal)"
    SUPPORT = "SUPPORT", "補助 (Support)"
    NONE = "NONE", "なし (None)"


class SpecialEffect(models.TextChoices):
    """Optional special effects a Part's skill may carry."""

    PIERCE = "PIERCE", "貫通 (Pierce)"
    DOT = "DOT", "継続ダメージ (Damage over Time)"
    REFLECT = "REFLECT", "反射 (Reflect)"
    NONE = "NONE", "なし (None)"


class Personality(models.TextChoices):
    """Medal personality — determines the AI target-selection strategy."""

    LEADER = "LEADER", "リーダー狙い (Attack Leader)"
    WEAK = "WEAK", "弱者狙い (Attack Weakest)"
    RANDOM = "RANDOM", "ランダム (Attack Random)"
    STRONG = "STRONG", "強者狙い (Attack Strongest)"


# ---------------------------------------------------------------------------
# Attribute (部品ステータス)
# ---------------------------------------------------------------------------

class Attribute(models.Model):
    """Holds the five integer stats that define a Part's combat performance.

    Fields
    ------
    armor   : 装甲 — maximum / current HP of this part.
    success : 成功 — base accuracy value used in hit-rate formula.
    power   : 威力 — base damage multiplier.
    charge  : 充填 — reduces the time required to charge (reach Command Line).
    cooldown: 放熱 — reduces the time required to cool down (return to Base Line).
    """

    armor: int = models.PositiveIntegerField(default=50, verbose_name="装甲 (HP)")
    success: int = models.PositiveIntegerField(default=60, verbose_name="成功 (Hit Rate)")
    power: int = models.PositiveIntegerField(default=40, verbose_name="威力 (Power)")
    charge: int = models.PositiveIntegerField(default=50, verbose_name="充填 (Charge Speed)")
    cooldown: int = models.PositiveIntegerField(default=50, verbose_name="放熱 (Cooldown Speed)")

    class Meta:
        verbose_name = "属性"
        verbose_name_plural = "属性一覧"

    def __str__(self) -> str:
        return (
            f"Attr(armor={self.armor}, suc={self.success}, "
            f"pow={self.power}, chg={self.charge}, cd={self.cooldown})"
        )


# ---------------------------------------------------------------------------
# Part (部品)
# ---------------------------------------------------------------------------

class Part(models.Model):
    """A single component equipped on a Medarot.

    Each Part belongs to one of four systems (HEAD, RA, LA, LEG), carries an
    action definition (SkillKind), optional special effects, and a reference
    Attribute block for its base stats.
    """

    name: str = models.CharField(max_length=64, verbose_name="部品名")
    system: str = models.CharField(
        max_length=4,
        choices=PartSystem.choices,
        default=PartSystem.HEAD,
        verbose_name="系統",
    )
    skill_kind: str = models.CharField(
        max_length=8,
        choices=SkillKind.choices,
        default=SkillKind.NONE,
        verbose_name="スキル種別",
    )
    special_effect: str = models.CharField(
        max_length=8,
        choices=SpecialEffect.choices,
        default=SpecialEffect.NONE,
        verbose_name="特殊効果",
    )
    attribute: Attribute = models.OneToOneField(
        Attribute,
        on_delete=models.CASCADE,
        related_name="part",
        verbose_name="ステータス",
    )

    class Meta:
        verbose_name = "部品"
        verbose_name_plural = "部品一覧"

    def __str__(self) -> str:
        return f"{self.name} [{self.system}]"


# ---------------------------------------------------------------------------
# Medal (メダル)
# ---------------------------------------------------------------------------

class Medal(models.Model):
    """The AI core of a Medarot — personality and skill levels.

    Personality dictates which enemy is targeted each turn.
    Skill levels apply a multiplier correction to the Part attributes they
    correspond to (HEAD, RA, LA, LEG each have an independent level 1-10).
    """

    name: str = models.CharField(max_length=64, verbose_name="メダル名")
    personality: str = models.CharField(
        max_length=8,
        choices=Personality.choices,
        default=Personality.RANDOM,
        verbose_name="性格",
    )
    # Skill levels (1-10) — one per Part system
    skill_head: int = models.PositiveSmallIntegerField(default=5, verbose_name="頭部熟練度")
    skill_ra: int = models.PositiveSmallIntegerField(default=5, verbose_name="右腕熟練度")
    skill_la: int = models.PositiveSmallIntegerField(default=5, verbose_name="左腕熟練度")
    skill_leg: int = models.PositiveSmallIntegerField(default=5, verbose_name="脚部熟練度")

    class Meta:
        verbose_name = "メダル"
        verbose_name_plural = "メダル一覧"

    def __str__(self) -> str:
        return f"{self.name} ({self.get_personality_display()})"

    def skill_for_system(self, system: str) -> int:
        """Return the skill level that corresponds to *system* (PartSystem value)."""
        mapping = {
            PartSystem.HEAD: self.skill_head,
            PartSystem.RIGHT_ARM: self.skill_ra,
            PartSystem.LEFT_ARM: self.skill_la,
            PartSystem.LEG: self.skill_leg,
        }
        return mapping.get(system, 5)


# ---------------------------------------------------------------------------
# Medarot (機体)
# ---------------------------------------------------------------------------

class Medarot(models.Model):
    """A robot unit composed of four Parts and one Medal.

    The LEG part defines movement / charge / cooldown speed.
    The HEAD part is critical — destroying it incapacitates the entire unit.
    """

    name: str = models.CharField(max_length=64, verbose_name="機体名")
    medal: Medal = models.ForeignKey(
        Medal,
        on_delete=models.CASCADE,
        related_name="medarots",
        verbose_name="メダル",
    )
    part_head: Part = models.OneToOneField(
        Part,
        on_delete=models.CASCADE,
        related_name="medarot_head",
        verbose_name="頭部パーツ",
    )
    part_ra: Part = models.OneToOneField(
        Part,
        on_delete=models.CASCADE,
        related_name="medarot_ra",
        verbose_name="右腕パーツ",
    )
    part_la: Part = models.OneToOneField(
        Part,
        on_delete=models.CASCADE,
        related_name="medarot_la",
        verbose_name="左腕パーツ",
    )
    part_leg: Part = models.OneToOneField(
        Part,
        on_delete=models.CASCADE,
        related_name="medarot_leg",
        verbose_name="脚部パーツ",
    )

    class Meta:
        verbose_name = "メダロット"
        verbose_name_plural = "メダロット一覧"

    def __str__(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# Team (チーム)
# ---------------------------------------------------------------------------

class Team(models.Model):
    """A team of exactly 3 Medarots with one designated leader.

    The leader's destruction triggers total team defeat.
    """

    name: str = models.CharField(max_length=64, verbose_name="チーム名")
    medarot_1: Medarot = models.ForeignKey(
        Medarot,
        on_delete=models.CASCADE,
        related_name="team_slot_1",
        verbose_name="1番機",
    )
    medarot_2: Medarot = models.ForeignKey(
        Medarot,
        on_delete=models.CASCADE,
        related_name="team_slot_2",
        verbose_name="2番機",
    )
    medarot_3: Medarot = models.ForeignKey(
        Medarot,
        on_delete=models.CASCADE,
        related_name="team_slot_3",
        verbose_name="3番機",
    )
    leader_index: int = models.PositiveSmallIntegerField(
        default=0,
        verbose_name="リーダー番号 (0/1/2)",
    )

    class Meta:
        verbose_name = "チーム"
        verbose_name_plural = "チーム一覧"

    def __str__(self) -> str:
        return self.name

    @property
    def medarots(self) -> list[Medarot]:
        """Return the three Medarots as an ordered list."""
        return [self.medarot_1, self.medarot_2, self.medarot_3]


# ---------------------------------------------------------------------------
# BattleSession (バトルセッション)
# ---------------------------------------------------------------------------

class BattleSession(models.Model):
    """Persists the serialised JSON state of an ongoing battle.

    The engine_logic module converts this JSON blob to/from Pydantic models
    each step so the view layer remains thin.
    """

    team_a: Team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name="sessions_as_a",
        verbose_name="チームA",
    )
    team_b: Team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name="sessions_as_b",
        verbose_name="チームB",
    )
    state_json: str = models.TextField(
        default="{}",
        verbose_name="バトル状態 (JSON)",
    )
    is_finished: bool = models.BooleanField(default=False, verbose_name="終了フラグ")
    winner: str = models.CharField(
        max_length=1,
        choices=[("A", "チームA"), ("B", "チームB"), ("", "未決定")],
        default="",
        blank=True,
        verbose_name="勝者",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "バトルセッション"
        verbose_name_plural = "バトルセッション一覧"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Session #{self.pk}: {self.team_a} vs {self.team_b}"
