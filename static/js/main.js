/**
 * VenuePulseAI – Main client-side JavaScript
 */

document.addEventListener("DOMContentLoaded", () => {
    console.log("VenuePulseAI loaded.");

    const isAuthenticated = document.body?.dataset?.authenticated === "true";
    const userId = document.body?.dataset?.userId || "";
    const userName = (document.body?.dataset?.userName || "").trim();

    function removeAllChatStorage() {
        try {
            Object.keys(localStorage).forEach((key) => {
                if (key.startsWith("vp_chat_")) {
                    localStorage.removeItem(key);
                }
            });
        } catch (_err) {
            // Ignore storage access issues in locked-down browsers.
        }
    }

    // Auto-dismiss flash alerts after 5 seconds
    document.querySelectorAll(".alert").forEach((alert) => {
        setTimeout(() => {
            alert.style.transition = "opacity 0.4s ease";
            alert.style.opacity = "0";
            setTimeout(() => alert.remove(), 400);
        }, 5000);
    });

    // Never keep chat data for anonymous views to avoid leaking prior user sessions.
    if (!isAuthenticated || !userId) {
        removeAllChatStorage();
        return;
    }

    const chatFab = document.getElementById("chatFab");
    const chatPopover = document.getElementById("chatPopover");
    const chatClose = document.getElementById("chatClose");
    const chatMessages = document.getElementById("chatMessages");
    const chatInput = document.getElementById("chatInput");
    const chatMicBtn = document.getElementById("chatMicBtn");
    const chatSendBtn = document.getElementById("chatSendBtn");

    if (!chatFab || !chatPopover || !chatClose || !chatMessages || !chatInput || !chatMicBtn || !chatSendBtn) {
        return;
    }

    const CHAT_HISTORY_KEY = `vp_chat_history_v1_${userId}`;
    const CHAT_UI_STATE_KEY = `vp_chat_ui_state_v1_${userId}`;
    const CHAT_VOICE_GREETING_KEY = `vp_chat_voice_greeted_v1_${userId}`;
    const MAX_HISTORY_MESSAGES = 60;

    // Clean up legacy non-scoped keys created before per-user isolation.
    localStorage.removeItem("vp_chat_history_v1");
    localStorage.removeItem("vp_chat_ui_state_v1");

    let isSending = false;
    let isListening = false;

    const SpeechRecognitionApi = window.SpeechRecognition || window.webkitSpeechRecognition;
    const recognition = SpeechRecognitionApi ? new SpeechRecognitionApi() : null;

    if (recognition) {
        recognition.continuous = false;
        recognition.interimResults = false;
        recognition.maxAlternatives = 1;
        recognition.lang = navigator.language || "en-US";
    }

    function setMicListeningState(listening) {
        isListening = listening;
        chatMicBtn.classList.toggle("listening", listening);
        chatMicBtn.setAttribute("aria-pressed", listening ? "true" : "false");

        const micIcon = chatMicBtn.querySelector("i");
        if (micIcon) {
            micIcon.className = listening ? "bi bi-mic-fill" : "bi bi-mic";
        }
    }

    function speakInitialGreetingIfNeeded() {
        if (localStorage.getItem(CHAT_VOICE_GREETING_KEY) === "true") {
            return;
        }

        localStorage.setItem(CHAT_VOICE_GREETING_KEY, "true");
        if (!("speechSynthesis" in window)) {
            return;
        }

        const greetingText = userName
            ? `Hi ${userName}. Welcome to VenuePulse AI. Ask me about upcoming events and pricing.`
            : "Hi. Welcome to VenuePulse AI. Ask me about upcoming events and pricing.";

        const utterance = new SpeechSynthesisUtterance(greetingText);
        utterance.lang = "en-US";
        utterance.rate = 1;
        utterance.pitch = 1;

        window.speechSynthesis.cancel();
        window.speechSynthesis.speak(utterance);
    }

    async function translateToEnglish(text) {
        const sourceText = (text || "").trim();
        if (!sourceText) {
            return "";
        }

        const endpoint = "https://translate.googleapis.com/translate_a/single"
            + `?client=gtx&sl=auto&tl=en&dt=t&q=${encodeURIComponent(sourceText)}`;

        try {
            const response = await fetch(endpoint);
            if (!response.ok) {
                return sourceText;
            }

            const payload = await response.json();
            const translatedText = Array.isArray(payload?.[0])
                ? payload[0].map((part) => (Array.isArray(part) ? part[0] : "")).join("")
                : "";

            return (translatedText || sourceText).trim();
        } catch (_err) {
            return sourceText;
        }
    }

    function loadHistory() {
        try {
            const raw = localStorage.getItem(CHAT_HISTORY_KEY);
            const parsed = raw ? JSON.parse(raw) : [];
            return Array.isArray(parsed) ? parsed : [];
        } catch (_err) {
            return [];
        }
    }

    function saveHistory(history) {
        localStorage.setItem(CHAT_HISTORY_KEY, JSON.stringify(history.slice(-MAX_HISTORY_MESSAGES)));
    }

    function persistUiState(isOpen) {
        localStorage.setItem(CHAT_UI_STATE_KEY, JSON.stringify({ open: isOpen }));
    }

    function restoreUiState() {
        try {
            const raw = localStorage.getItem(CHAT_UI_STATE_KEY);
            const state = raw ? JSON.parse(raw) : {};
            return Boolean(state.open);
        } catch (_err) {
            return false;
        }
    }

    function scrollChatToBottom() {
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function renderBotMessageContent(container, text) {
        const supportButtonHtml = "<br><br><a href='/support/submit' class='btn btn-sm btn-primary text-white' style='border-radius: 8px;'>Open Support Ticket</a>";
        const supportButtonIndex = text.indexOf(supportButtonHtml);

        if (supportButtonIndex === -1) {
            container.textContent = text;
            return;
        }

        const beforeText = text.slice(0, supportButtonIndex);
        if (beforeText.trim()) {
            container.appendChild(document.createTextNode(beforeText));
        }

        container.appendChild(document.createElement("br"));
        container.appendChild(document.createElement("br"));

        const supportLink = document.createElement("a");
        supportLink.href = "/support/submit";
        supportLink.className = "btn btn-sm btn-primary text-white";
        supportLink.style.borderRadius = "8px";
        supportLink.textContent = "Open Support Ticket";
        container.appendChild(supportLink);

        const afterText = text.slice(supportButtonIndex + supportButtonHtml.length).trim();
        if (afterText) {
            container.appendChild(document.createElement("br"));
            container.appendChild(document.createTextNode(afterText));
        }
    }

    function appendChatBubble(text, role, persist = true) {
        const bubble = document.createElement("div");
        bubble.className = "vp-chat-bubble";

        if (role === "user") {
            bubble.classList.add("vp-chat-bubble--user");
            bubble.textContent = text;
        } else {
            bubble.classList.add("vp-chat-bubble--bot");
            renderBotMessageContent(bubble, text);
        }
        chatMessages.appendChild(bubble);

        if (persist) {
            const history = loadHistory();
            history.push({ role, text });
            saveHistory(history);
        }

        scrollChatToBottom();
        return bubble;
    }

    function restoreHistory() {
        const history = loadHistory();
        if (!history.length) {
            scrollChatToBottom();
            return;
        }

        chatMessages.innerHTML = "";
        history.forEach((item) => {
            if (item && (item.role === "user" || item.role === "bot") && typeof item.text === "string") {
                appendChatBubble(item.text, item.role, false);
            }
        });
        scrollChatToBottom();
    }

    function setSendingState(sending) {
        isSending = sending;
        chatInput.disabled = sending;
        chatSendBtn.disabled = sending;
    }

    async function sendChatMessage() {
        if (isSending) {
            return;
        }

        const userMessage = chatInput.value.trim();
        if (!userMessage) {
            return;
        }

        appendChatBubble(userMessage, "user");
        chatInput.value = "";
        setSendingState(true);

        const thinkingBubble = appendChatBubble("AI is thinking...", "bot", false);

        try {
            const response = await fetch("/api/chat", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({ message: userMessage }),
            });

            let aiMessage = "Sorry, I couldn't process that right now. Please try again.";
            const payload = await response.json().catch(() => ({}));

            if (response.ok && payload.response) {
                aiMessage = payload.response;
            } else if (payload.error) {
                aiMessage = payload.error;
            }

            thinkingBubble.remove();
            appendChatBubble(aiMessage, "bot");
        } catch (_error) {
            thinkingBubble.remove();
            appendChatBubble("Network error. Please check your connection and try again.", "bot");
        } finally {
            setSendingState(false);
            chatInput.focus();
        }
    }

    chatFab.addEventListener("click", () => {
        chatPopover.classList.toggle("open");
        chatFab.classList.toggle("active");
        persistUiState(chatPopover.classList.contains("open"));
        if (chatPopover.classList.contains("open")) {
            chatInput.focus();
            scrollChatToBottom();
            speakInitialGreetingIfNeeded();
        }
    });

    chatClose.addEventListener("click", () => {
        chatPopover.classList.remove("open");
        chatFab.classList.remove("active");
        persistUiState(false);
    });

    chatSendBtn.addEventListener("click", sendChatMessage);
    chatInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            sendChatMessage();
        }
    });

    if (!recognition) {
        chatMicBtn.disabled = true;
        chatMicBtn.title = "Voice input is not supported in this browser.";
    } else {
        recognition.onresult = async (event) => {
            const transcript = event?.results?.[0]?.[0]?.transcript?.trim() || "";
            if (!transcript) {
                return;
            }

            const translatedText = await translateToEnglish(transcript);
            if (!translatedText) {
                return;
            }

            chatInput.value = translatedText;
            sendChatMessage();
        };

        recognition.onerror = () => {
            setMicListeningState(false);
            appendChatBubble(
                "I couldn't capture your voice input. Please allow microphone access and try again.",
                "bot",
                false
            );
        };

        recognition.onend = () => {
            setMicListeningState(false);
        };

        chatMicBtn.addEventListener("click", () => {
            if (isSending) {
                return;
            }

            if (isListening) {
                recognition.stop();
                return;
            }

            recognition.lang = navigator.language || "en-US";

            try {
                recognition.start();
                setMicListeningState(true);
            } catch (_err) {
                setMicListeningState(false);
                appendChatBubble(
                    "Microphone could not start. Please try again.",
                    "bot",
                    false
                );
            }
        });
    }

    document.querySelectorAll("[data-logout-link='true']").forEach((logoutLink) => {
        logoutLink.addEventListener("click", () => {
            localStorage.removeItem(CHAT_HISTORY_KEY);
            localStorage.removeItem(CHAT_UI_STATE_KEY);
            localStorage.removeItem(CHAT_VOICE_GREETING_KEY);
        });
    });

    restoreHistory();
    if (restoreUiState()) {
        chatPopover.classList.add("open");
        chatFab.classList.add("active");
        speakInitialGreetingIfNeeded();
    }
});
