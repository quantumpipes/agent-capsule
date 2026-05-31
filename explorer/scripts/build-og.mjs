// Render OG + Apple touch icon PNGs from the SVG sources.
//
// OG images and Apple touch icons must be PNG for broad crawler/platform
// support; we keep the source in SVG (tiny, hand-editable) and rasterize at
// build time. Self-hosted woff2 fonts are loaded so typography matches the
// site. Fail-soft: if @resvg/resvg-js or the SVG is missing, warn and exit 0
// so `npm run build` never breaks over a social-preview image.

import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const publicDir = join(here, "..", "public");

async function main() {
  let Resvg;
  try {
    ({ Resvg } = await import("@resvg/resvg-js"));
  } catch {
    console.warn("⚠  @resvg/resvg-js not installed — skipping OG PNG generation.");
    return;
  }

  const fonts = ["outfit.woff2", "inter.woff2", "jetbrains-mono.woff2"]
    .map((f) => join(publicDir, "fonts", f))
    .filter(existsSync)
    .map((p) => readFileSync(p));

  const render = (svgFile, outFile, width) => {
    const svgPath = join(publicDir, svgFile);
    if (!existsSync(svgPath)) {
      console.warn(`⚠  ${svgFile} missing — skipping ${outFile}.`);
      return;
    }
    const resvg = new Resvg(readFileSync(svgPath), {
      fitTo: { mode: "width", value: width },
      font: { fontBuffers: fonts, loadSystemFonts: false },
    });
    writeFileSync(join(publicDir, outFile), resvg.render().asPng());
    console.log(`✓ ${outFile}`);
  };

  render("og.svg", "og.png", 1200);
  render("favicon.svg", "apple-touch-icon.png", 180);
}

main().catch((e) => {
  console.warn("⚠  OG build failed (non-fatal):", e?.message ?? e);
});
