"""Receive an owner-provided browser session without exposing it in jobs or logs."""
import re
import tempfile
from pathlib import Path

import requests
from goofish_apis import XianyuApis
from utils.goofish_utils import generate_device_id
from .platform import Platform, PlatformError, save_session


def browser_session(cookie, expected):
    if not isinstance(cookie,str) or not 1<=len(cookie)<=65536 or any(ord(c)<32 or ord(c)>126 for c in cookie):
        raise ValueError('请粘贴闲鱼请求头中的 Cookie 值')
    values={}
    for part in cookie.split(';'):
        key,separator,value=part.strip().partition('=')
        if not separator or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',key) or key in values:
            raise ValueError('Cookie 格式不完整，请重新复制请求头中的值')
        values[key]=value
    if values.get('unb')!=expected:
        raise ValueError('网页登录账号与要恢复的账号不一致，未保存登录态')
    if not values.get('cookie2') or not values.get('_m_h5_tk'):
        raise ValueError('登录态不完整，请刷新已登录的闲鱼网页后重新复制')
    api=XianyuApis({},generate_device_id(expected))
    for key,value in values.items():
        api.session.cookies.set_cookie(requests.cookies.create_cookie(name=key,value=value,
            domain='.goofish.com',path='/',secure=True))
    return api


def import_browser_session(accounts,monitor,directory,uid,cookie):
    target=accounts.resolve(uid)
    if getattr(target.platform,'demo',False):raise ValueError('演示空间不接收真实登录态')
    api=browser_session(cookie,uid)
    # Verify in a private staging directory before replacing the original account.
    with tempfile.TemporaryDirectory(prefix='.session-import-',dir=Path(directory)) as temporary:
        path=Path(temporary)/'session.json';save_session(path,api)
        probe=Platform(path)
        try:
            health=probe.check()
            if not health.get('can_message'):raise PlatformError('未确认消息连接')
            api=probe.api()
        except Exception:
            raise PlatformError('网页登录态未通过消息连接核验，原账号登录态未替换') from None
        if api.session.cookies.get_dict().get('unb')!=uid:
            raise ValueError('核验后的账号身份不一致，原账号登录态未替换')
        accounts.register(api,expected=uid)
        with target.lock:
            checked=target.store.get(uid,'settings','account-check')
            if not checked.get('can_message'):
                return {'state':'saved','account_id':uid,'reason':'网页登录态已保存，消息连接尚未确认恢复'}
            monitor.connection_restored(target)
    return {'state':'connected','account_id':uid,'reason':'原账号连接已核验，已开启的监听正在重连'}
