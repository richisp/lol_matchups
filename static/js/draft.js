// ---- AJAX refresh: re-render the draft form in place instead of
//      doing a full-page navigation. Preserves scroll position and
//      avoids the constant reload churn during champ select.
let inflight = null;
// Lane currently being dragged in the enemy team (null when no drag is active).
// Declared up here because pollLcu() — invoked immediately below — reads it.
let dragSrcLane = null;
async function refreshDraft() {
    if (inflight) inflight.abort();
    const ctrl = new AbortController();
    inflight = ctrl;

    const form = document.getElementById('draft-form');
    const params = new URLSearchParams();
    // FormData picks up controls associated via form="draft-form" too
    // (e.g. the tier select). Last-wins on duplicate keys.
    for (const [k, v] of new FormData(form)) {
        if (v !== '') params.set(k, v);
    }
    const url = `/draft?${params.toString()}`;
    history.replaceState({}, '', url);

    // Preserve recs scroll across the swap.
    const oldScroll = document.querySelector('.recs-scroll');
    const savedScrollTop = oldScroll ? oldScroll.scrollTop : 0;

    // Preserve focus + cursor position. Otherwise an LCU-driven swap
    // happening while the user is clicking/typing in an input will
    // blow away their focus, making clicks feel unresponsive.
    const focused = document.activeElement;
    let focusInfo = null;
    if (focused && form.contains(focused) && focused.name) {
        focusInfo = { name: focused.name };
        if (typeof focused.selectionStart === 'number') {
            focusInfo.selStart = focused.selectionStart;
            focusInfo.selEnd = focused.selectionEnd;
        }
    }

    try {
        const r = await fetch(url, { cache: 'no-store', signal: ctrl.signal });
        if (!r.ok) return;
        const html = await r.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const newForm = doc.getElementById('draft-form');
        if (newForm) {
            form.replaceWith(newForm);
            const newScroll = document.querySelector('.recs-scroll');
            if (newScroll) newScroll.scrollTop = savedScrollTop;
            if (focusInfo) {
                const target = newForm.querySelector(`[name="${focusInfo.name}"]`);
                if (target) {
                    target.focus();
                    if (focusInfo.selStart != null && target.setSelectionRange) {
                        try { target.setSelectionRange(focusInfo.selStart, focusInfo.selEnd); }
                        catch (_) { /* not all input types support selection */ }
                    }
                }
            }
        }
    } catch (e) {
        if (e.name !== 'AbortError') console.error('refresh error', e);
    } finally {
        if (inflight === ctrl) inflight = null;
    }
}

// Event delegation: handlers stay live after refreshDraft swaps the form.
const RECS_NATURAL_DESC = new Set(['fit', 'winrate', 'counter', 'synergy', 'roles', 'lane_share', 'comp']);

document.addEventListener('click', (e) => {
    const slotPick = e.target.closest('.slot-pick');
    if (slotPick) {
        e.preventDefault();
        document.getElementById('active-input').value = slotPick.dataset.active;
        refreshDraft();
        return;
    }
    const recPick = e.target.closest('.rec-pick');
    if (recPick) {
        e.preventDefault();
        const active = document.getElementById('active-input').value;
        const slot = document.querySelector(`input[name="my_${active}"]`);
        if (slot) {
            slot.value = recPick.dataset.name;
            refreshDraft();
        }
        return;
    }
    const sortHeader = e.target.closest('.recs-table th[data-sort]');
    if (sortHeader) {
        e.preventDefault();
        const col = sortHeader.dataset.sort;
        const inp = document.getElementById('rec-sort-input');
        const current = inp ? inp.value : '';
        // Sort is always signed (+col / -col). Toggling means flipping the sign;
        // first-time click on a column uses the column's natural default.
        let next;
        if (current === '+' + col)      next = '-' + col;
        else if (current === '-' + col) next = '+' + col;
        else                            next = RECS_NATURAL_DESC.has(col) ? '-' + col : '+' + col;
        if (inp) inp.value = next;
        refreshDraft();
    }
});
document.addEventListener('change', (e) => {
    if (e.target.matches('#draft-form input[type="text"], select[name="tier"]')) {
        refreshDraft();
    }
});
document.addEventListener('submit', (e) => {
    if (e.target.id === 'draft-form') {
        e.preventDefault();
        refreshDraft();
    }
});

// ---- Hover tooltip: per-row breakdown of fit-score contributors ----
const recTooltip = document.getElementById('rec-tooltip');
let recHoverRow = null;

