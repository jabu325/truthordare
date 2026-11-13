import secrets
from datetime import timedelta
from pathlib import Path

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from .constants import PROMPT_CATEGORY_ALIASES
from .models import GameRoom, GameRound, Player, Prompt

PRESENCE_TIMEOUT = timedelta(seconds=75)


def home(request):
    return render(request, "games/home.html")


@require_safe
def service_worker(request):
    worker_path = Path(__file__).parent / "static" / "games" / "service-worker.js"
    response = HttpResponse(
        worker_path.read_text(encoding="utf-8"),
        content_type="application/javascript",
    )
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


def store_player_token(request, room, player):
    tokens = request.session.get("room_player_tokens", {})

    tokens[str(room.pk)] = player.session_key

    request.session["room_player_tokens"] = tokens


def get_player_token(request, room):
    tokens = request.session.get("room_player_tokens", {})
    return tokens.get(str(room.pk))


@require_POST
def create_room(request):
    nickname = request.POST.get("nickname", "").strip()

    if not nickname:
        messages.error(request, "Please enter a nickname.")
        return redirect("games:home")

    if len(nickname) > 24:
        messages.error(request, "Nicknames cannot exceed 24 characters.")
        return redirect("games:home")

    with transaction.atomic():
        room = GameRoom.objects.create()

        player = Player.objects.create(
            room=room,
            nickname=nickname,
            session_key=secrets.token_hex(20),
            is_host=True,
            is_connected=True,
            last_seen=timezone.now(),
        )

        room.host = player
        room.save(update_fields=["host"])

    store_player_token(request, room, player)

    return redirect("games:room", code=room.code)

@require_POST
def join_room(request):
    nickname = request.POST.get("nickname", "").strip()
    code = request.POST.get("code", "").strip().upper()

    if not nickname or not code:
        messages.error(request, "Enter both a nickname and room code.")
        return redirect("games:home")

    if len(nickname) > 24:
        messages.error(request, "Nicknames cannot exceed 24 characters.")
        return redirect("games:home")

    with transaction.atomic():

        room = (
            GameRoom.objects
            .select_for_update()
            .filter(code=code)
            .first()
        )

        if room is None:
            messages.error(request, "Room not found.")
            return redirect("games:home")

        # Allow an existing player to return to their room.
        token = get_player_token(request, room)

        if token:
            existing_player = Player.objects.filter(
                room=room,
                session_key=token,
                nickname__iexact=nickname,
            ).first()

            if existing_player:
                existing_player.is_connected = True
                existing_player.last_seen = timezone.now()
                existing_player.save(
                    update_fields=["is_connected", "last_seen"]
                )
                return redirect("games:room", code=room.code)

        # A matching room code and nickname let a returning player reclaim
        # their existing seat from a new browser session.
        existing_player = (
            Player.objects
            .filter(room=room, nickname__iexact=nickname)
            .first()
        )
        if existing_player:
            existing_player.is_connected = True
            existing_player.last_seen = timezone.now()
            existing_player.save(
                update_fields=["is_connected", "last_seen"]
            )
            store_player_token(request, room, existing_player)
            return redirect("games:room", code=room.code)

        if room.status != "WAITING":
            messages.error(
                request,
                "This room is underway. Enter the nickname you used for your seat to rejoin.",
            )
            return redirect("games:home")

        if room.players.count() >= 2:
            messages.error(request, "This room already has two players.")
            return redirect("games:home")

        if room.players.filter(nickname__iexact=nickname).exists():
            messages.error(request, "That nickname is already taken in this room.")
            return redirect("games:home")

        player = Player.objects.create(
            room=room,
            nickname=nickname,
            session_key=secrets.token_hex(20),
            is_host=False,
            is_connected=True,
            last_seen=timezone.now(),
        )
        room.status = "ACTIVE"
        room.phase = "CHOOSING_CATEGORY"
        room.save(update_fields=["status", "phase"])

    store_player_token(request, room, player)

    return redirect("games:room", code=room.code)


