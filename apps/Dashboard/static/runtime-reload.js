(() => {
  const storageKey = 'fate-runtime-revision';
  let knownRevision = null;
  let stopped = false;

  try {
    knownRevision = sessionStorage.getItem(storageKey);
  } catch (_error) {
    // In-memory tracking still works when browser storage is unavailable.
  }

  async function checkRuntimeRevision() {
    if (stopped) return;
    try {
      const response = await fetch('/api/runtime-revision', {
        cache: 'no-store',
        credentials: 'same-origin',
      });
      if (!response.ok) return;
      const payload = await response.json();
      const revision = typeof payload.revision === 'string' ? payload.revision : null;
      if (!revision) return;

      if (knownRevision && revision !== knownRevision) {
        stopped = true;
        try {
          sessionStorage.setItem(storageKey, revision);
        } catch (_error) {
          // The in-memory guard prevents a duplicate reload in this document.
        }
        location.reload();
        return;
      }

      knownRevision = revision;
      try {
        sessionStorage.setItem(storageKey, revision);
      } catch (_error) {
        // Browser storage is optional for the live reload signal.
      }
    } catch (_error) {
      // The bot may be reconnecting; the next check will retry quietly.
    } finally {
      if (!stopped) window.setTimeout(checkRuntimeRevision, 2000);
    }
  }

  window.addEventListener('pagehide', () => { stopped = true; }, { once: true });
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    stopped = false;
    void checkRuntimeRevision();
  });
  void checkRuntimeRevision();
})();
