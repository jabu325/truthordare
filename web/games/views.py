import hashlib
import json
import logging
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import cloudinary
import cloudinary.exceptions
import cloudinary.uploader
from cloudinary.utils import cloudinary_url
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe
from pywebpush import WebPushException, webpush

from .constants import PROMPT_CATEGORY_ALIASES
from .models import (
    AnswerReaction,
    AnswerProofView,
    GameRoom,
    GameRound,
    Player,
    Prompt,
    PushSubscription,
    RoomMessage,
)

PRESENCE_TIMEOUT = timedelta(seconds=75)
CHAT_HISTORY_LIMIT = 100
MAX_PROOF_IMAGE_SIZE = 5 * 1024 * 1024
MAX_PROOF_VIDEO_SIZE = 25 * 1024 * 1024
ALLOWED_PROOF_CONTENT_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
ALLOWED_PROOF_VIDEO_CONTENT_TYPES = {
    "video/mp4",
    "video/webm",
}
logger = logging.getLogger(__name__)


def home(request):
    return render(request, "games/home.html")


@staff_member_required(login_url="admin:login")
def management_dashboard(request):
    filters = {
        "player": request.GET.get("player", "").strip()[:80],
        "difficulty": request.GET.get("difficulty", "").strip(),
        "game_type": request.GET.get("game_type", "").strip(),
        "status": request.GET.get("status", "").strip(),
    }
    if filters["difficulty"] not in dict(GameRoom.DIFFICULTIES):
        filters["difficulty"] = ""
    if filters["game_type"] not in dict(GameRoom.GAME_CATEGORIES):
        filters["game_type"] = ""
    if filters["status"] not in dict(GameRoom.STATUSES):
        filters["status"] = ""

    rooms = (
        GameRoom.objects
        .annotate(
            dashboard_player_count=Count("players", distinct=True),
            dashboard_round_count=Count("rounds", distinct=True),
        )
        .order_by("-created_at")
    )
    if filters["player"]:
        rooms = rooms.filter(
            players__nickname__icontains=filters["player"],
        ).distinct()
    if filters["difficulty"] in dict(GameRoom.DIFFICULTIES):
        rooms = rooms.filter(difficulty=filters["difficulty"])
    if filters["game_type"] in dict(GameRoom.GAME_CATEGORIES):
        rooms = rooms.filter(game_type=filters["game_type"])
    if filters["status"] in dict(GameRoom.STATUSES):
        rooms = rooms.filter(status=filters["status"])

    paginator = Paginator(rooms, 25)
    room_page = paginator.get_page(request.GET.get("page"))
    pagination_params = request.GET.copy()
    pagination_params.pop("page", None)

    previous_page_url = None
    next_page_url = None
    if room_page.has_previous():
        pagination_params["page"] = room_page.previous_page_number()
        previous_page_url = f"?{pagination_params.urlencode()}"
    if room_page.has_next():
        pagination_params["page"] = room_page.next_page_number()
        next_page_url = f"?{pagination_params.urlencode()}"

    return render(
        request,
        "games/management.html",
        {
            "room_page": room_page,
            "filters": filters,
            "difficulty_choices": GameRoom.DIFFICULTIES,
            "game_type_choices": GameRoom.GAME_CATEGORIES,
            "status_choices": GameRoom.STATUSES,
            "previous_page_url": previous_page_url,
            "next_page_url": next_page_url,
            "filtered_room_total": paginator.count,
            "room_total": GameRoom.objects.count(),
            "active_room_total": GameRoom.objects.filter(status="ACTIVE").count(),
            "player_total": Player.objects.count(),
            "round_total": GameRound.objects.count(),
        },
    )


@staff_member_required(login_url="admin:login")
def management_room_detail(request, code):
    room = get_object_or_404(
        GameRoom.objects.prefetch_related("players"),
        code=code.upper(),
    )
    rounds = room.rounds.select_related(
        "selected_by",
        "challenged_player",
    ).order_by("-game_number", "-round_number")

    return render(
        request,
        "games/management_room.html",
        {
            "room": room,
            "players": room.players.all(),
            "rounds": rounds,
            "room_messages": room.messages.select_related("player").order_by(
                "created_at",
                "pk",
            ),
        },
    )


@staff_member_required(login_url="admin:login")
@require_safe
def management_answer_proof(request, code, round_id):
    game_round = get_object_or_404(
        GameRound.objects.select_related("room"),
        pk=round_id,
        room__code=code.upper(),
    )
    if game_round.answer_type == "pending" or not game_round.proof_public_id:
        return HttpResponse("Proof media not found.", status=404)
    if not cloudinary_is_configured():
        return HttpResponse(
            "Proof media delivery is not configured.",
            status=503,
        )
    return get_proof_media_response(game_round)


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


