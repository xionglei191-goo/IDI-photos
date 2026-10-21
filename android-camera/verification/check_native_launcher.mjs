import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const page = fs.readFileSync(new URL('../app/src/main/assets/idi-native-camera/index.html', import.meta.url), 'utf8');
const script = page.match(/<script>([\s\S]*?)<\/script>/)[1];
const fields = new Map([
  ['appid', 'SYNTHETIC'], ['signature', 'abc+/def=='],
  ['案件号', 'ZLBH202610200000000001'], ['小区', '铂悦府（测试）'],
  ['位置', '1栋 · 201室'], ['环节', '施工 · 楼栋'],
  ['内容', 'C++ 验收 + A&B = 100%\n"第二行"'],
]);
let destination;
vm.runInNewContext(script, {
  URLSearchParams,
  window: {location: {search: '?' + new URLSearchParams(fields), replace: value => { destination = value; }}},
});
assert(destination.startsWith('../XheyCameraSDKAssets/index.html?'));
// Match the supplied renderer's parser: it decodes percent escapes, not form '+'.
const actual = new Map(destination.split('?')[1].split('&').map(pair => pair.split('=').map(decodeURIComponent)));
for (const [name, value] of fields) assert.equal(actual.get(name), value, name);
assert.equal(actual.get('watermarkScale'), '0.68');
console.log('Native launcher: signed values, spaces, literal plus, Unicode and newlines preserved; SDK scale 0.68.');
