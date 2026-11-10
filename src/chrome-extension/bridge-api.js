/**
 * 本地后端的唯一出口（CARD-09b 第 6 条）。
 *
 * 只认 127.0.0.1:18765：地址写死在这里，manifest 里也只申请了这一条
 * host_permissions；token 只从 chrome.storage.local 读进来，不拼进 URL、
 * 不写日志、不进 DOM 的隐藏字段以外的地方。
 */

export const BASE = "http://127.0.0.1:18765";
export const HEALTH_TIMEOUT_MS = 3000;        // 卡面：/health 3 秒超时即红字（≤10s）
export const JOB_TTL_MS = 24 * 60 * 60 * 1000; // 24h 过期清理
export const PURPOSES = ["test", "review", "accept"];

/** 后端返回的错误按种类归一，界面只认 kind，不去解析文案。 */
export class BridgeError extends Error {
  constructor(kind, message, { status = 0, code = "" } = {}) {
    super(message);
    this.name = "BridgeError";
    this.kind = kind;      // offline | unauthorized | bad_request | not_found | server
    this.status = status;
    this.code = code;
  }
}

function classify(status, payload) {
  const code = String((payload && payload.error) || "");
  const message = String((payload && payload.message) || code || `HTTP ${status}`);
  if (status === 401) return new BridgeError("unauthorized", message, { status, code });
  if (status === 404) return new BridgeError("not_found", message, { status, code });
  if (status === 400 || status === 413 || status === 429) {
    return new BridgeError("bad_request", message, { status, code });
  }
  return new BridgeError("server", message, { status, code });
}

async function call(path, { token, method = "GET", body, timeout = 0 } = {}) {
  const controller = timeout > 0 ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeout) : 0;
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(BASE + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller ? controller.signal : undefined,
      cache: "no-store",
      credentials: "omit",
    });
  } catch (error) {
    const aborted = error && error.name === "AbortError";
    throw new BridgeError("offline",
      aborted ? `本地后端 ${timeout} 毫秒内没有响应` : "连不上本地后端",
      { code: aborted ? "timeout" : "network" });
  } finally {
    if (timer) clearTimeout(timer);
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) throw classify(response.status, payload);
  return payload || {};
}

/** /health 不需要 token；超时 3 秒，用来点亮「在线/离线」。 */
export function health() {
  return call("/health", { timeout: HEALTH_TIMEOUT_MS });
}

/** /endpoints 列出 tiers.json 里登记过的端与用途档位。 */
export function endpoints(token) {
  return call("/endpoints", { token });
}

/** /ask：purpose 必填（test/review/accept），request_id 用于后端去重。 */
export function ask(token, { endpoints: names, prompt, purpose, requestId }) {
  return call("/ask", {
    token,
    method: "POST",
    body: { endpoints: names, prompt, purpose, request_id: requestId },
  });
}

/** /jobs/<id>：重启后仍从 runs\jobs 读得到；404 表示这个 job 没了。 */
export function job(token, jobId) {
  return call(`/jobs/${encodeURIComponent(jobId)}`, { token });
}
