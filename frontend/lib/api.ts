import useSWR, { type SWRConfiguration } from "swr";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

function errorMessage(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (typeof d === "object" && d && "msg" in d ? `${(d as { loc?: unknown[] }).loc?.slice(-1)[0] ?? ""}: ${(d as { msg: string }).msg}` : String(d)))
      .join("; ");
  }
  return "Er ging iets mis";
}

export async function api<T = unknown>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, ...rest } = init;
  const headers = new Headers(rest.headers);
  if (json !== undefined) headers.set("content-type", "application/json");
  const res = await fetch(path, { ...rest, headers, body: json !== undefined ? JSON.stringify(json) : rest.body });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  const data = text ? (() => { try { return JSON.parse(text); } catch { return text; } })() : undefined;
  if (!res.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : data;
    throw new ApiError(errorMessage(detail), res.status);
  }
  return data as T;
}

export const fetcher = <T,>(path: string) => api<T>(path);

export function useApi<T>(path: string | null, config?: SWRConfiguration<T>) {
  return useSWR<T>(path, fetcher, { revalidateOnFocus: true, keepPreviousData: true, ...config });
}

/** Upload with progress (fetch has no upload progress events). */
export function uploadFile<T = unknown>(
  path: string,
  form: FormData,
  onProgress?: (fraction: number) => void,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let data: unknown = undefined;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        data = xhr.responseText;
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data as T);
      else {
        const detail = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : data;
        reject(new ApiError(errorMessage(detail), xhr.status));
      }
    };
    xhr.onerror = () => reject(new ApiError("Upload mislukt (netwerkfout)", 0));
    xhr.send(form);
  });
}
