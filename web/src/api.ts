export type MountTel = {
  connected: boolean;
  ra_hours: number | null;
  dec_deg: number | null;
  alt_deg: number | null;
  az_deg: number | null;
  slewing: boolean;
  tracking: boolean;
  parked: boolean;
};

export type CameraTel = {
  connected: boolean;
  exposing: boolean;
  exposure_remaining: number;
};

export type FocuserTel = {
  connected: boolean;
  position: number | null;
  moving: boolean;
};

export type FilterTel = {
  connected: boolean;
  position: number | null;
  name: string | null;
  names: string[];
  moving: boolean;
};

export type ExecutorTel = {
  state: string; // idle | running | paused
  block_id: string | null;
  object: string | null;
  mode: string | null;
  step: string | null;
  current_step: number;
  total: number;
  n_done: number;
  n_failed: number;
  exposure_remaining: number;
  manual_override: boolean;
  auto_execute: boolean;
  awaiting_confirm?: string | null;
};

export type GuidingTel = {
  connected: boolean;
  state: string;
  rms_ra: number | null;
  rms_dec: number | null;
  n: number;
};

export type GuideSample = { t: string; ra: number | null; dec: number | null; snr: number | null };
export type GuideGraph = GuidingTel & { samples: GuideSample[] };

export type PrecisionTel = {
  busy: boolean;
  enabled: boolean;
  center: {
    running: boolean; ok: boolean | null; iterations: number;
    error_arcsec: number | null; message: string;
    solved: { ra_deg: number; dec_deg: number } | null;
  };
  autofocus: {
    running: boolean; ok: boolean | null; best_position: number | null; best_hfr: number | null;
    samples: { position: number; hfr: number | null; stars: number }[]; message: string;
  };
};

export type SafetyTel = {
  state: string; // safe | warn | unsafe | fault
  reasons: string[];
  override: boolean;
  estop: boolean;
  enabled: boolean;
  weather: {
    humidity?: number; wind_speed?: number; temperature?: number;
    clouds?: number; rain?: boolean; condition?: string; source?: string; updated_at?: number;
  };
  sun_alt: number | null;
};

export type Telemetry = {
  ts: string;
  indi_connected: boolean;
  server: { host: string; port: number };
  last_image_at: string | null;
  last_guide_image_at: string | null;
  devices: IndiDevice[];
  mount: MountTel | null;
  camera: CameraTel | null;
  guider: CameraTel | null;
  focuser: FocuserTel | null;
  filter: FilterTel | null;
  bindings: Record<string, string | null>;
  executor?: ExecutorTel | null;
  guiding?: GuidingTel | null;
  safety?: SafetyTel | null;
  precision?: PrecisionTel | null;
};

export type IndiDevice = {
  device: string;
  roles: string[];
  connected: boolean;
  conn_state: string | null; // INDI CONNECTION state: "Ok" | "Alert" | "Busy" | "Idle"
  bound_as: string | null;
  has_port: boolean;
  port: string | null;
};

// The standard observatory roles, shown in fixed order regardless of what is
// plugged in. A single INDI device may be a candidate for several of these (a
// QHY camera+filter bundle, or an imaging camera that can also guide).
export const ROLES = ["mount", "camera", "guide", "focuser", "filter"] as const;
export type Role = (typeof ROLES)[number];

export const ROLE_LABELS: Record<Role, string> = {
  mount: "Mount",
  camera: "Main Camera",
  guide: "Guide Camera",
  focuser: "Focuser",
  filter: "Filter Wheel",
};

export type ImageRec = {
  id: string;
  obsid: string;
  date_obs: string;
  exptime: number;
  image_type: string;
  object_name: string;
  filter: string | null;
  ra_deg: number | null;
  dec_deg: number | null;
  width: number | null;
  height: number | null;
  created_at: string;
};

// --- transient follow-up --------------------------------------------------
export type Candidate = {
  id: string;
  alert_id: string;
  ut_date: string;
  class_label: string | null;
  class_prob: number | null;
  ra_deg: number;
  dec_deg: number;
  mag: number | null;
  state: string; // new | notified | approved_queue | approved_execute | rejected | expired
  score: number;
  window_start_utc: string | null;
  window_end_utc: string | null;
  max_alt_deg: number | null;
  min_airmass: number | null;
  moon_sep_deg: number | null;
  moon_illum_frac: number | null;
  observable: boolean; // clears the horizon limit during tonight's dark window
  decided_by: string | null;
  request_id: string | null;
};

export type CandidateGroups = {
  groups: Record<string, Candidate[]>;
  count: number;
  observable: number;
};

