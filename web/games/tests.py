from datetime import timedelta
import json
from pathlib import Path

from django.test import Client, TestCase
from django.utils import timezone

from .models import GameRoom, GameRound, Player, Prompt


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

    def test_heartbeat_requires_a_room_player_session(self):
        response = Client().post(f"/room/{self.room.code}/heartbeat/")

        self.assertEqual(response.status_code, 403)

    def test_service_worker_allows_root_scope_and_avoids_private_page_caching(self):
        response = Client().get("/service-worker.js")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Service-Worker-Allowed"], "/")
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