function positionRecTooltip(row) {
    const rect = row.getBoundingClientRect();
    // Reveal hidden first so we can measure.
    recTooltip.hidden = false;
    const tip = recTooltip.getBoundingClientRect();
    const margin = 12;
    // Default: place to the right of the row.
    let left = rect.right + margin;
    if (left + tip.width > window.innerWidth - 8) {
        // Not enough room on the right; fall back to the left.
        left = rect.left - tip.width - margin;
        if (left < 8) left = 8;
    }
    // Vertically center on the row, clamped to viewport.
    let top = rect.top + rect.height / 2 - tip.height / 2;
    top = Math.max(8, Math.min(top, window.innerHeight - tip.height - 8));
    recTooltip.style.left = `${left}px`;
    recTooltip.style.top = `${top}px`;
}

document.addEventListener('mouseover', (e) => {
    // Either a recommendation row OR an already-picked-champion slot —
    // both stash a `.rec-breakdown-src` div with the rendered tooltip body.
    const row = e.target.closest('.rec-row, .slot');
    if (!row || row === recHoverRow) return;
    const src = row.querySelector('.rec-breakdown-src');
    if (!src || !src.innerHTML.trim()) return;
    recTooltip.innerHTML = src.innerHTML;
    recHoverRow = row;
    positionRecTooltip(row);
});
document.addEventListener('mouseout', (e) => {
    if (!recHoverRow) return;
    // Still inside the same row? Ignore — only hide on real exit.
    if (e.relatedTarget && recHoverRow.contains(e.relatedTarget)) return;
    recTooltip.hidden = true;
    recHoverRow = null;
});
// The recs list scrolls; hide the (now-stale-positioned) tooltip on scroll.
document.addEventListener('scroll', () => {
    if (!recTooltip.hidden) {
        recTooltip.hidden = true;
        recHoverRow = null;
    }
}, true);

// ---- LCU auto-sync ----
const lcuStatus = document.getElementById('lcu-status');
const lcuToggle = document.getElementById('lcu-sync');

function setBadge(text, cls) {
    lcuStatus.textContent = text;
    lcuStatus.className = 'lcu-badge ' + cls;
}

// Track what LCU last reported for each slot. The auto-sync only
// overwrites a slot when the form value still matches what LCU last
// wrote (or is empty). Anything else means the user typed an override
// — leave it alone until they clear the field.
// Pre-seed `lane` with the form's current active value so the server's
// default of BOT isn't treated as a user override against LCU's
// actual my_lane on the first sync.
const lcuApplied = {
    my: {},
    enemy: {},
    lane: (document.getElementById('active-input') || {}).value || '',
};

function syncSlot(inp, lcuValue, lastApplied) {
    const overridden = inp.value !== '' && inp.value !== lastApplied;
    if (overridden || inp.value === lcuValue) return false;
    inp.value = lcuValue;
    return true;
}

// Were we in champ select on the previous poll? Used to detect the *start* of
// a new champ select (the rising edge) so we wipe last game's board exactly
// once, right as a new draft begins — never during or after the game itself.
let wasInChampSelect = false;

// Clear inputs + override tracking. Does NOT refresh on its own — the caller
// re-renders (usually the same poll's sync pass immediately repopulates it).
// Clearing the tracking is what lets the next game's LCU picks land: an
// overridden slot (inp.value !== lastApplied) would otherwise stay sticky.
function clearDraftBoard() {
    const form = document.getElementById('draft-form');
    if (!form) return false;
    let changed = false;
    for (const inp of form.querySelectorAll('input[type="text"], input[name="my_bans"], input[name="enemy_bans"]')) {
        if (inp.value !== '') { inp.value = ''; changed = true; }
    }
    lcuApplied.my = {};
    lcuApplied.enemy = {};
    lcuApplied.my_bans = '';
    lcuApplied.enemy_bans = '';
    lcuApplied.lane = (document.getElementById('active-input') || {}).value || '';
    return changed;
}

// Human-readable badge for phases outside champ select. We deliberately keep
// the board populated across all of these — the user wants to keep seeing who
// counters them during the game — and only wipe when the next champ select
// starts (see the rising-edge handling in pollLcu).
function badgeForNonChampSelect(phase) {
    if (phase === 'InProgress') return ['LCU: in game (draft kept)', 'lcu-idle'];
    if (phase === 'WaitingForStats' || phase === 'PreEndOfGame' || phase === 'EndOfGame')
        return ['LCU: game over (draft kept)', 'lcu-idle'];
    if (phase === 'Reconnect') return ['LCU: reconnecting', 'lcu-idle'];
    return ['LCU: not in champ select', 'lcu-idle'];
}