def room_detail(request, code):
    room = get_object_or_404(GameRoom, code=code.upper())

    player = get_room_player(request, room)

    if player is None:
        messages.error(request, "You don't have access to this room.")
        return redirect("games:home")

    now = timezone.now()
    Player.objects.filter(pk=player.pk).update(last_seen=now)
    player.last_seen = now

    can_act = (
        (room.phase == "CHOOSING_CATEGORY" and player.is_host)
        or (room.phase == "CHOOSING_DIFFICULTY" and player.is_host)
        or (
            room.phase == "CHOOSING_TYPE"
            and room.current_turn_id == player.id
        )
        or (
            room.phase == "CHOOSING_PROMPT"
            and room.current_turn_id != player.id
        )
        or (
            room.phase == "AWAITING_ANSWER"
            and room.current_turn_id == player.id
        )
    )
    prompt_options = []
    if room.phase == "CHOOSING_PROMPT":
        prompts_by_id = Prompt.objects.in_bulk(room.prompt_options)
        prompt_options = [
            prompts_by_id[prompt_id]
            for prompt_id in room.prompt_options
            if prompt_id in prompts_by_id
        ]
    room_players = list(room.players.all())
    active_since = now - PRESENCE_TIMEOUT
    for member in room_players:
        member.is_active = (
            member.last_seen is not None
            and member.last_seen >= active_since
        )
    opponent = next(
        (member for member in room_players if member.pk != player.pk),
        None,
    )

    return render(
        request,
        "games/room.html",
        {
            "room": room,
            "player": player,
            "players": room_players,
            "opponent": opponent,
            "rounds": room.rounds.select_related(
                "challenged_player",
                "selected_by",
            ).order_by("-round_number"),
            "current_round": room.rounds.order_by("-round_number").first(),
            "prompt_options": prompt_options,
            "can_act": can_act,
            "should_refresh": room.phase != "FINISHED" and not can_act,
        },
    )


@require_POST
def heartbeat(request, code):
    room = get_object_or_404(GameRoom, code=code.upper())
    player = get_room_player(request, room)

    if player is None:
        return HttpResponseForbidden("You don't have access to this room.")

    now = timezone.now()
    Player.objects.filter(pk=player.pk).update(last_seen=now)
    active_since = now - PRESENCE_TIMEOUT
    players = room.players.all()

    return JsonResponse({
        "players": [
            {
                "id": member.pk,
                "active": member.pk == player.pk
                or (
                    member.last_seen is not None
                    and member.last_seen >= active_since
                ),
            }
            for member in players
        ],
    })


def get_room_player(request, room, *, host_only=False):
    token = get_player_token(request, room)
    if not token:
        return None

    players = Player.objects.filter(room=room, session_key=token)
    if host_only:
        players = players.filter(is_host=True)
    return players.first()


@require_POST
def configure_category(request, code):
    category = request.POST.get("game_type", "")

    allowed_categories = dict(GameRoom.GAME_CATEGORIES)

    if category not in allowed_categories:
        messages.error(request, "Invalid game category.")
        return redirect("games:room", code=code)

    with transaction.atomic():
        room = get_object_or_404(
            GameRoom.objects.select_for_update(),
            code=code.upper(),
        )

        player = get_room_player(request, room, host_only=True)

        if player is None:
            messages.error(request, "Only the host can configure this game.")
            return redirect("games:room", code=code)

        if room.phase != "CHOOSING_CATEGORY":
            messages.error(request, "Category selection is not available now.")
            return redirect("games:room", code=code)

        if room.players.count() != 2:
            messages.error(request, "Waiting for the second player.")
            return redirect("games:room", code=code)

        room.game_type = category
        room.phase = "CHOOSING_DIFFICULTY"
        room.save(update_fields=["game_type", "phase"])

    return redirect("games:room", code=code)


@require_POST
def configure_difficulty(request, code):
    difficulty = request.POST.get("difficulty", "")

    with transaction.atomic():
        room = get_object_or_404(
            GameRoom.objects.select_for_update(),
            code=code.upper(),
        )

        player = get_room_player(request, room, host_only=True)

        if player is None:
            messages.error(request, "Only the host can configure this game.")
            return redirect("games:room", code=code)

        if room.phase != "CHOOSING_DIFFICULTY":
            messages.error(request, "Difficulty selection is not available now.")
            return redirect("games:room", code=code)

        allowed_difficulties = {"easy", "hard"}

        if room.game_type in {"best_friends", "couples"}:
            allowed_difficulties.add("spicy")

        if difficulty not in allowed_difficulties:
            messages.error(request, "That difficulty is not available.")
            return redirect("games:room", code=code)

        room.difficulty = difficulty
        room.phase = "CHOOSING_TYPE"
        room.current_turn = player
        room.status = "ACTIVE"
        room.started_at = timezone.now()
        room.save(
            update_fields=[
                "difficulty",
                "phase",
                "current_turn",
                "status",
                "started_at",
            ]
        )

    return redirect("games:room", code=code)


