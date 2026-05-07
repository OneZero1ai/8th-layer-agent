/**
 * find_expert — group cq knowledge units by created_by to answer
 * "who knows about <topic>?".
 *
 * Strategy:
 *   1. FTS5 match `topic` against cq's summary/detail/action.
 *   2. Also domain-tag match: split topic into words, any word that's
 *      a known domain tag pulls in those units too.
 *   3. Union results; group by created_by; rank by count then recency.
 *
 * Reads ~/.local/share/cq/local.db directly (readonly) — no dependency
 * on cq being up as an MCP server in this session.
 */
import Database from "better-sqlite3";
import { existsSync } from "fs";
import { homedir } from "os";
import { join } from "path";

const DEFAULT_DB = join(homedir(), ".local", "share", "cq", "local.db");

let _db = null;
function cqDb() {
  if (_db) return _db;
  const path = process.env.CQ_DB_PATH || DEFAULT_DB;
  if (!existsSync(path)) return null;
  _db = new Database(path, { readonly: true, fileMustExist: true });
  return _db;
}

/**
 * FTS5 needs careful tokenization. Hyphenated tokens like "shim-test" tokenize
 * as `shim` followed by `test`, and `test*` is then treated as a column name,
 * blowing the query with "no such column: test". Split on both whitespace
 * AND hyphens, strip other non-word chars, and join with OR for generous recall.
 */
function sanitizeFtsQuery(topic) {
  const tokens = topic
    .trim()
    .toLowerCase()
    .split(/[\s-]+/)
    .map((t) => t.replace(/[^\w]/g, ""))
    .filter((t) => t.length > 0);
  if (!tokens.length) return null;
  return tokens.map((t) => `${t}*`).join(" OR ");
}

/**
 * @param {string} topic
 * @param {number} limit
 * @returns {Array<{session: string, units: number, most_recent: string|null, top_domain: string|null}>}
 */
export function findExpert(topic, limit = 5) {
  const db = cqDb();
  if (!db) return [];

  const ftsQ = sanitizeFtsQuery(topic);
  if (!ftsQ) return [];

  // Tokens also used for domain tag match.
  const tokens = topic.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const placeholders = tokens.map(() => "?").join(",");

  // Collect matching unit IDs (FTS + domain-tag match, union).
  const hitsSql = `
    SELECT id FROM knowledge_units_fts WHERE knowledge_units_fts MATCH ?
    UNION
    SELECT unit_id AS id FROM knowledge_unit_domains
      WHERE domain IN (${placeholders || "''"})
  `;

  try {
    const hitIds = db.prepare(hitsSql).all(ftsQ, ...tokens).map((r) => r.id);
    if (!hitIds.length) return [];

    const inList = hitIds.map(() => "?").join(",");
    const rows = db
      .prepare(`
        SELECT
          COALESCE(NULLIF(json_extract(u.data,'$.created_by'),''), 'unknown') AS author,
          COUNT(DISTINCT u.id) AS units,
          MAX(json_extract(u.data,'$.evidence.last_confirmed')) AS most_recent
        FROM knowledge_units u
        WHERE u.id IN (${inList})
        GROUP BY author
        ORDER BY units DESC, most_recent DESC
        LIMIT ?
      `)
      .all(...hitIds, limit);

    // Step 3: per-author top domain (cheap — one query per ranked row).
    return rows.map((r) => {
      const topDomainRow = db
        .prepare(`
          SELECT d.domain AS domain, COUNT(*) AS n
          FROM knowledge_unit_domains d
          JOIN knowledge_units u2 ON u2.id = d.unit_id
          WHERE d.unit_id IN (${inList})
            AND COALESCE(NULLIF(json_extract(u2.data,'$.created_by'),''), 'unknown') = ?
          GROUP BY d.domain
          ORDER BY n DESC
          LIMIT 1
        `)
        .get(...hitIds, r.author);
      return {
        session: r.author,
        units: r.units,
        most_recent: r.most_recent ? String(r.most_recent).slice(0, 10) : null,
        top_domain: topDomainRow?.domain || null,
      };
    });
  } catch (err) {
    process.stderr.write(`[find_expert] query failed: ${err.message}\n`);
    return [];
  }
}
