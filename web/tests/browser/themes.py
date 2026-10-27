"""Theme regression on an isolated DemoPlatform store, with a synthetic Agent catalog."""
import importlib.util,json,subprocess,tempfile,time,urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[4]
spec=importlib.util.spec_from_file_location('theme_audit',ROOT/'agent-tools/tests/browser/workspace_themes.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)
with tempfile.TemporaryDirectory(prefix='xianyu-themes-') as data:
    with open(Path(data)/'server.log','w') as log:
        server=subprocess.Popen([str(ROOT/'xianyu/.venv/bin/python'),'-m','web','--demo','--no-auth','--port','18870','--data-dir',data],cwd=ROOT/'xianyu',stdout=log,stderr=log)
        try:
            base='http://127.0.0.1:18870'
            for attempt in range(80):
                try:assert json.load(urllib.request.urlopen(base+'/api/meta',timeout=1))['demo'];break
                except OSError:time.sleep(.15)
            else:raise AssertionError('Demo startup failed')
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox'])
                context=browser.new_context(viewport={'width':1440,'height':1000});page=context.new_page();errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                choices={purpose:[{'id':'synthetic','name':'演示助手'}] for purpose in ['dm','comment','content']}
                page.context.route('**/api/models',lambda route:route.fulfill(json={'connected':True,'demo':True,'multica_url':'https://example.test','agent_choices':choices,'agent_bindings':{}}))
                page.goto(base+'/models')
                expect(page.locator('.flow span')).to_have_count(4)
                audit.audit(page,'xianyu')
                for route in ['overview','products','messages','publish','templates','accounts','tasks','dm','comments']:
                    page.locator('a.navlink[href="/'+route+'"]').click()
                    expect(page.locator('.page-loading')).to_have_count(0)
                    expect(page.locator('.error-panel')).to_have_count(0)
                    for mode in ['light','dark']:
                        if page.locator('html').get_attribute('data-workspace-theme')!=mode:page.locator('[data-workspace-action="theme"]').click()
                        expect(page.locator('html')).to_have_attribute('data-workspace-theme',mode)
                page.locator('a.navlink[href="/publish"]').click()
                page.locator('#draft-description').fill('切换主题与缓存页面不丢草稿')
                page.locator('a.navlink[href="/models"]').click()
                page.locator('[data-workspace-action="theme"]').click()
                page.locator('a.navlink[href="/publish"]').click()
                expect(page.locator('#draft-description')).to_have_value('切换主题与缓存页面不丢草稿')
                page.locator('a.navlink[href="/models"]').click()
                page.locator('[data-act="activity"]').click()
                expect(page.locator('#dialog')).to_be_visible()
                assert page.locator('#dialog').evaluate('node=>getComputedStyle(node).backgroundColor')=='rgb(255, 255, 255)'
                page.get_by_role('button',name='关闭',exact=True).last.click()
                page.locator('[data-workspace-action="theme"]').click()
                page.locator('[data-act="activity"]').click()
                assert page.locator('#dialog').evaluate('node=>getComputedStyle(node).backgroundColor')=='rgb(38, 38, 38)'
                page.keyboard.press('Escape')
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                page.screenshot(path=str(audit.OUT/'xianyu-mobile.png'),full_page=True)
                assert not errors,errors
                browser.close()
        finally:server.terminate();server.wait(timeout=10)
(audit.OUT/'xianyu-results.json').write_text(json.dumps(audit.RESULTS,ensure_ascii=False,indent=2))
print({'routes':10,'modes':['light','dark'],'cached_draft_preserved':True,'dialogs':True,'mobile':True,'errors':errors})
