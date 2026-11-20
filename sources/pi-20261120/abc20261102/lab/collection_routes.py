"""Authenticated-by-Caddy environment and collection-batch read endpoints."""
import asyncio
import csv
import io
import json
from aiohttp import web,ClientSession,ClientTimeout


def install(server,app):
    async def environment(request):
        endpoint='history' if request.path.endswith('/history') else 'latest'
        allowed={'zone_id','field','since_ms','until_ms','minutes','limit'}
        params={k:v for k,v in request.query.items() if k in allowed}
        try:
            async with ClientSession(timeout=ClientTimeout(total=4)) as client:
                async with client.get('http://127.0.0.1:28984/env/v1/'+endpoint,params=params) as response:
                    data=await response.json()
                    return web.json_response(data,status=response.status,headers={'Cache-Control':'no-store'})
        except (OSError,asyncio.TimeoutError,ValueError) as e:
            return web.json_response({'status':'environment_unavailable','error':str(e)},status=503)

    async def batches(request):
        store=server.batch_store
        if store is None:return web.json_response({'error':'Batch store unavailable on this host'},status=503)
        try:
            batch_id=request.match_info.get('batch_id')
            loop=asyncio.get_running_loop()
            if not batch_id:
                q=request.query
                def num(name):
                    value=q.get(name)
                    return None if value in (None,'') else int(value)
                data=await loop.run_in_executor(None,lambda:store.list_filtered(
                    query=q.get('q') or None,view=q.get('view','all'),channel=q.get('channel') or None,
                    since_ms=num('since_ms'),until_ms=num('until_ms'),
                    limit=num('limit') if num('limit') is not None else 1000,offset=num('offset') or 0))
                return web.json_response({**data,'active_batch_id':server.active_batch_id,
                                          'capture_error':server.batch_capture_error},headers={'Cache-Control':'no-store'})
            if request.path.endswith('/environment'):
                data=await loop.run_in_executor(None,lambda:store.environment(batch_id,int(request.query.get('after_id',0)),int(request.query.get('limit',1000))))
                if request.query.get('format')=='csv':
                    text=io.StringIO();writer=csv.writer(text)
                    fields=['archive_id','batch_id','location','collection_name','run_id','event_id','device_id','sensor_id','data_time_ms','received_at_ms','temperature_c','humidity_rh','co2_ppm','quality','clock_status']
                    writer.writerow(fields)
                    b=data['batch']
                    snapshot_last_id=b['max_archive_id']
                    def csv_row(r):
                        row={**r,**r.get('values',{}),'batch_id':batch_id,'location':b['location'],
                             'collection_name':b['name'],'run_id':b['run_id']}
                        def safe(v):return "'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v
                        return [safe(row.get(f,'')) for f in fields]
                    for r in data['rows']:
                        if r['archive_id']<=snapshot_last_id:writer.writerow(csv_row(r))
                    if request.query.get('all')=='1':
                        response=web.StreamResponse(status=200,headers={
                            'Content-Type':'text/csv; charset=utf-8','Cache-Control':'no-store',
                            'Content-Disposition':'attachment; filename="environment-'+batch_id+'.csv"',
                            'X-Snapshot-Through-Id':str(snapshot_last_id)})
                        await response.prepare(request)
                        await response.write(('\ufeff'+text.getvalue()).encode('utf-8'))
                        cursor=data['next_after_id']
                        while data['rows'] and cursor<snapshot_last_id:
                            data=await loop.run_in_executor(None,lambda:store.environment(batch_id,cursor,1000))
                            text=io.StringIO();writer=csv.writer(text)
                            for r in data['rows']:
                                if r['archive_id']<=snapshot_last_id:writer.writerow(csv_row(r))
                            await response.write(text.getvalue().encode('utf-8'))
                            cursor=data['next_after_id']
                        await response.write_eof()
                        return response
                    return web.Response(body=('\ufeff'+text.getvalue()).encode('utf-8'),content_type='text/csv',
                        headers={'Content-Disposition':'attachment; filename="environment-'+batch_id+'.csv"',
                                 'X-Next-After-Id':str(data['next_after_id']),
                                 'X-Has-More':str(data['has_more']).lower(),'Cache-Control':'no-store'})
                return web.json_response(data,headers={'Cache-Control':'no-store'})
            data=await loop.run_in_executor(None,lambda:store.get(batch_id))
            return web.json_response(data,headers={'Cache-Control':'no-store'})
        except KeyError:return web.json_response({'error':'Batch not found'},status=404)
        except (TypeError,ValueError) as e:return web.json_response({'error':str(e)},status=400)

    async def longterm(request):
        from urllib.parse import urlsplit
        if request.method=='POST':
            origin=request.headers.get('Origin')
            if origin and urlsplit(origin).netloc!=request.host:return web.json_response({'error':'不允许跨站修改'},status=403)
            if request.content_type!='application/json':return web.json_response({'error':'需要 JSON'},status=415)
        endpoint='history' if request.path.endswith('/history') else 'settings'
        try:
            body=await request.json() if request.method=='POST' else None
            async with ClientSession(timeout=ClientTimeout(total=15)) as client:
                async with client.request(request.method,'http://127.0.0.1:28984/env/v1/longterm/'+endpoint,json=body,params={'after_id':request.query.get('after_id','0')}) as response:
                    return web.json_response(await response.json(),status=response.status,headers={'Cache-Control':'no-store'})
        except (OSError,asyncio.TimeoutError,ValueError) as e:return web.json_response({'error':str(e)},status=503)
    app.router.add_get('/api/environment/longterm/settings',longterm)
    app.router.add_post('/api/environment/longterm/settings',longterm)
    app.router.add_get('/api/environment/longterm/history',longterm)

    async def manage(request):
        from urllib.parse import urlsplit
        origin=request.headers.get('Origin')
        if origin and urlsplit(origin).netloc!=request.host:return web.json_response({'error':'不允许跨站修改'},status=403)
        if request.content_type!='application/json':return web.json_response({'error':'需要 JSON 请求'},status=415)
        if request.content_length and request.content_length>128000:return web.json_response({'error':'请求过大'},status=413)
        if server.batch_store is None:return web.json_response({'error':'批次库不可用'},status=503)
        try:
            data=await request.json()
            if not isinstance(data,dict):raise ValueError('无效请求')
            result=await asyncio.get_running_loop().run_in_executor(None,server.batch_store.manage,data.get('action'),data.get('batch_ids'))
            return web.json_response(result)
        except KeyError:return web.json_response({'error':'有批次不存在，请刷新列表'},status=404)
        except RuntimeError as e:return web.json_response({'error':str(e)},status=409)
        except (ValueError,TypeError) as e:return web.json_response({'error':str(e)},status=400)
    app.router.add_post('/api/collection-batches/manage',manage)

    async def experiment(request):
        archive=getattr(server,'archive',None)
        if archive is None:return web.json_response({'error':'结构化归档暂不可用'},status=503)
        bid=request.match_info['batch_id']
        try:
            loop=asyncio.get_running_loop()
            if request.path.endswith('/summary'):
                return web.json_response(await loop.run_in_executor(None,archive.summary,bid),headers={'Cache-Control':'no-store'})
            path=await loop.run_in_executor(None,archive.export,bid)
            try:
                response=web.StreamResponse(headers={'Content-Type':'application/zip','Content-Disposition':'attachment; filename="experiment-'+bid+'.zip"','Content-Length':str(path.stat().st_size)})
                await response.prepare(request)
                with path.open('rb') as f:
                    while True:
                        chunk=await loop.run_in_executor(None,f.read,256*1024)
                        if not chunk:break
                        await response.write(chunk)
                await response.write_eof();return response
            finally:
                import shutil
                shutil.rmtree(path.parent)
        except KeyError:return web.json_response({'error':'批次不存在'},status=404)
        except ValueError as e:return web.json_response({'error':str(e)},status=409)
    app.router.add_get('/api/collection-batches/{batch_id}/summary',experiment)
    app.router.add_get('/api/collection-batches/{batch_id}/export',experiment)
    app.router.add_get('/api/environment/latest',environment)
    app.router.add_get('/api/environment/history',environment)
    app.router.add_get('/api/collection-batches',batches)
    app.router.add_get('/api/collection-batches/{batch_id}',batches)
    app.router.add_get('/api/collection-batches/{batch_id}/environment',batches)


async def sync_loop(server):
    async def sync_active():
        if server.batch_store is not None and server.active_batch_id:
            batch_id=server.active_batch_id
            await asyncio.to_thread(server.batch_store.sync,batch_id)
            state=await asyncio.to_thread(server.batch_store.get,batch_id)
            server.batch_capture_error=state.get('capture_error')

    while True:
        try:
            lock = getattr(server, 'session_lock', None)
            if lock is None:
                await sync_active()
            else:
                async with lock:
                    await sync_active()
            await asyncio.sleep(2)
        except asyncio.CancelledError:raise
        except Exception as e:
            server.batch_capture_error=type(e).__name__+': '+str(e)
            await asyncio.sleep(2)