export type QueueBlock = {
  id: string;
  request_id: string;
  state: string;
  seq: number;
  total_steps: number;
  current_step: number;
  n_done: number;
  n_failed: number;
  class_label: string | null;
  request?: {
    object_name: string;
    mode: string;
    ra_deg: number;
    dec_deg: number;
    window_start_utc: string | null;
    window_end_utc: string | null;
    recipe_json?: ExposureSet[] | null;
  };
};

export type NightInfo = {
  ut_date: string;
  start_utc: string;
  end_utc: string;
  twilight_used: number;
  n_samples: number;
};

export type ActivityEvent = { ts: string; msg: string; kind: string };

export type ResolveResult = { name: string; ra_hours: number; ra_deg: number; dec_deg: number };

export type ExposureSet = {
  filter_slot: number | null;
  filter_name: string;
  exptime_s: number;
  count: number;
  binning: number;
  dither_px: number;
  image_type?: string; // LIGHT | BIAS | DARK | FLAT
};

export type Plan = {
  id: string;
  name: string;
  object_name: string;
  ra_deg: number | null;
  dec_deg: number | null;
  recipe_json: ExposureSet[] | null;
  repeat: number;
  autofocus: boolean;
  center: boolean;
  source: string | null;
  last_block_id: string | null;
  last_run_at: string | null;
  updated_at: string;
};

// Active site backend base URL ("" = same-origin). Selecting a telescope on the
// dashboard points all API/WS/image calls at that site's backend.
let apiBase = "";
let token = localStorage.getItem("cassa_token") || "";
let role = localStorage.getItem("cassa_role") || "";
let username = localStorage.getItem("cassa_user") || "";

export const ROLE_RANK: Record<string, number> = { viewer: 1, observer: 2, operator: 3, admin: 4 };

export function setApiBase(b: string | null | undefined): void {
  apiBase = b ? b.replace(/\/+$/, "") : "";
}
export function getApiBase(): string {
  return apiBase;
}
export function setAuth(t: string, r: string, u: string): void {
  token = t; role = r; username = u;
  localStorage.setItem("cassa_token", t);
  localStorage.setItem("cassa_role", r);
  localStorage.setItem("cassa_user", u);
}
export function clearAuth(): void {
  token = ""; role = ""; username = "";
  localStorage.removeItem("cassa_token");
  localStorage.removeItem("cassa_role");
  localStorage.removeItem("cassa_user");
}
export function getToken(): string { return token; }
export function getRole(): string { return role; }
export function getUsername(): string { return username; }
export function isAuthed(): boolean { return !!token; }
export function atLeast(r: string): boolean {
  return (ROLE_RANK[role] || 0) >= (ROLE_RANK[r] || 99);
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  return token ? { ...(extra || {}), Authorization: `Bearer ${token}` } : (extra || {});
}

export function apiUrl(path: string): string {
  return apiBase + path;
}
// for <img src>/<a href> — the browser can't send headers, so pass the token as a query param
export function mediaUrl(path: string): string {
  const u = apiBase + path;
  return token ? u + (u.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token) : u;
}
export function wsUrl(path: string): string {
  const host = !apiBase
    ? `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}`
    : (() => {
        const u = new URL(apiBase);
        return `${u.protocol === "https:" ? "wss:" : "ws:"}//${u.host}`;
      })();
  return host + path + (token ? `?token=${encodeURIComponent(token)}` : "");
}

export async function del(path: string): Promise<unknown> {
  const res = await fetch(apiBase + path, { method: "DELETE", headers: authHeaders() });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json();
}

export async function post(path: string, body?: unknown): Promise<unknown> {
  const res = await fetch(apiBase + path, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json();
}

export async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(apiBase + path, { headers: authHeaders() });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json() as Promise<T>;
}

// authed cross-site fetch (dashboard queries each site's backend; same token works everywhere)
export async function siteGet<T>(baseUrl: string, path: string): Promise<T> {
  const res = await fetch((baseUrl || "") + path, { headers: authHeaders() });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

// --- locations dashboard --------------------------------------------------
export type SiteRef = { id: string; name: string; url: string };
export type DashTelescope = { id: string; name: string; indi_host: string; indi_port: number; status: string };
export type SiteInfo = {
  id: string;
  name: string;
  status: string;
  location: { latitude_deg?: number; longitude_deg?: number; timezone?: string };
  weather: { condition?: string | null; seeing?: string | null; humidity?: number | null; temperature?: number | null } | null;
  safety?: string | null;
  telescopes: DashTelescope[];
  indi_connected: boolean;
};
