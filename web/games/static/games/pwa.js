(() => {
    if ("serviceWorker" in navigator) {
        window.addEventListener("load", () => {
            navigator.serviceWorker.register("/service-worker.js").catch((error) => {
                console.error("Could not register the app's offline support.", error);
            });
        });
    }

    const installButtons = document.querySelectorAll("[data-install-app]");
    const installHelp = document.querySelector("[data-install-help]");
    const isStandalone = window.matchMedia("(display-mode: standalone)").matches
        || window.navigator.standalone === true;
    const isAppleMobile = /iphone|ipad|ipod/i.test(window.navigator.userAgent)
        || (window.navigator.platform === "MacIntel" && window.navigator.maxTouchPoints > 1);
    let deferredPrompt = null;

    if (isStandalone) {
        installButtons.forEach((button) => {
            button.hidden = true;
        });
        return;
    }

    if (!("serviceWorker" in navigator)) {
        return;
    }

    installButtons.forEach((button) => {
        button.hidden = false;
        if (isAppleMobile) {
            button.textContent = "Add to Home Screen";
        }
    });

    window.addEventListener("beforeinstallprompt", (event) => {
        event.preventDefault();
        deferredPrompt = event;
    });

    installButtons.forEach((button) => {
        button.addEventListener("click", async () => {
            if (!deferredPrompt) {
                if (installHelp) {
                    installHelp.textContent = isAppleMobile
                        ? "Tap Share, then choose Add to Home Screen."
                        : "Open your browser menu and choose Install app or Add to Home Screen.";
                    installHelp.hidden = !installHelp.hidden;
                }
                return;
            }

            await deferredPrompt.prompt();
            const choice = await deferredPrompt.userChoice;
            deferredPrompt = null;
            if (choice.outcome === "accepted") {
                installButtons.forEach((installButton) => {
                    installButton.hidden = true;
                });
            }
        });
    });

    window.addEventListener("appinstalled", () => {
        installButtons.forEach((button) => {
            button.hidden = true;
        });
    });
})();
