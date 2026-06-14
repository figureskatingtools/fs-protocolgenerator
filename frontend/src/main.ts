import './style.css'
import { renderSiteNav, initSiteNav, injectSiteNavStyles } from '@figureskatingtools/shared-ui';
import type { CompetitionDetails, Structure, Category, Segment, SlotTarget, FileMeta } from './types';
import { attachPreview } from './preview';

injectSiteNavStyles();

function escapeHtml(str: string): string {
  const div = document.createElement('div');
  div.textContent = str ?? '';
  return div.innerHTML;
}

/** JSON for a single-quoted HTML attribute. */
function attr(obj: unknown): string {
  return JSON.stringify(obj).replace(/'/g, '&#39;');
}

interface ClientPrincipal {
  userId: string;
  userRoles: string[];
  identityProvider: string;
  userDetails: string;
}

// ── module state ──
let currentId: string | null = null;
let details: CompetitionDetails | null = null;
const openCats = new Set<string>();

const appElement = document.querySelector<HTMLDivElement>('#app')!;

appElement.innerHTML = `
  <div id="site-nav-container"></div>
  <main>
    <div id="loading-view" class="loading-screen">
      <h2>Authenticating…</h2>
      <p>Please wait while we verify your credentials.</p>
    </div>

    <div id="error-view" class="error-screen hidden"></div>

    <div id="landing-view" class="hidden">
      <div class="card landing-card reveal">
        <span class="micro-label">Protocol Generator</span>
        <h2>Build a competition protocol</h2>
        <p class="lead">
          Upload a competition's schedule, drop the result PDFs and photos into place,
          and generate one bound protocol PDF. Missing graphics fall back to defaults.
        </p>
        <div class="landing-contact">
          <p>To access the application, please contact the administrator:</p>
          <a href="mailto:markus@lintuala.fi">markus@lintuala.fi</a>
        </div>
        <div style="margin-top: 2rem;">
          <a href="/.auth/login/aad?post_login_redirect_url=/" class="btn btn-primary">Sign In to Continue</a>
        </div>
      </div>
    </div>

    <div id="main-content" class="hidden">
      <div id="modal-overlay" class="modal-overlay hidden">
        <div class="modal">
          <h3 id="modal-title">Confirm</h3>
          <p id="modal-message" class="modal-message"></p>
          <div id="modal-extra"></div>
          <div class="modal-actions">
            <button id="modal-cancel" class="btn btn-ghost btn-sm">Cancel</button>
            <button id="modal-confirm" class="btn btn-primary btn-sm">Confirm</button>
          </div>
        </div>
      </div>

      <div id="view-competitions" class="hidden">
        <div class="card reveal">
          <div class="view-header">
            <h2>Competitions</h2>
            <button id="btn-create-comp" class="btn btn-primary btn-sm">Create New</button>
          </div>
          <div id="competitions-list"><p class="text-muted">Loading…</p></div>
        </div>
      </div>

      <div id="view-create-competition" class="hidden">
        <div class="card reveal" style="max-width: 600px; margin: 0 auto;">
          <span class="micro-label">New Competition</span>
          <h2>Create New Competition</h2>
          <div style="margin: 1.25rem 0 1.5rem;">
            <label class="form-label">Competition name</label>
            <input type="text" id="comp-name-input" class="form-input" placeholder="e.g. Winter Cup 2026">
            <p class="text-muted" style="margin-top: 0.5rem;">Dates are filled in automatically from the schedule you upload.</p>
          </div>
          <div class="form-actions">
            <button id="btn-cancel-create" class="btn btn-ghost">Cancel</button>
            <button id="btn-confirm-create" class="btn btn-primary">Create</button>
          </div>
        </div>
      </div>

      <div id="view-detail" class="hidden">
        <div class="card reveal">
          <div class="view-header">
            <div class="view-header-lead">
              <button id="btn-back-list" class="btn btn-sm btn-ghost">← Back</button>
              <h2 id="detail-title">Competition</h2>
            </div>
          </div>
          <div id="detail-body"></div>
        </div>
      </div>

      <div id="view-welcome" class="card reveal" style="max-width: 800px; margin: 0 auto;">
        <span class="micro-label">Protocol Generator</span>
        <h2 style="margin-bottom: 1.25rem;">Welcome</h2>
        <ol class="howto-list">
          <li>Create a competition and upload its <strong>schedule PDF</strong>.</li>
          <li>Fill in the event details and drop result PDFs and photos into the slots.</li>
          <li>Drag files between slots to fix placements; hover to preview.</li>
          <li>Press <strong>Generate Protocol</strong> to build the bound PDF.</li>
        </ol>
        <button id="action-btn" class="btn btn-primary">Go to Competitions</button>
      </div>
    </div>
  </main>
`;

function showView(viewId: string) {
  ['view-welcome', 'view-competitions', 'view-create-competition', 'view-detail'].forEach(id => {
    document.getElementById(id)?.classList.toggle('hidden', id !== viewId);
  });
}

// ── API helpers ──
async function apiGet(path: string): Promise<Response> {
  return fetch(path);
}
async function apiJson(path: string, body: unknown): Promise<Response> {
  return fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}
async function apiRaw(path: string, body: Blob | ArrayBuffer): Promise<Response> {
  return fetch(path, { method: 'POST', body });
}

// ── competitions list ──
function fmtDate(value: string): string {
  if (!value || value === '-') return '-';
  try {
    const d = new Date(value);
    return `${String(d.getDate()).padStart(2, '0')}.${String(d.getMonth() + 1).padStart(2, '0')}.${d.getFullYear()}`;
  } catch { return '-'; }
}

async function loadCompetitions() {
  showView('view-competitions');
  const list = document.getElementById('competitions-list')!;
  list.innerHTML = '<p class="text-muted">Loading…</p>';
  try {
    const resp = await apiGet('/api/list_competitions');
    if (!resp.ok) throw new Error(String(resp.status));
    const comps: any[] = await resp.json();
    if (comps.length === 0) {
      list.innerHTML = '<p class="text-muted">No competitions yet. Create one to get started.</p>';
      return;
    }
    list.innerHTML = comps.map(c => `
      <div class="comp-row">
        <div class="comp-row-head">
          <span class="comp-row-name">${escapeHtml(c.name)}</span>
          <div class="comp-row-actions">
            <button class="btn btn-sm btn-ghost" data-open="${escapeHtml(c.id)}" data-name="${escapeHtml(c.name)}">Open</button>
            <button class="btn btn-sm btn-ghost btn-ghost--danger" data-del="${escapeHtml(c.id)}" data-name="${escapeHtml(c.name)}">Delete</button>
          </div>
        </div>
        <div class="comp-row-meta">
          <span>Creator: ${escapeHtml(c.createdBy)}</span>
          <span>Created: ${escapeHtml(fmtDate(c.createdDate))}</span>
          <span>Deletes: ${escapeHtml(fmtDate(c.deletionDate))}</span>
        </div>
      </div>`).join('');
    list.querySelectorAll<HTMLElement>('[data-open]').forEach(b =>
      b.addEventListener('click', () => openCompetition(b.dataset.open!, b.dataset.name!)));
    list.querySelectorAll<HTMLElement>('[data-del]').forEach(b =>
      b.addEventListener('click', () => confirmDeleteCompetition(b.dataset.del!, b.dataset.name!)));
  } catch {
    list.innerHTML = '<p class="text-error">Failed to load competitions.</p>';
  }
}

// ── detail view ──
async function openCompetition(id: string, name: string) {
  currentId = id;
  showView('view-detail');
  document.getElementById('detail-title')!.textContent = name;
  document.getElementById('detail-body')!.innerHTML = '<p class="text-muted">Loading…</p>';
  await loadDetails();
}

async function loadDetails() {
  if (!currentId) return;
  try {
    const resp = await apiGet(`/api/get_competition_details?id=${encodeURIComponent(currentId)}`);
    if (!resp.ok) throw new Error(String(resp.status));
    details = await resp.json();
    renderDetails();
  } catch {
    document.getElementById('detail-body')!.innerHTML = '<p class="text-error">Failed to load competition.</p>';
  }
}

function fileMeta(fileId: string | null): FileMeta | null {
  if (!fileId || !details) return null;
  return details.structure.files[fileId] || null;
}

function fileUrl(fileId: string): string {
  return `/api/get_file?competition=${encodeURIComponent(currentId!)}&fileId=${encodeURIComponent(fileId)}`;
}

function chipHtml(fileId: string): string {
  const m = fileMeta(fileId);
  if (!m) return '';
  const kindClass = m.kind === 'image' ? 'chip-kind--image' : m.kind === 'xml' ? 'chip-kind--xml' : '';
  return `<span class="file-chip" draggable="true" data-file-id="${escapeHtml(fileId)}">
      <span class="chip-kind ${kindClass}">${escapeHtml(m.kind)}</span>
      <span class="chip-name" title="${escapeHtml(m.filename)}">${escapeHtml(m.filename)}</span>
      <button class="chip-x" data-del-file="${escapeHtml(fileId)}" title="Delete file">×</button>
    </span>`;
}

function slotHtml(label: string, target: SlotTarget, fileId: string | null, required = false): string {
  const filled = fileId ? 'is-filled' : '';
  const reqCls = required && !fileId ? 'is-missing' : '';
  const inner = fileId ? chipHtml(fileId) : '<div class="slot-empty">drop a file</div>';
  return `<div class="slot ${filled} ${reqCls}" data-target='${attr(target)}'>
      <span class="slot-label">${escapeHtml(label)}${required ? ' <span class="req">•</span>' : ''}</span>
      ${inner}
    </div>`;
}

/** Required-slot fill progress for a category (title + total results + each
 * segment's results PDF). Drives the per-category readiness badge. */
function categoryReadiness(cat: Category): { filled: number; total: number; ready: boolean } {
  let total = 0, filled = 0;
  const req = (v: string | null | undefined) => { total++; if (v) filled++; };
  req(cat.titlePdf);
  req(cat.totalResultsPdf);
  (cat.segments || []).forEach(s => req(s.resultsPdf));
  return { filled, total, ready: total > 0 && filled === total };
}

function segmentHtml(cat: Category, seg: Segment): string {
  return `<div class="segment-block">
      <div class="segment-head">
        <input class="form-input segment-name" style="max-width: 320px;" value="${escapeHtml(seg.name)}"
               data-edit="set_segment" data-cat="${cat.id}" data-seg="${seg.id}" data-field="name">
        <button class="btn btn-xs btn-ghost btn-ghost--danger" data-rm-seg="${seg.id}" data-cat="${cat.id}">Remove segment</button>
      </div>
      <div class="segment-roles">
        ${slotHtml('Segment Results', { kind: 'segment', categoryId: cat.id, segmentId: seg.id, role: 'results' }, seg.resultsPdf, true)}
        ${slotHtml('Panel of Judges', { kind: 'segment', categoryId: cat.id, segmentId: seg.id, role: 'panel' }, seg.panelPdf)}
        ${slotHtml('Judges Scores Details Without Referee', { kind: 'segment', categoryId: cat.id, segmentId: seg.id, role: 'judgesDetails' }, seg.judgesDetailsPdf)}
      </div>
    </div>`;
}

function teamHtml(cat: Category, team: Category['teams'][number]): string {
  const roster = team.members && team.members.length
    ? `<ul>${team.members.map(m => `<li>${escapeHtml(m)}</li>`).join('')}</ul>`
    : '<span class="team-roster-empty">No roster yet — upload a DT_PARTIC XML.</span>';
  return `<div class="team-card">
      <div class="team-head">
        <div class="team-fields">
          <input class="form-input" placeholder="Team name" value="${escapeHtml(team.name)}"
                 data-edit="set_team" data-cat="${cat.id}" data-team="${team.id}" data-field="name">
          <input class="form-input" placeholder="Organization / club" value="${escapeHtml(team.org)}"
                 data-edit="set_team" data-cat="${cat.id}" data-team="${team.id}" data-field="org">
        </div>
        <button class="btn btn-xs btn-ghost btn-ghost--danger" data-rm-team="${team.id}" data-cat="${cat.id}">Remove</button>
      </div>
      <div class="team-body">
        <div class="slot-grid">
          ${slotHtml('Team photo', { kind: 'teamPhoto', categoryId: cat.id, teamId: team.id }, team.photo)}
        </div>
        <div class="team-roster">SKATERS (${team.members?.length || 0})${roster}</div>
      </div>
    </div>`;
}

function categoryHtml(cat: Category): string {
  const isOpen = openCats.has(cat.id);
  const isSynchro = cat.discipline === 'synchro';
  const podiumNames = (cat.podium?.names || ['', '', '']).slice(0, 3);
  const teamsSection = isSynchro ? `
      <div class="section">
        <div class="section-head"><h3>Teams</h3>
          <button class="btn btn-xs btn-primary" data-add-team="${cat.id}">Add team</button>
        </div>
        <p class="section-sub">Rosters are imported for the whole competition at once (see <strong>Team rosters</strong> above) and matched to their category automatically.</p>
        ${(cat.teams || []).map(t => teamHtml(cat, t)).join('') || '<p class="section-sub">No teams yet.</p>'}
      </div>` : '';

  const r = categoryReadiness(cat);
  const readyBadge = `<span class="cat-ready ${r.ready ? 'is-ready' : ''}" title="Required files uploaded">${r.ready ? '✓ ' : ''}${r.filled}/${r.total} uploaded</span>`;

  return `<div class="category-card">
      <div class="category-header ${isSynchro ? 'is-synchro' : ''}" data-toggle-cat="${cat.id}">
        <div class="category-head-lead">
          <span class="category-title">${escapeHtml(cat.name || '(unnamed)')}</span>
          ${isSynchro ? '<span class="tag-synchro">Synchro</span>' : ''}
        </div>
        <div class="category-head-tail">
          ${readyBadge}
          <span class="micro-label">${(cat.segments || []).length} seg</span>
          <span class="toggle-icon">${isOpen ? '▴' : '▾'}</span>
        </div>
      </div>
      <div class="category-content" style="display:${isOpen ? 'block' : 'none'};">
        <div class="cat-meta">
          <input class="form-input" style="max-width: 320px;" value="${escapeHtml(cat.name)}"
                 data-edit="set_category" data-cat="${cat.id}" data-field="name">
          <select class="discipline-select" data-discipline="${cat.id}">
            ${['single', 'pair', 'dance', 'synchro'].map(d =>
              `<option value="${d}" ${cat.discipline === d ? 'selected' : ''}>${d}</option>`).join('')}
          </select>
          <button class="btn btn-xs btn-ghost btn-ghost--danger" data-rm-cat="${cat.id}">Remove category</button>
        </div>

        ${teamsSection}

        <div class="section">
          <div class="section-head"><h3>Category pages</h3></div>
          <div class="slot-grid">
            ${slotHtml('Protocol Head Page (PDF)', { kind: 'categoryTitle', categoryId: cat.id }, cat.titlePdf, true)}
            ${slotHtml('Podium photo', { kind: 'podiumPhoto', categoryId: cat.id }, cat.podium?.photo || null)}
            ${slotHtml('Total results (PDF)', { kind: 'totalResults', categoryId: cat.id }, cat.totalResultsPdf, true)}
          </div>
          <div class="podium-names" style="margin-top: 0.75rem;">
            ${['1st', '2nd', '3rd'].map((p, i) => `
              <div class="podium-name-row">
                <span class="podium-place">${p} place</span>
                <input class="form-input" placeholder="Name(s)" value="${escapeHtml(podiumNames[i] || '')}"
                       data-podium="${cat.id}" data-place="${i}">
              </div>`).join('')}
          </div>
        </div>

        <div class="section">
          <div class="section-head"><h3>Segments</h3>
            <button class="btn btn-xs btn-primary" data-add-seg="${cat.id}">Add segment</button>
          </div>
          ${(cat.segments || []).slice().sort((a, b) => a.order - b.order).map(s => segmentHtml(cat, s)).join('')
            || '<p class="section-sub">No segments yet.</p>'}
        </div>
      </div>
    </div>`;
}

function renderDetails() {
  if (!details) return;
  const s: Structure = details.structure;
  document.getElementById('detail-title')!.textContent = s.name;

  const ev = s.event;
  const field = (key: keyof typeof ev, label: string, wide = false) =>
    `<div class="event-field ${wide ? 'event-field--wide' : ''}">
       <label>${label}</label>
       <input class="form-input" data-event="${key}" value="${escapeHtml(ev[key] || '')}">
     </div>`;

  const trayChips = details.unassigned.length
    ? details.unassigned.map(chipHtml).join('')
    : '<span class="tray-empty">No unassigned files. Uploads land here, then drag them into slots.</span>';

  const scheduleSection = s.scheduleParsed
    ? `<p class="section-sub">${(s.categories || []).length} categories parsed from the schedule.
         <button class="btn btn-xs btn-ghost" id="btn-reparse">Replace schedule…</button></p>`
    : `<div class="upload-area" id="schedule-drop">
         <p class="upload-title">Drop the competition schedule (DT_SCHEDULE XML or PDF)</p>
         <p class="upload-or">or</p>
         <button class="btn btn-sm btn-primary" id="schedule-browse">Browse…</button>
         <input type="file" id="schedule-input" accept=".xml,.pdf" style="display:none;">
       </div>
       <p class="section-sub">An ISU <strong>DT_SCHEDULE</strong> XML is preferred — it carries exact times, disciplines, segments and the ice rink.</p>`;

  const gen = details.generatedFiles || [];
  const genHtml = gen.length ? gen.map(g => `
      <div class="gen-file">
        <a class="gen-file-link" href="${g.url}" target="_blank" rel="noopener noreferrer">
          <span>${escapeHtml(g.fileName)}</span>
          <span class="gen-badge">${g.size ? Math.round(Number(g.size) / 1024) + ' KB' : ''}</span>
        </a>
      </div>`).join('') : '<p class="section-sub">No protocol generated yet.</p>';

  document.getElementById('detail-body')!.innerHTML = `
    <div class="section">
      <div class="section-head"><h3>Event details</h3>
        <button class="btn btn-xs btn-ghost" id="btn-save-event">Save</button>
      </div>
      <div class="event-form">
        ${field('title', 'Protocol title', true)}
        ${field('organization', 'Organized by')}
        ${field('authorization', 'With authorization of')}
        ${field('city', 'Held in (city)')}
        ${field('rink', 'Ice rink / arena')}
        ${field('dates', 'Dates')}
      </div>
    </div>

    <div class="section">
      <div class="section-head"><h3>Schedule</h3></div>
      ${scheduleSection}
    </div>

    <div class="section">
      <div class="section-head"><h3>Cover &amp; last page</h3></div>
      <p class="section-sub">Leave empty to use the default placeholder pages.</p>
      <div class="page-slot-row">
        ${slotHtml('Cover page (PDF or image)', { kind: 'cover' }, s.coverPage.fileId)}
        ${slotHtml('Last page (PDF or image)', { kind: 'lastPage' }, s.lastPage.fileId)}
      </div>
    </div>

    <div class="section">
      <div class="section-head"><h3>Header &amp; footer</h3></div>
      <p class="section-sub">Optional. Stamped on every generated page. Leave empty to use the generic placeholder band (<code>EXAMPLE HEADER</code> / <code>EXAMPLE FOOTER</code>).</p>
      <div class="page-slot-row">
        ${slotHtml('Competition header (image)', { kind: 'header' }, s.header?.fileId || null)}
        ${slotHtml('Competition footer (image)', { kind: 'footer' }, s.footer?.fileId || null)}
      </div>
    </div>

    <div class="section">
      <div class="section-head"><h3>Uploads</h3>
        <button class="btn btn-xs btn-primary" id="tray-browse">Upload files…</button>
        <input type="file" id="tray-input" multiple accept=".pdf,.png,.jpg,.jpeg,.gif,.webp,.xml" style="display:none;">
      </div>
      <p class="section-sub">Drop PDFs and photos here or into any slot; drag chips between slots to move them.</p>
      <div class="tray" data-target='${attr({ kind: 'tray' })}'>
        <div class="tray-chips">${trayChips}</div>
      </div>
    </div>

    ${(s.categories || []).some(c => c.discipline === 'synchro') ? `
    <div class="section">
      <div class="section-head"><h3>Team rosters</h3>
        <button class="btn btn-xs btn-ghost" id="btn-import-rosters">Import teams (DT_PARTIC)…</button>
      </div>
      <p class="section-sub">Select the competition's <strong>DT_PARTIC_TEAMS</strong> and <strong>DT_PARTIC</strong> XML files together — one pair covers the whole competition. Teams are matched to their synchro category automatically.</p>
    </div>` : ''}

    <div class="section">
      <div class="section-head"><h3>Categories</h3>
        <button class="btn btn-xs btn-primary" id="btn-add-cat">Add category</button>
      </div>
      ${(s.categories || []).slice().sort((a, b) => a.order - b.order).map(categoryHtml).join('')
        || '<p class="section-sub">No categories yet. Upload a schedule or add one manually.</p>'}
    </div>

    <div class="action-bar">
      <div class="gen-list">${genHtml}</div>
      <button class="btn btn-primary btn-generate" id="btn-generate">Generate Protocol</button>
    </div>
  `;

  wireDetail();
}

// ── wiring ──
function wireDetail() {
  const body = document.getElementById('detail-body')!;

  // Hover previews + drag handles for every chip.
  body.querySelectorAll<HTMLElement>('.file-chip').forEach(chip => {
    const fid = chip.dataset.fileId!;
    const meta = fileMeta(fid);
    if (meta) attachPreview(chip, fileUrl(fid), meta);
    chip.addEventListener('dragstart', e => {
      e.dataTransfer!.setData('text/plain', fid);
      e.dataTransfer!.effectAllowed = 'move';
      chip.classList.add('dragging');
    });
    chip.addEventListener('dragend', () => chip.classList.remove('dragging'));
  });

  // Drop zones: slots + tray.
  body.querySelectorAll<HTMLElement>('[data-target]').forEach(zone => {
    zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
    zone.addEventListener('drop', e => {
      e.preventDefault();
      zone.classList.remove('dragover');
      const target = JSON.parse(zone.getAttribute('data-target')!) as SlotTarget;
      if (e.dataTransfer?.files?.length) {
        uploadFiles(e.dataTransfer.files, target);
        return;
      }
      const fid = e.dataTransfer?.getData('text/plain');
      if (fid) assignFile(fid, target);
    });
  });

  // Delete-file buttons (stop the drag/drop & prevent chip dragstart issues).
  body.querySelectorAll<HTMLElement>('[data-del-file]').forEach(b =>
    b.addEventListener('click', e => { e.stopPropagation(); deleteFile(b.dataset.delFile!); }));

  // Category collapse toggles.
  body.querySelectorAll<HTMLElement>('[data-toggle-cat]').forEach(h =>
    h.addEventListener('click', e => {
      if ((e.target as HTMLElement).closest('input,select,button')) return;
      const id = h.dataset.toggleCat!;
      if (openCats.has(id)) openCats.delete(id); else openCats.add(id);
      renderDetails();
    }));

  // Event detail inputs (save on blur).
  body.querySelectorAll<HTMLInputElement>('[data-event]').forEach(inp =>
    inp.addEventListener('change', () => saveEvent()));
  document.getElementById('btn-save-event')?.addEventListener('click', () => saveEvent());

  // Structure text edits (category / segment / team).
  body.querySelectorAll<HTMLInputElement>('[data-edit]').forEach(inp =>
    inp.addEventListener('change', () => {
      const op = inp.dataset.edit!;
      const payload: any = { op, categoryId: inp.dataset.cat };
      if (inp.dataset.seg) payload.segmentId = inp.dataset.seg;
      if (inp.dataset.team) payload.teamId = inp.dataset.team;
      payload[inp.dataset.field!] = inp.value;
      editStructure(payload, false);
    }));

  // Discipline change (re-render to toggle synchro team UI).
  body.querySelectorAll<HTMLSelectElement>('[data-discipline]').forEach(sel =>
    sel.addEventListener('change', () =>
      editStructure({ op: 'set_category', categoryId: sel.dataset.discipline, discipline: sel.value }, true)));

  // Podium names.
  body.querySelectorAll<HTMLInputElement>('[data-podium]').forEach(inp =>
    inp.addEventListener('change', () => {
      const catId = inp.dataset.podium!;
      const cat = details!.structure.categories.find(c => c.id === catId);
      if (!cat) return;
      const names = (cat.podium?.names || ['', '', '']).slice(0, 3);
      names[Number(inp.dataset.place)] = inp.value;
      cat.podium.names = names;
      editStructure({ op: 'set_podium', categoryId: catId, names }, false);
    }));

  // Add / remove buttons.
  document.getElementById('btn-add-cat')?.addEventListener('click', () =>
    editStructure({ op: 'add_category', name: 'New Category', discipline: 'single' }, true));
  body.querySelectorAll<HTMLElement>('[data-rm-cat]').forEach(b =>
    b.addEventListener('click', () => editStructure({ op: 'remove_category', categoryId: b.dataset.rmCat }, true)));
  body.querySelectorAll<HTMLElement>('[data-add-seg]').forEach(b =>
    b.addEventListener('click', () => editStructure({ op: 'add_segment', categoryId: b.dataset.addSeg, name: 'Segment' }, true)));
  body.querySelectorAll<HTMLElement>('[data-rm-seg]').forEach(b =>
    b.addEventListener('click', () => editStructure({ op: 'remove_segment', categoryId: b.dataset.cat, segmentId: b.dataset.rmSeg }, true)));
  body.querySelectorAll<HTMLElement>('[data-add-team]').forEach(b =>
    b.addEventListener('click', () => editStructure({ op: 'add_team', categoryId: b.dataset.addTeam }, true)));
  body.querySelectorAll<HTMLElement>('[data-rm-team]').forEach(b =>
    b.addEventListener('click', () => editStructure({ op: 'remove_team', categoryId: b.dataset.cat, teamId: b.dataset.rmTeam }, true)));

  // Roster import (two DT_PARTIC XML files, one pair for the whole competition).
  document.getElementById('btn-import-rosters')?.addEventListener('click', () => pickRosters());

  // Schedule upload.
  const schedBrowse = document.getElementById('schedule-browse');
  const schedInput = document.getElementById('schedule-input') as HTMLInputElement | null;
  schedBrowse?.addEventListener('click', () => schedInput?.click());
  schedInput?.addEventListener('change', () => { if (schedInput.files?.[0]) parseSchedule(schedInput.files[0]); });
  const schedDrop = document.getElementById('schedule-drop');
  schedDrop?.addEventListener('dragover', e => { e.preventDefault(); schedDrop.classList.add('dragover'); });
  schedDrop?.addEventListener('dragleave', () => schedDrop.classList.remove('dragover'));
  schedDrop?.addEventListener('drop', e => {
    e.preventDefault(); schedDrop.classList.remove('dragover');
    if (e.dataTransfer?.files?.[0]) parseSchedule(e.dataTransfer.files[0]);
  });
  document.getElementById('btn-reparse')?.addEventListener('click', () => {
    const inp = document.createElement('input');
    inp.type = 'file'; inp.accept = '.xml,.pdf';
    inp.onchange = () => { if (inp.files?.[0]) parseSchedule(inp.files[0], true); };
    inp.click();
  });

  // Tray upload.
  const trayBrowse = document.getElementById('tray-browse');
  const trayInput = document.getElementById('tray-input') as HTMLInputElement | null;
  trayBrowse?.addEventListener('click', () => trayInput?.click());
  trayInput?.addEventListener('change', () => { if (trayInput.files?.length) uploadFiles(trayInput.files); trayInput.value = ''; });

  // Generate.
  document.getElementById('btn-generate')?.addEventListener('click', generate);
}

// ── mutations ──
async function saveEvent() {
  if (!currentId) return;
  const event: Record<string, string> = {};
  document.querySelectorAll<HTMLInputElement>('[data-event]').forEach(inp => {
    event[inp.dataset.event!] = inp.value;
    if (details) (details.structure.event as any)[inp.dataset.event!] = inp.value;
  });
  try { await apiJson('/api/save_event_settings', { id: currentId, event }); } catch { /* ignore */ }
}

async function assignFile(fileId: string, target: SlotTarget) {
  if (!currentId) return;
  try {
    const resp = await apiJson('/api/assign_file', { id: currentId, fileId, target });
    if (!resp.ok) { alert('Could not move file: ' + (await resp.text())); return; }
    await loadDetails();
  } catch { alert('Network error moving file.'); }
}

async function uploadFiles(files: FileList, target?: SlotTarget) {
  if (!currentId) return;
  for (const file of Array.from(files)) {
    const params = new URLSearchParams({ competition: currentId, filename: file.name });
    if (target && target.kind !== 'tray') {
      params.set('slotKind', target.kind);
      if (target.categoryId) params.set('categoryId', target.categoryId);
      if (target.segmentId) params.set('segmentId', target.segmentId);
      if (target.teamId) params.set('teamId', target.teamId);
      if (target.role) params.set('role', target.role);
    }
    try {
      const resp = await apiRaw(`/api/upload_file?${params.toString()}`, file);
      if (!resp.ok) alert(`Upload failed for ${file.name}: ${await resp.text()}`);
    } catch { alert(`Upload error for ${file.name}.`); }
  }
  await loadDetails();
}

async function deleteFile(fileId: string) {
  if (!currentId) return;
  try {
    await fetch(`/api/delete_file?competition=${encodeURIComponent(currentId)}&fileId=${encodeURIComponent(fileId)}`, { method: 'DELETE' });
    await loadDetails();
  } catch { alert('Could not delete file.'); }
}

async function editStructure(payload: any, reload: boolean) {
  if (!currentId) return;
  try {
    const resp = await apiJson('/api/edit_structure', { id: currentId, ...payload });
    if (!resp.ok) { alert('Edit failed: ' + (await resp.text())); return; }
    if (reload) await loadDetails();
  } catch { alert('Network error editing competition.'); }
}

async function parseSchedule(file: File, force = false) {
  if (!currentId) return;
  const url = `/api/parse_schedule?competition=${encodeURIComponent(currentId)}${force ? '&force=true' : ''}`;
  try {
    const resp = await apiRaw(url, file);
    if (resp.status === 409) {
      if (confirm('This competition already has categories. Replace them from this schedule?')) {
        return parseSchedule(file, true);
      }
      return;
    }
    if (!resp.ok) { alert('Schedule parse failed: ' + (await resp.text())); return; }
    await loadDetails();
  } catch { alert('Network error parsing schedule.'); }
}

function pickRosters() {
  const inp = document.createElement('input');
  inp.type = 'file'; inp.accept = '.xml'; inp.multiple = true;
  inp.onchange = async () => {
    const files = Array.from(inp.files || []);
    if (!files.length) return;
    const texts = await Promise.all(files.map(f => f.text()));
    let teamsXml = '', particXml = '';
    texts.forEach(t => {
      if (/DocumentType="DT_PARTIC_TEAMS"/.test(t)) teamsXml = t;
      else if (/DocumentType="DT_PARTIC"/.test(t)) particXml = t;
    });
    // Fallbacks if DocumentType isn't present: use filenames, then order.
    if (!teamsXml || !particXml) {
      files.forEach((f, i) => {
        if (/teams/i.test(f.name) && !teamsXml) teamsXml = texts[i];
        else if (!particXml) particXml = texts[i];
      });
    }
    if (!teamsXml) { alert('Could not find a DT_PARTIC_TEAMS file among the selected files.'); return; }
    await importRosters(teamsXml, particXml);
  };
  inp.click();
}

async function importRosters(teamsXml: string, particXml: string) {
  if (!currentId) return;
  try {
    const resp = await apiJson('/api/import_rosters', { id: currentId, teamsXml, particXml });
    if (!resp.ok) { alert('Roster import failed: ' + (await resp.text())); return; }
    const data = await resp.json();
    await loadDetails();
    const parts = [`Imported ${data.imported} team(s) into ${data.categories} categor${data.categories === 1 ? 'y' : 'ies'}`];
    if (Array.isArray(data.unmatched) && data.unmatched.length) {
      parts.push(`no category matched: ${data.unmatched.join(', ')}`);
    }
    flash(parts.join(' — ') + '.');
  } catch { alert('Network error importing rosters.'); }
}

/** Brief transient status toast reusing the upload-status look. */
function flash(msg: string) {
  let el = document.getElementById('pg-flash');
  if (!el) {
    el = document.createElement('div');
    el.id = 'pg-flash';
    el.style.cssText = 'position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);background:var(--ink);color:#fff;padding:0.6rem 1.1rem;border-radius:0.5rem;box-shadow:var(--shadow-lg);z-index:90;font-size:0.85rem;';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.style.opacity = '1';
  setTimeout(() => { if (el) el.style.opacity = '0'; el!.style.transition = 'opacity 0.5s'; }, 2500);
}

async function generate() {
  if (!currentId) return;
  const btn = document.getElementById('btn-generate') as HTMLButtonElement;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Generating…';
  try {
    const resp = await apiJson('/api/generate_protocol', { id: currentId });
    if (resp.ok) {
      btn.textContent = 'Done!';
      btn.classList.add('btn-success');
      setTimeout(() => { btn.classList.remove('btn-success'); btn.textContent = 'Generate Protocol'; btn.disabled = false; loadDetails(); }, 1500);
    } else {
      alert('Generation failed: ' + (await resp.text()));
      btn.textContent = 'Generate Protocol'; btn.disabled = false;
    }
  } catch {
    alert('Network error generating protocol.');
    btn.textContent = 'Generate Protocol'; btn.disabled = false;
  }
}

// ── delete competition modal ──
function confirmDeleteCompetition(id: string, name: string) {
  const overlay = document.getElementById('modal-overlay')!;
  document.getElementById('modal-title')!.textContent = 'Delete competition?';
  document.getElementById('modal-message')!.innerHTML =
    `Delete <strong>${escapeHtml(name)}</strong>? This permanently removes its files.`;
  document.getElementById('modal-extra')!.innerHTML = '';
  const confirm = document.getElementById('modal-confirm') as HTMLButtonElement;
  const cancel = document.getElementById('modal-cancel') as HTMLButtonElement;
  confirm.className = 'btn btn-danger btn-sm';
  confirm.textContent = 'Delete';
  overlay.classList.remove('hidden');
  const close = () => overlay.classList.add('hidden');
  cancel.onclick = close;
  confirm.onclick = async () => {
    confirm.disabled = true;
    try {
      await apiGet(`/api/delete_competition?id=${encodeURIComponent(id)}`);
      close(); loadCompetitions();
    } catch { alert('Delete failed.'); }
    finally { confirm.disabled = false; }
  };
}

// ── create competition ──
async function createCompetition() {
  const name = (document.getElementById('comp-name-input') as HTMLInputElement).value.trim();
  if (!name) { alert('Please enter a name.'); return; }
  const btn = document.getElementById('btn-confirm-create') as HTMLButtonElement;
  btn.disabled = true; btn.textContent = 'Creating…';
  try {
    const resp = await apiGet(`/api/create_competition?name=${encodeURIComponent(name)}`);
    if (resp.ok) {
      const data = await resp.json();
      (document.getElementById('comp-name-input') as HTMLInputElement).value = '';
      openCompetition(data.id, data.name);
    } else {
      alert('Create failed: ' + (await resp.text()));
    }
  } catch { alert('Network error creating competition.'); }
  finally { btn.disabled = false; btn.textContent = 'Create'; }
}

// ── init / auth ──
async function init() {
  const loadingView = document.getElementById('loading-view')!;
  const landingView = document.getElementById('landing-view')!;
  const mainContent = document.getElementById('main-content')!;
  const navContainer = document.getElementById('site-nav-container')!;

  try {
    let principal: ClientPrincipal | null = null;
    try {
      const userInfo = await (await fetch('/userinfo')).json();
      if (userInfo?.authenticated) {
        principal = {
          userId: userInfo.userId || '',
          identityProvider: userInfo.identityProvider || 'aad',
          userDetails: userInfo.userDetails || '',
          userRoles: userInfo.userRoles || ['authenticated'],
        };
      }
    } catch { /* unauthenticated */ }

    navContainer.innerHTML = renderSiteNav({
      activeApp: 'protocolgenerator',
      logoUrl: '/logo.png',
      ...(principal ? {
        appNavItems: [
          { id: 'competitions', label: 'Competitions', enabled: true },
          { id: 'new-competition', label: 'New Competition', enabled: true },
        ],
      } : {}),
    });
    initSiteNav();
    const userSection = document.getElementById('fst-nav-right')!;

    if (!principal) {
      userSection.innerHTML = `<a href="/.auth/login/aad" class="btn btn-primary btn-sm">Sign In</a>`;
      loadingView.classList.add('hidden');
      landingView.classList.remove('hidden');
      return;
    }

    setupUserMenu(userSection, principal);
    loadingView.classList.add('hidden');
    mainContent.classList.remove('hidden');

    document.querySelectorAll<HTMLElement>('[data-nav-action]').forEach(el =>
      el.addEventListener('click', e => {
        e.preventDefault();
        document.querySelectorAll('.fst-dropdown-menu').forEach(m => m.classList.remove('fst-dropdown-menu--open'));
        const action = (e.currentTarget as HTMLElement).dataset.navAction;
        if (action === 'competitions') loadCompetitions();
        else if (action === 'new-competition') showView('view-create-competition');
      }));

    document.getElementById('btn-back-list')?.addEventListener('click', loadCompetitions);
    document.getElementById('action-btn')?.addEventListener('click', loadCompetitions);
    document.getElementById('btn-create-comp')?.addEventListener('click', () => showView('view-create-competition'));
    document.getElementById('btn-cancel-create')?.addEventListener('click', loadCompetitions);
    document.getElementById('btn-confirm-create')?.addEventListener('click', createCompetition);

    loadCompetitions();
  } catch {
    loadingView.classList.add('hidden');
    document.getElementById('error-view')!.classList.remove('hidden');
    document.getElementById('error-view')!.innerHTML = '<h2>Error</h2><p>Failed to initialize application.</p>';
  }
}

function setupUserMenu(container: HTMLElement, user: ClientPrincipal) {
  container.innerHTML = `
    <div class="user-menu-container">
      <button id="user-menu-btn" class="user-btn">
        <span>${escapeHtml(user.userDetails)}</span>
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16">
          <path fill-rule="evenodd" d="M1.646 4.646a.5.5 0 0 1 .708 0L8 10.293l5.646-5.647a.5.5 0 0 1 .708.708l-6 6a.5.5 0 0 1-.708 0l-6-6a.5.5 0 0 1 0-.708z"/>
        </svg>
      </button>
      <div id="user-dropdown" class="dropdown-menu">
        <div class="dropdown-header">Signed in as <br> <strong>${escapeHtml(user.userDetails)}</strong></div>
        <a href="/.auth/logout?post_logout_redirect_uri=/" class="dropdown-item">Sign Out</a>
      </div>
    </div>`;
  const btn = document.getElementById('user-menu-btn')!;
  const dropdown = document.getElementById('user-dropdown')!;
  btn.addEventListener('click', e => { e.stopPropagation(); dropdown.classList.toggle('show'); });
  document.addEventListener('click', () => dropdown.classList.remove('show'));
  dropdown.addEventListener('click', e => e.stopPropagation());
}

init();
