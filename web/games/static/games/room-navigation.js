(() => {
    document.addEventListener("submit", (event) => {
        const form = event.target.closest("[data-confirm-restart]");
        if (!form) {
            return;
        }

        const action = event.submitter?.value;
        const opponentRequested = form.dataset.opponentRequested === "true";
        const message = action === "cancel"
            ? "Cancel your request to restart the game?"
            : opponentRequested
                ? "Your friend also wants to restart. Agreeing will begin a fresh game setup for both of you."
                : "Request a fresh game setup? The game only restarts if your friend also agrees.";

        if (!window.confirm(message)) {
            event.preventDefault();
        }
    }, true);
})();
