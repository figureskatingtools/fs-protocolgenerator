// Shape of the competition structure document returned by the backend
// (mirrors infra/functions/structure.py).

export type Discipline = 'single' | 'pair' | 'dance' | 'synchro';
export type FileKind = 'pdf' | 'image' | 'xml' | 'other';
export type SegmentRole = 'results' | 'panel' | 'judgesDetails';

export interface FileMeta {
  filename: string;
  kind: FileKind;
  size: number;
  uploadedAt: string;
  blob?: string;
}

export interface PageRef {
  mode: 'default' | 'custom';
  fileId: string | null;
}

export interface Podium {
  photo: string | null;
  names: string[];
}

export interface Team {
  id: string;
  code: string;
  event: string;
  org: string;
  name: string;
  photo: string | null;
  /** Accreditation picture bulk-imported from a ZIP — used at generation only
   * when the team has no competition (kiss'n'cry) photo. */
  photoFallback?: string | null;
  members: string[];
}

export interface Segment {
  id: string;
  name: string;
  order: number;
  /** Competition units that performed this segment (auto-filled from the results
   * PDF, user-correctable; null = unknown). Feeds the information-page counts. */
  unitCount: number | null;
  resultsPdf: string | null;
  panelPdf: string | null;
  judgesDetailsPdf: string | null;
}

export interface Category {
  id: string;
  name: string;
  code?: string;
  discipline: Discipline;
  order: number;
  titlePdf: string | null;
  podium: Podium;
  totalResultsPdf: string | null;
  teams: Team[];
  segments: Segment[];
}

/** A registered team the matcher could not place into a category. */
export interface RosterUnmatched {
  name: string;
  org: string;
  eventLabel: string;
  reason: string;
}

/** A team that registered for an event but appears in no result sheet. */
export interface RosterWithdrawn {
  name: string;
  org: string;
  eventLabel: string;
}

/** Outcome of the last roster import (or automatic re-match), persisted in
 * metadata.json so the UI can show it long after the import request. */
export interface RosterImport {
  at: string;
  imported: number;
  moved: number;
  unmatched: RosterUnmatched[];
  withdrawn: RosterWithdrawn[];
}

export interface EventInfo {
  title: string;
  organization: string;
  authorization: string;
  city: string;
  rink: string;
  dates: string;
}

export interface Structure {
  id: string;
  name: string;
  createdBy: string;
  createdDate: string;
  event: EventInfo;
  coverPage: PageRef;
  lastPage: PageRef;
  header: PageRef;
  footer: PageRef;
  footerEnabled: boolean;
  scheduleParsed: boolean;
  files: Record<string, FileMeta>;
  categories: Category[];
  rosterImport?: RosterImport;
  schedule?: any[];
}

// A slot target — where a file can be dropped (matches structure.assign_file).
export interface SlotTarget {
  kind:
    | 'cover'
    | 'lastPage'
    | 'header'
    | 'footer'
    | 'tray'
    | 'categoryTitle'
    | 'totalResults'
    | 'podiumPhoto'
    | 'teamPhoto'
    | 'teamPhotoFallback'
    | 'segment';
  categoryId?: string;
  segmentId?: string;
  teamId?: string;
  role?: SegmentRole;
}

export interface CompetitionDetails {
  structure: Structure;
  unassigned: string[];
  generatedFiles: GeneratedFile[];
}

export interface GeneratedFile {
  fileName: string;
  url: string;
  description: string;
  expiration: string;
  size: number | string;
}
