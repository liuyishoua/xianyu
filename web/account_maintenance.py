import json
from pathlib import Path
import sys
from flask import request,jsonify
from .phone_login import PhoneLoginError,create_phone_login
from .platform import PlatformError
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'account-maintenance'))
from bridge import Bridge


def register(app,accounts,directory,monitor):
    bridge=Bridge(directory);phone=create_phone_login(directory)
    @app.before_request
    def maintenance_auth():
        if request.path.startswith('/maintenance/') and not bridge.authorized(request.headers):return jsonify(error='Maintenance credential required'),401

    @app.get('/maintenance/accounts')
    def directory_view():
        return {'items':[{'id':str(a['id']),'external_id':str(a['id']),'name':a.get('label') or a['name'],'kind':'web','status':a['status'],
            'capability':'sms','login_methods':['sms','qr'],'url':'https://d.58nft.net:9448/accounts'} for a in accounts.list()]}

    @app.get('/maintenance/events')
    def events():return {'items':bridge.events(max(0,int(request.args.get('after',0))))}

    @app.get('/maintenance/attempts/<id>')
    def attempt(id):
        r=bridge.attempt(id)
        if not r:return jsonify(error='Attempt absent'),404
        return {'state':r['state'],'remote_id':r['remote_id'],'updated':r['updated'],**json.loads(r['result'])}

    def selected(data):
        if not isinstance(data,dict) or str(data.get('external_id'))!=str(data.get('account_id')):raise ValueError('账号身份不匹配')
        target=accounts.resolve(str(data.get('account_id')))
        if getattr(target.platform,'demo',False):raise ValueError('演示模式不发起真实短信登录')
        return target

    def restore_connection(target):
        with target.lock:
            health=target.platform.check()
            if not health.get('can_message'):
                raise PlatformError('闲鱼消息连接未确认恢复')
            target.store.put(target.uid(),'settings',health,'account-check')
            monitor.connection_restored(target)

    @app.post('/maintenance/start')
    def start():
        data=request.get_json();target=selected(data);id=str(data.get('request_id',''))
        if not 1<=len(id)<=128:raise ValueError('请求ID无效')
        r,fresh=bridge.reserve(id,data['account_id'],data['external_id'],str(data.get('phone','')),{'force_login':True} if data.get('force_login') else None)
        if not fresh:return {'state':r['state'],'remote_id':r['remote_id'],'updated':r['updated'],**json.loads(r['result'])}
        try:result=phone.start(id,data['phone'],expected=data['external_id'])
        except requests_error():
            result={'state':'uncertain','reason':'发码请求响应不确定，未自动重试，请核对手机短信'}
        except (ValueError,PlatformError):result={'state':'needs_user','reason':'官方登录初始化未完成，请到闲鱼工作台验证'}
        bridge.update(id,result['state'],result.get('remote_id',''),{'reason':result.get('reason','')})
        return result

    @app.post('/maintenance/repair-listener')
    def repair_listener():
        data=request.get_json();target=selected(data)
        component=data.get('component','account')
        if component in ('listener.dm','listener.comment'):
            try:config=target.store.get(data['account_id'],'monitor',component.split('.')[1])
            except LookupError:return {'state':'inactive'}
            if not config.get('enabled'):return {'state':'inactive'}
        try:
            restore_connection(target)
            return {'state':'connected'}
        except PlatformError as exc:
            code=str(exc)
            return {'state':'expired' if 'SESSION_EXPIRED' in code else 'risk' if 'USER_VALIDATE' in code or 'RGV587_ERROR' in code else 'unknown'}
        except Exception:return {'state':'unknown'}

    @app.post('/maintenance/confirm')
    def confirm():
        data=request.get_json();target=selected(data);id=str(data.get('request_id',''));r=bridge.attempt(id)
        if not r:raise ValueError('登录请求不存在')
        bridge.reserve(id,data['account_id'],data['external_id'],data['phone'],{'force_login':True} if data.get('force_login') else None)
        if not bridge.claim_confirmation(id):return {'state':r['state'],**json.loads(r['result'])}
        try:
            api=phone.confirm(id,data['phone'],data['code'],data['external_id'])
            accounts.register(api,expected=data['external_id'])
            # Login verification must read the platform, not rely on Cookie presence alone.
            restore_connection(target)
            result={'state':'confirmed','reason':'闲鱼已核验原账号身份并保存登录态'}
            bridge.notify(data['account_id'],data['external_id'],'connected',id)
        except PhoneLoginError as exc:result={'state':'needs_user','reason':str(exc)}
        except (ValueError,PlatformError):result={'state':'needs_user','reason':'闲鱼未确认恢复，请在工作台完成平台验证'}
        except Exception:result={'state':'uncertain','reason':'确认响应不确定，未重复提交验证码'}
        bridge.update(id,result['state'],r['remote_id'],{'reason':result['reason']})
        return result


def requests_error():
    import requests
    return requests.RequestException