@require_POST
def choose_type(request, code):
    prompt_type = request.POST.get("prompt_type", "")
    if prompt_type not in dict(Prompt.PROMPT_TYPES):
        messages.error(request, "Choose either Truth or Dare.")
        return redirect("games:room", code=code)

    with transaction.atomic():
        room = get_object_or_404(
            GameRoom.objects.select_for_update(),
            code=code.upper(),
        )
        player = get_room_player(request, room)

        if player is None:
            messages.error(request, "You don't have access to this room.")
            return redirect("games:home")

        if room.phase != "CHOOSING_TYPE" or room.current_turn_id != player.id:
            messages.error(request, "It isn't your turn to choose Truth or Dare.")
            return redirect("games:room", code=code)

        prompt_categories = [
            value
            for value, category in PROMPT_CATEGORY_ALIASES.items()
            if category == room.game_type
        ]
        prompts = list(
            Prompt.objects.filter(
                type=prompt_type,
                difficulty=room.difficulty,
                game_type__in=prompt_categories,
                is_active=True,
            ).order_by("?")[:3]
        )

        if not prompts:
            messages.error(
                request,
                "There are no active prompts for that choice yet.",
            )
            return redirect("games:room", code=code)

        room.prompt_options = [prompt.id for prompt in prompts]
        room.phase = "CHOOSING_PROMPT"
        room.save(update_fields=["prompt_options", "phase"])

    return redirect("games:room", code=code)


@require_POST
def choose_prompt(request, code):
    try:
        prompt_id = int(request.POST.get("prompt_id", ""))
    except (TypeError, ValueError):
        messages.error(request, "Choose one of the offered prompts.")
        return redirect("games:room", code=code)

    with transaction.atomic():
        room = get_object_or_404(
            GameRoom.objects.select_for_update(),
            code=code.upper(),
        )
        player = get_room_player(request, room)

        if player is None:
            messages.error(request, "You don't have access to this room.")
            return redirect("games:home")

        if (
            room.phase != "CHOOSING_PROMPT"
            or room.current_turn_id == player.id
        ):
            messages.error(request, "You can't choose a prompt right now.")
            return redirect("games:room", code=code)

        if prompt_id not in room.prompt_options:
            messages.error(request, "That prompt wasn't offered for this round.")
            return redirect("games:room", code=code)

        prompt = get_object_or_404(Prompt, pk=prompt_id)
        challenged_player = room.current_turn
        if challenged_player is None:
            messages.error(request, "The game has no active player turn.")
            return redirect("games:room", code=code)

        GameRound.objects.create(
            room=room,
            round_number=room.rounds.count() + 1,
            prompt=prompt,
            prompt_text=prompt.text,
            prompt_type=prompt.type,
            selected_by=player,
            challenged_player=challenged_player,
        )
        room.prompt_options = []
        room.phase = "AWAITING_ANSWER"
        room.save(update_fields=["prompt_options", "phase"])

    return redirect("games:room", code=code)


@require_POST
def submit_answer(request, code):
    answer_text = request.POST.get("answer", "").strip()
    if not answer_text:
        messages.error(request, "Please enter your answer.")
        return redirect("games:room", code=code)

    if len(answer_text) > 5000:
        messages.error(request, "Answers cannot exceed 5000 characters.")
        return redirect("games:room", code=code)

    with transaction.atomic():
        room = get_object_or_404(
            GameRoom.objects.select_for_update(),
            code=code.upper(),
        )
        player = get_room_player(request, room)

        if player is None:
            messages.error(request, "You don't have access to this room.")
            return redirect("games:home")

        if (
            room.phase != "AWAITING_ANSWER"
            or room.current_turn_id != player.id
        ):
            messages.error(request, "You can't answer right now.")
            return redirect("games:room", code=code)

        current_round = (
            room.rounds
            .filter(challenged_player=player, answer_type="pending")
            .order_by("-round_number")
            .first()
        )
        next_player = room.players.exclude(pk=player.pk).first()

        if current_round is None or next_player is None:
            messages.error(request, "The current round is unavailable.")
            return redirect("games:room", code=code)

        current_round.answer_text = answer_text
        current_round.answer_type = "text"
        current_round.answered_at = timezone.now()
        current_round.save(
            update_fields=["answer_text", "answer_type", "answered_at"]
        )
        room.current_turn = next_player
        room.phase = "CHOOSING_TYPE"
        room.save(update_fields=["current_turn", "phase"])

    return redirect("games:room", code=code)
