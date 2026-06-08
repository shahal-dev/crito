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

export type Telemetry = {
  ts: string;
  indi_connected: boolean;
  last_image_at: string | null;
  mount: MountTel | null;
  camera: CameraTel | null;
  focuser: FocuserTel | null;
  filter: FilterTel | null;
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

export async function post(path: string, body?: unknown): Promise<unknown> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json();
}

export async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json() as Promise<T>;
}
