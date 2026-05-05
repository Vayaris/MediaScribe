(function () {
  const root = document.documentElement;
  const storedTheme = localStorage.getItem("mediascribe-theme") || "light";

  function applyTheme(theme) {
    root.dataset.theme = theme;
    localStorage.setItem("mediascribe-theme", theme);
    document.querySelectorAll("[data-theme-icon]").forEach((node) => {
      node.textContent = theme === "dark" ? "☀" : "☾";
    });
  }

  applyTheme(storedTheme);

  window.toggleTheme = function () {
    applyTheme(root.dataset.theme === "dark" ? "light" : "dark");
  };

  window.copyTranscript = async function () {
    const el = document.getElementById("transcript");
    if (!el) return;
    const text = el.value || el.textContent || "";
    await navigator.clipboard.writeText(text);
    const confirmation = document.querySelector("[data-copy-confirm]");
    if (confirmation) {
      confirmation.classList.add("visible");
      setTimeout(() => confirmation.classList.remove("visible"), 1600);
    }
  };

  function initDropZones() {
    document.querySelectorAll("[data-drop-zone]").forEach((zone) => {
      const input = zone.querySelector("input[type='file']");
      const fileName = zone.querySelector("[data-file-name]");
      if (!input) return;

      ["dragenter", "dragover"].forEach((eventName) => {
        zone.addEventListener(eventName, () => zone.classList.add("is-dragover"));
      });

      ["dragleave", "drop"].forEach((eventName) => {
        zone.addEventListener(eventName, () => zone.classList.remove("is-dragover"));
      });

      input.addEventListener("change", () => {
        if (fileName) {
          fileName.textContent = input.files && input.files[0] ? input.files[0].name : "";
        }
      });
    });
  }

  function initModeTabs() {
    const tabs = document.querySelectorAll("[data-mode-tab]");
    const panels = document.querySelectorAll("[data-mode-panel]");
    tabs.forEach((tab) => {
      tab.addEventListener("click", () => {
        const mode = tab.dataset.modeTab;
        tabs.forEach((item) => item.classList.toggle("active", item === tab));
        panels.forEach((panel) => panel.classList.toggle("active", panel.dataset.modePanel === mode));
      });
    });
  }

  function setLiveStatus(text, className) {
    const status = document.querySelector("[data-live-status]");
    if (!status) return;
    status.textContent = text;
    status.className = `status ${className || ""}`.trim();
  }

  function setLiveMessage(text, isError) {
    const message = document.querySelector("[data-live-message]");
    if (!message) return;
    message.textContent = text || "";
    message.classList.toggle("error-text", Boolean(isError));
  }

  function chooseRecorderMimeType() {
    const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
    return candidates.find((type) => window.MediaRecorder && MediaRecorder.isTypeSupported(type)) || "";
  }

  function connectAudioStream(audioContext, destination, stream) {
    const audioTracks = stream.getAudioTracks();
    if (!audioTracks.length) return false;
    const source = audioContext.createMediaStreamSource(new MediaStream(audioTracks));
    source.connect(destination);
    return true;
  }

  function requestNotificationPermission() {
    if (!("Notification" in window)) return Promise.resolve("unsupported");
    if (Notification.permission === "granted") return Promise.resolve("granted");
    if (Notification.permission === "denied") return Promise.resolve("denied");
    return Notification.requestPermission();
  }

  function notifyDone(title, body) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    new Notification(title, { body });
  }

  function initNotificationButtons() {
    document.querySelectorAll("[data-enable-notifications]").forEach((button) => {
      button.addEventListener("click", async () => {
        const permission = await requestNotificationPermission();
        const feedback = document.querySelector("[data-notification-feedback]");
        const writeMessage = (text, isError) => {
          if (feedback) {
            feedback.textContent = text;
            feedback.classList.toggle("error-text", Boolean(isError));
          } else {
            setLiveMessage(text, isError);
          }
        };
        if (permission === "granted") {
          writeMessage("Notifications activées.");
        } else if (permission === "denied") {
          writeMessage("Notifications refusées dans le navigateur.", true);
        } else {
          writeMessage("Notifications non supportées par ce navigateur.", true);
        }
      });
    });
  }

  function createMeter(audioContext, stream, bar, label, activeText) {
    const tracks = stream.getAudioTracks();
    if (!tracks.length || !bar) {
      if (label) label.textContent = "Aucun signal";
      return () => {};
    }
    const analyser = audioContext.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.78;
    const source = audioContext.createMediaStreamSource(new MediaStream(tracks));
    const data = new Uint8Array(analyser.fftSize);
    let stopped = false;
    source.connect(analyser);
    if (label) label.textContent = activeText;

    function draw() {
      if (stopped) {
        bar.style.width = "0%";
        return;
      }
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (const value of data) {
        const centered = value - 128;
        sum += centered * centered;
      }
      const rms = Math.sqrt(sum / data.length);
      const level = Math.min(100, Math.round(rms * 4));
      bar.style.width = `${level}%`;
      requestAnimationFrame(draw);
    }
    draw();
    return () => {
      stopped = true;
      if (label) label.textContent = "Arrêté";
    };
  }

  function initLiveTranscript() {
    const startButton = document.querySelector("[data-live-start]");
    const stopButton = document.querySelector("[data-live-stop]");
    const cancelButton = document.querySelector("[data-live-cancel]");
    const language = document.getElementById("live-language");
    const micMeter = document.querySelector("[data-mic-meter]");
    const micMeterLabel = document.querySelector("[data-mic-meter-label]");
    const shareMeter = document.querySelector("[data-share-meter]");
    const shareMeterLabel = document.querySelector("[data-share-meter-label]");
    if (!startButton || !stopButton || !cancelButton || !language) return;

    let audioContext = null;
    let recorder = null;
    let mixedStream = null;
    let streams = [];
    let sessionId = null;
    let jobId = null;
    let sequence = 0;
    let pollTimer = null;
    let stopTimer = null;
    let pendingUploads = [];
    let stopMeters = [];
    let stopping = false;
    let recording = false;
    let stopFinalized = false;
    let completionNotified = false;

    function setControls(recording) {
      startButton.disabled = recording;
      stopButton.disabled = !recording;
      cancelButton.disabled = !recording;
    }

    function stopStreams() {
      streams.forEach((stream) => stream.getTracks().forEach((track) => track.stop()));
      streams = [];
      stopMeters.forEach((stop) => stop());
      stopMeters = [];
      if (audioContext) {
        audioContext.close().catch(() => {});
        audioContext = null;
      }
      mixedStream = null;
    }

    async function postForm(url, formData) {
      const response = await fetch(url, {
        method: "POST",
        body: formData,
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }

    async function postEmpty(url) {
      const response = await fetch(url, { method: "POST", credentials: "same-origin" });
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }

    function uploadChunk(blob) {
      if (!sessionId || !blob || !blob.size) return Promise.resolve();
      const formData = new FormData();
      formData.append("sequence", String(sequence));
      formData.append("chunk", blob, `chunk-${String(sequence).padStart(6, "0")}.webm`);
      sequence += 1;
      const upload = postForm(`/live/${sessionId}/chunks`, formData).catch((error) => {
        setLiveMessage(`Erreur d'envoi d'un morceau live: ${error.message}`, true);
      });
      pendingUploads.push(upload);
      upload.finally(() => {
        pendingUploads = pendingUploads.filter((item) => item !== upload);
      });
      return upload;
    }

    async function finalizeStop() {
      if (stopFinalized) return;
      stopFinalized = true;
      await Promise.allSettled(pendingUploads);
      if (sessionId && stopping) {
        await postEmpty(`/live/${sessionId}/stop`);
        setLiveStatus("Traitement", "running");
        pollStatus();
      }
      stopStreams();
    }

    function startRecorderCycle(chunkMs) {
      if (!recording || stopping || !mixedStream) return;
      const mimeType = chooseRecorderMimeType();
      const current = new MediaRecorder(mixedStream, mimeType ? { mimeType } : undefined);
      const parts = [];
      recorder = current;
      current.addEventListener("dataavailable", (event) => {
        if (event.data && event.data.size) parts.push(event.data);
      });
      current.addEventListener("stop", () => {
        const blob = parts.length ? new Blob(parts, { type: mimeType || "audio/webm" }) : null;
        if (blob && blob.size) uploadChunk(blob);
        if (recording && !stopping) {
          startRecorderCycle(chunkMs);
        } else if (stopping) {
          finalizeStop().catch((error) => setLiveMessage(error.message || "Erreur de finalisation live.", true));
        }
      });
      current.start();
      stopTimer = setTimeout(() => {
        if (current.state === "recording") current.stop();
      }, chunkMs);
    }

    async function pollStatus() {
      if (!sessionId) return;
      try {
        const response = await fetch(`/live/${sessionId}/status`, { credentials: "same-origin" });
        if (!response.ok) return;
        const data = await response.json();
        if (data.error) setLiveMessage(data.error, true);
        if (data.status === "completed" || data.job_status === "completed") {
          setLiveStatus("Terminé", "completed");
          clearInterval(pollTimer);
          pollTimer = null;
          if (!completionNotified) {
            completionNotified = true;
            notifyDone("MediaScribe", "Votre transcription live est terminée.");
          }
          if (stopping && data.job_id) window.location.href = `/jobs/${data.job_id}`;
        } else if (data.status === "failed" || data.job_status === "failed") {
          setLiveStatus("Échec", "failed");
          setControls(false);
        }
      } catch (_) {
        setLiveMessage("Impossible de récupérer l'état live pour le moment.", true);
      }
    }

    startButton.addEventListener("click", async () => {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        setLiveMessage("Le navigateur ne permet pas la capture micro sur cette page HTTPS.", true);
        return;
      }
      if (!window.MediaRecorder || !window.AudioContext) {
        setLiveMessage("Ce navigateur ne supporte pas l'enregistrement audio requis.", true);
        return;
      }

      const mode = document.querySelector("input[name='live_mode']:checked")?.value || "mic";
      const panel = document.querySelector("[data-live-chunk-seconds]");
      const chunkSeconds = Math.max(2, Math.min(10, Number(panel?.dataset.liveChunkSeconds || 4)));
      const formData = new FormData();
      formData.append("mode", mode);
      formData.append("language", language.value || "fr");

      try {
        requestNotificationPermission().catch(() => {});
        setLiveStatus("Permission", "running");
        setLiveMessage("");
        sequence = 0;
        stopping = false;
        recording = false;
        stopFinalized = false;
        completionNotified = false;
        const live = await postForm("/live/start", formData);
        sessionId = live.session_id;
        jobId = live.job_id;
        const displayJobNumber = live.user_job_number || jobId;

        audioContext = new AudioContext();
        const destination = audioContext.createMediaStreamDestination();
        const micStream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true },
          video: false,
        });
        streams.push(micStream);
        connectAudioStream(audioContext, destination, micStream);
        stopMeters.push(createMeter(audioContext, micStream, micMeter, micMeterLabel, "Signal actif"));

        if (mode === "mic_display") {
          if (!navigator.mediaDevices.getDisplayMedia) {
            setLiveMessage("Le navigateur ne propose pas le partage d'écran avec audio. Le micro seul sera enregistré.", true);
            if (shareMeterLabel) shareMeterLabel.textContent = "Indisponible";
          } else {
            const displayStream = await navigator.mediaDevices.getDisplayMedia({
              video: true,
              audio: true,
              systemAudio: "include",
              surfaceSwitching: "include",
            });
            streams.push(displayStream);
            if (!connectAudioStream(audioContext, destination, displayStream)) {
              setLiveMessage("Aucune piste audio n'a été fournie par le partage. Vérifiez l'option de partage audio dans Chrome/Edge.", true);
              if (shareMeterLabel) shareMeterLabel.textContent = "Aucun audio détecté";
            } else {
              stopMeters.push(createMeter(audioContext, displayStream, shareMeter, shareMeterLabel, "Signal actif"));
            }
          }
        } else if (shareMeterLabel) {
          shareMeterLabel.textContent = "Non utilisé";
        }

        mixedStream = destination.stream;
        recording = true;
        startRecorderCycle(chunkSeconds * 1000);
        setControls(true);
        setLiveStatus("En cours", "running");
        setLiveMessage(displayJobNumber ? `Enregistrement live #${displayJobNumber} démarré. La transcription sera disponible à la fin.` : "Enregistrement live démarré.");
        pollTimer = setInterval(pollStatus, 1000);
      } catch (error) {
        setLiveStatus("Erreur", "failed");
        setLiveMessage(error.message || "Impossible de démarrer le live transcript.", true);
        setControls(false);
        stopStreams();
        if (sessionId) {
          postEmpty(`/live/${sessionId}/cancel`).catch(() => {});
        }
      }
    });

    stopButton.addEventListener("click", async () => {
      stopping = true;
      recording = false;
      setControls(false);
      setLiveStatus("Arrêt", "running");
      if (stopTimer) clearTimeout(stopTimer);
      if (recorder && recorder.state === "recording") {
        recorder.stop();
      } else {
        finalizeStop().catch((error) => setLiveMessage(error.message || "Erreur de finalisation live.", true));
      }
    });

    cancelButton.addEventListener("click", async () => {
      const activeSession = sessionId;
      sessionId = null;
      stopping = false;
      recording = false;
      if (pollTimer) clearInterval(pollTimer);
      if (stopTimer) clearTimeout(stopTimer);
      pollTimer = null;
      if (recorder && recorder.state !== "inactive") recorder.stop();
      stopStreams();
      if (activeSession) await postEmpty(`/live/${activeSession}/cancel`).catch(() => {});
      jobId = null;
      setControls(false);
      setLiveStatus("Annulé", "");
      setLiveMessage("Live transcript annulé.");
    });

  }

  function initJobStatusPolling() {
    const detail = document.querySelector("[data-job-detail]");
    if (!detail) return;
    const jobId = detail.dataset.jobDetail;
    const bar = detail.querySelector("[data-progress-bar]");
    const percent = detail.querySelector("[data-progress-percent]");
    const stage = detail.querySelector("[data-progress-stage]");
    const transcript = document.getElementById("transcript");
    const errorNode = detail.querySelector(".error");
    let notified = false;
    let wasActive = false;

    async function refresh() {
      const response = await fetch(`/jobs/${jobId}/status`, { credentials: "same-origin" });
      if (!response.ok) return false;
      const data = await response.json();
      const value = Math.max(0, Math.min(100, Number(data.progress_percent || 0)));
      if (bar) bar.style.width = `${value}%`;
      if (percent) percent.textContent = `${value}%`;
      if (stage) stage.textContent = data.progress_stage || data.status_label || "";
      if (transcript && data.transcript_text) transcript.value = data.transcript_text;
      if (errorNode && data.error) errorNode.textContent = data.error;
      const active = data.status === "queued" || data.status === "running";
      if (!notified && wasActive && data.status === "completed") {
        notified = true;
        notifyDone("MediaScribe", "Votre transcription est terminée.");
        window.location.reload();
      }
      wasActive = wasActive || active;
      return active;
    }

    refresh().then((keepGoing) => {
      if (!keepGoing) return;
      const timer = setInterval(async () => {
        const active = await refresh().catch(() => true);
        if (!active) clearInterval(timer);
      }, 2000);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    initDropZones();
    initModeTabs();
    initNotificationButtons();
    initLiveTranscript();
    initJobStatusPolling();
  });
})();
