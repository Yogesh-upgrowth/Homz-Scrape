// Vercel serverless function — the site's `/api/data?city={segment}` endpoint.
//
// Replaces the standalone "Homz-Back" project: that repo had drifted out of
// sync (stale 36-record data, and a different file shape) from what
// `homz export feed` actually produces, and maintaining two repos for one
// endpoint wasn't buying anything. This lives in the same repo as the
// scraper/exporter, reading the same `data/feed/*.json` files directly.
//
// Deploy note: `homz export feed` writes each city+category segment already
// wrapped as `{success, city, page, limit, total, results}` — not a bare
// array like the old service expected. This handler re-slices `results` by
// the *request's* page/limit (not whatever was baked in at export time), so
// pagination still works even though the file itself is written in full.
const fs = require("fs");
const path = require("path");

module.exports = function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");

  if (req.method === "OPTIONS") {
    return res.status(200).end();
  }

  const { city, page = "1", limit = "500" } = req.query;

  if (!city) {
    return res.status(400).json({ error: "City is required" });
  }

  const filePath = path.join(process.cwd(), "data", "feed", `${city}.json`);
  if (!fs.existsSync(filePath)) {
    return res.status(404).json({ error: "City not found" });
  }

  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(filePath, "utf-8"));
  } catch {
    return res.status(500).json({ error: "Server error" });
  }

  const allResults = Array.isArray(payload.results) ? payload.results : [];
  const total = typeof payload.total === "number" ? payload.total : allResults.length;

  const pageNum = Math.max(parseInt(page, 10) || 1, 1);
  const limitNum = Math.max(parseInt(limit, 10) || 500, 1);
  const start = (pageNum - 1) * limitNum;

  return res.status(200).json({
    success: true,
    city,
    page: pageNum,
    limit: limitNum,
    total,
    results: allResults.slice(start, start + limitNum),
  });
};
