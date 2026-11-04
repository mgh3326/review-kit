/**
 * Snapshot settings passed in by `review-kit site build` (review_kit/site.py).
 * A build without them fails: every page must show when its data was read.
 * The values are checked here because the banner renders them as HTML.
 */
function setting(name: string, pattern: RegExp): string {
  const value = process.env[name];
  if (!value || !pattern.test(value)) {
    throw new Error(`${name} is missing or malformed; build with \`review-kit site build\``);
  }
  return value;
}

/** e.g. "2026-11-04 09:07 +09:00" */
export const SNAPSHOT = setting("REVIEW_SITE_SNAPSHOT", /^\d{4}-\d{2}-\d{2} \d{2}:\d{2} [+-]\d{2}:\d{2}$/);
