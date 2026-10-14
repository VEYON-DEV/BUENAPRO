import asyncio,json,subprocess,re
from pathlib import Path
from playwright.async_api import async_playwright
R=Path.cwd();O='http://127.0.0.1:3013';out=R/'docs/new-style/qa'
cookie=subprocess.run(['node','-e',"const {encode}=require('next-auth/jwt');encode({secret:'participation-isolated-ui-qa-only-20261013',maxAge:1800,token:{sub:'user-qa',userId:'user-qa',tenantId:'tenant-qa',role:'owner'}}).then(x=>process.stdout.write(x))"],capture_output=True,text=True,check=True).stdout
async def main():
 async with async_playwright() as p:
  b=await p.chromium.launch();c=await b.new_context(viewport={'width':1600,'height':1000})
  await c.add_cookies([{'name':'next-auth.session-token','value':cookie,'url':O,'httpOnly':True,'sameSite':'Lax'}])
  async def gate(route):
   assert route.request.url.startswith(O) and route.request.method in ['GET','HEAD']
   await route.continue_()
  await c.route('**/*',gate);page=await c.new_page();shots=[];contrast=[]
  for s in ['prod2','prod4','prod6']:
   await page.goto(f'{O}/feed?source={s}&deadline=all',wait_until='networkidle')
   await page.get_by_role('heading',name='Cómo puedes participar').wait_for()
   assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
   # Badge contrast is calculated against its actual pastel surface blended over white rows.
   colors=await page.locator('[data-participation-label]').evaluate_all('els=>els.map(el=>{const x=el.nextElementSibling,s=getComputedStyle(x);return {text:x.textContent,color:s.color,bg:s.backgroundColor}})')
   def rgb(v): return [float(n) for n in re.findall(r'[\d.]+',v)]
   def luminance(v):
    v=[n/255 for n in v[:3]];v=[n/12.92 if n<=0.04045 else ((n+0.055)/1.055)**2.4 for n in v]
    return .2126*v[0]+.7152*v[1]+.0722*v[2]
   for x in colors:
    fg,bg=rgb(x['color']),rgb(x['bg']);alpha=bg[3] if len(bg)==4 else 1
    bg=[v*alpha+255*(1-alpha) for v in bg[:3]]
    l1,l2=sorted([luminance(fg),luminance(bg)])
    ratio=(l2+.05)/(l1+.05);assert ratio>=4.5,(s,x,ratio)
    contrast.append(round(ratio,2))
   name=f'participation-{s}-feed-wide-20261013.png';await page.screenshot(path=str(out/name));shots.append(name)
  result=json.loads((out/'participation-ui-result-20261013.json').read_text())
  result.update(wide_viewport={'width':1600,'height':1000},badge_contrast_min=min(contrast),badge_contrast_checks=len(contrast))
  result['screenshots']=list(dict.fromkeys(result['screenshots']+shots))
  (out/'participation-ui-result-20261013.json').write_text(json.dumps(result,indent=2)+'\n')
  print(json.dumps({'screenshots':len(result['screenshots']),'badge_contrast_min':min(contrast),'badge_contrast_checks':len(contrast)}))
  await b.close()
asyncio.run(main())