def invite_error_redirect(request, code):
    if request.POST.get("invite_flow") == "1" and code:
        return redirect("games:room_invite", code=code)
    return redirect("games:home")


def get_room_state(room, players=None, current_round=None):
    if players is None:
        players = list(room.players.all())
    if current_round is None:
        current_round = (
            room.rounds
            .filter(game_number=room.game_number)
            .order_by("-round_number")
            .first()
        )

    latest_round = None
    if current_round is not None:
        latest_round = {
            "id": current_round.pk,
            "answer_type": current_round.answer_type,
            "review_status": current_round.review_status,
            "has_proof": bool(current_round.proof_public_id),
            "challenged_player_id": current_round.challenged_player_id,
            "answered_at": (
                current_round.answered_at.isoformat()
                if current_round.answered_at
                else None
            ),
        }

    return {
        "status": room.status,
        "phase": room.phase,
        "game_number": room.game_number,
        "current_turn_id": room.current_turn_id,
        "winner_id": room.winner_id,
        "prompt_options": room.prompt_options,
        "player_ids": sorted(member.pk for member in players),
        "player_hearts": [
            {"id": member.pk, "hearts": member.hearts}
            for member in sorted(players, key=lambda member: member.pk)
        ],
        "restart_requested_player_ids": sorted(
            member.pk for member in players if member.restart_requested
        ),
        "latest_round": latest_round,
    }


def get_action_player_id(room):
    if room.phase in {"CHOOSING_CATEGORY", "CHOOSING_DIFFICULTY"}:
        return room.host_id
    if room.phase == "CHOOSING_PROMPT":
        if room.current_turn_id is None:
            return None
        return (
            room.players.exclude(pk=room.current_turn_id)
            .values_list("pk", flat=True)
            .first()
        )
    if room.phase in {"CHOOSING_TYPE", "AWAITING_ANSWER", "REVIEWING_ANSWER"}:
        return room.current_turn_id
    return None


def send_turn_notification(room_id):
    if not (
        settings.VAPID_PUBLIC_KEY
        and settings.VAPID_PRIVATE_KEY
        and settings.VAPID_SUBJECT
    ):
        return

    room = GameRoom.objects.filter(pk=room_id).first()
    if room is None:
        return
    player_id = get_action_player_id(room)
    if player_id is None:
        return

    player = Player.objects.filter(pk=player_id).first()
    if player is None:
        return

    payload = json.dumps({
        "title": "Your turn in Truth or Dare",
        "body": f"It's your turn to play, {player.nickname}.",
        "url": reverse("games:room", kwargs={"code": room.code}),
        "tag": f"turn-{room.code}",
    })
    for subscription in player.push_subscriptions.all():
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {
                        "p256dh": subscription.p256dh,
                        "auth": subscription.auth,
                    },
                },
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_SUBJECT},
                ttl=3600,
                timeout=5,
            )
        except WebPushException as error:
            status_code = (
                error.response.status_code
                if error.response is not None
                else None
            )
            if status_code in {404, 410}:
                subscription.delete()
                logger.info(
                    "Removed expired push subscription %s for player %s.",
                    subscription.pk,
                    player.pk,
                )
            else:
                logger.exception(
                    "Could not send a turn notification to player %s in room %s.",
                    player.pk,
                    room.code,
                )
        except ValueError:
            logger.exception(
                "Could not send a turn notification to player %s in room %s because the push configuration or subscription is invalid.",
                player.pk,
                room.code,
            )


def queue_turn_notification(room):
    transaction.on_commit(
        lambda room_id=room.pk: send_turn_notification(room_id)
    )


def is_supported_push_endpoint(endpoint):
    try:
        parsed = urlsplit(endpoint)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False

    supported_host = (
        hostname == "fcm.googleapis.com"
        or hostname == "push.services.mozilla.com"
        or hostname.endswith(".push.services.mozilla.com")
        or hostname == "push.apple.com"
        or hostname.endswith(".push.apple.com")
        or hostname == "notify.windows.com"
        or hostname.endswith(".notify.windows.com")
    )
    return (
        parsed.scheme == "https"
        and bool(hostname)
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and not parsed.fragment
        and supported_host
    )


