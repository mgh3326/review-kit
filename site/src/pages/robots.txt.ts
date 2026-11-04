export const prerender = true;

// A review snapshot is private operator material behind Cloudflare Access:
// nothing should index or train on it even if it were reachable.
export function GET() {
  const body = ["User-agent: *", "Disallow: /", ""].join("\n");
  return new Response(body, {
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
}
