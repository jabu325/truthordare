(() => {
    const tools = document.querySelector("[data-invite-tools]");
    if (!tools) {
        return;
    }

    const inviteUrl = tools.dataset.inviteUrl;
    const roomCode = tools.dataset.roomCode;
    const feedback = tools.querySelector("[data-invite-feedback]");

    const setBusy = (busy) => {
        window.truthDareInviteBusy = busy;
        if (!busy) {
            document.dispatchEvent(new Event("inviteactioncomplete"));
        }
    };

    const copyText = async (value) => {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(value);
            return;
        }

        const field = document.createElement("textarea");
        field.value = value;
        field.setAttribute("readonly", "");
        field.style.position = "fixed";
        field.style.opacity = "0";
        document.body.appendChild(field);
        field.select();
        const copied = document.execCommand("copy");
        field.remove();
        if (!copied) {
            throw new Error("Clipboard access is unavailable.");
        }
    };

    const showFeedback = (message, isError = false) => {
        feedback.textContent = message;
        feedback.classList.toggle("invite-feedback-error", isError);
    };

    tools.querySelector("[data-copy-invite]")?.addEventListener("click", async () => {
        setBusy(true);
        try {
            await copyText(inviteUrl);
            showFeedback("Invite link copied. Send it to your friend!");
        } catch (error) {
            console.error("Could not copy the invite link.", error);
            showFeedback("Could not copy the link. You can still share the room code.", true);
        } finally {
            setBusy(false);
        }
    });

    tools.querySelector("[data-copy-code]")?.addEventListener("click", async () => {
        setBusy(true);
        try {
            await copyText(roomCode);
            showFeedback("Room code copied as a backup.");
        } catch (error) {
            console.error("Could not copy the room code.", error);
            showFeedback("Could not copy the code.", true);
        } finally {
            setBusy(false);
        }
    });

    tools.querySelector("[data-share-invite]")?.addEventListener("click", async () => {
        setBusy(true);
        if (!navigator.share) {
            try {
                await copyText(inviteUrl);
                showFeedback("Sharing isn’t available here, so the invite link was copied.");
            } catch (error) {
                console.error("Could not share or copy the invite link.", error);
                showFeedback("Could not share the invite link.", true);
            } finally {
                setBusy(false);
            }
            return;
        }

        try {
            await navigator.share({
                title: "Join my Truth or Dare game",
                text: "Pick a nickname and join my game!",
                url: inviteUrl,
            });
            showFeedback("Invite ready to share.");
        } catch (error) {
            if (error.name !== "AbortError") {
                console.error("Could not share the invite link.", error);
                showFeedback("Could not share the invite link.", true);
            }
        } finally {
            setBusy(false);
        }
    });
})();
