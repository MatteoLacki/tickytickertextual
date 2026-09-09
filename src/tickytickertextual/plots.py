"""Scalable plot documents generated without filesystem artifacts."""
import html
import json
import xml.etree.ElementTree as ET

import numpy as np


def svg_viewer_html(svg, filename):
    # Data enters as a JSON string, never executable markup from XML metadata.
    payload = json.dumps(svg).replace("<", "\\u003c")
    name = json.dumps(filename).replace("<", "\\u003c")
    return f'''<!doctype html><meta charset="utf-8"><title>{html.escape(filename)}</title>
<style>body{{margin:0;background:#0d1117;color:white}}button{{position:sticky;top:0;padding:12px}}
svg{{display:block;width:100%;height:auto}}</style>
<button id="save">Download hi-res SVG</button><div id="plot"></div>
<script>
const svgText = {payload};
const parsed = new DOMParser().parseFromString(svgText, 'image/svg+xml');
document.getElementById('plot').appendChild(document.importNode(parsed.documentElement, true));
let downloadUrl;
document.getElementById('save').onclick = () => {{
  if (!downloadUrl) downloadUrl = URL.createObjectURL(new Blob([svgText], {{type:'image/svg+xml'}}));
  const link = document.createElement('a'); link.href=downloadUrl; link.download={name}; link.click();
}};
window.addEventListener('pagehide', () => {{
  if (downloadUrl) URL.revokeObjectURL(downloadUrl); downloadUrl = null;
}});
</script>'''


def event_histogram_svg(result):
    counts = np.asarray(result.histogram, dtype=np.uint64)
    logged = np.log1p(counts.astype(float))
    maximum = max(1.0, float(logged.max(initial=0)))
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1000" viewBox="0 0 1600 1000">',
             '<rect width="100%" height="100%" fill="#0d1117"/>',
             '<g fill="#c9d1d9" font-family="monospace" font-size="22">',
             '<text x="800" y="40" text-anchor="middle">Raw-event intensity histogram</text>',
             '<text x="800" y="965" text-anchor="middle">Raw intensity (last bin: ≥128)</text>',
             '<text x="25" y="480" transform="rotate(-90 25 480)" text-anchor="middle">Event count (log scale)</text>']
    left, top, width, height = 150, 90, 1390, 800
    for fraction in np.linspace(0, 1, 6):
        y = top + height * (1-fraction)
        count = int(round(np.expm1(maximum * fraction)))
        parts.append(f'<path d="M {left} {y} h {width}" stroke="#30363d"/>')
        parts.append(f'<text x="140" y="{y+7}" text-anchor="end">{count:,}</text>')
    bin_width = width / max(1, len(counts))
    for index, value in enumerate(logged):
        bar_height = float(value) / maximum * height
        parts.append(f'<rect x="{left+index*bin_width:.3f}" y="{top+height-bar_height:.3f}" '
                     f'width="{max(.1, bin_width-1):.3f}" height="{bar_height:.3f}" fill="#4c78a8"/>')
    threshold = float(result.settings.min_intensity)
    if 1 <= threshold <= 128:
        x = left + (threshold-1) * bin_width
        parts.append(f'<path d="M {x} {top} v {height}" stroke="#ff7b72" stroke-width="3"/>')
    parts.append(f'<text x="1540" y="70" text-anchor="end">minimum={threshold:g}</text>')
    for intensity in (1, 32, 64, 96, 128):
        parts.append(f'<text x="{left+(intensity-1)*bin_width}" y="925">{intensity}</text>')
    return "\n".join(parts + ['</g></svg>'])


def combined_chromatograms_svg(datasets):
    from .app import tic_trace_svg
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="{1120*len(datasets)}" '
             f'viewBox="0 0 1600 {1120*len(datasets)}">',
             '<rect width="100%" height="100%" fill="#0d1117"/>']
    for index, (path, description, result) in enumerate(datasets):
        nested = ET.fromstring(tic_trace_svg(result, path))
        nested.set("y", str(index*1120+60))
        parts.append(f'<text x="30" y="{index*1120+35}" fill="#f0f6fc" font-size="20" '
                     f'font-family="monospace">{html.escape(path.name)} — {html.escape(description)}</text>')
        parts.append(ET.tostring(nested, encoding="unicode"))
        parts.append(f'<path d="M 0 {index*1120+1115} h 1600" stroke="#8b949e" stroke-width="3"/>')
    return "\n".join(parts + ['</svg>'])
