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
  members: string[];
}

export interface Segment {
  id: string;
  name: string;
  order: number;
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