async function pollLcu() {
    // A form swap mid-drag would cancel the drag (the source node is removed).
    // Skip this tick; the next one (2s later) re-syncs.
    if (dragSrcLane != null) return;
    try {
        const tierEl = document.querySelector('select[name="tier"]');
        const tier = tierEl ? tierEl.value : '';
        const lcuUrl = tier
            ? `/api/lcu?tier=${encodeURIComponent(tier)}`
            : '/api/lcu';
        const r = await fetch(lcuUrl, { cache: 'no-store' });
        const d = await r.json();

        if (!d.connected) {
            setBadge('LCU: client not running', 'lcu-off');
            wasInChampSelect = false;
            return;
        }
        if (!d.in_champ_select) {
            const [text, cls] = badgeForNonChampSelect(d.phase || '');
            setBadge(text, cls);
            // NOTE: intentionally do not clear the board here — it stays visible
            // through the game and is only wiped when the next draft starts.
            wasInChampSelect = false;
            return;
        }
        setBadge('LCU: in champ select', 'lcu-live');

        // Rising edge into a fresh champ select: wipe the previous game's board
        // so stale picks/bans don't linger. Only when auto-sync owns the board.
        if (!wasInChampSelect && lcuToggle.checked) clearDraftBoard();
        wasInChampSelect = true;

        if (!lcuToggle.checked) return;

        const form = document.getElementById('draft-form');
        let dirty = false;
        const positions = ['TOP', 'JUNGLE', 'MID', 'BOT', 'SUPPORT'];

        for (const [prefix, team, applied] of [
            ['my',    d.my_team    || {}, lcuApplied.my],
            ['enemy', d.enemy_team || {}, lcuApplied.enemy],
        ]) {
            for (const pos of positions) {
                const inp = form.querySelector(`input[name="${prefix}_${pos}"]`);
                if (!inp) continue;
                const lcuValue = team[pos] || '';
                if (syncSlot(inp, lcuValue, applied[pos] || '')) dirty = true;
                applied[pos] = lcuValue;
            }
        }

        // Per-team bans drive both the visual icon rows and scoring (the
        // server unions them to exclude banned champs from candidates).
        for (const [side, listKey] of [['my', 'my_bans'], ['enemy', 'enemy_bans']]) {
            const inp = form.querySelector(`input[name="${listKey}"]`);
            if (!inp) continue;
            const lcuStr = ((d[listKey] || [])).join(',');
            const last = lcuApplied[listKey] || '';
            if (lcuStr !== last && lcuStr !== inp.value) {
                inp.value = lcuStr;
                dirty = true;
            }
            lcuApplied[listKey] = lcuStr;
        }

        const activeInp = document.getElementById('active-input');
        if (activeInp && d.my_lane) {
            if (syncSlot(activeInp, d.my_lane, lcuApplied.lane)) dirty = true;
            lcuApplied.lane = d.my_lane;
        }

        if (dirty) refreshDraft();
    } catch (e) {
        console.error('LCU poll error', e);
    }
}

setInterval(pollLcu, 2000);
pollLcu();

// ---- Settings: manual League install path ----
// Lets a user whose client lives somewhere non-standard point the LCU
// integration at the right folder (the one holding `lockfile`).
const settingsToggle = document.getElementById('settings-toggle');
const settingsPanel = document.getElementById('settings-panel');
const leaguePathInput = document.getElementById('league-path-input');
const leaguePathSave = document.getElementById('league-path-save');
const settingsStatus = document.getElementById('settings-status');

function renderSettingsStatus(d) {
    if (!settingsStatus) return;
    if (d.lockfile_found) {
        settingsStatus.textContent = '✓ Client found: ' + d.lockfile_path;
        settingsStatus.className = 'settings-status ok';
    } else {
        settingsStatus.textContent = '✗ No lockfile found — set the folder above, or start the client.';
        settingsStatus.className = 'settings-status err';
    }
}

async function loadSettings() {
    try {
        const r = await fetch('/api/settings', { cache: 'no-store' });
        const d = await r.json();
        if (leaguePathInput && document.activeElement !== leaguePathInput) {
            leaguePathInput.value = d.league_path || '';
        }
        renderSettingsStatus(d);
    } catch (_) { /* offline / not frozen — ignore */ }
}