def cloudinary_is_configured():
    try:
        configuration = cloudinary.config()
    except ValueError:
        logger.exception("Cloudinary configuration is invalid.")
        return False
    return bool(
        configuration.cloud_name
        and configuration.api_key
        and configuration.api_secret
    )


def is_supported_proof_image(uploaded_file):
    if uploaded_file.size > MAX_PROOF_IMAGE_SIZE:
        return False

    signature = uploaded_file.read(12)
    uploaded_file.seek(0)
    return (
        signature.startswith(b"\xff\xd8\xff")
        or signature.startswith(b"\x89PNG\r\n\x1a\n")
        or signature.startswith((b"GIF87a", b"GIF89a"))
        or (
            signature.startswith(b"RIFF")
            and signature[8:12] == b"WEBP"
        )
    )


def is_supported_proof_video(uploaded_file):
    if uploaded_file.size > MAX_PROOF_VIDEO_SIZE:
        return False

    signature = uploaded_file.read(64)
    uploaded_file.seek(0)
    if len(signature) >= 12 and signature[4:8] == b"ftyp":
        return signature[8:12] in {
            b"isom",
            b"iso2",
            b"iso5",
            b"iso6",
            b"mp41",
            b"mp42",
            b"avc1",
            b"M4V ",
        }
    return signature.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in signature.lower()


def upload_proof_media(uploaded_file: UploadedFile, resource_type):
    result = cloudinary.uploader.upload(
        uploaded_file,
        public_id=f"truth-dare/proofs/{secrets.token_urlsafe(24)}",
        overwrite=False,
        resource_type=resource_type,
        type="authenticated",
        tags=["truth-dare-proof"],
    )
    if not result.get("public_id") or not result.get("version") or not result.get("format"):
        raise ValueError("Cloudinary returned incomplete proof media metadata.")
    return result


def get_proof_media_response(game_round):
    resource_type = game_round.proof_resource_type
    if resource_type not in {"image", "video"}:
        logger.error(
            "Round %s has an unsupported proof resource type.",
            game_round.pk,
        )
        return HttpResponse("Proof media could not be loaded.", status=502)
    max_size = (
        MAX_PROOF_VIDEO_SIZE if resource_type == "video" else MAX_PROOF_IMAGE_SIZE
    )
    allowed_content_types = (
        ALLOWED_PROOF_VIDEO_CONTENT_TYPES
        if resource_type == "video"
        else ALLOWED_PROOF_CONTENT_TYPES
    )
    signed_url, _options = cloudinary_url(
        game_round.proof_public_id,
        resource_type=resource_type,
        type="authenticated",
        version=game_round.proof_version,
        format=game_round.proof_format,
        secure=True,
        sign_url=True,
    )
    try:
        timeout = 60 if resource_type == "video" else 10
        with urlopen(signed_url, timeout=timeout) as cloudinary_response:
            content_type = cloudinary_response.headers.get_content_type()
            media_data = cloudinary_response.read(max_size + 1)
    except (HTTPError, URLError, TimeoutError) as error:
        logger.error(
            "Cloudinary proof delivery failed for round %s (%s).",
            game_round.pk,
            type(error).__name__,
        )
        return HttpResponse("Proof media could not be loaded.", status=502)

    if (
        content_type not in allowed_content_types
        or len(media_data) > max_size
    ):
        logger.error(
            "Cloudinary returned invalid proof media for round %s.",
            game_round.pk,
        )
        return HttpResponse("Proof media could not be loaded.", status=502)

    response = HttpResponse(media_data, content_type=content_type)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def matching_prompts(room, prompt_type):
    prompt_categories = [
        value
        for value, category in PROMPT_CATEGORY_ALIASES.items()
        if category == room.game_type
    ]
    return Prompt.objects.filter(
        type=prompt_type,
        difficulty=room.difficulty,
        game_type__in=prompt_categories,
        is_active=True,
    )


def available_prompts(room, prompt_type, selecting_player):
    used_prompt_ids = room.rounds.filter(
        game_number=room.game_number,
        selected_by=selecting_player,
        prompt__isnull=False,
    ).values("prompt_id")
    return matching_prompts(room, prompt_type).exclude(pk__in=used_prompt_ids)


def get_prompt_availability(room, selecting_player):
    used_prompt_ids = room.rounds.filter(
        game_number=room.game_number,
        selected_by=selecting_player,
        prompt__isnull=False,
    ).values("prompt_id")
    availability = {}
    for prompt_type, _label in Prompt.PROMPT_TYPES:
        matching = matching_prompts(room, prompt_type)
        availability[prompt_type] = {
            "remaining": matching.exclude(pk__in=used_prompt_ids).count(),
            "has_prompts": matching.exists(),
        }
    return availability


