// Render terminal SVGs with system fonts, preserving cell widths and clipping.
const fs = require('node:fs');
const { Resvg } = require('@resvg/resvg-js');
const [input, output] = process.argv.slice(2);
if (!input || !output) throw new Error('Usage: node render-svg.cjs input.svg output.png');
const svg = new Resvg(fs.readFileSync(input), {
  font: { loadSystemFonts: true, defaultFontFamily: 'Menlo' },
});
fs.writeFileSync(output, svg.render().asPng());
