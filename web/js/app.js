/**
 * AI-Юрист України — Клієнтська логіка веб-чату та сайту
 * Працює паралельно з Telegram-ботом через спільний бекенд
 */

document.addEventListener("DOMContentLoaded", () => {
  // --- Елементи DOM ---
  const chatMessages = document.getElementById("chatMessages");
  const chatForm = document.getElementById("chatForm");
  const chatInput = document.getElementById("chatInput");
  const btnSend = document.getElementById("btnSend");
  const btnVoice = document.getElementById("btnVoice");
  const btnClearChat = document.getElementById("btnClearChat");
  const typingIndicator = document.getElementById("typingIndicator");
  const typingLabel = document.getElementById("typingLabel");
  const btnModeQuick = document.getElementById("btnModeQuick");
  const btnModeCase = document.getElementById("btnModeCase");
  const caseInfoBanner = document.getElementById("caseInfoBanner");
  const caseSummaryPreview = document.getElementById("caseSummaryPreview");
  const btnResetCase = document.getElementById("btnResetCase");
  const charCounter = document.getElementById("charCounter");
  const suggestionsList = document.getElementById("suggestionsList");
  const systemStatus = document.getElementById("systemStatus");
  const mobileToggle = document.getElementById("mobileToggle");
  const navLinks = document.getElementById("navLinks");
  const tgHeaderBtn = document.getElementById("tgHeaderBtn");
  const tgHeroBtn = document.getElementById("tgHeroBtn");
  const tgFooterBtn = document.getElementById("tgFooterBtn");

  // --- Стан клієнта ---
  let currentMode = "quick"; // 'quick' або 'case'
  let sessionId = getOrCreateSessionId();
  let caseSummary = "";
  let isSending = false;

  // --- Ініціалізація ---
  fetchServerStatus();
  setupTextareaAutoResize();
  setupVoiceInput();

  // --- Перемикач мобільного меню ---
  if (mobileToggle && navLinks) {
    mobileToggle.addEventListener("click", () => {
      navLinks.classList.toggle("mobile-open");
    });
    navLinks.querySelectorAll("a").forEach(link => {
      link.addEventListener("click", () => navLinks.classList.remove("mobile-open"));
    });
  }

  // --- Генерація / Отримання Session ID ---
  function getOrCreateSessionId() {
    let sid = sessionStorage.getItem("legal_ai_session_id");
    if (!sid) {
      sid = "web_" + Math.random().toString(36).substring(2, 11) + "_" + Date.now();
      sessionStorage.setItem("legal_ai_session_id", sid);
    }
    return sid;
  }

  // --- Перемикання режимів чату ---
  btnModeQuick.addEventListener("click", () => switchMode("quick"));
  btnModeCase.addEventListener("click", () => switchMode("case"));

  function switchMode(mode) {
    currentMode = mode;
    if (mode === "quick") {
      btnModeQuick.classList.add("active");
      btnModeCase.classList.remove("active");
      caseInfoBanner.style.display = "none";
      chatInput.placeholder = "Швидка консультація: задайте ваше юридичне запитання...";
    } else {
      btnModeCase.classList.add("active");
      btnModeQuick.classList.remove("active");
      caseInfoBanner.style.display = "flex";
      chatInput.placeholder = "Ведення справи: опишіть деталі справи, ШІ враховуватиме всю розмову...";
      updateCaseBanner();
    }
  }

  function updateCaseBanner() {
    if (caseSummary) {
      caseSummaryPreview.textContent = "Контекст справи: " + caseSummary;
    } else {
      caseSummaryPreview.textContent = "Контекст новий. ШІ формуватиме резюме вашої справи.";
    }
  }

  if (btnResetCase) {
    btnResetCase.addEventListener("click", () => {
      caseSummary = "";
      sessionId = "web_" + Math.random().toString(36).substring(2, 11) + "_" + Date.now();
      sessionStorage.setItem("legal_ai_session_id", sessionId);
      updateCaseBanner();
      appendSystemMessage("🔄 Пам'ять справи скинута. Розпочато нову юридичну справу.");
    });
  }

  // --- Очищення чату / Новий діалог ---
  btnClearChat.addEventListener("click", () => {
    if (confirm("Розпочати новий діалог? Поточні повідомлення будуть очищені з екрану.")) {
      chatMessages.innerHTML = `
        <div class="chat-msg ai-msg">
          <div class="msg-avatar">⚖️</div>
          <div class="msg-content">
            <div class="msg-author-header">
              <span class="author-name">AI-Юрист</span>
              <span class="author-badge">Офіційна нормативна база</span>
              <span class="msg-time">Зараз</span>
            </div>
            <div class="msg-body">
              <p>Новий діалог розпочато! Опишіть вашу ситуацію або виберіть тему для консультації.</p>
            </div>
          </div>
        </div>
      `;
      if (currentMode === "case") {
        caseSummary = "";
        sessionId = "web_" + Math.random().toString(36).substring(2, 11) + "_" + Date.now();
        sessionStorage.setItem("legal_ai_session_id", sessionId);
        updateCaseBanner();
      }
    }
  });

  // --- Клік по чіпсах з підказками ---
  if (suggestionsList) {
    suggestionsList.addEventListener("click", (e) => {
      const chip = e.target.closest(".suggestion-chip");
      if (!chip) return;
      const prompt = chip.getAttribute("data-prompt");
      if (prompt) {
        chatInput.value = prompt;
        chatInput.dispatchEvent(new Event("input"));
        chatForm.dispatchEvent(new Event("submit"));
      }
    });
  }

  // --- Автоматичне масштабування текстового поля та лічильник ---
  function setupTextareaAutoResize() {
    chatInput.addEventListener("input", () => {
      chatInput.style.height = "auto";
      chatInput.style.height = Math.min(chatInput.scrollHeight, 140) + "px";
      const len = chatInput.value.length;
      charCounter.textContent = `${len} / 2000`;
      if (len > 2000) {
        charCounter.style.color = "var(--danger)";
      } else {
        charCounter.style.color = "var(--text-light)";
      }
    });

    chatInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        chatForm.dispatchEvent(new Event("submit"));
      }
    });
  }

  // --- Голосовий ввід (Web Speech API - українська мова) ---
  function setupVoiceInput() {
    if (!btnVoice) return;

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      btnVoice.title = "Голосовий ввід не підтримується цим браузером";
      btnVoice.style.opacity = "0.5";
      btnVoice.addEventListener("click", () => {
        alert("Ваш браузер не підтримує розпізнавання мови. Спробуйте Google Chrome, Edge або Safari.");
      });
      return;
    }

    const recognition = new SpeechRecognition();
    recognition.lang = "uk-UA";
    recognition.continuous = false;
    recognition.interimResults = false;

    let isRecording = false;

    btnVoice.addEventListener("click", () => {
      if (isRecording) {
        recognition.stop();
        return;
      }
      try {
        recognition.start();
        isRecording = true;
        btnVoice.classList.add("recording");
        btnVoice.title = "Слухаю... Говоріть ваше юридичне питання українською";
      } catch (err) {
        console.warn("Speech start warning:", err);
      }
    });

    recognition.onresult = (event) => {
      const transcript = event.results[0][0].transcript;
      if (transcript) {
        chatInput.value = (chatInput.value ? chatInput.value + " " : "") + transcript;
        chatInput.dispatchEvent(new Event("input"));
      }
    };

    recognition.onerror = (event) => {
      console.warn("Speech recognition error:", event.error);
      isRecording = false;
      btnVoice.classList.remove("recording");
      btnVoice.title = "Натисніть для голосового вводу";
    };

    recognition.onend = () => {
      isRecording = false;
      btnVoice.classList.remove("recording");
      btnVoice.title = "Натисніть для голосового вводу";
    };
  }

  // --- Відправка форми чату ---
  chatForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (isSending) return;

    const query = chatInput.value.trim();
    if (!query) return;

    if (query.length > 2000) {
      alert("Будь ласка, скоротіть ваш запит до 2000 символів.");
      return;
    }

    // 1. Додаємо повідомлення користувача в чат
    appendUserMessage(query);
    chatInput.value = "";
    chatInput.style.height = "auto";
    charCounter.textContent = "0 / 2000";

    // 2. Вмикаємо стан очікування
    setLoadingState(true);

    try {
      // 3. Відправляємо запит на бекенд
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          message: query,
          session_id: sessionId,
          mode: currentMode,
          summary: caseSummary
        })
      });

      if (!response.ok) {
        throw new Error(`Помилка сервера HTTP ${response.status}`);
      }

      const data = await response.json();

      if (data.status === "ok") {
        appendAIMessage(data.response);
        if (data.summary && currentMode === "case") {
          caseSummary = data.summary;
          updateCaseBanner();
        }
      } else {
        appendErrorMessage(data.error || "Вибачте, виникла помилка під час обробки запиту.");
      }
    } catch (err) {
      console.error("Chat error:", err);
      appendErrorMessage("Не вдалося зв'язатися з сервером AI-Юриста. Перевірте з'єднання з інтернетом або спробуйте пізніше.");
    } finally {
      setLoadingState(false);
      chatInput.focus();
    }
  });

  // --- Функції додавання повідомлень ---
  function appendUserMessage(text) {
    const timeStr = getCurrentTime();
    const msgEl = document.createElement("div");
    msgEl.className = "chat-msg user-msg";
    msgEl.innerHTML = `
      <div class="msg-avatar">👤</div>
      <div class="msg-content">
        <div class="msg-author-header">
          <span class="author-name">Ви</span>
          <span class="msg-time">${timeStr}</span>
        </div>
        <div class="msg-body">
          <p>${escapeHtml(text).replace(/\n/g, "<br>")}</p>
        </div>
      </div>
    `;
    chatMessages.appendChild(msgEl);
    scrollToBottom();
  }

  function appendAIMessage(markdownText) {
    const timeStr = getCurrentTime();
    const htmlContent = renderMarkdown(markdownText);
    const msgEl = document.createElement("div");
    msgEl.className = "chat-msg ai-msg";

    // Створюємо елемент
    msgEl.innerHTML = `
      <div class="msg-avatar">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/>
          <path d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/>
          <path d="M7 21h10"/>
          <path d="M12 3v18"/>
          <path d="M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/>
        </svg>
      </div>
      <div class="msg-content">
        <div class="msg-author-header">
          <span class="author-name">AI-Юрист</span>
          <span class="author-badge">Нормативно перевірено</span>
          <span class="msg-time">${timeStr}</span>
        </div>
        <div class="msg-body">${htmlContent}</div>
        <div class="msg-actions">
          <button class="btn-copy" title="Скопіювати відповідь">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>
            <span>Скопіювати</span>
          </button>
          <button class="btn-copy btn-print" title="Роздрукувати правову довідку">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="6 9 6 2 18 2 18 9"/>
              <path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>
              <rect x="6" y="14" width="12" height="8"/>
            </svg>
            <span>Друк</span>
          </button>
          <div class="msg-feedback">
            <button class="btn-feedback" data-val="up" title="Корисна відповідь">👍</button>
            <button class="btn-feedback" data-val="down" title="Потребує уточнення">👎</button>
          </div>
        </div>
      </div>
    `;

    // Обробник копіювання
    const btnCopy = msgEl.querySelector(".btn-copy");
    btnCopy.addEventListener("click", () => {
      navigator.clipboard.writeText(markdownText).then(() => {
        btnCopy.innerHTML = `
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2">
            <polyline points="20 6 9 17 4 12"/>
          </svg>
          <span style="color:#10b981">Скопійовано!</span>
        `;
        setTimeout(() => {
          btnCopy.innerHTML = `
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>
            <span>Скопіювати</span>
          `;
        }, 2000);
      });
    });

    // Обробник друку
    const btnPrint = msgEl.querySelector(".btn-print");
    btnPrint.addEventListener("click", () => {
      const printWindow = window.open("", "_blank");
      printWindow.document.write(`
        <!DOCTYPE html>
        <html lang="uk">
        <head>
          <meta charset="UTF-8">
          <title>Правова консультація — AI-Юрист</title>
          <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; line-height: 1.6; padding: 2rem; color: #111; max-width: 800px; margin: 0 auto; }
            h1 { font-size: 1.4rem; border-bottom: 2px solid #1e3a8a; padding-bottom: 0.5rem; color: #1e3a8a; }
            h2, h3 { color: #0f172a; margin-top: 1.5rem; }
            a { color: #1e40af; text-decoration: underline; }
            ul { margin-left: 1.5rem; }
            .header-info { display: flex; justify-content: space-between; font-size: 0.85rem; color: #64748b; margin-bottom: 1.5rem; }
            .disclaimer { margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #e2e8f0; font-size: 0.75rem; color: #64748b; }
          </style>
        </head>
        <body>
          <div class="header-info">
            <strong>AI-ЮРИСТ УКРАЇНИ • Нормативно-правова довідка</strong>
            <span>Дата: ${new Date().toLocaleDateString('uk-UA')}</span>
          </div>
          <h1>Юридичний висновок та аналіз законодавства</h1>
          <div>${htmlContent}</div>
          <div class="disclaimer">
            Довідка сформована на базі чинного законодавства України з офіційного веб-порталу Верховної Ради (zakon.rada.gov.ua). Носить аналітичний характер відповідно до ст. 131-2 Конституції України.
          </div>
        </body>
        </html>
      `);
      printWindow.document.close();
      printWindow.focus();
      setTimeout(() => { printWindow.print(); }, 250);
    });

    // Обробники оцінки
    msgEl.querySelectorAll(".btn-feedback").forEach(fBtn => {
      fBtn.addEventListener("click", () => {
        const val = fBtn.getAttribute("data-val");
        msgEl.querySelector(".msg-feedback").innerHTML = `<span style="font-size:0.75rem; color:#10b981;">Дякуємо за відгук!</span>`;
      });
    });

    chatMessages.appendChild(msgEl);
    scrollToBottom();
  }

  function appendErrorMessage(text) {
    const msgEl = document.createElement("div");
    msgEl.className = "chat-msg ai-msg";
    msgEl.innerHTML = `
      <div class="msg-avatar" style="background:#fee2e2; color:#ef4444;">⚠️</div>
      <div class="msg-content">
        <div class="msg-body" style="background:#fef2f2; border-color:#fecaca; color:#991b1b;">
          <p><strong>Повідомлення системи:</strong> ${escapeHtml(text)}</p>
        </div>
      </div>
    `;
    chatMessages.appendChild(msgEl);
    scrollToBottom();
  }

  function appendSystemMessage(text) {
    const msgEl = document.createElement("div");
    msgEl.style.textAlign = "center";
    msgEl.style.fontSize = "0.82rem";
    msgEl.style.color = "var(--text-muted)";
    msgEl.style.margin = "0.5rem 0";
    msgEl.textContent = text;
    chatMessages.appendChild(msgEl);
    scrollToBottom();
  }

  function setLoadingState(isLoading) {
    isSending = isLoading;
    btnSend.disabled = isLoading;
    if (isLoading) {
      typingIndicator.style.display = "flex";
      typingLabel.textContent = "Здійснюю правовий аналіз та пошук у базі ВРУ...";
    } else {
      typingIndicator.style.display = "none";
    }
    scrollToBottom();
  }

  function scrollToBottom() {
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  function getCurrentTime() {
    const now = new Date();
    return now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function escapeHtml(str) {
    return str
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // --- Легкий та надійний Markdown парсер для юридичного тексту ---
  function renderMarkdown(md) {
    if (!md) return "";
    let html = escapeHtml(md);

    // Заголовки (### h3, ## h2, # h1)
    html = html.replace(/^### (.*$)/gim, "<h3>$1</h3>");
    html = html.replace(/^## (.*$)/gim, "<h2>$1</h2>");
    html = html.replace(/^# (.*$)/gim, "<h1>$1</h1>");

    // Горизонтальні лінії
    html = html.replace(/^---$/gim, "<hr>");

    // Жирний шрифт **text**
    html = html.replace(/\*\*(.*?)\*\*/gim, "<strong>$1</strong>");

    // Курсив *text*
    html = html.replace(/\*(.*?)\*/gim, "<em>$1</em>");

    // Посилання в форматі [Текст](URL)
    html = html.replace(/\[(.*?)\]\((https?:\/\/[^\s)]+)\)/gim, '<a href="$2" target="_blank" rel="noopener noreferrer">$1 ↗</a>');

    // Списки з дефісом (- пункт)
    html = html.replace(/^\- (.*$)/gim, "<li>$1</li>");
    // Обгортка сусідніх <li> у <ul>
    html = html.replace(/(<li>.*<\/li>)/gms, "<ul>$1</ul>");
    // Очищення подвійних вкладених ul
    html = html.replace(/<\/ul>\s*<ul>/g, "");

    // Абзаци: розбиття по подвійному переносу рядка
    const paragraphs = html.split(/\n{2,}/);
    html = paragraphs.map(p => {
      p = p.trim();
      if (!p) return "";
      if (p.startsWith("<h") || p.startsWith("<ul") || p.startsWith("<hr")) {
        return p;
      }
      return `<p>${p.replace(/\n/g, "<br>")}</p>`;
    }).join("");

    return html;
  }

  // --- Запит статусу сервера ---
  async function fetchServerStatus() {
    try {
      const res = await fetch("/api/status");
      if (res.ok) {
        const data = await res.json();
        if (data.status === "ok") {
          systemStatus.innerHTML = `
            <span class="status-dot"></span>
            <span class="status-label">RAG ВРУ Онлайн (${data.version || 'v2.5'})</span>
          `;
          if (data.bot_username) {
            const tgUrl = `https://t.me/Ai_lawwyer_bot${data.bot_username}`;
            if (tgHeaderBtn) tgHeaderBtn.href = tgUrl;
            if (tgHeroBtn) tgHeroBtn.href = tgUrl;
            if (tgFooterBtn) tgFooterBtn.href = tgUrl;
          }
        }
      }
    } catch (e) {
      console.warn("Status check notice:", e);
    }
  }
});

