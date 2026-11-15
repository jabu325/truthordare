from datetime import timedelta
import json
from pathlib import Path

from django.test import Client, TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.utils import timezone
from unittest.mock import MagicMock, patch

from .models import (
    AnswerReaction,
    AnswerProofView,
    GameRoom,
    GameRound,
    Player,
    Prompt,
    RoomMessage,
)
from .views import get_proof_media_response, is_supported_proof_video


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

    def review_round(self, reviewer, game_round, review="valid"):
        return reviewer.post(
            f"/room/{self.room.code}/round/{game_round.pk}/review/",
            {"review": review},
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
        self.assertEqual(game_round.review_status, "pending")
        self.assertEqual(self.room.phase, "REVIEWING_ANSWER")
        self.assertEqual(self.room.current_turn, self.guest)

        self.review_round(self.guest_client, game_round)
        self.room.refresh_from_db()
        game_round.refresh_from_db()
        self.assertEqual(self.room.phase, "CHOOSING_TYPE")
        self.assertEqual(self.room.current_turn, self.guest)
        self.assertEqual(game_round.review_status, "accepted")

    def test_selected_prompt_is_not_offered_again_during_the_same_game(self):
        self.configure_game()
        self.add_prompts(count=3)

        self.host_client.post(
            f"/room/{self.room.code}/type/",
            {"prompt_type": "truth"},
        )
        self.room.refresh_from_db()
        chosen_prompt_id = self.room.prompt_options[0]
        self.guest_client.post(
            f"/room/{self.room.code}/prompt/",
            {"prompt_id": chosen_prompt_id},
        )
        self.host_client.post(
            f"/room/{self.room.code}/answer/",
            {"answer": "A test answer"},
        )
        self.review_round(
            self.guest_client,
            GameRound.objects.get(room=self.room, round_number=1),
        )

        # The host has not selected this prompt, so it remains available to them.
        self.guest_client.post(
            f"/room/{self.room.code}/type/",
            {"prompt_type": "truth"},
        )
        self.room.refresh_from_db()
        self.assertIn(chosen_prompt_id, self.room.prompt_options)
        self.host_client.post(
            f"/room/{self.room.code}/prompt/",
            {"prompt_id": chosen_prompt_id},
        )
        self.guest_client.post(
            f"/room/{self.room.code}/answer/",
            {"answer": "Another test answer"},
        )
        self.review_round(
            self.host_client,
            GameRound.objects.get(room=self.room, round_number=2),
        )

        # After the guest has selected it too, it is no longer offered to them.
        self.host_client.post(
            f"/room/{self.room.code}/type/",
            {"prompt_type": "truth"},
        )
        self.room.refresh_from_db()
        self.assertEqual(len(self.room.prompt_options), 2)
        self.assertNotIn(chosen_prompt_id, self.room.prompt_options)

        guest_chosen_prompt_id = self.room.prompt_options[0]
        self.guest_client.post(
            f"/room/{self.room.code}/prompt/",
            {"prompt_id": guest_chosen_prompt_id},
        )
        self.host_client.post(
            f"/room/{self.room.code}/answer/",
            {"answer": "A third test answer"},
        )
        self.review_round(
            self.guest_client,
            GameRound.objects.get(room=self.room, round_number=3),
        )
        self.guest_client.post(
            f"/room/{self.room.code}/type/",
            {"prompt_type": "truth"},
        )
        self.room.refresh_from_db()
        self.assertEqual(len(self.room.prompt_options), 2)
        self.assertNotIn(chosen_prompt_id, self.room.prompt_options)

    def test_only_the_other_player_can_review_and_rejection_costs_answerer_a_heart(self):
        self.room.phase = "REVIEWING_ANSWER"
        self.room.current_turn = self.guest
        self.room.save(update_fields=["phase", "current_turn"])
        self.host.hearts = 1
        self.host.save(update_fields=["hearts"])
        game_round = GameRound.objects.create(
            room=self.room,
            game_number=self.room.game_number,
            round_number=1,
            prompt_text="A prompt",
            prompt_type="truth",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_text="An answer",
            answer_type="text",
        )

        wrong_reviewer = self.host_client.post(
            f"/room/{self.room.code}/round/{game_round.pk}/review/",
            {"review": "unsatisfying"},
            follow=True,
        )
        self.assertEqual(wrong_reviewer.status_code, 200)
        self.host.refresh_from_db()
        self.assertEqual(self.host.hearts, 1)
        self.room.refresh_from_db()
        self.assertEqual(self.room.phase, "REVIEWING_ANSWER")

        response = self.review_round(
            self.guest_client,
            game_round,
            review="unsatisfying",
        )
        self.assertRedirects(
            response,
            f"/room/{self.room.code}/",
            fetch_redirect_response=False,
        )
        self.room.refresh_from_db()
        self.host.refresh_from_db()
        game_round.refresh_from_db()
        self.assertEqual(self.host.hearts, 0)
        self.assertEqual(self.room.phase, "FINISHED")
        self.assertEqual(self.room.status, "FINISHED")
        self.assertEqual(self.room.winner, self.guest)
        self.assertEqual(game_round.review_status, "rejected")

    def test_new_game_resets_hearts_after_a_winner_is_declared(self):
        self.room.phase = "FINISHED"
        self.room.status = "FINISHED"
        self.room.winner = self.guest
        self.room.save(update_fields=["phase", "status", "winner"])
        self.host.hearts = 0
        self.host.save(update_fields=["hearts"])
        self.guest.hearts = 3
        self.guest.save(update_fields=["hearts"])

        self.host_client.post(f"/room/{self.room.code}/new-game/")

        self.room.refresh_from_db()
        self.host.refresh_from_db()
        self.guest.refresh_from_db()
        self.assertEqual(self.room.phase, "FINISHED")
        self.assertEqual(self.host.hearts, 0)
        self.assertTrue(self.host.restart_requested)

        self.guest_client.post(f"/room/{self.room.code}/new-game/")

        self.room.refresh_from_db()
        self.host.refresh_from_db()
        self.guest.refresh_from_db()
        self.assertEqual(self.room.phase, "CHOOSING_CATEGORY")
        self.assertEqual(self.room.status, "ACTIVE")
        self.assertIsNone(self.room.winner)
        self.assertEqual(self.host.hearts, 5)
        self.assertEqual(self.guest.hearts, 5)
        self.assertFalse(self.host.restart_requested)
        self.assertFalse(self.guest.restart_requested)

    def test_unsatisfying_answer_costs_one_heart_and_advances_the_game(self):
        self.room.phase = "REVIEWING_ANSWER"
        self.room.current_turn = self.guest
        self.room.save(update_fields=["phase", "current_turn"])
        game_round = GameRound.objects.create(
            room=self.room,
            game_number=self.room.game_number,
            round_number=1,
            prompt_text="A prompt",
            prompt_type="truth",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_text="An answer",
            answer_type="text",
        )

        self.review_round(
            self.guest_client,
            game_round,
            review="unsatisfying",
        )

        self.room.refresh_from_db()
        self.host.refresh_from_db()
        self.assertEqual(self.host.hearts, 4)
        self.assertEqual(self.room.phase, "CHOOSING_TYPE")
        self.assertEqual(self.room.current_turn, self.guest)

    @patch("games.views.cloudinary_is_configured", return_value=True)
    @patch(
        "games.views.cloudinary.uploader.upload",
        return_value={
            "public_id": "truth-dare/proofs/test-proof",
            "version": 123,
            "format": "png",
        },
    )
    def test_dare_proof_upload_uses_authenticated_cloudinary_storage(
        self,
        upload,
        _configured,
    ):
        self.room.phase = "AWAITING_ANSWER"
        self.room.current_turn = self.host
        self.room.game_type = "friends"
        self.room.difficulty = "easy"
        self.room.save(
            update_fields=[
                "phase",
                "current_turn",
                "game_type",
                "difficulty",
            ]
        )
        dare = GameRound.objects.create(
            room=self.room,
            game_number=self.room.game_number,
            round_number=1,
            prompt_text="Do a dare",
            prompt_type="dare",
            selected_by=self.guest,
            challenged_player=self.host,
        )
        proof = SimpleUploadedFile(
            "proof.png",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 24,
            content_type="image/png",
        )

        response = self.host_client.post(
            f"/room/{self.room.code}/answer/",
            {"answer": "Done!", "proof_image": proof},
        )

        self.assertRedirects(
            response,
            f"/room/{self.room.code}/",
            fetch_redirect_response=False,
        )
        dare.refresh_from_db()
        self.room.refresh_from_db()
        self.assertEqual(dare.proof_public_id, "truth-dare/proofs/test-proof")
        self.assertEqual(dare.proof_version, 123)
        self.assertEqual(dare.proof_format, "png")
        self.assertEqual(dare.answer_type, "media")
        self.assertEqual(self.room.phase, "REVIEWING_ANSWER")
        self.assertEqual(upload.call_args.kwargs["type"], "authenticated")
        self.assertEqual(upload.call_args.kwargs["resource_type"], "image")

    @patch("games.views.cloudinary_is_configured", return_value=True)
    @patch(
        "games.views.cloudinary.uploader.upload",
        return_value={
            "public_id": "truth-dare/proofs/image-only",
            "version": 124,
            "format": "png",
        },
    )
    def test_dare_proof_image_can_be_submitted_without_text(
        self,
        upload,
        _configured,
    ):
        self.room.phase = "AWAITING_ANSWER"
        self.room.current_turn = self.host
        self.room.game_type = "friends"
        self.room.difficulty = "easy"
        self.room.save(
            update_fields=[
                "phase",
                "current_turn",
                "game_type",
                "difficulty",
            ]
        )
        dare = GameRound.objects.create(
            room=self.room,
            game_number=self.room.game_number,
            round_number=1,
            prompt_text="Do a dare",
            prompt_type="dare",
            selected_by=self.guest,
            challenged_player=self.host,
        )
        proof = SimpleUploadedFile(
            "proof.png",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 24,
            content_type="image/png",
        )

        response = self.host_client.post(
            f"/room/{self.room.code}/answer/",
            {"answer": "", "proof_image": proof},
        )

        self.assertRedirects(
            response,
            f"/room/{self.room.code}/",
            fetch_redirect_response=False,
        )
        dare.refresh_from_db()
        self.assertEqual(dare.answer_text, "")
        self.assertEqual(dare.answer_type, "media")
        self.assertEqual(dare.proof_public_id, "truth-dare/proofs/image-only")
        self.assertEqual(upload.call_count, 1)

    @patch("games.views.cloudinary_is_configured", return_value=True)
    @patch(
        "games.views.cloudinary.uploader.upload",
        return_value={
            "public_id": "truth-dare/proofs/test-video",
            "version": 125,
            "format": "mp4",
        },
    )
    def test_dare_video_proof_upload_uses_authenticated_video_storage(
        self,
        upload,
        _configured,
    ):
        self.room.phase = "AWAITING_ANSWER"
        self.room.current_turn = self.host
        self.room.save(update_fields=["phase", "current_turn"])
        dare = GameRound.objects.create(
            room=self.room,
            game_number=self.room.game_number,
            round_number=1,
            prompt_text="Do a dare",
            prompt_type="dare",
            selected_by=self.guest,
            challenged_player=self.host,
        )
        video = SimpleUploadedFile(
            "proof.mp4",
            b"\x00\x00\x00\x18ftypisom" + b"\x00" * 24,
            content_type="video/mp4",
        )

        response = self.host_client.post(
            f"/room/{self.room.code}/answer/",
            {"answer": "Done!", "proof_media": video},
        )

        self.assertRedirects(
            response,
            f"/room/{self.room.code}/",
            fetch_redirect_response=False,
        )
        dare.refresh_from_db()
        self.assertEqual(dare.answer_type, "media")
        self.assertEqual(dare.proof_resource_type, "video")
        self.assertEqual(dare.proof_public_id, "truth-dare/proofs/test-video")
        self.assertEqual(upload.call_args.kwargs["resource_type"], "video")
        self.assertEqual(upload.call_args.kwargs["type"], "authenticated")

    def test_dare_video_proof_rejects_unsupported_media_type(self):
        self.room.phase = "AWAITING_ANSWER"
        self.room.current_turn = self.host
        self.room.save(update_fields=["phase", "current_turn"])
        dare = GameRound.objects.create(
            room=self.room,
            game_number=self.room.game_number,
            round_number=1,
            prompt_text="Do a dare",
            prompt_type="dare",
            selected_by=self.guest,
            challenged_player=self.host,
        )
        invalid_video = SimpleUploadedFile(
            "proof.mp4",
            b"not a video",
            content_type="video/mp4",
        )

        response = self.host_client.post(
            f"/room/{self.room.code}/answer/",
            {"answer": "Done!", "proof_media": invalid_video},
            follow=True,
        )

        dare.refresh_from_db()
        self.assertEqual(dare.answer_type, "pending")
        self.assertContains(response, "MP4 or WebM video up to 25 MB")

    def test_video_proof_upload_enforces_the_25_mb_limit(self):
        video = SimpleUploadedFile(
            "proof.mp4",
            b"\x00\x00\x00\x18ftypisom",
            content_type="video/mp4",
        )

        with patch("games.views.MAX_PROOF_VIDEO_SIZE", 8):
            self.assertFalse(is_supported_proof_video(video))

    def test_webm_video_signature_is_supported(self):
        video = SimpleUploadedFile(
            "proof.webm",
            b"\x1a\x45\xdf\xa3\x9f\x42\x82\x84webm",
            content_type="video/webm",
        )

        self.assertTrue(is_supported_proof_video(video))

    def test_video_proof_delivery_uses_signed_cloudinary_video_url(self):
        game_round = GameRound.objects.create(
            room=self.room,
            game_number=self.room.game_number,
            round_number=1,
            prompt_text="A dare",
            prompt_type="dare",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_text="Done",
            answer_type="media",
            proof_public_id="truth-dare/proofs/test-video",
            proof_version=125,
            proof_format="mp4",
            proof_resource_type="video",
        )
        cloudinary_response = MagicMock()
        cloudinary_response.__enter__.return_value = cloudinary_response
        cloudinary_response.headers.get_content_type.return_value = "video/mp4"
        cloudinary_response.read.return_value = b"video-data"

        with patch(
            "games.views.cloudinary_url",
            return_value=("https://signed.example/video", {}),
        ) as signed_url:
            with patch(
                "games.views.urlopen",
                return_value=cloudinary_response,
            ):
                response = get_proof_media_response(game_round)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertEqual(response.content, b"video-data")
        self.assertEqual(
            signed_url.call_args.kwargs["resource_type"],
            "video",
        )
        cloudinary_response.headers.get_content_type.assert_called_once_with()
        cloudinary_response.read.assert_called_once_with(25 * 1024 * 1024 + 1)

    def test_answer_requires_text_or_proof(self):
        self.room.phase = "AWAITING_ANSWER"
        self.room.current_turn = self.host
        self.room.save(update_fields=["phase", "current_turn"])
        dare = GameRound.objects.create(
            room=self.room,
            game_number=self.room.game_number,
            round_number=1,
            prompt_text="Do a dare",
            prompt_type="dare",
            selected_by=self.guest,
            challenged_player=self.host,
        )

        response = self.host_client.post(
            f"/room/{self.room.code}/answer/",
            {"answer": "  "},
            follow=True,
        )

        dare.refresh_from_db()
        self.assertEqual(dare.answer_type, "pending")
        self.assertContains(
            response,
            "Enter a text answer or upload proof media.",
        )

    @patch("games.views.cloudinary_is_configured", return_value=True)
    def test_dare_answer_form_allows_proof_without_text(self, _configured):
        self.room.phase = "AWAITING_ANSWER"
        self.room.current_turn = self.host
        self.room.save(update_fields=["phase", "current_turn"])
        GameRound.objects.create(
            room=self.room,
            game_number=self.room.game_number,
            round_number=1,
            prompt_text="Do a dare",
            prompt_type="dare",
            selected_by=self.guest,
            challenged_player=self.host,
        )

        response = self.host_client.get(f"/room/{self.room.code}/")

        self.assertContains(
            response,
            "Type your answer, or attach proof media...",
        )
        self.assertNotRegex(
            response.content.decode(),
            r'<textarea[^>]*name="answer"[^>]*required',
        )

    def test_proof_is_one_time_for_each_player_and_not_available_to_nonplayers(self):
        game_round = GameRound.objects.create(
            room=self.room,
            game_number=self.room.game_number,
            round_number=1,
            prompt_text="A dare",
            prompt_type="dare",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_text="Done",
            answer_type="media",
            proof_public_id="truth-dare/proofs/test-proof",
            proof_version=123,
            proof_format="png",
        )
        url = f"/room/{self.room.code}/round/{game_round.pk}/proof/"
        image_response = HttpResponse(b"image-bytes", content_type="image/png")
        with patch("games.views.cloudinary_is_configured", return_value=True):
            with patch(
                "games.views.get_proof_media_response",
                return_value=image_response,
            ) as get_image:
                first_view = self.host_client.post(url)
                self.assertEqual(first_view.status_code, 200)
                self.assertEqual(first_view.content, b"image-bytes")
                self.assertEqual(first_view["Cache-Control"], "private, no-store")

                repeated_view = self.host_client.post(url)
                self.assertEqual(repeated_view.status_code, 410)

                other_player_view = self.guest_client.post(url)
                self.assertEqual(other_player_view.status_code, 200)

                forbidden_view = Client().post(url)
                self.assertEqual(forbidden_view.status_code, 403)

        self.assertEqual(get_image.call_count, 2)
        self.assertEqual(AnswerProofView.objects.filter(game_round=game_round).count(), 2)

    def test_video_proof_is_only_delivered_once_to_each_player(self):
        game_round = GameRound.objects.create(
            room=self.room,
            game_number=self.room.game_number,
            round_number=1,
            prompt_text="A dare",
            prompt_type="dare",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_text="Done",
            answer_type="media",
            proof_public_id="truth-dare/proofs/test-video",
            proof_version=125,
            proof_format="mp4",
            proof_resource_type="video",
        )
        url = f"/room/{self.room.code}/round/{game_round.pk}/proof/"
        video_response = HttpResponse(b"video-data", content_type="video/mp4")
        with patch("games.views.cloudinary_is_configured", return_value=True):
            with patch(
                "games.views.get_proof_media_response",
                return_value=video_response,
            ) as get_media:
                first_view = self.host_client.post(url)
                repeated_view = self.host_client.post(url)
                other_player_view = self.guest_client.post(url)

        self.assertEqual(first_view.status_code, 200)
        self.assertEqual(first_view["Content-Type"], "video/mp4")
        self.assertEqual(repeated_view.status_code, 410)
        self.assertEqual(other_player_view.status_code, 200)
        self.assertEqual(get_media.call_count, 2)
        self.assertEqual(AnswerProofView.objects.filter(game_round=game_round).count(), 2)

    def test_failed_video_delivery_does_not_consume_a_player_view(self):
        game_round = GameRound.objects.create(
            room=self.room,
            game_number=self.room.game_number,
            round_number=1,
            prompt_text="A dare",
            prompt_type="dare",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_type="media",
            proof_public_id="truth-dare/proofs/test-video",
            proof_version=125,
            proof_format="mp4",
            proof_resource_type="video",
        )
        response = HttpResponse("Proof media could not be loaded.", status=502)

        with patch("games.views.cloudinary_is_configured", return_value=True):
            with patch(
                "games.views.get_proof_media_response",
                return_value=response,
            ):
                failed_view = self.host_client.post(
                    f"/room/{self.room.code}/round/{game_round.pk}/proof/",
                )

        self.assertEqual(failed_view.status_code, 502)
        self.assertFalse(
            AnswerProofView.objects.filter(
                game_round=game_round,
                player=self.host,
            ).exists(),
        )

    def test_video_proof_controls_are_rendered_for_room_players(self):
        self.room.phase = "REVIEWING_ANSWER"
        self.room.current_turn = self.guest
        self.room.save(update_fields=["phase", "current_turn"])
        game_round = GameRound.objects.create(
            room=self.room,
            game_number=self.room.game_number,
            round_number=1,
            prompt_text="A dare",
            prompt_type="dare",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_type="media",
            proof_public_id="truth-dare/proofs/test-video",
            proof_version=125,
            proof_format="mp4",
            proof_resource_type="video",
        )

        response = self.guest_client.get(f"/room/{self.room.code}/")

        self.assertContains(response, 'data-resource-type="video"')
        self.assertContains(response, "Play video once")
        self.assertContains(
            response,
            f"/room/{self.room.code}/round/{game_round.pk}/proof/",
        )

    def test_exhausted_game_restarts_only_after_both_players_confirm(self):
        self.room.game_type = "friends"
        self.room.difficulty = "easy"
        self.room.phase = "CHOOSING_TYPE"
        self.room.current_turn = self.host
        self.room.save(
            update_fields=[
                "game_type",
                "difficulty",
                "phase",
                "current_turn",
            ]
        )
        prompts = [
            *self.add_prompts(prompt_type="truth", count=1),
            *self.add_prompts(prompt_type="dare", count=1),
        ]
        for round_number, prompt in enumerate(prompts, start=1):
            GameRound.objects.create(
                room=self.room,
                game_number=1,
                round_number=round_number,
                prompt=prompt,
                prompt_text=prompt.text,
                prompt_type=prompt.type,
                selected_by=self.guest,
                challenged_player=self.host,
                answer_text="An answer",
                answer_type="text",
            )

        exhausted_page = self.host_client.get(f"/room/{self.room.code}/")
        self.assertContains(
            exhausted_page,
            "Guest has chosen every prompt for this setup.",
        )
        self.assertContains(exhausted_page, "Restart game")
        self.assertContains(exhausted_page, 'value="truth" disabled')
        self.assertContains(exhausted_page, 'value="dare" disabled')

        first_confirmation = self.guest_client.post(
            f"/room/{self.room.code}/new-game/",
            {"restart_action": "request"},
        )
        self.assertRedirects(
            first_confirmation,
            f"/room/{self.room.code}/",
            fetch_redirect_response=False,
        )
        self.room.refresh_from_db()
        self.guest.refresh_from_db()
        self.assertEqual(self.room.game_number, 1)
        self.assertTrue(self.guest.restart_requested)

        response = self.host_client.post(
            f"/room/{self.room.code}/new-game/",
            {"restart_action": "request"},
        )
        self.assertRedirects(
            response,
            f"/room/{self.room.code}/",
            fetch_redirect_response=False,
        )
        self.room.refresh_from_db()
        self.assertEqual(self.room.game_number, 2)
        self.assertEqual(self.room.phase, "CHOOSING_CATEGORY")
        self.assertEqual(self.room.game_type, "")
        self.assertEqual(self.room.difficulty, "")
        self.assertEqual(self.room.current_turn, self.host)
        self.assertEqual(self.room.rounds.count(), 2)

        self.configure_game()
        self.host_client.post(
            f"/room/{self.room.code}/type/",
            {"prompt_type": "truth"},
        )
        self.room.refresh_from_db()
        self.assertEqual(len(self.room.prompt_options), 1)
        self.assertEqual(self.room.prompt_options[0], prompts[0].pk)

    def test_restart_can_be_requested_during_a_round_and_cancelled(self):
        self.room.phase = "AWAITING_ANSWER"
        self.room.save(update_fields=["phase"])

        requested = self.host_client.post(
            f"/room/{self.room.code}/new-game/",
            {"restart_action": "request"},
        )

        self.assertRedirects(
            requested,
            f"/room/{self.room.code}/",
            fetch_redirect_response=False,
        )
        self.room.refresh_from_db()
        self.host.refresh_from_db()
        self.assertEqual(self.room.phase, "AWAITING_ANSWER")
        self.assertEqual(self.room.game_number, 1)
        self.assertTrue(self.host.restart_requested)

        heartbeat = self.guest_client.post(
            f"/room/{self.room.code}/heartbeat/",
        )
        self.assertEqual(
            heartbeat.json()["room_state"]["restart_requested_player_ids"],
            [self.host.pk],
        )

        self.guest_client.post(
            f"/room/{self.room.code}/new-game/",
            {"restart_action": "cancel"},
        )
        self.host.refresh_from_db()
        self.guest.refresh_from_db()
        self.assertTrue(self.host.restart_requested)
        self.assertFalse(self.guest.restart_requested)

        cancelled = self.host_client.post(
            f"/room/{self.room.code}/new-game/",
            {"restart_action": "cancel"},
        )
        self.assertRedirects(
            cancelled,
            f"/room/{self.room.code}/",
            fetch_redirect_response=False,
        )
        self.host.refresh_from_db()
        self.assertFalse(self.host.restart_requested)

    def test_restart_requires_two_room_players(self):
        second_room = GameRoom.objects.create()
        solo_player = Player.objects.create(
            room=second_room,
            nickname="Solo",
            is_host=True,
            session_key="solo-session-token",
        )
        solo_room_client = self.make_client_for_room(second_room, solo_player)

        response = solo_room_client.post(
            f"/room/{second_room.code}/new-game/",
            {"restart_action": "request"},
            follow=True,
        )

        second_room.refresh_from_db()
        solo_player.refresh_from_db()
        self.assertEqual(second_room.game_number, 1)
        self.assertFalse(solo_player.restart_requested)
        self.assertContains(
            response,
            "Both players must join before requesting a restart.",
        )

    def make_client_for_room(self, room, player):
        client = Client()
        session = client.session
        session["room_player_tokens"] = {
            str(room.pk): player.session_key,
        }
        session.save()
        return client

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
        self.assertContains(response, 'aria-label="Room navigation"')
        self.assertContains(response, 'aria-label="Home"')
        self.assertContains(response, 'aria-label="Request game restart"')
        self.assertContains(response, '<nav class="room-heading-actions"')
        self.assertContains(response, "room-navigation.js")

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

    def test_heartbeat_returns_new_prompt_options_for_the_other_player(self):
        self.configure_game()
        self.add_prompts(prompt_type="dare")
        self.room.current_turn = self.host
        self.room.phase = "CHOOSING_TYPE"
        self.room.save(update_fields=["current_turn", "phase"])

        self.host_client.post(
            f"/room/{self.room.code}/type/",
            {"prompt_type": "dare"},
        )

        heartbeat = self.guest_client.post(
            f"/room/{self.room.code}/heartbeat/",
        )

        state = heartbeat.json()["room_state"]
        self.assertEqual(state["phase"], "CHOOSING_PROMPT")
        self.assertTrue(state["prompt_options"])

    def test_heartbeat_requires_a_room_player_session(self):
        response = Client().post(f"/room/{self.room.code}/heartbeat/")

        self.assertEqual(response.status_code, 403)

    def test_service_worker_allows_root_scope_and_avoids_private_page_caching(self):
        response = Client().get("/service-worker.js")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertContains(response, 'const CACHE_NAME = "truth-dare-arcade-v16"')
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

    def test_staff_room_history_displays_proof_images_without_player_view_limits(self):
        game_round = GameRound.objects.create(
            room=self.room,
            round_number=1,
            prompt_text="A dare",
            prompt_type="dare",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_text="Done",
            answer_type="media",
            proof_public_id="truth-dare/proofs/test-proof",
            proof_version=123,
            proof_format="png",
        )
        self.client.force_login(self.admin_user)

        with patch("games.views.cloudinary_is_configured", return_value=True):
            with patch(
                "games.views.get_proof_media_response",
                return_value=HttpResponse(
                    b"proof-image",
                    content_type="image/png",
                ),
            ) as get_image:
                response = self.client.get(
                    f"/management/rooms/{self.room.code}/",
                )
                proof_url = (
                    f"/management/rooms/{self.room.code}/"
                    f"round/{game_round.pk}/proof/"
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, proof_url)

                first_view = self.client.get(proof_url)
                second_view = self.client.get(proof_url)

        self.assertEqual(first_view.status_code, 200)
        self.assertEqual(second_view.status_code, 200)
        self.assertEqual(first_view.content, b"proof-image")
        self.assertEqual(second_view.content, b"proof-image")
        self.assertEqual(get_image.call_count, 2)
        self.assertFalse(
            AnswerProofView.objects.filter(game_round=game_round).exists(),
        )

    def test_management_proof_image_requires_staff_login(self):
        game_round = GameRound.objects.create(
            room=self.room,
            round_number=1,
            prompt_text="A dare",
            prompt_type="dare",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_text="Done",
            answer_type="media",
            proof_public_id="truth-dare/proofs/test-proof",
            proof_version=123,
            proof_format="png",
        )

        response = self.client.get(
            f"/management/rooms/{self.room.code}/round/{game_round.pk}/proof/",
        )

        self.assertRedirects(
            response,
            f"/admin/login/?next=/management/rooms/{self.room.code}/"
            f"round/{game_round.pk}/proof/",
            fetch_redirect_response=False,
        )

    def test_staff_room_history_renders_video_proof_with_replay_controls(self):
        game_round = GameRound.objects.create(
            room=self.room,
            round_number=1,
            prompt_text="A dare",
            prompt_type="dare",
            selected_by=self.guest,
            challenged_player=self.host,
            answer_type="media",
            proof_public_id="truth-dare/proofs/test-video",
            proof_version=125,
            proof_format="mp4",
            proof_resource_type="video",
        )
        self.client.force_login(self.admin_user)

        response = self.client.get(f"/management/rooms/{self.room.code}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<video controls preload=\"metadata\">")
        self.assertContains(
            response,
            f"/management/rooms/{self.room.code}/"
            f"round/{game_round.pk}/proof/",
        )
        self.assertContains(response, 'type="video/mp4"')

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
