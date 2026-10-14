import asyncio,json,subprocess
from pathlib import Path
from playwright.async_api import async_playwright
ROOT=Path.cwd(); OUT=ROOT/'docs/new-style/qa'; ORIGIN='http://127.0.0.1:3013'
script="const {encode}=require('next-auth/jwt'); encode({secret:'participation-isolated-ui-qa-only-20261013',maxAge:1800,token:{sub:'user-qa',userId:'user-qa',tenantId:'tenant-qa',role:'owner',name:'Vista de prueba'}}).then(x=>process.stdout.write(x))"
cookie=subprocess.run(['node','-e',script],capture_output=True,text=True,check=True).stdout
async def main():
  errors=[]; writes=[]; external=[]; screenshots=[]
  async with async_playwright() as p:
    browser=await p.chromium.launch()
    context=await browser.new_context()
    await context.add_cookies([{'name':'next-auth.session-token','value':cookie,'url':ORIGIN,'httpOnly':True,'sameSite':'Lax'}])
    async def local_only(route):
      request=route.request
      if not request.url.startswith(ORIGIN): external.append(request.url.split('?')[0]); return await route.abort()
      if request.method not in ('GET','HEAD'): writes.append(request.method); return await route.abort()
      await route.continue_()
    await context.route('**/*',local_only)
    page=await context.new_page(); page.on('pageerror',lambda e:errors.append(str(e)))
    for source in ['prod2','prod4','prod6']:
      for viewport,w,h in [('desktop',1440,1000),('laptop',1024,900),('mobile',390,844)]:
        await page.set_viewport_size({'width':w,'height':h})
        await page.goto(f'{ORIGIN}/feed?source={source}&deadline=all',wait_until='networkidle')
        await page.locator('[data-participation-label]:visible').first.wait_for() if w < 701 or source == 'prod6' and w < 1051 else await page.get_by_text('Subcontratación',exact=True).first.wait_for()
        assert await page.get_by_text('Consorcio',exact=True).count()>0
        assert await page.get_by_text('No permitida',exact=True).count()>0
        assert await page.get_by_text('Con condiciones',exact=True).count()>0
        assert await page.get_by_text('Por confirmar',exact=True).count()>0
        assert await page.get_by_text('Por leer',exact=True).count()>0
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'{source}-{viewport} overflow'
        name=f'participation-{source}-feed-{viewport}-20261013.png'; await page.screenshot(path=str(OUT/name),full_page=viewport!='mobile'); screenshots.append(name)
      # Open the current reading and use native details from the keyboard.
      await page.set_viewport_size({'width':1440,'height':1000})
      await page.goto(f'{ORIGIN}/feed?source={source}&deadline=all',wait_until='networkidle')
      if not await page.get_by_role('heading',name='Cómo puedes participar').count(): await page.locator('article button').first.press('Enter')
      inspector=page.get_by_role('complementary').filter(has=page.get_by_role('heading',name='Cómo puedes participar'))
      await inspector.get_by_text('Ver cita y condiciones',exact=True).first.press('Enter')
      await inspector.get_by_text('El contratista no podrá subcontratar',exact=False).wait_for()
      name=f'participation-{source}-inspector-20261013.png'; await page.screenshot(path=str(OUT/name),full_page=True); screenshots.append(name)
      route={'prod2':'/oportunidad/prod2/1001','prod4':'/oportunidad/seace/1250001','prod6':'/oportunidad/8001'}[source]
      for viewport,w,h in [('desktop',1440,1000),('laptop',1024,900),('mobile',390,844)]:
        await page.set_viewport_size({'width':w,'height':h})
        await page.goto(ORIGIN+route,wait_until='networkidle')
        await page.get_by_role('heading',name='Cómo puedes participar').wait_for()
        await page.get_by_text('Ver cita y condiciones',exact=True).first.press('Enter')
        await page.get_by_text('El contratista no podrá subcontratar',exact=False).wait_for()
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'),f'{source}-{viewport} detail overflow'
        name=f'participation-{source}-detail-{viewport}-20261013.png'; await page.screenshot(path=str(OUT/name),full_page=viewport!='mobile'); screenshots.append(name)
      # Changing selection retains only this row's evidence while its detail loads/fails.
      await page.set_viewport_size({'width':1440,'height':1000})
      await page.goto(f'{ORIGIN}/feed?source={source}&deadline=all',wait_until='networkidle')
      second_id={'prod2':'1002','prod4':'1250002','prod6':'8002'}[source]
      endpoint=f'**/api/{source}/opportunities/{second_id}*' if source!='prod6' else f'**/api/contracts/{second_id}'
      release=asyncio.Event()
      async def blocked_detail(route):
        await release.wait()
        await route.fulfill(status=503,content_type='application/json',body='{"error":"QA detail unavailable"}')
      await page.route(endpoint,blocked_detail)
      await page.locator('article button[aria-expanded]').nth(1).press('Enter')
      inspector=page.get_by_role('complementary').filter(has=page.get_by_role('heading',name='Cómo puedes participar'))
      await inspector.get_by_text('Ver cita y condiciones',exact=True).first.press('Enter')
      await inspector.get_by_text('Se permite subcontratar actividades accesorias',exact=False).wait_for()
      assert await inspector.get_by_text('El contratista no podrá subcontratar',exact=False).count()==0
      await inspector.get_by_text('Autorización previa de la entidad.',exact=True).wait_for()
      name=f'participation-{source}-loading-20261013.png'; await page.screenshot(path=str(OUT/name)); screenshots.append(name)
      release.set()
      if source=='prod6':
        await inspector.locator('summary').filter(has_text='Más información').press('Enter')
        await inspector.get_by_text('No se pudo cargar el análisis completo.',exact=True).wait_for()
      else:
        await inspector.get_by_role('alert').wait_for()
      assert await inspector.get_by_text('Se permite subcontratar actividades accesorias',exact=False).count()==1
      name=f'participation-{source}-error-20261013.png'; await page.screenshot(path=str(OUT/name)); screenshots.append(name)
      await page.unroute(endpoint,blocked_detail)
      await page.goto(f'{ORIGIN}/feed?source={source}&deadline=all&q=__qa_empty__',wait_until='networkidle')
      await page.get_by_role('heading',name='No hay oportunidades con esos filtros' if source=='prod6' else 'No hay concursos con esos filtros').wait_for()
      assert await page.get_by_role('heading',name='Cómo puedes participar').count()==0
      name=f'participation-{source}-empty-20261013.png'; await page.screenshot(path=str(OUT/name)); screenshots.append(name)
    await page.goto(ORIGIN+'/design-system',wait_until='networkidle')
    await page.get_by_text('Sin resultado',exact=True).first.wait_for()
    assert not errors,errors
    assert not writes,writes
    assert not external,external
    result={'fixture':True,'screenshots':screenshots,'client_errors':errors,'writes':writes,'external_requests':external}
    (OUT/'participation-ui-result-20261013.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({'screenshots':len(screenshots),'client_errors':len(errors),'writes':len(writes),'external_requests':len(external)}))
    await browser.close()
asyncio.run(main())
