import { createHash, createHmac } from "node:crypto";

function storageConfig() {
  const account = process.env.R2_ACCOUNT_ID;
  const endpoint = process.env.R2_ENDPOINT_URL ?? process.env.R2_ENDPOINT ?? (account ? `https://${account}.r2.cloudflarestorage.com` : undefined);
  const bucket = process.env.R2_BUCKET_NAME ?? process.env.R2_BUCKET;
  const accessKey = process.env.R2_ACCESS_KEY_ID;
  const secretKey = process.env.R2_SECRET_ACCESS_KEY;
  if (!endpoint || !bucket || !accessKey || !secretKey) return null;
  let url: URL;
  try { url = new URL(endpoint); } catch { return null; }
  if (url.protocol !== "https:" || !/^[a-z0-9]+\.r2\.cloudflarestorage\.com$/.test(url.hostname)
    || url.username || url.password || url.search || url.hash || !/^[a-z0-9][a-z0-9.-]{0,62}$/.test(bucket)) return null;
  return { origin: url.origin, bucket, accessKey, secretKey };
}

export function prod2StorageAvailable() { return storageConfig() !== null; }

/** The PDF may be a preview converted from ZIP/DOCX; its hash is separate from the original download. */
export function validProd2PreviewKey(storageKey: string, idConvocatoria: string, documentCode: string) {
  if (!/^[1-9]\d{0,18}$/.test(idConvocatoria) || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(documentCode)) return false;
  return new RegExp(`^prod2/${idConvocatoria}/${documentCode.toLowerCase()}/[0-9a-f]{64}\\.pdf$`).test(storageKey);
}

const encode = (value: string) => encodeURIComponent(value).replace(/[!'()*]/g, char => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
const hash = (value: string) => createHash("sha256").update(value).digest("hex");
const hmac = (key: string | Buffer, value: string) => createHmac("sha256", key).update(value).digest();

/** Short-lived read-only URL for a verified PROD2 object. Credentials never reach application JSON. */
export function signedProd2DocumentUrl(storageKey: string, idConvocatoria: string, documentCode: string, now = new Date()) {
  if (!validProd2PreviewKey(storageKey, idConvocatoria, documentCode)) return null;
  const config = storageConfig();
  if (!config) return null;
  const timestamp = now.toISOString().replace(/[:-]|\.\d{3}/g, "");
  const day = timestamp.slice(0, 8);
  const scope = `${day}/auto/s3/aws4_request`;
  const path = `/${encode(config.bucket)}/${storageKey.split("/").map(encode).join("/")}`;
  const entries = Object.entries({ "X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": `${config.accessKey}/${scope}`,
    "X-Amz-Date": timestamp, "X-Amz-Expires": "60", "X-Amz-SignedHeaders": "host" });
  const canonicalQuery = entries.map(([key, value]) => [encode(key), encode(value)]).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, value]) => `${key}=${value}`).join("&");
  const host = new URL(config.origin).host;
  const request = ["GET", path, canonicalQuery, `host:${host}\n`, "host", "UNSIGNED-PAYLOAD"].join("\n");
  const stringToSign = ["AWS4-HMAC-SHA256", timestamp, scope, hash(request)].join("\n");
  const key = hmac(hmac(hmac(hmac(`AWS4${config.secretKey}`, day), "auto"), "s3"), "aws4_request");
  const signature = createHmac("sha256", key).update(stringToSign).digest("hex");
  return `${config.origin}${path}?${canonicalQuery}&X-Amz-Signature=${signature}`;
}
