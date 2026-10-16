// FrameGrab workspace. Copyright (c) 2026 TheVibeFounder.
const searchExcerpts = new Map();
let searchGeneration = 0;
let searchTimer;
let assetGeneration = 0;

function updateQueueSummary() {
  const visible = cardData.filter(c => !c.dismissed);
  const active = visible.filter(c => !['done', 'existing', 'error', 'info-error'].includes(c.status)).length;
  const errors = visible.filter(c => ['error', 'info-error'].includes(c.status)).length;
  document.getElementById('queueSummary').textContent = visible.length
    ? `${active} active · ${errors} need attention · ${visible.length} submitted` : '';
}

async function findExistingReceipt(idx) {
  const card = cardData[idx];
  try {
    const response = await fetch(`/api/library?url=${encodeURIComponent(card.url)}&format=${encodeURIComponent(card.format)}`);
    if (!response.ok) return false;
    const existing = (await response.json()).results?.[0];
    if (!existing) return false;
    Object.assign(card, {status:'existing', existingId:existing.id, title:existing.title, uploader:existing.creator});
    renderCard(idx);
    return true;
  } catch { return false; }
}

async function restoreJobs() {
  try {
    const response = await fetch('/api/jobs');
    if (!response.ok) throw new Error('The queue could not be restored. Reload to reconnect.');
    const data = await response.json();
    for (const job of data.jobs || []) {
      if (cardData.some(c => c.jobId === job.job_id)) continue;
      const idx = cardData.length;
      const card = {url:job.url, format:job.format, title:job.title, uploader:job.uploader,
        duration:job.duration, thumbnail:job.thumbnail, jobId:job.job_id, status:job.status,
        error:job.error, errorKind:job.error_kind, requestId:job.request_key,
        mediaSaved:Boolean(job.file), previewAvailable:Boolean(job.preview),
        cancelRequested:job.cancel_requested};
      cardData.push(card);
      renderCard(idx);
      if (!['done', 'error'].includes(job.status)) pollCard(idx);
    }
    updateQueueSummary();
  } catch (error) {
    document.getElementById('queueSummary').textContent = error.message;
  }
}

async function retryExisting(idx) {
  const card = cardData[idx];
  stopPolling(card);
  try {
    // A disconnected browser reconnects to an active job instead of resubmitting.
    const response = await fetch(`/api/status/${card.jobId}`);
    const status = await response.json();
    if (!response.ok) {
      card.errorKind = 'unknown';
      card.requestId = crypto.randomUUID();
      return dlCard(idx);
    }
    if (status.status !== 'error') {
      if (!applyStatus(idx, card, status)) pollCard(idx);
      return;
    }
    await postJson(`/api/jobs/${card.jobId}/retry`, {});
    card.status = 'queued'; card.error = null; card.cancelRequested = false;
    renderCard(idx); pollCard(idx);
  } catch (error) {
    card.status = 'error'; card.error = error.message; renderCard(idx);
  }
}

function searchTranscript() {
  const generation = ++searchGeneration;
  clearTimeout(searchTimer);
  searchExcerpts.clear();
  renderHistory(true);
  const query = document.getElementById('historySearch').value.trim();
  if (!query) return;
  searchTimer = setTimeout(async () => {
    try {
      let offset = 0;
      const collected = [];
      while (true) {
        const response = await fetch(`/api/library?q=${encodeURIComponent(query)}&limit=200&offset=${offset}`);
        if (!response.ok) throw new Error('Transcript search is unavailable. Title and creator search still work.');
        const data = await response.json();
        if (generation !== searchGeneration) return;
        collected.push(...data.results);
        offset += data.results.length;
        if (!data.results.length || offset >= data.total) break;
      }
      for (const result of collected) searchExcerpts.set(result.id, result.excerpt);
      renderHistory(true);
    } catch (error) {
      if (generation !== searchGeneration) return;
      const notice = document.getElementById('libraryError');
      notice.textContent = error.message; notice.hidden = false;
    }
  }, 200);
}

