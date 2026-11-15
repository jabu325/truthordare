(() => {
    const configElement = document.getElementById("push-config");
    const config = configElement ? JSON.parse(configElement.textContent) : null;
    const button = document.querySelector("[data-turn-notification-toggle]");
    const status = document.querySelector("[data-turn-notification-status]");

    if (!config?.enabled || !button || !status) {
        return;
    }

    const setStatus = (message) => {
        status.textContent = message;
        status.hidden = !message;
    };

    const updateButton = (subscribed) => {
        button.textContent = subscribed
            ? "🔕 Turn alerts off"
            : "🔔 Enable turn alerts";
        button.setAttribute("aria-pressed", String(subscribed));
    };

    const supported = (
        "serviceWorker" in navigator
        && "PushManager" in window
        && "Notification" in window
    );
    if (!supported) {
        button.disabled = true;
        const isAppleMobile = /iphone|ipad|ipod/i.test(navigator.userAgent)
            || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
        setStatus(
            isAppleMobile
                ? "On iPhone or iPad, add this app to your Home Screen to enable push alerts."
                : "This browser does not support background push notifications.",
        );
        return;
    }

    const csrfToken = document.querySelector(
        '#csrf-token-form [name="csrfmiddlewaretoken"]',
    )?.value;
    if (!csrfToken) {
        button.disabled = true;
        setStatus("Turn alerts are unavailable because the security token is missing.");
        console.error("Could not enable turn alerts because the CSRF token is missing.");
        return;
    }

    const postSubscription = async (payload) => {
        const response = await fetch(config.subscription_url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken,
            },
            body: JSON.stringify(payload),
        });
        if (!response.ok) {
            const detail = await response.text();
            throw new Error(detail || `Subscription request failed (${response.status}).`);
        }
    };

    const decodeApplicationKey = (key) => {
        const base64 = key.replace(/-/g, "+").replace(/_/g, "/");
        const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
        const binary = window.atob(padded);
        return Uint8Array.from(binary, (character) => character.charCodeAt(0));
    };

    const getRegistration = () => navigator.serviceWorker.ready;

    const currentSubscription = async () => {
        const registration = await getRegistration();
        return registration.pushManager.getSubscription();
    };

    const syncCurrentSubscription = async () => {
        const subscription = await currentSubscription();
        updateButton(Boolean(subscription));
        if (subscription) {
            await postSubscription(subscription.toJSON());
        }
    };

    button.disabled = true;
    syncCurrentSubscription().catch((error) => {
        setStatus("Could not check the current turn-alert subscription. Try again.");
        console.error("Could not check the turn-alert subscription.", error);
    }).finally(() => {
        button.disabled = false;
    });

    button.addEventListener("click", async () => {
        button.disabled = true;
        setStatus("");
        let newSubscription = null;
        let subscriptionSaved = false;
        try {
            const registration = await getRegistration();
            const existingSubscription = await registration.pushManager.getSubscription();
            if (existingSubscription) {
                await postSubscription({
                    action: "unsubscribe",
                    endpoint: existingSubscription.endpoint,
                });
                await existingSubscription.unsubscribe();
                updateButton(false);
                setStatus("Turn alerts are off for this room.");
                return;
            }

            const permission = await Notification.requestPermission();
            if (permission !== "granted") {
                throw new Error(
                    permission === "denied"
                        ? "Notifications are blocked in your browser settings."
                        : "Allow notifications to receive turn alerts.",
                );
            }

            newSubscription = await registration.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: decodeApplicationKey(config.public_key),
            });
            await postSubscription(newSubscription.toJSON());
            subscriptionSaved = true;
            updateButton(true);
            setStatus("Turn alerts are on for this room.");
        } catch (error) {
            if (newSubscription && !subscriptionSaved) {
                try {
                    await newSubscription.unsubscribe();
                } catch (cleanupError) {
                    console.error(
                        "Could not remove an unsaved turn-alert subscription.",
                        cleanupError,
                    );
                }
            }
            setStatus(error.message || "Could not update turn alerts. Try again.");
            console.error("Could not update turn alerts.", error);
        } finally {
            button.disabled = false;
        }
    });
})();
