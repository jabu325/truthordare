from django.contrib import admin

from .models import (
    AnswerReaction,
    GameRoom,
    GameRound,
    Player,
    Prompt,
    RoomMessage,
)


@admin.register(Prompt)
class PromptAdmin(admin.ModelAdmin):
    list_display = (
        "text",
        "type",
        "difficulty",
        "game_type",
        "is_active",
    )

    list_filter = ("type", "difficulty", "game_type", "is_active")

    search_fields = ("text",)


class PlayerInline(admin.TabularInline):
    model = Player
    extra = 0
    readonly_fields = ("nickname", "joined_at", "is_host")


class GameRoundInline(admin.TabularInline):
    model = GameRound
    extra = 0
    readonly_fields = (
        "round_number",
        "prompt_text",
        "prompt_type",
        "answer_text",
        "answer_type",
        "created_at",
    )


@admin.register(GameRoom)
class GameRoomAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "game_type",
        "difficulty",
        "status",
        "phase",
        "created_at",
    )

    list_filter = ("status", "game_type", "difficulty")

    search_fields = ("code",)

    inlines = [PlayerInline, GameRoundInline]


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ("nickname", "room", "is_host", "is_connected")

    search_fields = ("nickname", "room__code")


@admin.register(GameRound)
class GameRoundAdmin(admin.ModelAdmin):
    list_display = (
        "room",
        "round_number",
        "prompt_type",
        "answer_type",
        "created_at",
    )

    list_filter = ("prompt_type", "answer_type")


@admin.register(RoomMessage)
class RoomMessageAdmin(admin.ModelAdmin):
    list_display = ("room", "player", "text", "created_at")
    list_filter = ("created_at",)
    search_fields = ("room__code", "player__nickname", "text")


@admin.register(AnswerReaction)
class AnswerReactionAdmin(admin.ModelAdmin):
    list_display = ("game_round", "player", "emoji", "created_at")
    list_filter = ("emoji", "created_at")
