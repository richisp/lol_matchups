// ---- AJAX refresh: re-render the draft form in place instead of
//      doing a full-page navigation. Preserves scroll position and
//      avoids the constant reload churn during champ select.
let inflight = null;
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
const RECS_NATURAL_DESC = new Set(['fit', 'base', 'counter', 'synergy']);

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
    const row = e.target.closest('.rec-row');
    if (!row || row === recHoverRow) return;
    const src = row.querySelector('.rec-breakdown-src');
    if (!src) return;
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
    bans: '',
    lane: (document.getElementById('active-input') || {}).value || '',
};

function syncSlot(inp, lcuValue, lastApplied) {
    const overridden = inp.value !== '' && inp.value !== lastApplied;
    if (overridden || inp.value === lcuValue) return false;
    inp.value = lcuValue;
    return true;
}

async function pollLcu() {
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
            return;
        }
        if (!d.in_champ_select) {
            setBadge('LCU: not in champ select', 'lcu-idle');
            return;
        }
        setBadge('LCU: in champ select', 'lcu-live');

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

        // Bans: sort to a canonical order so LCU's pick-order list and
        // the server's sorted output compare equal.
        const lcuBansSorted = (d.bans || []).slice().sort().join(', ');
        const bansInp = form.querySelector('input[name="bans"]');
        if (bansInp) {
            if (syncSlot(bansInp, lcuBansSorted, lcuApplied.bans)) dirty = true;
            lcuApplied.bans = lcuBansSorted;
        }
        // Per-team bans (visual-only). Server uses the combined `bans`
        // for scoring; these only drive the icon rows under each team.
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