def serialize_chat_messages(room):
    messages = list(
        room.messages.select_related("player")
        .order_by("-created_at", "-pk")[:CHAT_HISTORY_LIMIT]
    )
    return [
        {
            "id": message.pk,
            "player_id": message.player_id,
            "nickname": message.player.nickname,
            "is_host": message.player.is_host,
            "text": message.text,
            "created_at": message.created_at.isoformat(),
            "time": timezone.localtime(message.created_at).strftime("%H:%M"),
        }
        for message in reversed(messages)
    ]


def get_unread_messages(room, player):
    messages = room.messages.exclude(player=player)
    if player.last_read_message_id is not None:
        messages = messages.filter(pk__gt=player.last_read_message_id)
    return messages


def serialize_messages_after(room, message_id):
    messages = (
        room.messages.filter(pk__gt=message_id)
        .select_related("player")
        .order_by("pk")[:CHAT_HISTORY_LIMIT]
    )
    return [
        {
            "id": message.pk,
            "player_id": message.player_id,
            "nickname": message.player.nickname,
            "is_host": message.player.is_host,
            "text": message.text,
            "created_at": message.created_at.isoformat(),
            "time": timezone.localtime(message.created_at).strftime("%H:%M"),
        }
        for message in messages
    ]


def serialize_reactions(rounds, player):
    round_ids = [game_round.pk for game_round in rounds]
    counts = AnswerReaction.objects.filter(
        game_round_id__in=round_ids,
    ).values("game_round_id", "emoji").annotate(total=Count("pk"))
    selected = set(
        AnswerReaction.objects.filter(
            game_round_id__in=round_ids,
            player=player,
        ).values_list("game_round_id", "emoji")
    )
    result = {}
    for game_round in rounds:
        result[str(game_round.pk)] = {
            emoji: {
                "count": 0,
                "selected": (game_round.pk, emoji) in selected,
            }
            for emoji, _label in AnswerReaction.EMOJI_CHOICES
        }
    for item in counts:
        summary = result[str(item["game_round_id"])][item["emoji"]]
        summary["count"] = item["total"]
    return result


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


@require_safe
def room_invite(request, code):
    room = get_object_or_404(GameRoom, code=code.upper())
    invite_url = request.build_absolute_uri(
        reverse("games:room_invite", kwargs={"code": room.code})
    )
    return render(
        request,
        "games/invite.html",
        {
            "room": room,
            "invite_url": invite_url,
            "room_is_joinable": room.status == "WAITING" and room.players.count() < 2,
        },
    )


