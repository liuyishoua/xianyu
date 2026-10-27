from flask import request
from .channel_client import ChannelClient


def register_channel(app, service, config_path):
    def client(): return app.extensions['monitor'].client(service)
    def submit():
        return submit_analysis(service, app.extensions['monitor'], request.json)
    def status(event_id):return client().status(event_id,str(service.uid()))
    app.add_url_rule('/api/channel/messages','channel_submit',submit,methods=['POST'])
    app.add_url_rule('/api/channel/messages/<event_id>','channel_status',status)


def submit_analysis(service, monitor, data):
    def client(): return monitor.client(service)
    uid=service.uid();cid=str(data.get('conversation_id',''))
    text=data.get('text','根据最近会话起草一条自然的中文回复。')
    if not isinstance(text,str) or len(text)>4000:raise ValueError('处理要求最多 4000 字')
    context={}
    if cid:
        conv=service.store.get(uid,'conversation',cid)
        context['conversation']={k:conv.get(k) for k in ('id','name','peer_id','note','item_id')}
        rows=[m for m in service.store.list(uid,'message') if m.get('cid')==cid]
        rows.sort(key=lambda m:float(m.get('timestamp') or 0))
        context['messages']=[{k:m.get(k) for k in ('id','text','kind','outgoing','timestamp')} for m in rows[-20:]]
        if conv.get('item_id'):
            try:context['item']=service.store.get(uid,'product',str(conv['item_id']))
            except LookupError:pass
            if 'item' in context:context['item']={k:context['item'].get(k) for k in ('id','title','price','description','note')}
    elif data.get('draft_description') is not None:
        description = data['draft_description']
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 5000:
            raise ValueError('请先填写商品事实，最多 5000 字')
        context = {'draft': {'description': description, 'price': str(data.get('price') or '')[:30]},
            'instruction': '根据已提供的商品事实优化中文描述，不编造状态、配件、价格或售后承诺。返回商品描述正文。'}
    elif data.get('item_id'):
        item=service.store.get(uid,'product',str(data['item_id']))
        context={'topic_id':str(data['item_id']),'item':{k:item.get(k) for k in ('id','title','price','description','note')}}
    message_id=data.get('message_id')
    if not isinstance(message_id,str) or not 8<=len(message_id)<=160:raise ValueError('请提供唯一消息 ID')
    catalog = client().catalog()
    bindings = monitor.model_bindings(service, catalog)
    purpose = 'dm' if cid else 'content'
    route_id = bindings.get(purpose, '')
    from .monitoring import bound_route, agent_snapshot
    if not route_id and catalog.get('demo'): route_id = catalog['routes'][0]['id']
    if not route_id: raise ValueError('请先在模型配置中绑定当前账号的 Agent')
    route = bound_route(catalog, purpose, uid, route_id)
    if not route: raise ValueError('绑定 Agent 已不可用，请重新选择')
    expected = (bindings.get('agents', {}).get(purpose) or {}).get('id')
    if expected and agent_snapshot(catalog, route_id).get('id') != expected:
        raise ValueError('绑定 Agent 已变化，请重新选择')
    return client().send(str(uid),message_id,text,category='reply_draft' if cid else 'content_analysis',conversation_id=cid,context=context,surface='dm' if cid else 'content',event_type='dm.draft_requested' if cid else 'content.analysis_requested',route_id=route_id),202
