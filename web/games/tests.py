from datetime import timedelta
import json
from pathlib import Path

from django.test import Client, TestCase
from django.utils import timezone

from .models import (
    AnswerReaction,
    GameRoom,
    GameRound,
    Player,
    Prompt,
    RoomMessage,
)


class GameFlowTests(TestCase):
    def setUp(self):
        self.room = GameRoom.objects.create()
        self.host = Player.objects.create(
            room=self.room,
            nickname="Host",
            session_key="host-session-token",
            is_host=True,
        )
        self.guest = Player.objects.create(
            room=self.room,
            nickname="Guest",
            session_key="guest-session-token",
        )
        self.room.host = self.host
        self.room.status = "ACTIVE"
        self.room.phase = "CHOOSING_CATEGORY"
        self.room.save()
        self.host_client = self.make_client(self.host)
        self.guest_client = self.make_client(self.guest)

    def make_client(self, player):
        client = Client()
        session = client.session
        session["room_player_tokens"] = {
            str(self.room.pk): player.session_key,
        }
        session.save()
        return client

    def add_prompts(self, prompt_type="truth", count=3, category="friends"):
        return [
            Prompt.objects.create(
                type=prompt_type,
                difficulty="easy",
                game_type=category,
                text=f"{prompt_type.title()} prompt {index}",
            )
            for index in range(count)
        ]

    def configure_game(self):
        self.host_client.post(
            f"/room/{self.room.code}/category/",
            {"game_type": "friends"},
        )
        self.host_client.post(
            f"/room/{self.room.code}/difficulty/",
            {"difficulty": "easy"},
        )

    def test_round_flow_alternates_turn_and_records_answer(self):
        self.configure_game()
        self.add_prompts()

        self.host_client.post(
            f"/room/{self.room.code}/type/",
            {"prompt_type": "truth"},
        )
        self.room.refresh_from_db()
        self.assertEqual(self.room.phase, "CHOOSING_PROMPT")
        self.assertEqual(len(self.room.prompt_options), 3)

        chosen_prompt_id = self.room.prompt_options[0]
        self.guest_client.post(
            f"/room/{self.room.code}/prompt/",
            {"prompt_id": chosen_prompt_id},
        )
        self.room.refresh_from_db()
        self.assertEqual(self.room.phase, "AWAITING_ANSWER")

        game_round = GameRound.objects.get(room=self.room)
        self.assertEqual(game_round.selected_by, self.guest)
        self.assertEqual(game_round.challenged_player, self.host)

        self.host_client.post(
            f"/room/{self.room.code}/answer/",
            {"answer": "A test answer"},
        )
        self.room.refresh_from_db()
        game_round.refresh_from_db()

        self.assertEqual(game_round.answer_text, "A test answer")
        self.assertEqual(game_round.answer_type, "text")
        self.assertEqual(self.room.phase, "CHOOSING_TYPE")
        self.assertEqual(self.room.current_turn, self.guest)

    def test_round_history_shows_newest_round_first(self):
        GameRound.objects.create(
            room=self.room,
            round_number=1,
            prompt_text="Older round prompt",
            prompt_type="truth",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_text="Older answer",
            answer_type="text",
        )
        GameRound.objects.create(
            room=self.room,
            round_number=2,
            prompt_text="Recent round prompt",
            prompt_type="dare",
            selected_by=self.host,
            challenged_player=self.guest,
            answer_text="Recent answer",
            answer_type="text",
        )

        response = self.host_client.get(f"/room/{self.room.code}/")

        self.assertEqual(response.status_code, 200)
        recent_index = response.content.index(b"Recent round prompt")
        older_index = response.content.index(b"Older round prompt")
        self.assertLess(recent_index, older_index)
        self.assertContains(response, 'history-message-row speaker-host')
        self.assertContains(response, 'history-message-row speaker-guest')
        self.assertContains(response, 'avatar-player avatar-host">H')
        self.assertContains(response, 'avatar-player avatar-guest">G')

    def test_home_uses_bot_emoji_for_system_avatar(self):
        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-label="Game bot">🤖')
        self.assertContains(response, 'rel="manifest"')
        self.assertContains(response, 'data-install-app')
        self.assertContains(response, "Rejoin your seat")
        self.assertContains(response, 'id="rejoin-nickname"')
        self.assertContains(response, 'id="rejoin-room-code"')

    def test_returning_player_reclaims_their_seat_using_room_code_and_nickname(self):
        returning_client = Client()
        Player.objects.filter(pk=self.guest.pk).update(
            is_connected=False,
            last_seen=None,
        )

        response = returning_client.post(
            "/join/",
            {"code": self.room.code, "nickname": "gUeSt"},
        )

        self.assertRedirects(
            response,
            f"/room/{self.room.code}/",
            fetch_redirect_response=False,
        )
        self.guest.refresh_from_db()
        self.assertTrue(self.guest.is_connected)
        self.assertIsNotNone(self.guest.last_seen)
        self.assertEqual(
            returning_client.get(f"/room/{self.room.code}/").status_code,
            200,
        )
        self.assertEqual(self.room.players.count(), 2)

    def test_invite_link_shows_only_a_nickname_entry_and_app_options(self):
        room = GameRoom.objects.create()
        Player.objects.create(room=room, nickname="Host", is_host=True)
        response = Client().get(f"/invite/{room.code.lower()}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose your seat name")
        self.assertContains(response, 'id="invite-nickname"')
        self.assertContains(response, 'name="invite_flow" value="1"')
        self.assertContains(response, "Open in installed app")
        self.assertContains(response, "data-install-app")
        self.assertNotContains(response, 'type="text" name="code"')

    def test_invite_link_joins_a_waiting_room_without_manual_code_entry(self):
        room = GameRoom.objects.create()
        Player.objects.create(
            room=room,
            nickname="Host",
            is_host=True,
        )

        response = Client().post(
            "/join/",
            {
                "code": room.code,
                "nickname": "New Guest",
                "invite_flow": "1",
            },
        )

        self.assertRedirects(
            response,
            f"/room/{room.code}/",
            fetch_redirect_response=False,
        )
        room.refresh_from_db()
        self.assertEqual(room.status, "ACTIVE")
        self.assertTrue(room.players.filter(nickname="New Guest").exists())

    def test_invalid_invite_join_redirects_back_to_invite_with_message(self):
        client = Client()
        response = client.post(
            "/join/",
            {
                "code": self.room.code,
                "nickname": "Impostor",
                "invite_flow": "1",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertRedirects(response, f"/invite/{self.room.code}/")
        self.assertContains(
            response,
            "This room is underway. Enter the nickname you used for your seat to rejoin.",
        )

    def test_waiting_room_has_copyable_invite_and_no_periodic_page_refresh(self):
        room = GameRoom.objects.create()
        host = Player.objects.create(
            room=room,
            nickname="Waiting Host",
            session_key="waiting-host-session",
            is_host=True,
        )
        room.host = host
        room.save(update_fields=["host"])
        client = Client()
        session = client.session
        session["room_player_tokens"] = {
            str(room.pk): host.session_key,
        }
        session.save()

        response = client.get(f"/room/{room.code}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Copy invite link")
        self.assertContains(response, "Share invite")
        self.assertContains(response, "Copy code:")
        self.assertContains(response, f"/invite/{room.code}/")
        self.assertNotContains(response, 'http-equiv="refresh"')

    def test_chat_button_is_fixed_above_the_install_button(self):
        response = self.host_client.get(f"/room/{self.room.code}/")

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        chat_button_index = content.index('data-chat-open')
        install_button_index = content.index('data-install-app')
        self.assertLess(chat_button_index, install_button_index)
        self.assertContains(response, 'class="room-floating-actions"')

        stylesheet = (
            Path(__file__).parent
            / "static"
            / "games"
            / "style.css"
        ).read_text(encoding="utf-8")
        self.assertIn(".room-floating-actions {", stylesheet)
        self.assertIn("position: fixed;", stylesheet)

    def test_wrong_nickname_cannot_claim_a_seat_in_a_full_room(self):
        response = Client().post(
            "/join/",
            {"code": self.room.code, "nickname": "Impostor"},
            follow=True,
        )

        self.assertContains(
            response,
            "Enter the nickname you used for your seat to rejoin.",
        )
        self.assertEqual(self.room.players.count(), 2)

    def test_heartbeat_updates_activity_and_returns_room_presence(self):
        Player.objects.filter(pk=self.guest.pk).update(
            last_seen=timezone.now() - timedelta(seconds=100),
        )

        response = self.host_client.post(f"/room/{self.room.code}/heartbeat/")

        self.assertEqual(response.status_code, 200)
        presence = {
            entry["id"]: entry["active"]
            for entry in response.json()["players"]
        }
        self.assertTrue(presence[self.host.pk])
        self.assertFalse(presence[self.guest.pk])
        self.assertEqual(response.json()["room_state"]["phase"], self.room.phase)
        self.assertEqual(
            response.json()["room_state"]["player_ids"],
            sorted([self.host.pk, self.guest.pk]),
        )
        self.host.refresh_from_db()
        self.assertIsNotNone(self.host.last_seen)

    def test_room_chat_is_saved_and_returned_to_other_player(self):
        response = self.host_client.post(
            f"/room/{self.room.code}/chat/",
            {"message": "  Nice answer!  "},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["message"]["text"], "Nice answer!")
        self.assertEqual(RoomMessage.objects.get().player, self.host)

        heartbeat = self.guest_client.post(
            f"/room/{self.room.code}/heartbeat/",
        )
        self.assertEqual(
            heartbeat.json()["chat_messages"][0]["text"],
            "Nice answer!",
        )

    def test_room_chat_requires_room_access_and_valid_message(self):
        response = Client().post(
            f"/room/{self.room.code}/chat/",
            {"message": "Hello"},
        )
        self.assertEqual(response.status_code, 403)

        empty_response = self.host_client.post(
            f"/room/{self.room.code}/chat/",
            {"message": "  "},
        )
        self.assertEqual(empty_response.status_code, 400)
        self.assertEqual(RoomMessage.objects.count(), 0)

    def test_heartbeat_tracks_incoming_messages_and_read_cursor(self):
        own_message = RoomMessage.objects.create(
            room=self.room,
            player=self.host,
            text="My own message",
        )
        incoming_message = RoomMessage.objects.create(
            room=self.room,
            player=self.guest,
            text="A message for the host",
        )

        heartbeat = self.host_client.post(
            f"/room/{self.room.code}/heartbeat/",
            {"last_message_id": own_message.pk},
        )

        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(heartbeat.json()["unread_count"], 1)
        self.assertEqual(
            [message["id"] for message in heartbeat.json()["chat_messages"]],
            [incoming_message.pk],
        )

        marked_read = self.host_client.post(
            f"/room/{self.room.code}/chat/read/",
        )
        self.assertEqual(marked_read.status_code, 200)
        self.assertEqual(marked_read.json()["unread_count"], 0)
        self.assertEqual(
            marked_read.json()["chat_messages"][-1]["id"],
            incoming_message.pk,
        )
        self.host.refresh_from_db()
        self.assertEqual(
            self.host.last_read_message_id,
            incoming_message.pk,
        )

        next_message = RoomMessage.objects.create(
            room=self.room,
            player=self.guest,
            text="A later message",
        )
        next_heartbeat = self.host_client.post(
            f"/room/{self.room.code}/heartbeat/",
            {"last_message_id": incoming_message.pk},
        )
        self.assertEqual(next_heartbeat.json()["unread_count"], 1)
        self.assertEqual(
            next_heartbeat.json()["chat_messages"][0]["id"],
            next_message.pk,
        )

    def test_chat_read_marker_requires_room_access(self):
        response = Client().post(
            f"/room/{self.room.code}/chat/read/",
        )
        self.assertEqual(response.status_code, 403)

    def test_room_page_renders_persistent_unread_badge_and_notice(self):
        RoomMessage.objects.create(
            room=self.room,
            player=self.guest,
            text="An unread hello",
        )

        response = self.host_client.get(f"/room/{self.room.code}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-unread-count="1"')
        self.assertContains(response, "You’ve received a message")
        self.assertContains(response, "data-chat-badge")

    def test_player_can_toggle_or_change_one_reaction_per_answer(self):
        game_round = GameRound.objects.create(
            room=self.room,
            round_number=1,
            prompt_text="A prompt",
            prompt_type="truth",
            selected_by=self.guest,
            challenged_player=self.guest,
            answer_text="An answer",
            answer_type="text",
        )
        url = f"/room/{self.room.code}/round/{game_round.pk}/reaction/"

        first = self.host_client.post(url, {"emoji": "❤️"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["reactions"]["❤️"]["count"], 1)
        self.assertTrue(first.json()["reactions"]["❤️"]["selected"])

        changed = self.host_client.post(url, {"emoji": "😂"})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(AnswerReaction.objects.count(), 1)
        self.assertEqual(
            AnswerReaction.objects.get().emoji,
            "😂",
        )

        toggled_off = self.host_client.post(url, {"emoji": "😂"})
        self.assertEqual(toggled_off.status_code, 200)
        self.assertEqual(AnswerReaction.objects.count(), 0)

        self.host_client.post(url, {"emoji": "😲"})
        heartbeat = self.guest_client.post(
            f"/room/{self.room.code}/heartbeat/",
        )
        shared_reaction = heartbeat.json()["reactions"][str(game_round.pk)]["😲"]
        self.assertEqual(shared_reaction["count"], 1)
        self.assertFalse(shared_reaction["selected"])
        owner_page = self.guest_client.get(f"/room/{self.room.code}/")
        self.assertContains(owner_page, 'aria-label="Shocked 1"')

    def test_player_cannot_react_to_their_own_answer(self):
        game_round = GameRound.objects.create(
            room=self.room,
            round_number=1,
            prompt_text="A prompt",
            prompt_type="truth",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_text="An answer",
            answer_type="text",
        )
        response = self.host_client.post(
            f"/room/{self.room.code}/round/{game_round.pk}/reaction/",
            {"emoji": "😂"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "You can't react to your own answer.")
        self.assertFalse(AnswerReaction.objects.exists())

    def test_reactions_require_room_access_and_submitted_answer(self):
        game_round = GameRound.objects.create(
            room=self.room,
            round_number=1,
            prompt_text="A prompt",
            prompt_type="truth",
            selected_by=self.guest,
            challenged_player=self.host,
        )
        url = f"/room/{self.room.code}/round/{game_round.pk}/reaction/"

        forbidden = Client().post(url, {"emoji": "😂"})
        self.assertEqual(forbidden.status_code, 403)

        pending = self.guest_client.post(url, {"emoji": "😂"})
        self.assertEqual(pending.status_code, 400)

        invalid = self.guest_client.post(url, {"emoji": "🔥"})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(AnswerReaction.objects.count(), 0)

    def test_answer_owner_sees_received_reactions_and_other_player_can_react(self):
        game_round = GameRound.objects.create(
            room=self.room,
            round_number=1,
            prompt_text="A prompt",
            prompt_type="truth",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_text="An answer",
            answer_type="text",
        )
        AnswerReaction.objects.create(
            game_round=game_round,
            player=self.guest,
            emoji="😋",
        )

        response = self.host_client.get(f"/room/{self.room.code}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-label="Reactions received on your answer"')
        self.assertContains(response, 'data-reaction-emoji="😋"')
        self.assertContains(response, ">1</span>")
        self.assertNotContains(response, 'data-reaction-url=')

        other_player_response = self.guest_client.get(
            f"/room/{self.room.code}/",
        )
        self.assertContains(other_player_response, 'data-reaction-round="')
        self.assertContains(other_player_response, 'data-reaction-emoji="😋"')
        self.assertContains(other_player_response, 'data-reaction-url=')
        self.assertContains(response, "Player chat")
        self.assertContains(response, 'id="room-chat-drawer"')
        self.assertContains(other_player_response, "data-reaction-feedback")
        self.assertContains(other_player_response, "turn-alert.js")

    def test_heartbeat_includes_answer_owner_for_turn_alert(self):
        GameRound.objects.create(
            room=self.room,
            round_number=1,
            prompt_text="A prompt",
            prompt_type="truth",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_text="An answer",
            answer_type="text",
        )

        response = self.guest_client.post(
            f"/room/{self.room.code}/heartbeat/",
        )

        self.assertEqual(
            response.json()["room_state"]["latest_round"]["challenged_player_id"],
            self.host.pk,
        )

    def test_heartbeat_requires_a_room_player_session(self):
        response = Client().post(f"/room/{self.room.code}/heartbeat/")

        self.assertEqual(response.status_code, 403)

    def test_service_worker_allows_root_scope_and_avoids_private_page_caching(self):
        response = Client().get("/service-worker.js")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertContains(response, 'const CACHE_NAME = "truth-dare-arcade-v10"')
        self.assertContains(response, "event.request.mode === \"navigate\"")
        self.assertContains(response, "fetch(event.request).catch")
        self.assertNotContains(response, "cache.put(event.request")

    def test_web_app_manifest_has_installation_metadata(self):
        manifest_path = (
            Path(__file__).parent
            / "static"
            / "games"
            / "manifest.webmanifest"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(
            {icon["sizes"] for icon in manifest["icons"]},
            {"192x192", "512x512"},
        )

    def test_only_the_current_player_can_choose_truth_or_dare(self):
        self.configure_game()

        response = self.guest_client.post(
            f"/room/{self.room.code}/type/",
            {"prompt_type": "truth"},
            follow=True,
        )

        self.room.refresh_from_db()
        self.assertEqual(self.room.phase, "CHOOSING_TYPE")
        self.assertContains(
            response,
            "It isn&#x27;t your turn to choose Truth or Dare.",
            status_code=200,
        )

    def test_truth_or_dare_offers_available_prompts_when_fewer_than_three(self):
        self.configure_game()
        self.add_prompts(count=2)

        self.host_client.post(
            f"/room/{self.room.code}/type/",
            {"prompt_type": "truth"},
        )

        self.room.refresh_from_db()
        self.assertEqual(self.room.phase, "CHOOSING_PROMPT")
        self.assertEqual(len(self.room.prompt_options), 2)

    def test_legacy_prompt_category_aliases_are_supported(self):
        self.host_client.post(
            f"/room/{self.room.code}/category/",
            {"game_type": "best_friends"},
        )
        self.host_client.post(
            f"/room/{self.room.code}/difficulty/",
            {"difficulty": "easy"},
        )
        self.add_prompts(category="bestfriend")

        self.host_client.post(
            f"/room/{self.room.code}/type/",
            {"prompt_type": "truth"},
        )

        self.room.refresh_from_db()
        self.assertEqual(self.room.phase, "CHOOSING_PROMPT")

    def test_truth_or_dare_is_blocked_when_no_active_prompts_exist(self):
        self.configure_game()

        self.host_client.post(
            f"/room/{self.room.code}/type/",
            {"prompt_type": "truth"},
        )

        self.room.refresh_from_db()
        self.assertEqual(self.room.phase, "CHOOSING_TYPE")
        self.assertEqual(self.room.prompt_options, [])


class ManagementDashboardTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.admin_user = get_user_model().objects.create_user(
            username="game-admin",
            password="test-password",
            is_staff=True,
            is_superuser=True,
        )
        self.room = GameRoom.objects.create(
            game_type="friends",
            difficulty="easy",
            status="ACTIVE",
        )
        self.host = Player.objects.create(
            room=self.room,
            nickname="Host",
            is_host=True,
        )
        self.guest = Player.objects.create(
            room=self.room,
            nickname="Guest",
        )
        self.room.host = self.host
        self.room.save(update_fields=["host"])
        self.client = Client()

    def test_dashboard_requires_django_admin_login(self):
        response = self.client.get("/management/")

        self.assertRedirects(
            response,
            "/admin/login/?next=/management/",
            fetch_redirect_response=False,
        )

    def test_non_staff_users_cannot_view_dashboard(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(
            username="regular-user",
            password="test-password",
        )
        self.client.force_login(user)

        response = self.client.get("/management/")

        self.assertRedirects(
            response,
            "/admin/login/?next=/management/",
            fetch_redirect_response=False,
        )

    def test_staff_user_sees_game_record_dashboard(self):
        self.client.force_login(self.admin_user)

        response = self.client.get("/management/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Game records")
        self.assertContains(response, self.room.code)
        self.assertContains(response, "Host")
        self.assertContains(response, "Guest")
        self.assertContains(response, "Manage prompts")
        self.assertContains(response, "ACTIVE ROOMS")

    def test_staff_user_can_filter_rooms_by_player_difficulty_category_and_status(self):
        other_room = GameRoom.objects.create(
            game_type="couples",
            difficulty="hard",
            status="FINISHED",
        )
        Player.objects.create(room=other_room, nickname="Another")
        self.client.force_login(self.admin_user)

        response = self.client.get(
            "/management/",
            {
                "player": "gUeSt",
                "difficulty": "easy",
                "game_type": "friends",
                "status": "ACTIVE",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.room.code)
        self.assertNotContains(response, other_room.code)
        self.assertContains(response, "1 matching rooms")
        self.assertContains(response, 'value="gUeSt"')
        self.assertContains(response, '<option value="easy" selected>')
        self.assertContains(response, '<option value="friends" selected>')
        self.assertContains(response, '<option value="ACTIVE" selected>')

    def test_invalid_filter_choices_are_ignored(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(
            "/management/",
            {
                "difficulty": "unknown",
                "game_type": "unknown",
                "status": "unknown",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.room.code)

    def test_staff_user_can_view_room_round_history(self):
        GameRound.objects.create(
            room=self.room,
            round_number=1,
            prompt_text="A saved web prompt",
            prompt_type="truth",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_text="A saved web answer",
            answer_type="text",
        )
        self.client.force_login(self.admin_user)

        response = self.client.get(f"/management/rooms/{self.room.code}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A saved web prompt")
        self.assertContains(response, "A saved web answer")
        self.assertContains(response, "Guest selected")
        self.assertContains(response, "Host challenged")

    def test_staff_user_can_view_room_conversation_in_message_order(self):
        RoomMessage.objects.create(
            room=self.room,
            player=self.host,
            text="First private room message",
        )
        RoomMessage.objects.create(
            room=self.room,
            player=self.guest,
            text="Second private room message",
        )
        self.client.force_login(self.admin_user)

        response = self.client.get(f"/management/rooms/{self.room.code}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Player conversation")
        self.assertContains(response, "2 messages")
        first_index = response.content.index(b"First private room message")
        second_index = response.content.index(b"Second private room message")
        self.assertLess(first_index, second_index)
        self.assertContains(response, "Host")
        self.assertContains(response, "Guest")

    def test_staff_room_detail_shows_empty_conversation_message(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(f"/management/rooms/{self.room.code}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "No chat messages have been recorded for this room.",
        )
