"""URL patterns for the battle application."""

from django.urls import path

from . import views

app_name = "battle"

urlpatterns = [
    path("", views.battle_field, name="battle_field"),
    path("api/battle/<int:session_id>/state/", views.battle_state, name="battle_state"),
    path("api/battle/<int:session_id>/step/", views.battle_step, name="battle_step"),
    path("api/battle/new/", views.new_battle, name="new_battle"),
]
