from django.urls import path

from . import views

app_name = "games"

urlpatterns = [
    path("", views.home, name="home"),
    path("management/", views.management_dashboard, name="management"),
    path("invite/<str:code>/", views.room_invite, name="room_invite"),
    path(
        "management/rooms/<str:code>/",
        views.management_room_detail,
        name="management_room",
    ),
    path("create/", views.create_room, name="create_room"),
    path("join/", views.join_room, name="join_room"),
    path("service-worker.js", views.service_worker, name="service_worker"),
    path("room/<str:code>/", views.room_detail, name="room"),
    path("room/<str:code>/heartbeat/", views.heartbeat, name="heartbeat"),
    path("room/<str:code>/chat/", views.room_chat, name="room_chat"),
    path(
        "room/<str:code>/chat/read/",
        views.mark_chat_read,
        name="mark_chat_read",
    ),
    path(
        "room/<str:code>/round/<int:round_id>/reaction/",
        views.toggle_answer_reaction,
        name="toggle_answer_reaction",
    ),
    path("room/<str:code>/category/", views.configure_category, name="configure_category"),
    path("room/<str:code>/difficulty/", views.configure_difficulty, name="configure_difficulty"),
    path("room/<str:code>/type/", views.choose_type, name="choose_type"),
    path("room/<str:code>/prompt/", views.choose_prompt, name="choose_prompt"),
    path("room/<str:code>/answer/", views.submit_answer, name="submit_answer"),
]
