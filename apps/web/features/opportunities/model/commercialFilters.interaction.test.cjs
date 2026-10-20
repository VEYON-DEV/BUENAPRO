const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const web = path.resolve(__dirname, '../../..');
const repo = path.resolve(web, '../..');
const python = process.env.BUENAPRO_BROWSER_TEST_PYTHON || path.join(repo, '.venv/bin/python');
const browserAvailable = fs.existsSync(python) && spawnSync(python, ['-B', '-c', 'import playwright'], { timeout: 5000 }).status === 0;

test('real React category interaction auto-applies SEACE after commit and keeps minor filters manual',
  { skip: !browserAvailable && 'Optional Python Playwright runtime unavailable', timeout: 60000 }, async () => {
    const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'buenapro-category-interaction-'));
    try {
      fs.writeFileSync(path.join(folder, 'typescript-loader.cjs'), `const ts=require(${JSON.stringify(path.join(web, 'node_modules/typescript'))});module.exports=function(source){return ts.transpileModule(source,{fileName:this.resourcePath,compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText};`);
      fs.writeFileSync(path.join(folder, 'css-loader.cjs'), 'module.exports=function(){return "module.exports=new Proxy({},{get:(_,name)=>String(name)})"};');
      fs.writeFileSync(path.join(folder, 'entry.tsx'), `import React,{useEffect} from 'react';import {createRoot} from 'react-dom/client';import {Prod4OpportunityToolbar} from '@/features/opportunities/components/Prod4OpportunityToolbar';import {OpportunityToolbar} from '@/features/opportunities/components/OpportunityToolbar';function App(){useEffect(()=>{document.body.dataset.ready='true'},[]);const defaults=Object.fromEntries(new URLSearchParams(location.search));return window.__minor?<OpportunityToolbar defaults={defaults}/>:<Prod4OpportunityToolbar source="prod2" defaults={defaults}/>};createRoot(document.getElementById('root')!).render(<App/>);`);
      const { webpack } = require('next/dist/compiled/webpack/webpack');
      await new Promise((resolve, reject) => webpack({ mode: 'development', target: 'web', devtool: false,
        entry: path.join(folder, 'entry.tsx'), output: { path: path.join(folder, 'bundle'), filename: 'harness.js' },
        resolve: { extensions: ['.ts', '.tsx', '.js', '.json'], alias: { '@': web }, modules: [path.join(repo, 'node_modules')] },
        module: { rules: [{ test: /\.tsx?$/, use: path.join(folder, 'typescript-loader.cjs') }, { test: /\.css$/, use: path.join(folder, 'css-loader.cjs') }] },
        optimization: { minimize: false },
      }, (error, stats) => error || stats.hasErrors() ? reject(error || new Error('Browser interaction fixture compilation failed')) : resolve()));
      // All requests are fulfilled from this isolated fixture. No server, PG,
      // opportunity rows, source documents or model credentials are involved.
      const result = spawnSync(python, ['-B', '-c', String.raw`
import asyncio,json,re,sys
from pathlib import Path
from urllib.parse import parse_qs,urlencode,urlsplit
from playwright.async_api import async_playwright

async def main():
 bundle=Path(sys.argv[1]).read_bytes();errors=[];checks=[]
 async with async_playwright() as p:
  browser=await p.chromium.launch();context=await browser.new_context(service_workers='block')
  state={'minor':False,'listen':False,'query':None}
  async def fixture(route):
   request=route.request;url=urlsplit(request.url)
   assert request.method=='GET' and url.scheme=='http' and url.netloc=='category-qa.invalid','Unexpected external operation'
   if url.path=='/harness.js':return await route.fulfill(content_type='application/javascript',body=bundle)
   assert url.path=='/feed','Unexpected fixture path'
   if state['listen']:state['query']=parse_qs(url.query,keep_blank_values=True)
   html='<div id="root"></div><script>window.__minor='+str(state['minor']).lower()+'</script><script src="/harness.js"></script>'
   await route.fulfill(content_type='text/html',body=html)
  await context.route('**/*',fixture);page=await context.new_page();page.set_default_timeout(5000);page.on('pageerror',lambda e:errors.append(type(e).__name__))
  base={'source':'prod2','profile_id':'profile-test','q':'tableros','object':'good','page':'3'}
  cases=[('all-to-industrial',{},'industrial',False),('replace-category',{'commercial_category':'industrial'},'cloud_infraestructura',False),
    ('replace-legacy',{'commercial_area':'industrial_electrica','commercial_work':'licencias_suscripciones','commercial_status':'needs_review'},'industrial',False),
    ('clear-category',{'commercial_category':'industrial'},'',False),('minor-manual',{'source':'prod6','objeto':'2','deadline':'all'},'industrial',True)]
  for name,extra,value,minor in cases:
   state.update(minor=minor,listen=False,query=None)
   await page.goto('http://category-qa.invalid/feed?'+urlencode({**base,**extra}),wait_until='load');await page.wait_for_function('document.body.dataset.ready==="true"')
   panel=page.locator('#opportunity-filters' if minor else '#prod2-commercial-filters')
   if not await panel.count():await page.get_by_role('button',name=re.compile('Filtros')).click()
   select=panel.get_by_role('combobox',name='Categoría',exact=True);state['listen']=True
   if minor:
    await select.select_option(value);await page.wait_for_timeout(100);assert state['query'] is None,'Minor category must wait for Apply'
    async with page.expect_navigation(wait_until='load'):await panel.get_by_role('button',name='Aplicar filtros',exact=True).click()
   else:
    async with page.expect_navigation(wait_until='load'):await select.select_option(value)
   query=state['query'];assert query.get('commercial_category')==[value],name+' lost selected category'
   assert query.get('profile_id')==['profile-test'] and query.get('q')==['tableros'] and 'page' not in query
   assert query.get('source')==['prod6' if minor else 'prod2']
   if not minor:assert query.get('object')==['good']
   assert not any(k in query for k in ('commercial_area','commercial_work','commercial_specialty','commercial_status')),'Old hidden criteria were serialized'
   await page.wait_for_url(lambda url:parse_qs(urlsplit(str(url)).query,keep_blank_values=True).get('commercial_category')==[value])
   await page.wait_for_function('document.body.dataset.ready==="true"')
   if not minor:
    next_panel=page.locator('#prod2-commercial-filters')
    if await next_panel.count():assert await next_panel.get_attribute('aria-busy') is None,'Pending state must reset after navigation'
   checks.append(name)
  assert not errors,'React runtime error'
  await context.close();await browser.close();print(json.dumps({'checks':checks,'react_errors':errors,'external_calls':0,'pg_calls':0,'model_calls':0}))
asyncio.run(main())
`, path.join(folder, 'bundle/harness.js')], { encoding: 'utf8', timeout: 45000, maxBuffer: 20000 });
      assert.equal(result.status, 0, `Native browser interaction failed: ${result.stderr || result.stdout}`);
      const report = JSON.parse(result.stdout.trim());
      assert.equal(report.checks.length, 5);
      assert.deepEqual(report.react_errors, []);
      assert.equal(report.external_calls + report.pg_calls + report.model_calls, 0);
    } finally {
      fs.rmSync(folder, { recursive: true, force: true });
    }
  });
