(() => {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) {
        console.warn("Turn alert sound is unavailable in this browser.");
        return;
    }

    let audioContext;

    const unlockAudio = () => {
        if (!audioContext) {
            audioContext = new AudioContextClass();
        }
        if (audioContext.state === "suspended") {
            audioContext.resume().catch((error) => {
                console.error("Could not enable turn alert sound.", error);
            });
        }
    };

    document.addEventListener("pointerdown", unlockAudio, { once: true });
    document.addEventListener("keydown", unlockAudio, { once: true });

    window.truthDareTurnAlert = {
        play() {
            if (!audioContext || audioContext.state !== "running") {
                return;
            }

            const startAt = audioContext.currentTime;
            const oscillator = audioContext.createOscillator();
            const volume = audioContext.createGain();
            oscillator.type = "sine";
            oscillator.frequency.setValueAtTime(660, startAt);
            oscillator.frequency.setValueAtTime(880, startAt + 0.12);
            volume.gain.setValueAtTime(0.0001, startAt);
            volume.gain.exponentialRampToValueAtTime(0.16, startAt + 0.025);
            volume.gain.exponentialRampToValueAtTime(0.0001, startAt + 0.36);
            oscillator.connect(volume);
            volume.connect(audioContext.destination);
            oscillator.start(startAt);
            oscillator.stop(startAt + 0.37);
        },
    };
})();
