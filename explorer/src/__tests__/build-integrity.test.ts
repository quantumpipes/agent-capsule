import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const root = process.cwd();
const dist = (p: string) => join(root, "dist", p);
const built = existsSync(dist("index.html")); // requires `npm run build` first

describe.skipIf(!built)("build integrity (marketing-site playbook pillars)", () => {
  const html = built ? readFileSync(dist("index.html"), "utf8") : "";

  it("ships the discovery + security surface", () => {
    for (const f of ["robots.txt", "manifest.webmanifest", "sitemap-index.xml", "og.png", "favicon.svg", "_headers"]) {
      expect(existsSync(dist(f)), `dist/${f} should exist`).toBe(true);
    }
  });

  it("_headers carries a CSP, HSTS, and immutable asset caching", () => {
    const h = readFileSync(dist("_headers"), "utf8");
    expect(h).toMatch(/Content-Security-Policy:/);
    expect(h).toMatch(/Strict-Transport-Security:/);
    expect(h).toMatch(/max-age=31536000, immutable/);
  });

  it("home page has canonical, JSON-LD, OG image, and theme color", () => {
    expect(html).toMatch(/rel="canonical"/);
    expect(html).toMatch(/application\/ld\+json/);
    expect(html).toMatch(/property="og:image"/);
    expect(html).toMatch(/name="theme-color"/);
  });

  it("body uses min-h-svh (iOS-safe), not min-h-screen", () => {
    const body = html.match(/<body[^>]*>/)?.[0] ?? "";
    expect(body).toMatch(/min-h-svh/);
    expect(body).not.toMatch(/min-h-screen/);
  });

  it("inlines CSS (no render-blocking stylesheet link)", () => {
    expect(html).not.toMatch(/<link rel="stylesheet"/);
  });
});
