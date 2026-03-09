"""Django admin registrations for the battle application."""

from django.contrib import admin

from .models import Attribute, Part, Medal, Medarot, Team, BattleSession


@admin.register(Attribute)
class AttributeAdmin(admin.ModelAdmin):
    list_display = ("__str__", "armor", "success", "power", "charge", "cooldown")


@admin.register(Part)
class PartAdmin(admin.ModelAdmin):
    list_display = ("name", "system", "skill_kind", "special_effect")


@admin.register(Medal)
class MedalAdmin(admin.ModelAdmin):
    list_display = ("name", "personality", "skill_head", "skill_ra", "skill_la", "skill_leg")


@admin.register(Medarot)
class MedarotAdmin(admin.ModelAdmin):
    list_display = ("name", "medal")


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "leader_index")


@admin.register(BattleSession)
class BattleSessionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "is_finished", "winner", "created_at")
