(() => {
    const room = window.truthDareRoom;
    if (!room?.heartbeatUrl) {
        return;
    }

    const csrfToken = document.querySelector(
        '#csrf-token-form [name="csrfmiddlewaretoken"]',
    )?.value;

    if (!csrfToken) {
        console.error("Presence updates are unavailable because the CSRF token is missing.");
        return;
    }

    const updatePresence = async () => {
        if (document.visibilityState !== "visible") {
            return;
        }

        try {
            const response = await fetch(room.heartbeatUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Accept": "application/json",
                    "X-CSRFToken": csrfToken,
                },
            });
            if (!response.ok) {
                throw new Error(`Presence request failed with status ${response.status}.`);
            }

            const { players } = await response.json();
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
        } catch (error) {
            console.error("Could not update player presence.", error);
        }
    };

    updatePresence();
    window.setInterval(updatePresence, 20000);
    document.addEventListener("visibilitychange", updatePresence);
})();