@require_POST
def join_room(request):
    nickname = request.POST.get("nickname", "").strip()
    code = request.POST.get("code", "").strip().upper()

    if not nickname or not code:
        messages.error(request, "Enter both a nickname and room code.")
        return invite_error_redirect(request, code)

    if len(nickname) > 24:
        messages.error(request, "Nicknames cannot exceed 24 characters.")
        return invite_error_redirect(request, code)

    with transaction.atomic():

        room = (
            GameRoom.objects
            .select_for_update()
            .filter(code=code)
            .first()
        )

        if room is None:
            messages.error(request, "Room not found.")
            return invite_error_redirect(request, "")

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
            return invite_error_redirect(request, code)

        if room.players.count() >= 2:
            messages.error(request, "This room already has two players.")
            return invite_error_redirect(request, code)

        if room.players.filter(nickname__iexact=nickname).exists():
            messages.error(request, "That nickname is already taken in this room.")
            return invite_error_redirect(request, code)

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
        queue_turn_notification(room)

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
        or (
            room.phase == "REVIEWING_ANSWER"
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
    prompt_selector = next(
        (
            member
            for member in room_players
            if member.pk != room.current_turn_id
        ),
        None,
    )
    room_messages = serialize_chat_messages(room)
    rounds = list(
        room.rounds.select_related(
            "selected_by",
            "challenged_player",
        ).order_by("-game_number", "-round_number")
    )
    reaction_state = serialize_reactions(rounds, player)
    for game_round in rounds:
        game_round.reaction_summary = [
            {
                "emoji": emoji,
                "label": label,
                "count": reaction_state[str(game_round.pk)][emoji]["count"],
            }
            for emoji, label in AnswerReaction.EMOJI_CHOICES
        ]
    current_round = next(
        (
            game_round
            for game_round in rounds
            if game_round.game_number == room.game_number
        ),
        None,
    )
    viewed_proof_round_ids = set(
        AnswerProofView.objects.filter(
            game_round_id__in=[game_round.pk for game_round in rounds],
            player=player,
        ).values_list("game_round_id", flat=True)
    )
    for game_round in rounds:
        game_round.proof_viewed = game_round.pk in viewed_proof_round_ids
        game_round.is_current_review = (
            room.phase == "REVIEWING_ANSWER"
            and current_round is not None
            and game_round.pk == current_round.pk
        )
    invite_url = request.build_absolute_uri(
        reverse("games:room_invite", kwargs={"code": room.code})
    )

    return render(
        request,
        "games/room.html",
        {
            "room": room,
            "player": player,
            "players": room_players,
            "opponent": opponent,
            "opponent_restart_requested": (
                opponent.restart_requested if opponent is not None else False
            ),
            "prompt_selector": prompt_selector,
            "rounds": rounds,
            "current_round": current_round,
            "prompt_options": prompt_options,
            "can_act": can_act,
            "room_state": get_room_state(room, room_players, current_round),
            "invite_url": invite_url,
            "room_messages": room_messages,
            "unread_count": get_unread_messages(room, player).count(),
            "last_message_id": room_messages[-1]["id"] if room_messages else 0,
            "reaction_state": reaction_state,
            "reaction_choices": AnswerReaction.EMOJI_CHOICES,
            "cloudinary_available": cloudinary_is_configured(),
            "push_notifications_enabled": bool(
                settings.VAPID_PUBLIC_KEY
                and settings.VAPID_PRIVATE_KEY
                and settings.VAPID_SUBJECT
            ),
            "push_config": {
                "enabled": bool(
                    settings.VAPID_PUBLIC_KEY
                    and settings.VAPID_PRIVATE_KEY
                    and settings.VAPID_SUBJECT
                ),
                "public_key": settings.VAPID_PUBLIC_KEY,
                "subscription_url": reverse(
                    "games:push_subscription",
                    kwargs={"code": room.code},
                ),
            },
            "prompt_availability": (
                get_prompt_availability(
                    room,
                    room.players.exclude(pk=room.current_turn_id).first(),
                )
                if room.phase == "CHOOSING_TYPE"
                else {}
            ),
        },
    )


@require_POST
def heartbeat(request, code):
    room = get_object_or_404(GameRoom, code=code.upper())
    player = get_room_player(request, room)

    if player is None:
        return HttpResponseForbidden("You don't have access to this room.")

    try:
        last_message_id = int(request.POST.get("last_message_id", "0"))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Invalid message cursor."}, status=400)
    if last_message_id < 0:
        return JsonResponse({"error": "Invalid message cursor."}, status=400)

    now = timezone.now()
    Player.objects.filter(pk=player.pk).update(last_seen=now)
    active_since = now - PRESENCE_TIMEOUT
    players = room.players.all()

    current_round = (
        room.rounds
        .filter(game_number=room.game_number)
        .order_by("-round_number")
        .first()
    )
    chat_messages = serialize_messages_after(room, last_message_id)
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
        "room_state": get_room_state(room, players, current_round),
        "chat_messages": chat_messages,
        "last_message_id": (
            chat_messages[-1]["id"] if chat_messages else last_message_id
        ),
        "unread_count": get_unread_messages(room, player).count(),
        "reactions": serialize_reactions(
            list(room.rounds.only("pk")),
            player,
        ),
    })


@require_POST
def push_subscription(request, code):
    room = get_object_or_404(GameRoom, code=code.upper())
    player = get_room_player(request, room)
    if player is None:
        return JsonResponse(
            {"error": "You don't have access to this room."},
            status=403,
        )
    if len(request.body) > 8192:
        return JsonResponse({"error": "Invalid subscription data."}, status=400)

    try:
        payload = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid subscription data."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Invalid subscription data."}, status=400)

    endpoint = payload.get("endpoint")
    if (
        not isinstance(endpoint, str)
        or len(endpoint) > 4096
        or not is_supported_push_endpoint(endpoint)
    ):
        return JsonResponse(
            {"error": "Unsupported push subscription endpoint."},
            status=400,
        )

    if payload.get("action") == "unsubscribe":
        PushSubscription.objects.filter(
            player=player,
            endpoint_hash=hashlib.sha256(endpoint.encode()).hexdigest(),
        ).delete()
        return JsonResponse({"subscribed": False})

    keys = payload.get("keys")
    if not isinstance(keys, dict):
        return JsonResponse({"error": "Invalid subscription keys."}, status=400)
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if (
        not isinstance(p256dh, str)
        or not p256dh
        or len(p256dh) > 256
        or not isinstance(auth, str)
        or not auth
        or len(auth) > 256
    ):
        return JsonResponse({"error": "Invalid subscription keys."}, status=400)

    _subscription, created = PushSubscription.objects.update_or_create(
        player=player,
        endpoint_hash=hashlib.sha256(endpoint.encode()).hexdigest(),
        defaults={
            "endpoint": endpoint,
            "p256dh": p256dh,
            "auth": auth,
        },
    )
    return JsonResponse(
        {"subscribed": True},
        status=201 if created else 200,
    )


