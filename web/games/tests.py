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
