import secrets
import string

from django.conf import settings
from django.core.validators import MaxLengthValidator, MinLengthValidator
from django.db import models

from .constants import GAME_CATEGORIES, get_category_label


def generate_room_code():
    characters = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(characters) for _ in range(6))


class Prompt(models.Model):

    PROMPT_TYPES = [
        ("truth", "Truth"),
        ("dare", "Dare"),
    ]

    DIFFICULTIES = [
        ("easy", "Easy"),
        ("hard", "Hard"),
        ("spicy", "Spicy"),
    ]

    type = models.CharField(
        max_length=10,
        choices=PROMPT_TYPES,
    )

    difficulty = models.CharField(
        max_length=10,
        choices=DIFFICULTIES,
    )

    # Preserve the original category values.
    game_type = models.CharField(max_length=50)

    text = models.TextField()

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def category_label(self):
        return get_category_label(self.game_type)

    def __str__(self):
        return f"{self.get_type_display()} - {self.text[:60]}"


class GameRoom(models.Model):

    GAME_CATEGORIES = [
        ("friends", "Friends"),
        ("best_friends", "Best Friends"),
        ("couples", "Couples"),
    ]

    DIFFICULTIES = Prompt.DIFFICULTIES

    STATUSES = [
        ("WAITING", "Waiting for players"),
        ("ACTIVE", "Active"),
        ("FINISHED", "Finished"),
        ("CANCELLED", "Cancelled"),
    ]

    PHASES = [
    ("WAITING", "Waiting for players"),
    ("CHOOSING_CATEGORY", "Choosing game category"),
    ("CHOOSING_DIFFICULTY", "Choosing difficulty"),
    ("CHOOSING_TYPE", "Choosing Truth or Dare"),
    ("CHOOSING_PROMPT", "Choosing a prompt"),
    ("AWAITING_ANSWER", "Awaiting answer"),
    ("REVIEWING_ANSWER", "Reviewing answer"),
    ("ROUND_COMPLETE", "Round complete"),
    ("FINISHED", "Finished"),
    ]

    code = models.CharField(
        max_length=6,
        unique=True,
        default=generate_room_code,
        validators=[
            MinLengthValidator(6),
            MaxLengthValidator(6),
        ],
    )

    game_number = models.PositiveIntegerField(default=1)

    game_type = models.CharField(
        max_length=20,
        choices=GAME_CATEGORIES,
        blank=True,
        default="",
    )

    difficulty = models.CharField(
        max_length=10,
        choices=DIFFICULTIES,
        blank=True,
        default="",
    )

    status = models.CharField(
        max_length=12,
        choices=STATUSES,
        default="WAITING",
    )

    phase = models.CharField(
        max_length=20,
        choices=PHASES,
        default="WAITING",
    )

    host = models.ForeignKey(
        "Player",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="hosted_rooms",
    )

    current_turn = models.ForeignKey(
        "Player",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="active_turns",
    )

    winner = models.ForeignKey(
        "Player",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="won_games",
    )

    prompt_options = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    started_at = models.DateTimeField(null=True, blank=True)

    finished_at = models.DateTimeField(null=True, blank=True)

    @property
    def player_count(self):
        return self.players.count()

    @property
    def is_full(self):
        return self.player_count >= 2

    @property
    def category_label(self):
        return dict(self.GAME_CATEGORIES).get(
            self.game_type,
            "Not selected",
        )

    def __str__(self):
        return f"{self.code} ({self.get_status_display()})"


class Player(models.Model):

    room = models.ForeignKey(
        GameRoom,
        on_delete=models.CASCADE,
        related_name="players",
    )

    nickname = models.CharField(max_length=24)

    # Links the player to their browser's Django session.
    session_key = models.CharField(
        max_length=40,
        unique=True,
        null=True,
        blank=True,
    )

    is_host = models.BooleanField(default=False)

    is_connected = models.BooleanField(default=False)

    last_seen = models.DateTimeField(null=True, blank=True)

    last_read_message_id = models.PositiveBigIntegerField(null=True, blank=True)

    hearts = models.PositiveSmallIntegerField(default=5)

    restart_requested = models.BooleanField(default=False)

    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["room", "nickname"],
                name="unique_nickname_per_room",
            ),
        ]

        ordering = ["joined_at"]

    def __str__(self):
        return f"{self.nickname} ({self.room.code})"


class PushSubscription(models.Model):
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name="push_subscriptions",
    )
    endpoint = models.TextField()
    endpoint_hash = models.CharField(max_length=64)
    p256dh = models.CharField(max_length=256)
    auth = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["player", "endpoint_hash"],
                name="unique_push_endpoint_per_player",
            ),
        ]

    def __str__(self):
        return f"Push subscription for {self.player}"


class GameRound(models.Model):

    ANSWER_TYPES = [
        ("pending", "Pending"),
        ("text", "Text"),
        ("media", "Media"),
    ]

    REVIEW_STATUSES = [
        ("pending", "Awaiting review"),
        ("accepted", "Satisfying"),
        ("rejected", "Unsatisfying"),
    ]

    room = models.ForeignKey(
        GameRoom,
        on_delete=models.CASCADE,
        related_name="rounds",
    )

    game_number = models.PositiveIntegerField(default=1)

    round_number = models.PositiveIntegerField()

    prompt = models.ForeignKey(
        Prompt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="played_rounds",
    )

    # Snapshot protects the historical round if a prompt is edited later.
    prompt_text = models.TextField()

    prompt_type = models.CharField(
        max_length=10,
        choices=Prompt.PROMPT_TYPES,
    )

    selected_by = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        related_name="selected_rounds",
    )

    challenged_player = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True,
        related_name="received_rounds",
    )

    answer_text = models.TextField(blank=True)

    answer_type = models.CharField(
        max_length=10,
        choices=ANSWER_TYPES,
        default="pending",
    )

    review_status = models.CharField(
        max_length=10,
        choices=REVIEW_STATUSES,
        default="pending",
    )

    reviewed_at = models.DateTimeField(null=True, blank=True)

    proof_public_id = models.CharField(max_length=255, blank=True)

    proof_version = models.PositiveBigIntegerField(null=True, blank=True)

    proof_format = models.CharField(max_length=10, blank=True)

    proof_resource_type = models.CharField(max_length=10, default="image")

    answered_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["room", "game_number", "round_number"],
                name="unique_round_number_per_game",
            ),
        ]

        ordering = ["game_number", "round_number"]

    def __str__(self):
        return f"{self.room.code} - Round {self.round_number}"


class AnswerProofView(models.Model):
    game_round = models.ForeignKey(
        GameRound,
        on_delete=models.CASCADE,
        related_name="proof_views",
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name="answer_proof_views",
    )
    viewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["game_round", "player"],
                name="unique_proof_view_per_player",
            ),
        ]


class RoomMessage(models.Model):
    room = models.ForeignKey(
        GameRoom,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name="room_messages",
    )
    text = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.player.nickname}: {self.text[:60]}"


class AnswerReaction(models.Model):
    EMOJI_CHOICES = [
        ("😂", "Laugh"),
        ("❤️", "Heart"),
        ("😲", "Shocked"),
        ("😋", "Yum"),
    ]

    game_round = models.ForeignKey(
        GameRound,
        on_delete=models.CASCADE,
        related_name="reactions",
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name="answer_reactions",
    )
    emoji = models.CharField(max_length=8, choices=EMOJI_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["game_round", "player"],
                name="unique_answer_reaction_per_player",
            ),
        ]

    def __str__(self):
        return f"{self.player.nickname} reacted {self.emoji}"