@require_POST
def mark_chat_read(request, code):
    room = get_object_or_404(GameRoom, code=code.upper())
    player = get_room_player(request, room)
    if player is None:
        return JsonResponse(
            {"error": "You don't have access to this room."},
            status=403,
        )

    with transaction.atomic():
        player = Player.objects.select_for_update().get(pk=player.pk)
        latest_message_id = (
            room.messages.order_by("-pk").values_list("pk", flat=True).first()
        )
        if (
            latest_message_id is not None
            and (
                player.last_read_message_id is None
                or latest_message_id > player.last_read_message_id
            )
        ):
            player.last_read_message_id = latest_message_id
            player.save(update_fields=["last_read_message_id"])

    return JsonResponse(
        {
            "unread_count": get_unread_messages(room, player).count(),
            "last_message_id": latest_message_id or 0,
            "chat_messages": serialize_chat_messages(room),
        }
    )


@require_POST
def room_chat(request, code):
    room = get_object_or_404(GameRoom, code=code.upper())
    player = get_room_player(request, room)
    if player is None:
        return JsonResponse(
            {"error": "You don't have access to this room."},
            status=403,
        )

    text = request.POST.get("message", "").strip()
    if not text:
        return JsonResponse({"error": "Write a message before sending."}, status=400)
    if len(text) > 1000:
        return JsonResponse(
            {"error": "Messages cannot exceed 1000 characters."},
            status=400,
        )

    message = RoomMessage.objects.create(room=room, player=player, text=text)
    return JsonResponse(
        {
            "message": {
                "id": message.pk,
                "nickname": player.nickname,
                "is_host": player.is_host,
                "text": message.text,
                "created_at": message.created_at.isoformat(),
                "time": timezone.localtime(message.created_at).strftime("%H:%M"),
            },
        },
        status=201,
    )


@require_POST
def toggle_answer_reaction(request, code, round_id):
    room = get_object_or_404(GameRoom, code=code.upper())
    player = get_room_player(request, room)
    if player is None:
        return JsonResponse(
            {"error": "You don't have access to this room."},
            status=403,
        )

    emoji = request.POST.get("emoji", "")
    if emoji not in dict(AnswerReaction.EMOJI_CHOICES):
        return JsonResponse({"error": "Choose a supported reaction."}, status=400)

    with transaction.atomic():
        game_round = get_object_or_404(
            GameRound.objects.select_for_update(),
            pk=round_id,
            room=room,
        )
        if game_round.challenged_player_id == player.pk:
            return JsonResponse(
                {"error": "You can't react to your own answer."},
                status=403,
            )
        if game_round.answer_type == "pending":
            return JsonResponse(
                {"error": "You can react after the answer is submitted."},
                status=400,
            )
        reaction = AnswerReaction.objects.filter(
            game_round=game_round,
            player=player,
        ).first()
        if reaction and reaction.emoji == emoji:
            reaction.delete()
        elif reaction:
            reaction.emoji = emoji
            reaction.save(update_fields=["emoji"])
        else:
            AnswerReaction.objects.create(
                game_round=game_round,
                player=player,
                emoji=emoji,
            )

    reactions = serialize_reactions([game_round], player)[str(game_round.pk)]
    return JsonResponse({"round_id": game_round.pk, "reactions": reactions})


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
        queue_turn_notification(room)

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
        queue_turn_notification(room)

    return redirect("games:room", code=code)


