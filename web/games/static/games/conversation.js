(() => {
    const drawer = document.querySelector("[data-chat-drawer]");
    const messageList = document.querySelector("[data-chat-list]");
    if (!drawer || !messageList) {
        return;
    }

    const openButton = document.querySelector("[data-chat-open]");
    const closeButtons = document.querySelectorAll("[data-chat-close]");
    const backdrop = document.querySelector(".room-chat-backdrop");
    const chatForm = document.querySelector("[data-chat-form]");
    const chatFeedback = document.querySelector("[data-chat-feedback]");
    const chatBadge = document.querySelector("[data-chat-badge]");
    const chatNotice = document.querySelector("[data-chat-notice]");
    const chatNoticeText = document.querySelector("[data-chat-notice-text]");
    const room = window.truthDareRoom;
    const csrfToken = document.querySelector(
        '#csrf-token-form [name="csrfmiddlewaretoken"]',
    )?.value;
    const initialMessages = document.getElementById("room-messages");
    const initialReactions = document.getElementById("reaction-state");
    let unreadCount = Number(openButton?.dataset.unreadCount || 0);
    let lastMessageId = Number(room?.lastMessageId || 0);
    let noticeTimeout;

    const updateUnreadCount = (count) => {
        unreadCount = Math.max(0, Number(count) || 0);
        openButton.dataset.unreadCount = String(unreadCount);
        openButton.setAttribute(
            "aria-label",
            `Open room chat${unreadCount ? `; ${unreadCount} unread message${unreadCount === 1 ? "" : "s"}` : ""}`,
        );
        chatBadge.textContent = unreadCount > 99 ? "99+" : String(unreadCount);
        chatBadge.hidden = unreadCount === 0;
        if (unreadCount === 0) {
            chatNotice.hidden = true;
        }
    };

    const showMessageNotice = (message) => {
        if (drawer.hidden) {
            chatNoticeText.textContent = message;
            chatNotice.hidden = false;
            window.clearTimeout(noticeTimeout);
            noticeTimeout = window.setTimeout(() => {
                chatNotice.hidden = true;
            }, 5000);
        }
    };

    const setDrawerOpen = (open) => {
        drawer.hidden = !open;
        backdrop.hidden = !open;
        document.body.classList.toggle("room-chat-open", open);
        openButton.setAttribute("aria-expanded", String(open));
        if (open) {
            messageList.scrollTop = messageList.scrollHeight;
            drawer.querySelector("textarea")?.focus();
            markChatRead();
        } else {
            openButton.focus();
        }
    };

    const markChatRead = async () => {
        try {
            const response = await fetch(room.markChatReadUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "X-CSRFToken": csrfToken,
                    "Accept": "application/json",
                },
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || "Could not mark messages as read.");
            }
            data.chat_messages.forEach(appendMessage);
            lastMessageId = Math.max(lastMessageId, data.last_message_id);
            updateUnreadCount(data.unread_count);
        } catch (error) {
            console.error("Could not mark room messages as read.", error);
            chatFeedback.textContent = error.message;
        }
    };

    const appendMessage = (message) => {
        if (messageList.querySelector(`[data-message-id="${message.id}"]`)) {
            return;
        }

        const item = document.createElement("li");
        item.className = `room-chat-message ${message.is_host ? "chat-host" : "chat-guest"}`;
        item.dataset.messageId = message.id;
        lastMessageId = Math.max(lastMessageId, Number(message.id));

        const meta = document.createElement("div");
        meta.className = "room-chat-message-meta";
        const author = document.createElement("strong");
        author.textContent = message.nickname;
        const time = document.createElement("time");
        time.dateTime = message.created_at;
        time.textContent = message.time || new Date(message.created_at).toLocaleTimeString(
            [],
            { hour: "2-digit", minute: "2-digit" },
        );
        meta.append(author, time);

        const text = document.createElement("p");
        text.textContent = message.text;
        item.append(meta, text);
        messageList.append(item);
        if (!drawer.hidden) {
            messageList.scrollTop = messageList.scrollHeight;
        }
    };

    const applyReactionState = (roundId, reactions) => {
        const group = document.querySelector(
            `[data-reaction-round="${roundId}"]`,
        );
        if (!group) {
            return;
        }
        group.querySelectorAll("[data-reaction-emoji]").forEach((button) => {
            const state = reactions[button.dataset.reactionEmoji];
            if (!state) {
                return;
            }
            button.querySelector("[data-reaction-count]").textContent = state.count;
            button.setAttribute("aria-pressed", String(state.selected));
            button.classList.toggle("reaction-selected", state.selected);
        });
    };

    const applyReactionSnapshot = (snapshot) => {
        Object.entries(snapshot || {}).forEach(([roundId, reactions]) => {
            applyReactionState(roundId, reactions);
        });
    };

    if (!csrfToken) {
        console.error("Chat and reactions are unavailable because the CSRF token is missing.");
        return;
    }

    openButton?.addEventListener("click", () => setDrawerOpen(true));
    closeButtons.forEach((button) => {
        button.addEventListener("click", () => setDrawerOpen(false));
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !drawer.hidden) {
            setDrawerOpen(false);
            return;
        }
        if (event.key === "Tab" && !drawer.hidden) {
            const focusable = drawer.querySelectorAll(
                'button:not([disabled]), textarea:not([disabled])',
            );
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (
                event.shiftKey
                && (
                    document.activeElement === first
                    || !drawer.contains(document.activeElement)
                )
            ) {
                event.preventDefault();
                last.focus();
            } else if (
                !event.shiftKey
                && (
                    document.activeElement === last
                    || !drawer.contains(document.activeElement)
                )
            ) {
                event.preventDefault();
                first.focus();
            }
        }
    });

    chatForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const submitButton = chatForm.querySelector('button[type="submit"]');
        submitButton.disabled = true;
        chatFeedback.textContent = "";
        try {
            const response = await fetch(chatForm.action, {
                method: "POST",
                credentials: "same-origin",
                headers: { "X-CSRFToken": csrfToken, "Accept": "application/json" },
                body: new FormData(chatForm),
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || "Could not send your message.");
            }
            appendMessage(data.message);
            chatForm.reset();
            messageList.scrollTop = messageList.scrollHeight;
            chatForm.querySelector("textarea").focus();
        } catch (error) {
            console.error("Could not send room chat message.", error);
            chatFeedback.textContent = error.message;
        } finally {
            submitButton.disabled = false;
        }
    });

    document.addEventListener("click", async (event) => {
        const button = event.target.closest("[data-reaction-emoji]");
        if (!button) {
            return;
        }
        const group = button.closest("[data-reaction-round]");
        const feedback = group.querySelector("[data-reaction-feedback]");
        button.disabled = true;
        feedback.textContent = "";
        try {
            const formData = new FormData();
            formData.set("emoji", button.dataset.reactionEmoji);
            const response = await fetch(button.dataset.reactionUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: { "X-CSRFToken": csrfToken, "Accept": "application/json" },
                body: formData,
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || "Could not save your reaction.");
            }
            applyReactionState(data.round_id, data.reactions);
        } catch (error) {
            console.error("Could not save answer reaction.", error);
            feedback.textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });

    window.truthDareConversation = {
        updateSnapshot({
            chat_messages,
            reactions,
            unread_count,
            last_message_id,
        }) {
            const newMessages = chat_messages.filter(
                (message) => Number(message.id) > lastMessageId,
            );
            newMessages.forEach((message) => {
                appendMessage(message);
                if (
                    Number(message.player_id) !== Number(room.playerId)
                    && drawer.hidden
                ) {
                    showMessageNotice(`You’ve received a message from ${message.nickname}`);
                }
            });
            lastMessageId = Math.max(
                lastMessageId,
                Number(last_message_id) || 0,
            );
            updateUnreadCount(unread_count);
            applyReactionSnapshot(reactions);
            if (!drawer.hidden && unreadCount > 0) {
                markChatRead();
            }
        },
        getLastMessageId() {
            return lastMessageId;
        },
    };

    if (initialMessages) {
        JSON.parse(initialMessages.textContent).forEach(appendMessage);
    }
    if (initialReactions) {
        applyReactionSnapshot(JSON.parse(initialReactions.textContent));
    }
    if (unreadCount > 0) {
        chatNotice.hidden = false;
    }
})();
