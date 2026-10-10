"""Select a non-self participant from an existing single-user conversation. Read-only."""
import asyncio,base64,json,re,time
import websockets
from run_local import load_api,LOCAL
from goofish_live import XianyuLive
from utils.goofish_utils import get_session_cookies_str,generate_mid
from account_action import save_result

async def main():
    api=load_api(); token=api.get_token()
    if not token.get('data',{}).get('accessToken'): raise RuntimeError('Login token unavailable')
    live=XianyuLive(get_session_cookies_str(api.session)); live.xianyu=api; live.device_id=api.device_id
    api.get_token=lambda:token
    pending={}; queried=set(); registered=False; sync_ready=False; candidates=[]
    deadline=time.monotonic()+35
    async with websockets.connect(live.base_url,extra_headers={'Cookie':get_session_cookies_str(api.session),'Origin':'https://www.goofish.com','User-Agent':'Mozilla/5.0'},open_timeout=15) as ws:
        reg_mid=None
        class Init:
            async def send(self,raw):
                nonlocal reg_mid
                m=json.loads(raw)
                if m.get('lwp')=='/reg': reg_mid=m['headers']['mid']
                await ws.send(raw)
        await live.init(Init())
        while time.monotonic()<deadline:
            m=json.loads(await asyncio.wait_for(ws.recv(),deadline-time.monotonic()))
            h=m.get('headers',{}); body=m.get('body',{}); lwp=m.get('lwp','')
            if h.get('mid')==reg_mid:
                if m.get('code')!=200: raise RuntimeError('Registration rejected')
                registered=True
            if lwp=='/s/vulcan':sync_ready=True
            if lwp.startswith('/s/'):
                await ws.send(json.dumps({'code':200,'headers':{k:h[k] for k in ('mid','sid','app-key','ua','dt') if k in h}}))
            if isinstance(body,dict):
                for entry in body.get('syncPushPackage',{}).get('data',[]):
                    try: data=json.loads(base64.b64decode(entry['data']))
                    except (ValueError,UnicodeError):continue
                    sid=data.get('sessionId','')
                    if isinstance(sid,str) and re.fullmatch(r'\d+(?:@goofish)?',sid):
                        candidates.append(sid.split('@')[0])
            if registered and sync_ready:
                for cid in candidates:
                    if cid in queried:continue
                    mid=generate_mid(); queried.add(cid); pending[mid]=cid
                    await ws.send(json.dumps({'lwp':'/r/MessageManager/listUserMessages','headers':{'mid':mid},'body':[f'{cid}@goofish',False,9007199254740991,20,False]}))
            if h.get('mid') in pending and m.get('code')==200:
                cid=pending[h['mid']]
                for row in body.get('userMessageModels',[]):
                    msg=row.get('message',{}); ext=msg.get('extension',{})
                    uid=str(ext.get('senderUserId','')); nick=ext.get('reminderTitle','')
                    if not uid.isdigit() or uid==live.myid or len(uid)<5:continue
                    if any(word in nick for word in ('官方','小助手','系统通知','服务通知','交易通知')):continue
                    config={'action':'send','cid':cid,'toid':uid,'text':'你好，测试一下消息接口，打扰了，无需回复。'}
                    save_result('selected-test-send.json',config)
                    save_result('selected-test-recipient.json',{'cid':cid,'toid':uid,'nickname':nick})
                    print(json.dumps({'selected':True,'nickname':nick,'recipient_id':uid,'cid':cid,'text':config['text']},ensure_ascii=False))
                    return
        raise RuntimeError('No eligible participant found')

if __name__=='__main__':asyncio.run(main())