@require_POST
def start_new_game(request, code):
    with transaction.atomic():
        room = get_object_or_404(
            GameRoom.objects.select_for_update(),
            code=code.upper(),
        )
        player = get_room_player(request, room)

        if player is None:
            messages.error(request, "You don't have access to this room.")
            return redirect("games:room", code=code)

        if room.phase in {"WAITING", "CANCELLED"} or room.players.count() != 2:
            messages.error(
                request,
                "Both players must join before requesting a restart.",
            )
            return redirect("games:room", code=code)

        action = request.POST.get("restart_action", "request")
        if action == "cancel":
            if not player.restart_requested:
                messages.error(request, "You haven't requested a restart.")
                return redirect("games:room", code=code)
            player.restart_requested = False
            player.save(update_fields=["restart_requested"])
            messages.info(request, "Your restart request was cancelled.")
        elif action == "request":
            if not player.restart_requested:
                player.restart_requested = True
                player.save(update_fields=["restart_requested"])

            if room.players.filter(restart_requested=True).count() == 2:
                room.game_number += 1
                room.game_type = ""
                room.difficulty = ""
                room.status = "ACTIVE"
                room.phase = "CHOOSING_CATEGORY"
                room.current_turn = room.host
                room.winner = None
                room.prompt_options = []
                room.started_at = None
                room.finished_at = None
                room.players.update(hearts=5, restart_requested=False)
                room.save(
                    update_fields=[
                        "game_number",
                        "game_type",
                        "difficulty",
                        "status",
                        "phase",
                        "current_turn",
                        "winner",
                        "prompt_options",
                        "started_at",
                        "finished_at",
                    ]
                )
                queue_turn_notification(room)
                messages.success(
                    request,
                    "Both players agreed. A fresh game is ready!",
                )
            else:
                messages.info(
                    request,
                    "Restart requested. Waiting for the other player to confirm.",
                )
        else:
            messages.error(request, "Choose a valid restart action.")

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

        selecting_player = room.players.exclude(pk=player.pk).first()
        if selecting_player is None:
            messages.error(request, "The other player is unavailable.")
            return redirect("games:room", code=code)

        prompts = list(
            available_prompts(room, prompt_type, selecting_player)
            .order_by("?")[:3]
        )

        if not prompts:
            if matching_prompts(room, prompt_type).exists():
                messages.error(
                    request,
                    f"{selecting_player.nickname} has already chosen all prompts of that type in this game.",
                )
            else:
                messages.error(request, "There are no active prompts for that choice yet.")
            return redirect("games:room", code=code)

        room.prompt_options = [prompt.id for prompt in prompts]
        room.phase = "CHOOSING_PROMPT"
        room.save(update_fields=["prompt_options", "phase"])
        queue_turn_notification(room)

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
            game_number=room.game_number,
            round_number=(
                room.rounds.filter(game_number=room.game_number).count() + 1
            ),
            prompt=prompt,
            prompt_text=prompt.text,
            prompt_type=prompt.type,
            selected_by=player,
            challenged_player=challenged_player,
        )
        room.prompt_options = []
        room.phase = "AWAITING_ANSWER"
        room.save(update_fields=["prompt_options", "phase"])
        queue_turn_notification(room)

    return redirect("games:room", code=code)


@require_POST
def submit_answer(request, code):
    answer_text = request.POST.get("answer", "").strip()
    proof_media = request.FILES.get("proof_media") or request.FILES.get(
        "proof_image",
    )
    proof_resource_type = None
    if proof_media:
        if is_supported_proof_image(proof_media):
            proof_resource_type = "image"
        elif is_supported_proof_video(proof_media):
            proof_resource_type = "video"
        else:
            messages.error(
                request,
                "Choose a JPG, PNG, GIF, or WebP image up to 5 MB, or an MP4 "
                "or WebM video up to 25 MB.",
            )
            return redirect("games:room", code=code)

    if not answer_text and not proof_media:
        messages.error(request, "Enter a text answer or upload proof media.")
        return redirect("games:room", code=code)

    if len(answer_text) > 5000:
        messages.error(request, "Answers cannot exceed 5000 characters.")
        return redirect("games:room", code=code)

    if proof_media:
        if not cloudinary_is_configured():
            messages.error(
                request,
                "Proof media is unavailable because Cloudinary is not configured.",
            )
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
            .filter(
                game_number=room.game_number,
                challenged_player=player,
                answer_type="pending",
            )
            .order_by("-round_number")
            .first()
        )
        next_player = room.players.exclude(pk=player.pk).first()

        if current_round is None or next_player is None:
            messages.error(request, "The current round is unavailable.")
            return redirect("games:room", code=code)

        if proof_media and current_round.prompt_type != "dare":
            messages.error(request, "Proof media can only be attached to a dare.")
            return redirect("games:room", code=code)

        proof = None
        if proof_media:
            try:
                proof = upload_proof_media(proof_media, proof_resource_type)
            except cloudinary.exceptions.Error:
                logger.exception(
                    "Cloudinary proof upload failed for room %s.",
                    room.code,
                )
                messages.error(
                    request,
                    "Cloudinary could not store your proof media. Please try again.",
                )
                return redirect("games:room", code=code)
            except ValueError:
                logger.exception(
                    "Cloudinary proof upload returned invalid configuration or metadata for room %s.",
                    room.code,
                )
                messages.error(
                    request,
                    "Cloudinary is not configured correctly for proof uploads.",
                )
                return redirect("games:room", code=code)

        current_round.answer_text = answer_text
        current_round.answer_type = "media" if proof else "text"
        if proof:
            current_round.proof_public_id = proof["public_id"]
            current_round.proof_version = proof["version"]
            current_round.proof_format = proof["format"]
            current_round.proof_resource_type = proof_resource_type
        current_round.answered_at = timezone.now()
        current_round.save(
            update_fields=[
                "answer_text",
                "answer_type",
                "proof_public_id",
                "proof_version",
                "proof_format",
                "proof_resource_type",
                "answered_at",
            ]
        )
        room.current_turn = next_player
        room.phase = "REVIEWING_ANSWER"
        room.save(update_fields=["current_turn", "phase"])
        queue_turn_notification(room)

    return redirect("games:room", code=code)


