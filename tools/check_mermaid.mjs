import fs from 'fs';
import { JSDOM } from 'jsdom';
const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>', { pretendToBeVisual: true });
for (const k of ['window','document','navigator','Element','SVGElement','Node','DOMParser','MutationObserver','getComputedStyle']) {
  try { Object.defineProperty(globalThis, k, { value: k==='window'?dom.window:dom.window[k], configurable: true, writable: true }); } catch(e){}
}
const mermaid = (await import('mermaid')).default;
mermaid.initialize({ startOnLoad: false, securityLevel: 'loose' });
let bad = 0, n = 0;
for (const file of process.argv.slice(2)) {
  const src = fs.readFileSync(file, 'utf8');
  const blocks = [...src.matchAll(/```mermaid\r?\n([\s\S]*?)```/g)].map(m => m[1]);
  for (const [i, b] of blocks.entries()) {
    n++;
    try { await mermaid.parse(b); console.log(`OK   ${file} #${i+1}  (${b.split('\n')[0].trim()})`); }
    catch (e) { bad++; console.log(`FAIL ${file} #${i+1}: ${String(e.message||e).split('\n').slice(0,6).join(' | ')}`); }
  }
}
console.log(`\n${n-bad}/${n} mermaid blocks valid`);
process.exit(bad ? 1 : 0);
