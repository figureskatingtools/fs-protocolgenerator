// Hover previews for uploaded files: image thumbnails and PDF first-page
// thumbnails rendered with pdf.js. Previews are lazy — pdf.js is only imported
// the first time a PDF is hovered.

import type { FileMeta } from './types';

let pop: HTMLDivElement | null = null;
let hoverToken = 0;
const pdfCache = new Map<string, string>(); // fileUrl -> dataURL of page 1

function ensurePop(): HTMLDivElement {
  if (!pop) {
    pop = document.createElement('div');
    pop.className = 'file-preview-pop hidden';
    document.body.appendChild(pop);
  }
  return pop;
}

function position(e: MouseEvent) {
  const el = ensurePop();
  const pad = 14;
  let x = e.clientX + pad;
  let y = e.clientY + pad;
  const rect = el.getBoundingClientRect();
  if (x + rect.width > window.innerWidth) x = e.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight) y = e.clientY - rect.height - pad;
  el.style.left = `${Math.max(4, x)}px`;
  el.style.top = `${Math.max(4, y)}px`;
}

function hide() {
  hoverToken++;
  if (pop) pop.classList.add('hidden');
}

async function renderPdfThumb(fileUrl: string): Promise<string | null> {
  if (pdfCache.has(fileUrl)) return pdfCache.get(fileUrl)!;
  try {
    const pdfjs = await import('pdfjs-dist');
    // Worker is bundled by Vite via the ?url import.
    const workerUrl = (await import('pdfjs-dist/build/pdf.worker.min.mjs?url')).default;
    (pdfjs as any).GlobalWorkerOptions.workerSrc = workerUrl;

    const resp = await fetch(fileUrl);
    const data = await resp.arrayBuffer();
    const doc = await (pdfjs as any).getDocument({ data }).promise;
    const page = await doc.getPage(1);
    const baseViewport = page.getViewport({ scale: 1 });
    const scale = Math.min(260 / baseViewport.width, 340 / baseViewport.height);
    const viewport = page.getViewport({ scale });
    const canvas = document.createElement('canvas');
    canvas.width = Math.ceil(viewport.width);
    canvas.height = Math.ceil(viewport.height);
    const ctx = canvas.getContext('2d')!;
    await page.render({ canvasContext: ctx, viewport }).promise;
    const url = canvas.toDataURL('image/png');
    pdfCache.set(fileUrl, url);
    return url;
  } catch (e) {
    console.warn('PDF preview failed', e);
    return null;
  }
}

/**
 * Attach hover-preview behaviour to a chip element.
 * fileUrl streams the bytes via /api/get_file.
 */
export function attachPreview(el: HTMLElement, fileUrl: string, meta: FileMeta) {
  el.addEventListener('mouseenter', async (e) => {
    const token = ++hoverToken;
    const box = ensurePop();
    const sizeKb = meta.size ? `${Math.round(meta.size / 1024)} KB` : '';
    const metaLine = `<div class="preview-meta">${meta.kind.toUpperCase()}${sizeKb ? ' · ' + sizeKb : ''}</div>`;

    if (meta.kind === 'image') {
      box.innerHTML = `<img src="${fileUrl}" alt="">${metaLine}`;
      box.classList.remove('hidden');
      position(e as MouseEvent);
    } else if (meta.kind === 'pdf') {
      box.innerHTML = `<div class="preview-meta">Rendering preview…</div>`;
      box.classList.remove('hidden');
      position(e as MouseEvent);
      const thumb = await renderPdfThumb(fileUrl);
      if (token !== hoverToken) return; // moved on already
      box.innerHTML = thumb ? `<img src="${thumb}" alt="">${metaLine}`
                            : `<div class="preview-meta">${meta.filename}</div>${metaLine}`;
      position(e as MouseEvent);
    } else {
      box.innerHTML = `<div class="preview-meta">${meta.filename}</div>${metaLine}`;
      box.classList.remove('hidden');
      position(e as MouseEvent);
    }
  });

  el.addEventListener('mousemove', (e) => {
    if (pop && !pop.classList.contains('hidden')) position(e as MouseEvent);
  });
  el.addEventListener('mouseleave', hide);
}