@require_POST
def review_answer(request, code, round_id):
    review = request.POST.get("review", "")
    if review not in {"valid", "unsatisfying"}:
        messages.error(request, "Choose whether the answer is satisfying.")
        return redirect("games:room", code=code)

    with transaction.atomic():
        room = get_object_or_404(
            GameRoom.objects.select_for_update(),
            code=code.upper(),
        )
        reviewer = get_room_player(request, room)
        if reviewer is None:
            messages.error(request, "You don't have access to this room.")
            return redirect("games:home")

        game_round = get_object_or_404(
            GameRound.objects.select_for_update(),
            pk=round_id,
            room=room,
            game_number=room.game_number,
        )
        if (
            room.phase != "REVIEWING_ANSWER"
            or room.current_turn_id != reviewer.pk
            or game_round.selected_by_id != reviewer.pk
            or game_round.answer_type == "pending"
            or game_round.review_status != "pending"
        ):
            messages.error(request, "You can't review this answer right now.")
            return redirect("games:room", code=code)

        if game_round.challenged_player_id is None:
            messages.error(request, "The player who answered is unavailable.")
            return redirect("games:room", code=code)

        game_round.review_status = (
            "accepted" if review == "valid" else "rejected"
        )
        game_round.reviewed_at = timezone.now()
        game_round.save(update_fields=["review_status", "reviewed_at"])

        if review == "unsatisfying":
            answering_player = Player.objects.select_for_update().get(
                pk=game_round.challenged_player_id,
                room=room,
            )
            answering_player.hearts = max(0, answering_player.hearts - 1)
            answering_player.save(update_fields=["hearts"])

            if answering_player.hearts == 0:
                room.winner = reviewer
                room.status = "FINISHED"
                room.phase = "FINISHED"
                room.finished_at = timezone.now()
                room.save(
                    update_fields=[
                        "winner",
                        "status",
                        "phase",
                        "finished_at",
                    ]
                )
            else:
                room.phase = "CHOOSING_TYPE"
                room.save(update_fields=["phase"])
                queue_turn_notification(room)
        else:
            room.phase = "CHOOSING_TYPE"
            room.save(update_fields=["phase"])
            queue_turn_notification(room)

    return redirect("games:room", code=code)


@require_POST
def view_answer_proof(request, code, round_id):
    with transaction.atomic():
        room = get_object_or_404(
            GameRoom.objects.select_for_update(),
            code=code.upper(),
        )
        player = get_room_player(request, room)
        if player is None:
            return HttpResponseForbidden("You don't have access to this room.")

        game_round = get_object_or_404(
            GameRound.objects.select_for_update(),
            pk=round_id,
            room=room,
        )
        if game_round.answer_type == "pending" or not game_round.proof_public_id:
            return HttpResponse("Proof media not found.", status=404)
        if AnswerProofView.objects.filter(
            game_round=game_round,
            player=player,
        ).exists():
            return HttpResponse("This proof media was already viewed.", status=410)
        if not cloudinary_is_configured():
            return HttpResponse(
                "Proof media delivery is not configured.",
                status=503,
            )

        proof_view = AnswerProofView.objects.create(
            game_round=game_round,
            player=player,
        )

    response = get_proof_media_response(game_round)
    if response.status_code != 200:
        proof_view.delete()
        return response

    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