async function saveSettings() {
    if (!leaguePathInput) return;
    if (settingsStatus) {
        settingsStatus.textContent = 'Saving…';
        settingsStatus.className = 'settings-status';
    }
    try {
        const r = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ league_path: leaguePathInput.value.trim() }),
        });
        renderSettingsStatus(await r.json());
        pollLcu();  // re-check LCU immediately with the new path
    } catch (_) {
        if (settingsStatus) {
            settingsStatus.textContent = 'Save failed';
            settingsStatus.className = 'settings-status err';
        }
    }
}

if (settingsToggle) settingsToggle.addEventListener('click', () => {
    settingsPanel.hidden = !settingsPanel.hidden;
    if (!settingsPanel.hidden) loadSettings();
});
if (leaguePathSave) leaguePathSave.addEventListener('click', saveSettings);
if (leaguePathInput) leaguePathInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); saveSettings(); }
});

// ---- Drag-and-drop: swap two enemy champions' lanes ----
// Enemy lanes from the LCU are often inferred and can be wrong. Dragging one
// enemy portrait onto another lane swaps the two `enemy_<LANE>` inputs, then
// refreshDraft() re-renders everything (recs, scores, risk) for the new
// assignment. The swap survives the 2s LCU poll because syncSlot() treats a
// value that differs from what LCU last wrote as a user override.
// (`dragSrcLane` is declared near the top of the file — see note there.)

function clearDropMarks() {
    document.querySelectorAll('.enemy-slot.drop-target, .enemy-slot.drag-source')
        .forEach(s => s.classList.remove('drop-target', 'drag-source'));
}

function swapEnemyLanes(a, b) {
    if (!a || !b || a === b) return;
    const form = document.getElementById('draft-form');
    const ia = form.querySelector(`input[name="enemy_${a}"]`);
    const ib = form.querySelector(`input[name="enemy_${b}"]`);
    if (!ia || !ib) return;
    const tmp = ia.value;
    ia.value = ib.value;
    ib.value = tmp;
    refreshDraft();
}

document.addEventListener('dragstart', (e) => {
    const handle = e.target.closest('.team-enemy .slot-icon[draggable="true"]');
    if (!handle) return;
    const slot = handle.closest('.enemy-slot');
    dragSrcLane = slot ? slot.dataset.lane : null;
    if (!dragSrcLane) { dragSrcLane = null; return; }
    e.dataTransfer.effectAllowed = 'move';
    // Some engines require data to be set for the drag to fire `drop`.
    try { e.dataTransfer.setData('text/plain', dragSrcLane); } catch (_) { /* ignore */ }
    // Use the portrait as the drag ghost (the div itself would drag blank).
    const img = handle.querySelector('img');
    if (img && e.dataTransfer.setDragImage) {
        try { e.dataTransfer.setDragImage(img, 26, 26); } catch (_) { /* ignore */ }
    }
    if (slot) slot.classList.add('drag-source');
    // The hover tooltip would otherwise hang over the drop zone.
    recTooltip.hidden = true;
    recHoverRow = null;
});

// A drop target must cancel BOTH dragenter and dragover.
document.addEventListener('dragenter', (e) => {
    if (dragSrcLane == null) return;
    if (e.target.closest('.team-enemy .enemy-slot')) e.preventDefault();
});

document.addEventListener('dragover', (e) => {
    if (dragSrcLane == null) return;
    const slot = e.target.closest('.team-enemy .enemy-slot');
    if (!slot) return;
    e.preventDefault();  // allow drop
    e.dataTransfer.dropEffect = 'move';
    if (slot.dataset.lane !== dragSrcLane) slot.classList.add('drop-target');
});

document.addEventListener('dragleave', (e) => {
    const slot = e.target.closest('.team-enemy .enemy-slot');
    if (slot && !slot.contains(e.relatedTarget)) slot.classList.remove('drop-target');
});

document.addEventListener('drop', (e) => {
    if (dragSrcLane == null) return;
    const slot = e.target.closest('.team-enemy .enemy-slot');
    if (!slot) return;
    e.preventDefault();
    const dstLane = slot.dataset.lane;
    clearDropMarks();
    swapEnemyLanes(dragSrcLane, dstLane);
    dragSrcLane = null;
});

document.addEventListener('dragend', () => {
    dragSrcLane = null;
    clearDropMarks();
});
