// Build step: split data/feed/<segment>.json into small static files that the
// CDN can cache, so the website stops calling /api/data for the whole feed.
//
// Why: /api/data read and parsed an 81 MB file on every call and sent back a
// 64 MB response. Vercel's CDN does not cache a response that large (it showed
// MISS every time), so each call cost function CPU plus ~64 MB of origin
// transfer, roughly 120 times a day. Static files are served from the CDN
// cache, compressed, with no function running.
//
// Output (public/feed/):
//   <segment>/manifest.json        { segment, total, parts, generatedAt, bytes }
//   <segment>/<n>.json             { results: [...] }   n = 0..parts-1, in feed order
//
// Each part stays under PART_BYTES so Next's own fetch cache (2 MB limit) can
// hold it in the website, where a single 80 MB response never fit.
const fs = require("fs");
const path = require("path");

const SRC = path.join(process.cwd(), "data", "feed");
const OUT = path.join(process.cwd(), "public", "feed");
const PART_BYTES = 1_800_000;

fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });

let files = 0;
let bytes = 0;
for (const name of fs.readdirSync(SRC).filter((f) => f.endsWith(".json")).sort()) {
  const segment = name.replace(/\.json$/, "");
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(path.join(SRC, name), "utf-8"));
  } catch (err) {
    console.warn(`split-feed: skipping ${name}: ${err.message}`);
    continue;
  }
  const results = Array.isArray(payload.results) ? payload.results : [];
  const dir = path.join(OUT, segment);
  fs.mkdirSync(dir, { recursive: true });

  const parts = [];
  let current = [];
  let size = 2;
  for (const row of results) {
    const s = JSON.stringify(row);
    if (current.length && size + Buffer.byteLength(s) + 1 > PART_BYTES) {
      parts.push(current);
      current = [];
      size = 2;
    }
    current.push(s);
    size += Buffer.byteLength(s) + 1;
  }
  if (current.length || parts.length === 0) parts.push(current);

  let segBytes = 0;
  parts.forEach((rows, i) => {
    const body = `{"results":[${rows.join(",")}]}`;
    fs.writeFileSync(path.join(dir, `${i}.json`), body);
    segBytes += Buffer.byteLength(body);
    files += 1;
  });
  fs.writeFileSync(
    path.join(dir, "manifest.json"),
    JSON.stringify({ segment, total: results.length, parts: parts.length, bytes: segBytes, generatedAt: new Date().toISOString() })
  );
  files += 1;
  bytes += segBytes;
  if (results.length) console.log(`split-feed: ${segment}: ${results.length} rows -> ${parts.length} parts`);
}
console.log(`split-feed: ${files} files, ${(bytes / 1e6).toFixed(1)} MB`);
