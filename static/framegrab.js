// Copyright (c) 2026 TheVibeFounder.

    const TRANSCRIBE_ENABLED = document.documentElement.dataset.transcribe === 'true';
    const RECENT_RECEIPT_LIMIT = 5;
    const HISTORY_PAGE_SIZE = 10;
    let cardData = [];
    let ledgerReceipts = [];
    let historyVisibleCount = HISTORY_PAGE_SIZE;
    let extracting = false;

    // A card owns its poller. Stopping it is the only way a wiped or retried
    // receipt stops receiving another job's status.
    function stopPolling(card) {
      if (!card) return;
      if (card.pollHandle) {
        clearTimeout(card.pollHandle);
        card.pollHandle = null;
      }
      // Retires a poll that is already awaiting a response, which clearTimeout
      // cannot: pollCard's `owns()` checks this token, so the in-flight callback
      // discards its payload instead of rescheduling.
      card.pollGen = null;
    }

    // Everything on a card that one specific job produced, as opposed to what the
    // source URL produced (url, format, title, uploader, duration, thumbnail,
    // formats). A retry is a different job, so none of this may survive into it:
    // `playable` keys off previewAvailable + jobId, and the thumbnail renders its
    // <video> outside every status branch, so a retained pair would put the
    // previous job's video on the card for as long as the card lives.
    const JOB_DERIVED_FIELDS = [
      'jobId', 'previewAvailable', 'folderName', 'filename', 'filenames', 'fileCount',
      'assetTypes', 'detectedFormat', 'limitReached', 'scriptStatus', 'scriptError',
      'scriptFilename', 'transcriptFilename', 'artifactPaths', 'error', 'errorKind', 'cancelRequested', 'mediaSaved',
    ];
    function clearJobState(card) {
      for (const key of JOB_DERIVED_FIELDS) delete card[key];
    }

    // Everything a /api/status payload is able to change on screen: applyStatus
    // writes `status` plus a subset of JOB_DERIVED_FIELDS and nothing else, so
    // deriving the signature from the same list keeps the two in step — a field
    // added to one is already covered by the other.
    function jobStateSignature(card) {
      return JSON.stringify([card.status, ...JOB_DERIVED_FIELDS.map((key) => card[key] ?? null)]);
    }

    // A failed job has nothing left to play: app.py's cleanup_job_artifacts removes
    // the files it wrote, and a job the server has forgotten cannot serve
    // /api/preview either. Marking the failure has to retract playability with it.
    function failCard(card, message) {
      card.status = 'error';
      card.error = message;
      card.previewAvailable = false;
    }

    // Losing the connection is not the server reporting a failure: nothing was
    // cleaned up, whatever was finalized is still on disk, and /api/preview will
    // still serve it the moment the network is back. So this failure keeps the
    // playability that failCard retracts.
    function failCardOffline(card, message) {
      card.status = 'error';
      card.error = message;
    }

    // Icon shows the theme you would switch to: sun on the dark shell, moon on light.
    const SUN_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4.5"/><line x1="12" y1="19.5" x2="12" y2="22"/><line x1="2" y1="12" x2="4.5" y2="12"/><line x1="19.5" y1="12" x2="22" y2="12"/><line x1="4.9" y1="4.9" x2="6.7" y2="6.7"/><line x1="17.3" y1="17.3" x2="19.1" y2="19.1"/><line x1="4.9" y1="19.1" x2="6.7" y2="17.3"/><line x1="17.3" y1="6.7" x2="19.1" y2="4.9"/></svg>';
    const MOON_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg>';
    function syncThemeToggle() {
      const light = document.documentElement.dataset.theme === 'light';
      const toggle = document.getElementById('themeToggle');
      toggle.innerHTML = light ? MOON_ICON : SUN_ICON;
      toggle.setAttribute('aria-pressed', String(light));
      toggle.setAttribute('aria-label', light ? 'Switch to dark theme' : 'Switch to light theme');
    }
    function toggleTheme() {
      const root = document.documentElement;
      if (root.dataset.theme === 'light') {
        delete root.dataset.theme;
        localStorage.removeItem('framegrab-theme');
        localStorage.removeItem('vidclip-theme');  // or the legacy key revives light
      } else {
        root.dataset.theme = 'light';
        localStorage.setItem('framegrab-theme', 'light');
      }
      syncThemeToggle();
    }
    syncThemeToggle();

    function parseUrls(text) {
      const seen = new Set();
      for (const token of text.split(/[\s,]+/)) {
        const candidate = token.trim();
        if (candidate.startsWith('http')) seen.add(candidate);
      }
      return [...seen];
    }

    function fmtDur(totalSeconds) {
      if (!totalSeconds) return '';
      const minutes = Math.floor(totalSeconds / 60);
      const seconds = String(Math.floor(totalSeconds % 60)).padStart(2, '0');
      return `${minutes}:${seconds}`;
    }

    function creatorLabel(uploader, title) {
      const inferred = /^Video by (.+)$/i.exec(title || '');
      const name = uploader || (inferred && inferred[1]);
      return name ? `By ${name}` : 'Creator unavailable';
    }

    async function postJson(route, payload) {
      const res = await fetch(route, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || `Request failed (${res.status})`);
      return data;
    }

    function viewArtifact(route) { window.open(route, '_blank', 'noopener,noreferrer'); }

    async function showInFolder(route, button) {
      const original = button.textContent;
      button.disabled = true;
      button.textContent = 'Opening...';
      try {
        const response = await fetch(route, {
          method: 'POST',
          headers: { 'X-FrameGrab-Action': 'reveal' },
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Could not open folder');
        button.textContent = result.manager === 'Finder' ? 'Shown in Finder' : 'Shown in folder';
      } catch {
        button.textContent = 'Could not open';
      } finally {
        button.disabled = false;
        window.setTimeout(() => { button.textContent = original; }, 1600);
      }
    }

    function el(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    }

    function svgIcon(pathMarkup) {
      const tpl = document.createElement('template');
      tpl.innerHTML = pathMarkup.trim();
      return tpl.content.firstChild;
    }

    const ERROR_TRANSLATIONS = [
      [/Unsupported URL/, 'This URL is not supported'],
      [/Video unavailable/, 'Video is unavailable or private'],
      [/Private video/, 'This video is private'],
      [/rate.?limited by source/i, 'Rate-limited by the source — wait a bit, then retry'],
      [/HTTP Error 403/, 'Access denied by the platform'],
      [/HTTP Error 404/, 'Video not found'],
      [/copyright/, 'Video blocked due to copyright'],
      [/geo/, 'Video not available in your region'],
      [/timed out/i, 'Request timed out — try again'],
      [/network/i, 'Network error — check your connection'],
    ];

    function friendlyError(err) {
      for (const [pattern, message] of ERROR_TRANSLATIONS) {
        if (pattern.test(err)) return message;
      }
      return err.length > 80 ? err.slice(0, 80) + '...' : err;
    }

    document.getElementById('urls').addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        go();
      }
    });

    async function expandPlaylists(urls, format) {
      // Playlist links become their individual video URLs, in place.
      if (format === 'image') return urls;
      const expanded = [];
      for (const url of urls) {
        if (!url.includes('list=')) {
          expanded.push(url);
          continue;
        }
        try {
          const data = await postJson('/api/playlist', { url });
          expanded.push(...(data.urls?.length ? data.urls : [url]));
        } catch {
          expanded.push(url);
        }
      }
      return expanded;
    }

    async function loadCardInfo(idx) {
      const c = cardData[idx];
      // These writes replace cardData[idx] outright, so a card that no longer holds
      // the index would not just be stale — it would evict whatever does hold it.
      const owns = () => cardData[idx] === c;
      try {
        const data = await postJson('/api/info', { url: c.url, format: c.format });
        if (!owns()) return;
        if (data.error) {
          cardData[idx] = { ...c, status: 'info-error', error: data.error };
        } else {
          cardData[idx] = {
            ...c,
            status: 'ready',
            title: data.title || '',
            thumbnail: data.thumbnail || '',
            duration: data.duration,
            uploader: data.uploader || '',
            formats: data.formats || [],
            selectedFormatId: data.formats?.[0]?.id || null,
          };
        }
      } catch (err) {
        if (!owns()) return;
        cardData[idx] = { ...c, status: 'info-error', error: err.message };
      }
      renderCard(idx);
    }

    async function go() {
      // Enter reaches this even while the button is disabled, so the guard lives
      // here rather than on the control.
      if (extracting) return;
      const pasted = parseUrls(document.getElementById('urls').value);
      if (!pasted.length) return;

      extracting = true;
      const btn = document.getElementById('goBtn');
      btn.disabled = true;
      btn.textContent = 'Loading...';

      try {
        const requestedFormat = document.getElementById('audioOnly').checked ? 'audio' : 'auto';
        const urls = await expandPlaylists(pasted, requestedFormat);
        const pending = urls.filter(url => !cardData.some(c => c.url === url && !['done', 'error', 'info-error'].includes(c.status)));
        const indexes = pending.map(url => {
          const idx = cardData.length;
          cardData.push({url, format: requestedFormat, status: 'loading', requestId: crypto.randomUUID()});
          renderCard(idx);
          return idx;
        });
        const work = indexes.slice();
        await Promise.all([0, 1].map(async () => {
          while (work.length) {
            const idx = work.shift();
            if (await findExistingReceipt(idx)) continue;
            await loadCardInfo(idx);
            if (cardData[idx].status === 'ready') await dlCard(idx);
          }
        }));
      } finally {
        extracting = false;
        btn.disabled = false;
        btn.textContent = 'Extract';
      }
    }

    function eyebrowFor(c) {
      if (c.status === 'existing') {
        const inspect = el('a', 'card-dl-btn done', 'Open saved asset');
        inspect.href = `#asset/${c.existingId}`; actions.appendChild(inspect);
        const again = el('button', 'q-chip', 'Download again');
        again.onclick = async () => {
          again.disabled = true;
          await loadCardInfo(idx);
          if (c.status === 'ready' || cardData[idx].status === 'ready') await dlCard(idx);
        };
        actions.appendChild(again);
        actions.appendChild(el('span', 'card-status done', 'Already in your library'));
      } else if (c.status === 'ready') {
        return c.format === 'audio' ? 'Ready · audio only' : 'Ready · detects all media';
      }
      if (c.status === 'queued') return 'Queued';
      if (c.status === 'downloading') return 'Downloading';
      if (c.status === 'transcribing') return 'Building script';
      if (c.status === 'done') {
        if (c.detectedFormat === 'mixed') return `Saved · ${c.fileCount} mixed assets`;
        if (c.detectedFormat === 'image') return `Saved · ${c.fileCount || 1} image asset${c.fileCount === 1 ? '' : 's'}`;
        if (c.detectedFormat === 'audio') return 'Saved · audio';
        if (TRANSCRIBE_ENABLED && c.scriptStatus === 'done') return 'Saved · media · script · timestamps';
        return 'Saved · media';
      }
      if (c.status === 'error') return 'Failed';
      return '';
    }

    function renderCard(idx) {
      const c = cardData[idx];
      if (!c) return;  // the batch this card belonged to was wiped
      let card = document.getElementById(`card-${idx}`);
      if (!card) {
        card = el('div', 'card');
        card.id = `card-${idx}`;
        document.getElementById('cards').appendChild(card);
      }
      card.textContent = '';

      // Loading skeleton
      if (c.status === 'loading') {
        card.className = 'card';
        const body = el('div', 'card-body');
        body.appendChild(el('div', 'skeleton-line short'));
        body.appendChild(el('div', 'skeleton-line medium'));
        card.appendChild(body);
        card.appendChild(el('div', 'card-thumb loading'));
        return;
      }

      // Fetch-error receipt
      if (c.status === 'info-error') {
        card.className = 'card card-error';
        const body = el('div', 'card-body');
        body.appendChild(el('div', 'card-eyebrow', 'Could not fetch'));
        body.appendChild(el('div', 'card-title', 'No receipt for this link'));
        body.appendChild(el('div', 'card-error-msg', friendlyError(c.error || '')));
        body.appendChild(el('div', 'card-error-url', c.url));
        card.appendChild(body);
        const thumb = el('div', 'card-thumb');
        const icon = el('div', 'card-error-icon');
        icon.appendChild(svgIcon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>'));
        thumb.appendChild(icon);
        card.appendChild(thumb);
        return;
      }

      // Ready / downloading / done / error receipts
      card.className = 'card' + (c.status === 'error' ? ' status-error' : '');
      const isAudio = c.format === 'audio';
      const isImage = c.format === 'image';
      const isAuto = c.format === 'auto';

      // Not tied to a phase: the server reports preview_available whenever the
      // finalized file is on disk, and that is when it should be reachable.
      const playable = Boolean(c.previewAvailable && c.jobId);

      const thumb = el('div', 'card-thumb');
      if (playable) {
        thumb.classList.add('card-video');
        const video = document.createElement('video');
        video.src = `/api/preview/${c.jobId}`;
        video.controls = true;
        video.preload = 'metadata';
        video.setAttribute('playsinline', '');
        thumb.appendChild(video);
      } else if (isAudio) {
        const noThumb = el('div', 'no-thumb');
        noThumb.appendChild(svgIcon('<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="5 3 19 12 5 21 5 3"/></svg>'));
        thumb.appendChild(noThumb);
      } else if (c.thumbnail && /^https?:\/\//i.test(c.thumbnail)) {
        const img = document.createElement('img');
        img.src = c.thumbnail;
        img.alt = '';
        img.loading = 'lazy'; img.referrerPolicy = 'no-referrer';
        thumb.appendChild(img);
      } else {
        const noThumb = el('div', 'no-thumb');
        noThumb.appendChild(svgIcon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20"/><circle cx="8" cy="8" r="1.5"/><path d="m21 15-5-5L5 21"/></svg>'));
        thumb.appendChild(noThumb);
      }

      const actions = el('div', 'card-actions');
      actions.setAttribute('aria-live', 'polite');
      const spinner = (label) => {
        const s = el('span', 'card-status downloading');
        s.appendChild(el('span', 'spin'));
        s.appendChild(document.createTextNode(' ' + label));
        return s;
      };
      // The saved file is reachable as soon as it exists, which is before the
      // script is built — /api/preview and /api/reveal both read job['preview'].
      const addMediaActions = () => {
        if (playable) {
          const videoBtn = el('button', 'card-dl-btn done', 'Play video');
          videoBtn.onclick = () => viewArtifact(`/api/preview/${c.jobId}`);
          actions.appendChild(videoBtn);
        }
        if (c.jobId && (c.status === 'done' || playable)) {
          const folderBtn = el('button', 'q-chip', 'Show in folder');
          folderBtn.onclick = () => showInFolder(`/api/reveal/${c.jobId}`, folderBtn);
          actions.appendChild(folderBtn);
        }
      };

      if (c.status === 'ready') {
        const btn = el('button', 'card-dl-btn', isAuto ? 'Extract media' : (isImage ? 'Download assets' : 'Download'));
        btn.onclick = () => dlCard(idx);
        actions.appendChild(btn);
        if (!isAudio && !isImage && !isAuto && c.formats && c.formats.length > 1) {
          for (const f of c.formats) {
            const chip = el('button', 'q-chip' + (f.id === c.selectedFormatId ? ' active' : ''), f.label);
            chip.onclick = () => pickFormat(idx, f.id);
            actions.appendChild(chip);
          }
        }
      } else if (c.status === 'queued') {
        actions.appendChild(spinner('Waiting for a slot...'));
      } else if (c.status === 'downloading') {
        actions.appendChild(spinner('Pulling source...'));
        addMediaActions();  // auto-extract returns here for the image pass, media in hand
      } else if (c.status === 'transcribing') {
        actions.appendChild(spinner('Building script...'));
        addMediaActions();
      } else if (c.status === 'done') {
        addMediaActions();
        if (c.scriptStatus === 'done') {
          const scriptBtn = el('button', 'card-dl-btn script', 'View transcript');
          scriptBtn.onclick = () => viewArtifact(`/api/script/${c.jobId}?view=1`);
          actions.appendChild(scriptBtn);
          const tsBtn = el('button', 'q-chip', 'View timestamps');
          tsBtn.onclick = () => viewArtifact(`/api/transcript/${c.jobId}?view=1`);
          actions.appendChild(tsBtn);
        } else if (c.scriptStatus === 'error') {
          actions.appendChild(el('span', 'card-status error', 'Script failed: ' + friendlyError(c.scriptError || '')));
        }
        // Only once the job is done: compression reads the receipt from the
        // ledger, and app.py writes that line before it announces done.
        if (playable) actions.appendChild(compressHost(c.jobId, null));
        const savedLabel = `Auto-saved · ${c.folderName || c.filename || 'Downloads'}`;
        actions.appendChild(el('span', 'card-status done', savedLabel));
      } else if (c.status === 'error') {
        const retryBtn = el('button', 'card-dl-btn', 'Retry');
        retryBtn.textContent = c.errorKind === 'metadata' ? 'Repair receipt' : c.errorKind === 'interrupted' ? 'Resume' : 'Retry';
        retryBtn.onclick = () => dlCard(idx);
        actions.appendChild(retryBtn);
        actions.appendChild(el('span', 'card-status error', friendlyError(c.error || 'Download failed')));
      }

      if (c.jobId && !['done', 'error'].includes(c.status)) {
        const cancel = el('button', 'q-chip', c.cancelRequested ? 'Cancelling…' : 'Cancel');
        cancel.disabled = Boolean(c.cancelRequested);
        cancel.onclick = async () => {
          try { await postJson(`/api/jobs/${c.jobId}/cancel`, {}); c.cancelRequested = true; renderCard(idx); }
          catch (error) { document.getElementById('queueSummary').textContent = error.message; }
        };
        actions.appendChild(cancel);
      }
      if (c.status === 'done') {
        const detail = el('a', 'q-chip', 'Inspect asset'); detail.href = `#asset/${c.jobId}`; actions.prepend(detail);
      }
      if (c.status === 'error' && c.mediaSaved) addMediaActions();
      if (c.status === 'error' && c.jobId) {
        const dismiss = el('button', 'q-chip', 'Dismiss');
        dismiss.onclick = async () => {
          try { await postJson(`/api/jobs/${c.jobId}/dismiss`, {}); stopPolling(c); c.dismissed = true; card.hidden = true; updateQueueSummary(); }
          catch (error) { document.getElementById('queueSummary').textContent = error.message; }
        };
        actions.appendChild(dismiss);
      }
      const body = el('div', 'card-body');
      const eyebrow = eyebrowFor(c);
      if (eyebrow) body.appendChild(el('div', 'card-eyebrow', eyebrow));
      body.appendChild(el('div', 'card-title', c.title || 'Untitled'));
      const creator = creatorLabel(c.uploader, c.title);
      body.appendChild(el('div', 'card-meta', creator + (c.duration ? ' · ' + fmtDur(c.duration) : '')));
      body.appendChild(actions);

      card.appendChild(body);
      card.appendChild(thumb);
      updateQueueSummary();
    }

    function pickFormat(idx, formatId) {
      cardData[idx].selectedFormatId = formatId;
      renderCard(idx);
    }

    async function dlCard(idx) {
      const c = cardData[idx];
      if (c.jobId && c.errorKind !== 'unknown') return retryExisting(idx);
      stopPolling(c);
      clearJobState(c); // ...and drops its identity, so nothing of it can still render
      c.status = 'queued';  // the server creates the job queued; the poll tells us when it runs
      renderCard(idx);

      try {
        const data = await postJson('/api/download', {
          request_id: c.requestId || (c.requestId = crypto.randomUUID()),
          url: c.url,
          format: c.format,
          format_id: c.selectedFormatId,
          title: c.title || '',
          uploader: c.uploader || '',
          duration: c.duration || null,
          thumbnail: c.thumbnail || '',
        });
        // The POST awaited. A retry is reachable outside `go()`, so a new Extract
        // can have wiped this card in the meantime; writing now would either
        // resurrect it or start a poller nothing will ever read.
        if (cardData[idx] !== c) return;
        if (data.error) {
          failCard(c, data.error);
        c.previewAvailable = Boolean(data.preview_available);
        c.artifactPaths = data.artifact_paths;
        } else {
          c.jobId = data.job_id;
          pollCard(idx);
        }
      } catch (err) {
        if (cardData[idx] !== c) return;
        failCard(c, err.message);
      }
      renderCard(idx);
    }

    function applyStatus(idx, card, data) {
      // Mirrors one /api/status payload into the card that owns the job; true when
      // terminal. The card is passed, never looked up: `idx` is only a DOM address,
      // and a new Extract can seat a different card there while a payload is in
      // flight. Refusing the write is also a reason to stop polling.
      if (cardData[idx] !== card) return true;
      const c = card;
      const before = jobStateSignature(c);
      Object.assign(c, {errorKind:data.error_kind, cancelRequested:data.cancel_requested, mediaSaved:data.media_saved});
      if (data.status === 'queued' || data.status === 'downloading') {
        // The server owns the phase. A job waits in `queued` behind the
        // concurrency slot and the per-host pacing, and auto-extract returns to
        // `downloading` for the image pass after transcription — so preview state
        // has to survive both.
        Object.assign(c, {
          status: data.status,
          previewAvailable: data.preview_available,
          folderName: data.folder_name || c.folderName,
        });
      } else if (data.status === 'done') {
        Object.assign(c, {
          status: 'done',
          filename: data.filename,
          filenames: data.filenames,
          fileCount: data.file_count,
          assetTypes: data.asset_types,
          detectedFormat: data.detected_format,
          limitReached: data.limit_reached,
          scriptStatus: data.script_status,
          scriptError: data.script_error,
          scriptFilename: data.script_filename,
          transcriptFilename: data.transcript_filename,
          folderName: data.folder_name,
          artifactPaths: data.artifact_paths,
          previewAvailable: data.preview_available,
        });
        scheduleLedgerRefresh();  // the receipt is on disk now — let the ledger say so
      } else if (data.status === 'transcribing') {
        // app.py finalizes the media before it starts transcribing, and says so
        // at /api/file. Carry the fields the player and folder chip need so the
        // file you already have is not held behind up to 30 minutes of Metal.
        Object.assign(c, {
          status: 'transcribing',
          filename: data.filename,
          scriptStatus: data.script_status,
          detectedFormat: data.detected_format,
          folderName: data.folder_name || c.folderName,
          previewAvailable: data.preview_available,
        });
      } else if (data.status === 'error') {
        if (!data.media_saved) {
          const handle = c.jobId;
          clearJobState(c);
          c.jobId = handle;
          c.errorKind = data.error_kind;
        }
        failCard(c, data.error);
        c.previewAvailable = Boolean(data.preview_available);
        c.artifactPaths = data.artifact_paths;
      }
      // renderCard is a teardown, not a diff: it empties the card and builds a
      // fresh <video>, so a repaint costs playback position and any button the
      // user is mid-click on. A poll that reports what the last one reported must
      // not pay that — the server holds one status for the whole of transcription
      // (up to 30 minutes) while preview_available is already true, and repainting
      // once a second would send the player back to 0:00 once a second for the
      // entire phase the inline preview exists for.
      if (jobStateSignature(c) !== before) renderCard(idx);
      return data.status === 'done' || data.status === 'error';
    }

    const POLL_INTERVAL_MS = 1000;
    const POLL_MISS_LIMIT = 5;  // consecutive transport failures before giving up
    let pollGeneration = 0;

    function pollCard(idx) {
      const c = cardData[idx];
      const jobId = c.jobId;
      stopPolling(c);  // a retry must not leave the previous job's poller running
      const gen = (c.pollGen = ++pollGeneration);
      c.pollMisses = 0;
      // This card still owns this job, and this chain is still the card's live
      // one: it was not wiped by a new Extract, not replaced at this index, not
      // retried onto a newer job, and not superseded by a later pollCard.
      const owns = () => cardData[idx] === c && c.jobId === jobId && c.pollGen === gen;

      // Self-rescheduling rather than setInterval, so at most one /api/status
      // request per card is ever outstanding. An interval does not wait for its
      // async callback: under the browser's six-connection cap two polls for the
      // same job can overlap and resolve out of order, and an older "downloading"
      // payload applied after "done" leaves a finished card on the spinner
      // forever. No ownership check can catch that — both payloads genuinely
      // belong to this card and this job. One in flight makes it unrepresentable.
      function again() {
        if (owns()) c.pollHandle = window.setTimeout(tick, POLL_INTERVAL_MS);
      }

      async function tick() {
        c.pollHandle = null;
        if (!owns()) return;
        try {
          const res = await fetch(`/api/status/${jobId}`);
          if (!owns()) return;  // re-check: the fetch awaited
          if (res.status === 404) {
            stopPolling(c);
            failCard(c, 'Job no longer exists on the server (it may have restarted)');
            renderCard(idx);
            return;
          }
          if (!res.ok) { again(); return; }  // transient server error — keep polling
          const data = await res.json();
          // Reading the body awaits too, so ownership is checked on the far side of
          // it as well — and applyStatus is handed the card, not just its index.
          if (!owns()) return;
          c.pollMisses = 0;
          if (applyStatus(idx, c, data)) { stopPolling(c); return; }
          again();
        } catch {
          if (!owns()) return;
          // One rejection is not a dead job. A closed lid, a wifi switch or a VPN
          // reconnect during a half-hour transcription all land here while the
          // server keeps working, so the card is only failed after several in a
          // row — and with failCardOffline, which does not retract a preview the
          // server never withdrew.
          c.pollMisses = (c.pollMisses || 0) + 1;
          if (c.pollMisses < POLL_MISS_LIMIT) { again(); return; }
          stopPolling(c);
          failCardOffline(c, 'Lost connection to server');
          renderCard(idx);
        }
      }

      c.pollHandle = window.setTimeout(tick, POLL_INTERVAL_MS);
    }

    // ── compress to fit · a receipt gains a second, smaller asset ──
    // Keyed by receipt id, never by card index or DOM node: the same receipt can
    // be on screen twice (live card and recent receipts), a history re-render
    // replaces its node, and the receipt outlives the job that made it. State
    // here is the single source both surfaces paint from.
    const COMPRESS_POLL_MS = 1200;
    const compressJobs = new Map();     // receipt id -> last /api/compress payload
    const compressPollGen = new Map();  // receipt id -> owning poll generation
    let compressGeneration = 0;

    function fmtBytes(bytes) {
      if (typeof bytes !== 'number' || !isFinite(bytes)) return '';
      const mb = bytes / (1024 * 1024);
      return mb >= 100 ? `${Math.round(mb)} MB` : `${mb.toFixed(1)} MB`;
    }

    // Progress lives in this node alone. renderCard and renderLedgerCard are
    // teardowns — repainting a whole card every poll would destroy the inline
    // <video> the same way an unchanged /api/status payload used to.
    function compressHost(id, receipt) {
      const host = el('span', 'compress-host');
      host.dataset.compressHost = id;
      // A compression recorded on the receipt in an earlier session needs no
      // poll and no request: the file is already on disk and named in the ledger.
      if (!compressJobs.has(id) && receipt && receipt.has_compressed) {
        const variants = Array.isArray(receipt.compressed_variants)
          ? receipt.compressed_variants : [];
        compressJobs.set(id, {
          status: 'done',
          filename: receipt.compressed_filename,
          size: receipt.compressed_size,
          target_mb: variants.length ? variants[variants.length - 1].target_mb : null,
          variants,
        });
      }
      paintCompress(host, id);
      return host;
    }

    function refreshCompress(id) {
      for (const host of document.querySelectorAll(`[data-compress-host="${id}"]`)) {
        paintCompress(host, id);
      }
    }

    function paintCompress(host, id) {
      const state = compressJobs.get(id) || { status: 'idle' };
      host.textContent = '';
      const chip = (label, onclick) => {
        const button = el('button', 'q-chip', label);
        button.onclick = onclick;
        host.appendChild(button);
        return button;
      };
      const spinner = (label) => {
        const node = el('span', 'card-status downloading');
        node.appendChild(el('span', 'spin'));
        node.appendChild(document.createTextNode(' ' + label));
        host.appendChild(node);
      };

      const variants = Array.isArray(state.variants) ? state.variants : [];
      const paintVariants = () => {
        if (!variants.length) {
          chip('Save compressed', () => viewArtifact(`/api/receipts/${id}/compressed`));
          const folderBtn = chip('Show in folder',
            () => showInFolder(`/api/receipts/${id}/compressed/reveal`, folderBtn));
          return;
        }
        for (const variant of variants) {
          const cap = variant.target_mb;
          const route = `/api/receipts/${id}/compressed/${cap}`;
          host.appendChild(el('span', 'card-status',
            `${cap} MB cap · ${fmtBytes(variant.size)}`));
          chip(variants.length === 1 ? 'Save compressed' : `Save ${cap} MB`,
            () => viewArtifact(route));
          const label = variants.length === 1 ? 'Show in folder' : `Show ${cap} MB`;
          const folderBtn = chip(label,
            () => showInFolder(`${route}/reveal`, folderBtn));
        }
      };

      if (state.status === 'picking') {
        const presets = state.presets || [];
        host.appendChild(el('span', 'card-status', 'Compress to'));
        for (const mb of presets) chip(`${mb} MB`, () => requestCompress(id, mb));
        chip('Custom', () => {
          compressJobs.set(id, { ...state, status: 'custom', custom_error: null });
          refreshCompress(id);
        });
        chip('Cancel', () => {
          if (variants.length) {
            const latest = variants[variants.length - 1];
            compressJobs.set(id, { ...state, ...latest, status: 'done', variants });
          } else {
            compressJobs.delete(id);
          }
          refreshCompress(id);
        });
      } else if (state.status === 'custom') {
        const min = Number.isInteger(state.target_min_mb) ? state.target_min_mb : 1;
        const max = Number.isInteger(state.target_max_mb) ? state.target_max_mb : 0;
        if (max < min) {
          host.appendChild(el('span', 'card-status', 'Already under 1 MB'));
        } else {
          host.appendChild(el('span', 'card-status', `Custom cap · ${min}–${max} MB`));
          const input = document.createElement('input');
          input.className = 'compress-input';
          input.type = 'number';
          input.min = String(min);
          input.max = String(max);
          input.step = '1';
          input.inputMode = 'numeric';
          input.placeholder = 'e.g. 12';
          input.value = state.custom_value || '';
          input.setAttribute('aria-label', 'Custom size in MB');
          host.appendChild(input);
          host.appendChild(el('span', 'card-status', 'MB'));
          const submit = () => {
            const target = Number(input.value);
            if (!Number.isInteger(target) || target < min || target > max) {
              compressJobs.set(id, {
                ...state, status: 'custom', custom_value: input.value,
                custom_error: `Enter a whole number from ${min} to ${max} MB`,
              });
              refreshCompress(id);
              return;
            }
            requestCompress(id, target);
          };
          chip('Compress', submit);
          input.onkeydown = (event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              submit();
            }
          };
          window.setTimeout(() => input.focus(), 0);
        }
        chip('Back', () => {
          compressJobs.set(id, { ...state, status: 'picking', custom_error: null });
          refreshCompress(id);
        });
        if (state.custom_error) {
          host.appendChild(el('span', 'card-status error', state.custom_error));
        }
      } else if (state.status === 'queued') {
        spinner(`Queued · ${state.target_mb} MB`);
      } else if (state.status === 'running') {
        spinner(`Compressing to ${state.target_mb} MB · pass ${state.pass || 1} of 2`);
      } else if (state.status === 'done') {
        const summary = variants.length > 1
          ? `Compressed · ${variants.length} sizes`
          : `Compressed · ${fmtBytes(state.size || (variants[0] || {}).size)}`;
        host.appendChild(el('span', 'card-status done', summary));
        paintVariants();
        chip('New size', () => openCompressPicker(id));
      } else if (state.status === 'error') {
        host.appendChild(el('span', 'card-status error',
          'Compress failed: ' + friendlyError(state.error || '')));
        if (variants.length) paintVariants();
        chip('Try again', () => openCompressPicker(id));
      } else {
        chip('Compress', () => openCompressPicker(id));
      }
    }

    // The caps come from the server, which knows the source's size: offering a
    // 512 MB cap for a 40 MB clip is offering a no-op.
    async function openCompressPicker(id) {
      try {
        const res = await fetch(`/api/compress/${id}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Could not read this receipt');
        if (data.status === 'queued' || data.status === 'running') {
          // Started elsewhere — another tab, or this page was reloaded mid-encode.
          compressJobs.set(id, data);
          refreshCompress(id);
          pollCompress(id);
          return;
        }
        compressJobs.set(id, { ...data, status: 'picking' });
      } catch (err) {
        compressJobs.set(id, { status: 'error', error: err.message || 'Could not read this receipt' });
      }
      refreshCompress(id);
    }

    async function requestCompress(id, targetMb) {
      const previous = compressJobs.get(id) || {};
      compressJobs.set(id, {
        ...previous, status: 'queued', target_mb: targetMb, pass: null, error: null,
      });
      refreshCompress(id);
      try {
        const data = await postJson(`/api/compress/${id}`, { target_mb: targetMb });
        if (data.error) throw new Error(data.error);
        compressJobs.set(id, data);
        refreshCompress(id);
        pollCompress(id);
      } catch (err) {
        compressJobs.set(id, {
          ...previous, status: 'error', target_mb: targetMb,
          error: err.message || 'Could not start compressing',
        });
        refreshCompress(id);
      }
    }

    // Same shape as pollCard: self-rescheduling so one request is in flight at a
    // time, and a generation token so a superseded poll cannot paint over the
    // compression that replaced it. An encode runs for minutes, which is long
    // enough for a lid to close, so transport failures are counted, not fatal.
    function pollCompress(id) {
      const gen = ++compressGeneration;
      compressPollGen.set(id, gen);
      let misses = 0;
      const owns = () => compressPollGen.get(id) === gen;
      const again = () => { if (owns()) window.setTimeout(tick, COMPRESS_POLL_MS); };

      async function tick() {
        if (!owns()) return;
        try {
          const res = await fetch(`/api/compress/${id}`);
          if (!owns()) return;  // re-check: the fetch awaited
          if (!res.ok) { again(); return; }  // transient, or a receipt mid-rewrite
          const data = await res.json();
          if (!owns()) return;
          misses = 0;
          compressJobs.set(id, data);
          refreshCompress(id);
          if (data.status === 'done' || data.status === 'error') {
            compressPollGen.delete(id);
            return;
          }
          again();
        } catch {
          if (!owns()) return;
          misses += 1;
          if (misses < POLL_MISS_LIMIT) { again(); return; }
          compressPollGen.delete(id);
          compressJobs.set(id, {
            ...(compressJobs.get(id) || {}),
            status: 'error', error: 'Lost connection to server',
          });
          refreshCompress(id);
        }
      }

      again();
    }

    // ── ledger · past receipts, straight from disk ──
    function fmtDate(epochSeconds) {
      if (!epochSeconds) return '';
      return new Date(epochSeconds * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    }

    function ledgerEyebrow(r) {
      let saved = 'Saved · media';
      if (r.detected_format === 'mixed') saved = `Saved · ${r.file_count} mixed assets`;
      else if (r.detected_format === 'image') saved = `Saved · ${r.file_count || 1} image asset${r.file_count === 1 ? '' : 's'}`;
      else if (r.detected_format === 'audio') saved = 'Saved · audio';
      else if (r.has_script) saved = r.has_transcript ? 'Saved · media · script · timestamps' : 'Saved · media · script';
      const date = fmtDate(r.saved_at);
      return date ? `${saved} · ${date}` : saved;
    }

    function renderLedgerCard(r) {
      const card = el('div', 'card');

      const body = el('div', 'card-body');
      body.appendChild(el('div', 'card-eyebrow', ledgerEyebrow(r)));
      body.appendChild(el('div', 'card-title', r.title || r.filename || 'Untitled'));
      const creator = creatorLabel(r.uploader, r.title);
      body.appendChild(el('div', 'card-meta', creator + (r.duration ? ' · ' + fmtDur(r.duration) : '')));

      const actions = el('div', 'card-actions');
      const detail = el('a', 'q-chip', 'Inspect asset');
      detail.href = `#asset/${r.id}`; actions.appendChild(detail);
      if (r.has_preview) {
        const videoBtn = el('button', 'card-dl-btn done', 'Play video');
        videoBtn.onclick = () => viewArtifact(`/api/receipts/${r.id}/preview`);
        actions.appendChild(videoBtn);
      }
      const folderBtn = el('button', 'q-chip', 'Show in folder');
      folderBtn.onclick = () => showInFolder(`/api/receipts/${r.id}/reveal`, folderBtn);
      actions.appendChild(folderBtn);
      if (r.has_script) {
        const scriptBtn = el('button', 'card-dl-btn script', 'View transcript');
        scriptBtn.onclick = () => viewArtifact(`/api/receipts/${r.id}/script?view=1`);
        actions.appendChild(scriptBtn);
      }
      if (r.has_transcript) {
        const tsBtn = el('button', 'q-chip', 'View timestamps');
        tsBtn.onclick = () => viewArtifact(`/api/receipts/${r.id}/transcript?view=1`);
        actions.appendChild(tsBtn);
      }
      if (r.has_preview && r.package_status !== 'error') actions.appendChild(compressHost(r.id, r));
      if (r.package_status === 'error') {
        const repair = el('a', 'q-chip', 'Repair from queue'); repair.href = '#'; actions.appendChild(repair);
        actions.appendChild(el('span', 'card-status error', 'Media saved · receipt needs repair'));
      } else actions.appendChild(el('span', 'card-status done', `Auto-saved · ${r.folder_name || 'Downloads'}`));
      body.appendChild(actions);
      if (searchExcerpts.get(r.id)) body.appendChild(el('p', 'search-excerpt', searchExcerpts.get(r.id)));

      const thumb = el('div', 'card-thumb');
      if (r.has_preview) {
        thumb.classList.add('card-video');
        const video = document.createElement('video');
        video.src = `/api/receipts/${r.id}/preview`;
        video.controls = true;
        video.preload = 'metadata';
        video.setAttribute('playsinline', '');
        thumb.appendChild(video);
      } else if (r.thumbnail && /^https?:\/\//i.test(r.thumbnail)) {
        const img = document.createElement('img');
        img.src = r.thumbnail;
        img.alt = '';
        img.loading = 'lazy'; img.referrerPolicy = 'no-referrer';
        thumb.appendChild(img);
      } else {
        const noThumb = el('div', 'no-thumb');
        noThumb.appendChild(svgIcon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20"/><circle cx="8" cy="8" r="1.5"/><path d="m21 15-5-5L5 21"/></svg>'));
        thumb.appendChild(noThumb);
      }

      card.appendChild(body);
      card.appendChild(thumb);
      return card;
    }

    function receiptSource(url) {
      try { return new URL(url).hostname.replace(/^www\./, ''); }
      catch { return 'Unknown source'; }
    }

    function populateHistorySelect(id, values, emptyLabel) {
      const select = document.getElementById(id);
      const current = select.value;
      select.textContent = '';
      const all = document.createElement('option');
      all.value = '';
      all.textContent = emptyLabel;
      select.appendChild(all);
      for (const value of values) {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = value;
        select.appendChild(option);
      }
      if (values.includes(current)) select.value = current;
    }

    function populateHistoryFilters() {
      const creators = [...new Set(ledgerReceipts.map(r => r.uploader).filter(Boolean))]
        .sort((a, b) => a.localeCompare(b));
      const sources = [...new Set(ledgerReceipts.map(r => receiptSource(r.url)).filter(Boolean))]
        .sort((a, b) => a.localeCompare(b));
      populateHistorySelect('historyCreator', creators, 'All creators');
      populateHistorySelect('historySource', sources, 'All sources');
    }

    function filteredHistoryReceipts() {
      const query = document.getElementById('historySearch').value.trim().toLocaleLowerCase();
      const creator = document.getElementById('historyCreator').value;
      const source = document.getElementById('historySource').value;
      const type = document.getElementById('historyType').value;
      return ledgerReceipts.filter(r => {
        const searchable = [r.title, r.uploader, r.filename, r.url]
          .filter(Boolean).join(' ').toLocaleLowerCase();
        return (!query || searchable.includes(query) || searchExcerpts.has(r.id))
          && (!creator || r.uploader === creator)
          && (!source || receiptSource(r.url) === source)
          && (!type || r.detected_format === type);
      });
    }

    function renderRecentReceipts() {
      const section = document.getElementById('ledger');
      const holder = document.getElementById('ledgerCards');
      holder.textContent = '';
      section.hidden = ledgerReceipts.length === 0;
      for (const receipt of ledgerReceipts.slice(0, RECENT_RECEIPT_LIMIT)) {
        holder.appendChild(renderLedgerCard(receipt));
      }
      document.getElementById('viewAllHistory').textContent = `View all history · ${ledgerReceipts.length}`;
    }

    function renderHistory(reset = false) {
      if (reset) historyVisibleCount = HISTORY_PAGE_SIZE;
      const matches = filteredHistoryReceipts();
      const visible = matches.slice(0, historyVisibleCount);
      const holder = document.getElementById('historyCards');
      holder.textContent = '';
      for (const receipt of visible) holder.appendChild(renderLedgerCard(receipt));

      const empty = document.getElementById('historyEmpty');
      empty.hidden = matches.length !== 0 || !document.getElementById('libraryError').hidden;
      empty.textContent = ledgerReceipts.length
        ? 'No receipts match those filters.'
        : 'No saved receipts yet.';

      const loadMore = document.getElementById('historyLoadMore');
      loadMore.hidden = visible.length >= matches.length;
      document.getElementById('historySummary').textContent = matches.length === ledgerReceipts.length
        ? `${ledgerReceipts.length} saved receipt${ledgerReceipts.length === 1 ? '' : 's'} · newest first`
        : `${matches.length} matching receipt${matches.length === 1 ? '' : 's'} · ${ledgerReceipts.length} total`;
    }

    function syncHistoryRoute() {
      const historyOpen = window.location.hash === '#history';
      const assetMatch = window.location.hash.match(/^#asset\/([0-9a-f]{10})$/);
      document.body.classList.toggle('asset-open', Boolean(assetMatch));
      document.getElementById('assetView').hidden = !assetMatch;
      document.getElementById('intakeView').hidden = historyOpen || Boolean(assetMatch);
      if (assetMatch) loadAsset(assetMatch[1]);
      else document.querySelectorAll('#assetPlayer video, #assetPlayer audio').forEach(player => player.pause());
      document.getElementById('historyView').hidden = !historyOpen;
      document.getElementById('coverSub').textContent = historyOpen
        ? 'Every saved receipt, ready when you need it.'
        : 'Paste a link. Get every source asset.';
      const nav = document.getElementById('historyNav');
      if (historyOpen) nav.setAttribute('aria-current', 'page');
      else nav.removeAttribute('aria-current');
      document.title = historyOpen
        ? 'History — FrameGrab'
        : 'FrameGrab — TheVibeFounder clip intake';
      if (historyOpen) renderHistory();
      // A poller is not the only way a receipt gets written: a new Extract stops
      // the pollers of jobs that are still running server-side, and those jobs
      // still file their receipts. applyStatus's "done" branch is otherwise the
      // only refresh trigger, so without this a switch to History would show a
      // list that has been stale since the last reload.
      scheduleLedgerRefresh();
      window.scrollTo(0, 0);
    }

    let ledgerRefreshTimer = null;

    async function loadLedger(reset = true) {
      // Any pending coalesced refresh is satisfied by this fetch.
      if (ledgerRefreshTimer) {
        clearTimeout(ledgerRefreshTimer);
        ledgerRefreshTimer = null;
      }
      try {
        const response = await fetch('/api/receipts');
        if (!response.ok) throw new Error('History could not be loaded. Saved files remain on disk.');
        ledgerReceipts = (await response.json()).receipts || [];
        document.getElementById('libraryError').hidden = true;
        document.getElementById('libraryRetry').hidden = true;
      } catch (error) {
        document.getElementById('libraryError').textContent = error.message;
        document.getElementById('libraryError').hidden = false;
        document.getElementById('libraryRetry').hidden = false;
      }
      renderRecentReceipts();
      populateHistoryFilters();
      renderHistory(reset);
    }

    function scheduleLedgerRefresh() {
      // Coalescing only, not a wait for the write: app.py sets a job to "done"
      // after its ledger line is on disk, so any delay here is safe and a delay of
      // zero would be too. The window exists because a multi-link extract finishes
      // several jobs at once and they should cost one fetch, not one each.
      // reset=false so a Load more or an active filter in History survives.
      if (ledgerRefreshTimer) clearTimeout(ledgerRefreshTimer);
      ledgerRefreshTimer = window.setTimeout(() => {
        ledgerRefreshTimer = null;
        loadLedger(false);
      }, 400);
    }

    document.getElementById('historySearch').addEventListener('input', searchTranscript);
    document.getElementById('libraryRetry').onclick = () => loadLedger();
    for (const id of ['historyCreator', 'historySource', 'historyType']) {
      document.getElementById(id).addEventListener('change', () => renderHistory(true));
    }
    document.getElementById('historyLoadMore').addEventListener('click', () => {
      historyVisibleCount += HISTORY_PAGE_SIZE;
      renderHistory();
    });
    window.addEventListener('hashchange', syncHistoryRoute);
    syncHistoryRoute();
    loadLedger();
    restoreJobs();
