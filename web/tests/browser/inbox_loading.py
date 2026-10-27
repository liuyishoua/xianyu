"""Browser regression against an isolated --demo --no-auth workspace only."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

base = os.environ.get('XIANYU_DEMO_URL', 'http://127.0.0.1:18770').rstrip('/')
out = Path(os.environ.get('XIANYU_QA_OUTPUT', '/tmp/xianyu-inbox-qa'))
out.mkdir(parents=True, exist_ok=True)
checks = []
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True, args=['--no-sandbox'])
    context = browser.new_context(viewport={'width': 1440, 'height': 1050})
    assert context.request.get(base + '/api/meta').json()['demo'], 'This test must never run against real accounts'
    headers = {'X-App-Request': '1', 'Origin': base}
    # Receive-only demo listener: verify stopping remains available during a
    # directory outage, without allowing even simulated automatic sends.
    response = context.request.put(base + '/api/monitoring/dm', headers=headers,
        data={'enabled': True, 'auto_reply': False, 'agent_id': ''})
    assert response.ok
    catalog = context.request.get(base + '/api/models').json()
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    held = []
    page.route('**/api/models', lambda route: held.append(route))
    page.goto(base + '/comments')
    expect(page.get_by_role('heading', name='留言监听', exact=True)).to_be_visible(timeout=2500)
    expect(page.get_by_role('heading', name='留言与回复记录', exact=True)).to_be_visible()
    expect(page.locator('#inbox-model-status')).to_have_text('正在加载回复助手列表…')
    expect(page.locator('#inbox-route')).to_be_disabled()
    page.locator('[data-act="inbox-refresh"]').click()
    expect(page.locator('[data-act="inbox-save"]')).to_be_disabled()
    checks.append('留言记录先显示，Agent 请求挂起不阻塞页面，刷新不误启用保存')
    page.screenshot(path=str(out / 'comments-loading.png'), full_page=True)
    page.locator('a.navlink[href="/dm"]').click()
    expect(page.get_by_role('heading', name='私信监听', exact=True)).to_be_visible()
    page.wait_for_function("document.querySelector('#inbox-model-status')?.textContent.includes('正在加载')")
    expect(page.locator('[data-act="inbox-stop"]')).to_be_enabled()
    page.locator('[data-act="inbox-stop"]').click()
    expect(page.locator('[data-act="inbox-stop"]')).to_be_disabled()
    checks.append('目录不可用时仍可停止已开启的私信监听')
    # Finish the old comment request after navigation; it must not touch DM UI.
    assert len(held) == 2
    held[0].fulfill(json={'connected': True, 'agent_choices': {'comment': [{'id': 'old', 'name': '过期留言助手'}]}})
    held[1].fulfill(json={'connected': False, 'error': '合成测试：助手目录离线'})
    expect(page.get_by_role('button', name='重试加载助手', exact=True)).to_be_visible()
    expect(page.locator('#inbox-route')).not_to_contain_text('过期留言助手')
    expect(page.get_by_role('heading', name='私信与回复记录', exact=True)).to_be_visible()
    checks.append('目录失败显示可重试提示，切页后旧响应不会覆盖当前页面')
    page.get_by_role('button', name='重试加载助手', exact=True).click()
    page.wait_for_timeout(100)
    assert len(held) == 3
    held[2].fulfill(json=catalog)
    expect(page.locator('#inbox-route')).to_be_enabled()
    expect(page.locator('[data-act="inbox-save"]')).to_be_enabled()
    expect(page.locator('#inbox-model-status')).to_have_text('')
    checks.append('重试成功后恢复助手选择与配置保存')
    page.unroute('**/api/models')
    page.goto(base + '/comments')
    expect(page.locator('#inbox-route')).to_be_enabled()
    page.screenshot(path=str(out / 'comments-desktop.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    page.screenshot(path=str(out / 'comments-mobile.png'), full_page=True)
    checks.append('桌面与手机布局正常')
    # A genuinely slow local inbox now has a visible, theme-aware loading state.
    local = []
    page.route('**/api/monitoring/comment', lambda route: local.append(route))
    page.goto(base + '/comments')
    expect(page.get_by_role('status').filter(has_text='正在读取留言监听')).to_be_visible()
    assert page.locator('.page-loading').evaluate('e => getComputedStyle(e).backgroundColor') != 'rgb(255, 255, 255)'
    local[0].fulfill(status=503, json={'error': '合成测试：留言读取失败'})
    expect(page.get_by_role('button', name='重新加载', exact=True)).to_be_visible()
    checks.append('本地记录加载和失败均有明确提示，不再显示无文字白块')
    assert not errors, errors
    browser.close()
print({'checks': checks, 'page_errors': errors, 'screenshots': str(out)})
