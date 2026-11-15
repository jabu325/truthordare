(() => {
    const room = window.truthDareRoom;
    if (!room?.heartbeatUrl) {
        return;
    }

    const csrfToken = document.querySelector(
        '#csrf-token-form [name="csrfmiddlewaretoken"]',
    )?.value;
    const stateElement = document.getElementById("room-state");
    const initialState = stateElement ? JSON.parse(stateElement.textContent) : null;

    if (!csrfToken) {
        console.error("Presence updates are unavailable because the CSRF token is missing.");
        return;
    }

    let currentState = JSON.stringify(initialState);
    let pendingReload = false;
    let requestInProgress = false;
    let formSubmitting = false;

    const reloadWhenSafe = () => {
        if (
            !pendingReload
            || requestInProgress
            || formSubmitting
            || window.truthDareInviteBusy
        ) {
            return;
        }

        if (document.activeElement?.matches("input, textarea, select")) {
            return;
        }

        window.location.reload();
    };

    const updatePresence = async () => {
        if (document.visibilityState !== "visible" || requestInProgress) {
            return;
        }

        requestInProgress = true;
        try {
            const body = new URLSearchParams({
                last_message_id: String(
                    window.truthDareConversation?.getLastMessageId()
                    ?? room.lastMessageId
                    ?? 0,
                ),
            });
            const response = await fetch(room.heartbeatUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Accept": "application/json",
                    "X-CSRFToken": csrfToken,
                },
                body,
            });
            if (!response.ok) {
                throw new Error(`Presence request failed with status ${response.status}.`);
            }

            const {
                players,
                room_state: roomState,
                chat_messages: chatMessages,
                reactions,
                unread_count: unreadCount,
                last_message_id: lastMessageId,
            } = await response.json();
            players.forEach(({ id, active }) => {
                const chip = document.querySelector(`[data-player-id="${id}"]`);
                if (!chip) {
                    return;
                }
                const dot = chip.querySelector(".presence-dot");
                const label = chip.querySelector("[data-presence-label]");
                dot.classList.toggle("presence-online", active);
                dot.classList.toggle("presence-offline", !active);
                label.textContent = active ? "Active" : "Inactive";
            });
            window.truthDareConversation?.updateSnapshot({
                chat_messages: chatMessages,
                reactions,
                unread_count: unreadCount,
                last_message_id: lastMessageId,
            });
            const nextState = JSON.stringify(roomState);
            if (nextState !== currentState) {
                const previousLatestRound = JSON.parse(currentState).latest_round;
                const nextLatestRound = roomState.latest_round;
                const answerJustArrived = (
                    nextLatestRound
                    && nextLatestRound.answer_type !== "pending"
                    && Number(nextLatestRound.challenged_player_id)
                        !== Number(room.playerId)
                    && (
                        previousLatestRound?.id !== nextLatestRound.id
                        || previousLatestRound.answer_type === "pending"
                    )
                );
                if (answerJustArrived) {
                    window.truthDareTurnAlert?.play();
                }
                currentState = nextState;
                pendingReload = true;
                window.setTimeout(reloadWhenSafe, 1000);
            }
        } catch (error) {
            console.error("Could not update player presence.", error);
        } finally {
            requestInProgress = false;
        }

        reloadWhenSafe();
    };

    updatePresence();
    window.setInterval(updatePresence, 5000);
    document.addEventListener("visibilitychange", updatePresence);
    document.addEventListener("focusout", () => {
        window.setTimeout(reloadWhenSafe, 0);
    });
    document.addEventListener("submit", (event) => {
        if (event.defaultPrevented) {
            return;
        }
        formSubmitting = true;
    });
    document.addEventListener("inviteactioncomplete", () => {
        window.setTimeout(reloadWhenSafe, 1500);
    });
})();
