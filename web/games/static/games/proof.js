(() => {
    document.addEventListener("submit", async (event) => {
        const form = event.target.closest("[data-proof-view]");
        if (!form) {
            return;
        }

        event.preventDefault();
        const button = form.querySelector("button[type='submit']");
        const feedback = form.parentElement.querySelector("[data-proof-feedback]");
        const display = form.parentElement.querySelector("[data-proof-display]");
        const csrfToken = form.querySelector("[name='csrfmiddlewaretoken']")?.value;
        if (!button || !feedback || !display || !csrfToken) {
            console.error("Proof viewing is unavailable because required page controls are missing.");
            return;
        }

        button.disabled = true;
        feedback.textContent = "Loading your one-time view…";
        try {
            const response = await fetch(form.action, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "X-CSRFToken": csrfToken,
                },
                body: new FormData(form),
            });
            if (!response.ok) {
                const errorMessage = await response.text();
                throw new Error(errorMessage || `Proof request failed with status ${response.status}.`);
            }

            const image = document.createElement("img");
            image.src = URL.createObjectURL(await response.blob());
            image.alt = "One-time dare proof image";
            display.replaceChildren(image);
            display.hidden = false;
            button.textContent = "Viewed once";
            feedback.textContent = "This proof can’t be opened again in the game.";
        } catch (error) {
            console.error("Could not display the one-time proof image.", error);
            feedback.textContent = error.message || "Could not load the proof image.";
            button.disabled = false;
        }
    });
})();
