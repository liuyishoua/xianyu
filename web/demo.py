"""Explicit demo data and a zero-network adapter for UI review and regression."""
import time
import uuid
from pathlib import Path
from .platform import Platform


class DemoPlatform:
    demo = True
    session_path = Path('/nonexistent/xianyu-demo/session.json')
    def identity(self): return {'id': 'demo-seller', 'name': '晴天的闲置小铺', 'status': 'connected'}
    def check(self): return {'checked_at': time.time(), 'can_message': True}
    def item(self, value):
        return sample_product(str(value) if str(value).isdigit() or str(value).startswith('demo-') else '1000000001', 0)
    def locations(self):
        return [{'prov':'广东省', 'city':'深圳市', 'area':'罗湖区', 'poi':'莲塘',
                 'divisionId':'440303', 'longitude':114.16, 'latitude':22.56, 'poiId':'demo'}]
    def recommend(self, description, media): return {'name':'数码配件', 'id':'demo-category', 'cards':3}
    def upload(self, path): return {'url':'/static/demo-product.svg','width':800,'height':800}
    def publish(self, draft, files, location):
        item_id = 'demo-' + uuid.uuid4().hex[:8]
        return {'item_id': item_id, 'url':'#demo-publication'}
    def socket(self, operation, **kwargs):
        if operation == 'sync':
            return {'conversations': [{'id': str(900000+i), **history(str(900000+i),i)} for i in range(3)],
                    'scope':'演示会话'}
        if operation == 'history': return history(kwargs['cid'],0)
        if operation == 'create': return {'id':'900003','peer_id':kwargs['peer_id']}
        if operation == 'send': return {'accepted':True,'message_id':'demo-'+uuid.uuid4().hex,'cid':kwargs['cid']}
        raise ValueError()


def sample_product(item_id, index):
    names = ['Fujifilm X100V 数码相机 · 银色', 'Keychron K2 无线机械键盘', '莲塘优格游泳馆次卡转让', 'MUJI 无印良品橡木边几', 'Marshall Emberton II 便携音箱', 'Kindle Paperwhite 电子书阅读器']
    prices = ['6899','268','1220','180','699','380']
    return {'id':item_id,'title':names[index%6],'description':names[index%6]+'，个人闲置，使用爱惜，实物状态良好。支持同城自提，欢迎咨询。',
            'price':prices[index%6], 'original_price':'9999','status':'在线', 'cover':'/static/demo-product.svg',
            'images':['/static/demo-product.svg'],'seller_id':'demo-seller','seller_name':'晴天的闲置小铺',
            'views':[128,96,43,67,89,51][index%6], 'wants':[8,5,2,3,6,4][index%6],
            'collects':[12,7,2,5,8,3][index%6], 'updated_at':time.time(), 'tags':['数码好物','数码好物','生活方式','家居闲置','数码好物','数码好物'][index%6]}


def history(cid, index):
    names=['小林同学','海边的卡夫卡','一只橘子']
    return {'messages':[{'id':f'demo-{cid}-1','sender_id':str(800000+index),'outgoing':False,'kind':'text',
             'text':['你好，相机还在吗？可以看一下实拍图吗？','键盘支持 Mac 吗？','你好，次卡可以转给别人用吗？'][index%3], 'images':[],'timestamp':time.time()-600},
            {'id':f'demo-{cid}-2','sender_id':'demo-seller','outgoing':True,'kind':'text','text':'你好，还在的，欢迎咨询～','images':[],'timestamp':time.time()-480}],
            'peer_id':str(800000+index),'has_more':False,'cursor':0}


def seed(service):
    uid=service.uid()
    if service.store.list(uid,'product'): return
    now=time.time()
    for i in range(6):
        p=sample_product(str(1000000001+i),i);service.save_product(uid,p)
        for j in range(7):
            service.store.put(uid,'snapshot',{'product_id':p['id'],'at':now-(6-j)*86400,
                'views':max(0,p['views']-(6-j)*15),'wants':max(0,p['wants']-(6-j)),'collects':p['collects']})
    for i,name in enumerate(['小林同学','海边的卡夫卡','一只橘子']):
        cid=str(900000+i);service.save_history(uid,cid,history(cid,i))
        conv=service.store.get(uid,'conversation',cid);conv.update(name=name,item_id=str(1000000001+i))
        service.store.put(uid,'conversation',conv)
    service.store.put(uid,'settings',{'items':service.platform.locations()},'locations')
    service.store.put(uid,'settings',service.platform.check(),'account-check')
    service.store.put(uid,'draft',{'description':'闲置复古台灯，暖光，状态良好。','price':'120','original_price':'299',
        'delivery':'包邮','postage':'0','self_pickup':True,'location_index':0,'media_ids':[],'status':'draft','updated_at':now})