async function loadAsset(id) {
  const generation = ++assetGeneration;
  const notice = document.getElementById('assetNotice');
  notice.textContent = 'Loading saved asset…';
  document.querySelectorAll('#assetPlayer video, #assetPlayer audio').forEach(player => player.pause());
  for (const name of ['assetPlayer', 'assetActions', 'assetSources', 'assetFrames', 'assetVariants', 'assetTranscript']) {
    document.getElementById(name).replaceChildren();
  }
  try {
    const response = await fetch(`/api/receipts/${id}/detail`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not load this asset.');
    if (generation !== assetGeneration || location.hash !== `#asset/${id}`) return;
    document.getElementById('assetTitle').textContent = data.title || 'Untitled';
    document.getElementById('assetMeta').textContent = `${data.creator || 'Unknown creator'} · ${data.assets.length} source asset${data.assets.length === 1 ? '' : 's'}`;
    document.title = `${data.title || 'Asset'} — FrameGrab`;
    notice.textContent = '';
    let selected;
    const playerHost = document.getElementById('assetPlayer');
    const actions = document.getElementById('assetActions');
    const capture = el('button', '', 'Capture this frame');
    const select = (asset) => {
      playerHost.querySelectorAll('video,audio').forEach(player => player.pause());
      playerHost.replaceChildren();
      selected = asset;
      capture.hidden = !(data.can_capture && asset.kind === 'video' && asset.index === data.assets.find(a => a.kind === 'video')?.index);
      if (['video', 'audio'].includes(asset.kind)) {
        const player = document.createElement(asset.kind);
        player.controls = true; player.preload = 'metadata'; player.src = asset.url;
        player.setAttribute('playsinline', '');
        playerHost.appendChild(player);
      } else if (asset.kind === 'image') {
        const img = document.createElement('img'); img.src = asset.url; img.alt = asset.filename;
        playerHost.appendChild(img);
      } else {
        const link = el('a', 'asset-link', `Open ${asset.filename}`); link.href = asset.url;
        playerHost.appendChild(link);
      }
    };
    for (const asset of data.assets) {
      const button = el('button', '', `${asset.index}. ${asset.kind}`);
      button.title = asset.filename; button.onclick = () => select(asset);
      document.getElementById('assetSources').appendChild(button);
    }
    if (data.assets.length) select(data.assets.find(a => a.kind === 'video') || data.assets[0]);
    const reveal = el('button', '', 'Show in folder');
    reveal.onclick = () => showInFolder(`/api/receipts/${id}/reveal`, reveal);
    actions.appendChild(reveal);
    if (/^https?:\/\//i.test(data.source_url || '')) {
      const source = el('a', 'asset-link', 'Original source'); source.href = data.source_url;
      source.target = '_blank'; source.rel = 'noopener noreferrer'; actions.appendChild(source);
    }
    const framesHost = document.getElementById('assetFrames');
    const addFrame = (frame) => {
      const existing = [...framesHost.querySelectorAll('a')].find(a => a.dataset.filename === frame.filename);
      if (existing) existing.remove();
      framesHost.querySelector('.hint')?.remove();
      const link = el('a', '', frame.filename); link.href = frame.url; link.target = '_blank'; link.rel = 'noopener';
      link.dataset.filename = frame.filename;
      const image = document.createElement('img'); image.src = frame.url; image.alt = frame.filename; image.loading = 'lazy';
      link.prepend(image); framesHost.appendChild(link);
    };
    for (const frame of data.frames) addFrame(frame);
    if (!data.frames.length) framesHost.appendChild(el('p', 'hint', 'Pause the video at a useful moment, then capture a frame.'));
    capture.onclick = async () => {
      const player = playerHost.querySelector('video');
      if (!player) return;
      capture.disabled = true; notice.textContent = 'Saving this frame…';
      try {
        const frame = await postJson(`/api/frames/${id}`, {time:player.currentTime});
        if (generation !== assetGeneration || location.hash !== `#asset/${id}`) return;
        addFrame(frame); notice.textContent = `Saved ${frame.filename} in this package’s Frames folder.`;
      } catch (error) { if (generation === assetGeneration) notice.textContent = error.message; }
      finally { capture.disabled = false; }
    };
    actions.appendChild(capture);
    const transcript = document.getElementById('assetTranscript');
    for (const segment of data.segments) {
      const button = el('button', 'transcript-line');
      button.appendChild(el('span', 'transcript-time', fmtDur(segment.start) || '0:00'));
      button.appendChild(document.createTextNode(segment.text));
      button.onclick = () => {
        const primary = data.assets.find(a => a.kind === 'video');
        if (!primary) return;
        if (selected !== primary) select(primary);
        const player = playerHost.querySelector('video');
        player.currentTime = segment.start;
        player.play().catch(() => {});
      };
      transcript.appendChild(button);
    }
    if (!data.segments.length) transcript.appendChild(el('p', '', data.script || 'No transcript is available for this asset.'));
    const variants = document.getElementById('assetVariants');
    for (const variant of data.variants) {
      const link = el('a', 'asset-link', `${variant.target_mb} MB version`);
      link.href = `/api/receipts/${id}/compressed/${variant.target_mb}`; variants.appendChild(link);
    }
    if (!data.variants.length) variants.appendChild(el('p', 'hint', 'No compressed versions yet. Use Compress on the saved receipt to create one.'));
  } catch (error) {
    if (generation === assetGeneration) notice.textContent = error.message;
  }
}
