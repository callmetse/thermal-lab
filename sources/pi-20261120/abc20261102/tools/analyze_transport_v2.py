"""Join transport browser and console evidence without pooling clocks or scenarios."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path

from report import percentile, summarize


ROOT=Path(__file__).resolve().parent.parent


def identity(payload):
    if payload.get('producer_instance_id') and payload.get('observation_seq') is not None:
        return (payload.get('project'),payload.get('run_id'),payload['producer_instance_id'],payload['observation_seq'])
    return (payload.get('project'),payload.get('run_id'),payload.get('producer'),
            payload.get('source_frame_id'),payload.get('jpeg_sha256'))


def deduplicate(rows):
    """Mark wire retransmissions while retaining every original row for traffic totals."""
    output=[];seen=set()
    for row in rows:
        copied=dict(row)
        if row['channel']=='observations':
            key=(row.get('console_boot_id','unknown'),identity(row['payload']))
            copied['duplicate']=bool(row.get('duplicate')) or key in seen
            seen.add(key)
        output.append(copied)
    return output


def per_page(client):
    fields=['index','chrome_version','window_ms','drawn_images','effective_draw_stream_fps',
            'whole_window_fps','start_to_first_draw_ms','max_draw_gap_ms','p95_draw_gap_ms',
            'max_silence_including_window_edges_ms','same_frame_mismatches','max_boxes',
            'browser_unique_observation_count','visibility_all_recorded_visible','visibility_events',
            'visibility_end','ack_requests','ack_non_200','ack_failed','ack_pending','errors','collection_error']
    out={k:client[k] for k in fields if k in client}
    seconds=client.get('window_ms',0)/1000
    out['websocket']={}
    for path,wire in client.get('wire',{}).items():
        out['websocket'][path]={**wire,
            'payload_bytes_s':wire.get('bytes',0)/seconds if seconds>0 else None,
            'message_hz':wire.get('messages',0)/seconds if seconds>0 else None}
    return out


def compression_stats(rows):
    observations=[row for row in rows if row['channel']=='observations']
    known=[row for row in observations if isinstance(row.get('wire_bytes'),(float,int))]
    plain=lambda row:len(json.dumps(row['payload'],ensure_ascii=False).encode('utf-8'))
    denominator=sum(plain(row) for row in known)
    wire=sum(row['wire_bytes'] for row in known)
    return {'received_observation_messages':len(observations),
            'messages_with_wire_size':len(known),
            'messages_without_wire_size':len(observations)-len(known),
            'wire_bytes_known_total':wire if known else None,
            'reconstructed_plain_json_bytes_all':sum(plain(row) for row in observations),
            'reconstructed_plain_json_bytes_for_known_wire':denominator if known else None,
            'weighted_wire_to_plain_ratio':wire/denominator if denominator else None,
            'weighted_payload_bytes_saved_fraction':1-wire/denominator if denominator else None,
            'compression_message_counts':dict(Counter(row.get('compression','not_logged') for row in observations)),
            'method':'Wire sizes are logged MQTT payload bytes; denominator reconstructs json.dumps(payload, ensure_ascii=False) with default spacing. Missing old wire sizes are not fabricated. Includes retransmissions and late observations.'}


def result_statistics(rows):
    latest={}
    for row in rows:
        p=row['payload']
        if row['channel']=='events' and p.get('kind')=='result_stats':
            key=(p.get('producer'),p.get('producer_instance_id'))
            if key not in latest or row['received_mono_ms']>=latest[key]['received_mono_ms']:
                latest[key]=row
    stats=[{**row['payload'],'received_mono_ms':row['received_mono_ms']} for row in latest.values()]
    return {'reported':bool(stats),'per_producer_instance':stats,
            'generated':sum(p.get('generated',0) for p in stats) if stats else None,
            'accepted_for_publish':sum(p.get('accepted_for_publish',0) for p in stats) if stats else None,
            'publish_rejected':sum(p.get('publish_rejected',0) for p in stats) if stats else None,
            'journal_paths':[p['journal_path'] for p in stats if p.get('journal_path')],
            'raw_journal_bytes':sum(p.get('raw_journal_bytes',0) for p in stats) if stats else None,
            'semantics':'Accepted is producer MQTT publish acceptance, not consumer receipt or disk fsync. Journals are producer-local paths. Includes full-session stats received after stop; duplicate stats per producer instance use the latest report.'}


def server_boot(rows, report):
    rid=report['run_id'];boot=report['console_boot_id']
    selected=[row for row in rows if row['payload'].get('run_id')==rid and row.get('console_boot_id','unknown')==boot]
    stops=[row['received_mono_ms'] for row in selected if row['channel']=='commands' and row['payload'].get('op')=='stop']
    starts=[row['received_mono_ms'] for row in selected if row['channel']=='commands' and row['payload'].get('op')=='start']
    stop=min(stops) if stops else float('inf')
    all_unique=[row for row in selected if row['channel']=='observations' and not row.get('duplicate')]
    timely=sorted([row for row in all_unique if row['received_mono_ms']<=stop],key=lambda row:row['received_mono_ms'])
    lag=[(row['received_mono_ms']-timely[0]['received_mono_ms'])-
         (row['payload']['source_pts_s']-timely[0]['payload']['source_pts_s'])*1000 for row in timely] if timely else []
    queue=[row['payload']['timing']['cloud_queue_ms'] for row in timely
           if isinstance(row['payload'].get('timing',{}).get('cloud_queue_ms'),(float,int))]
    result=result_statistics(selected)
    data={k:v for k,v in report.items() if k!='transport'}
    data.update(unique_observations_all_received=len(all_unique),unique_observations_before_stop=len(timely),
                unique_source_frame_ids_before_stop=len({row['payload'].get('source_frame_id') for row in timely}),
                out_of_order_observation_messages=sum(row.get('out_of_order',False) for row in selected if row['channel']=='observations'),
                unique_delivery_stream_hz=report['observed_delivery_hz'],
                whole_command_interval_observation_hz=len(timely)*1000/(stop-min(starts)) if starts and stops and stop>min(starts) else None,
                runtime_failure_events=[row['payload'] for row in selected if row['channel']=='events' and row['payload'].get('kind') in ['failed','decode_failed']],
                source_open_events=[row['payload'] for row in selected if row['payload'].get('kind')=='source_open'],
                vision_ready_events=[row['payload'] for row in selected if row['payload'].get('kind')=='vision_ready'],
                edge_transport=report['transport'],
                cloud_stats=[row['payload'] for row in selected if row['payload'].get('kind')=='cloud_stats'],
                result_stats=result,compression=compression_stats(selected),
                relative_lag={'samples':len(lag),'end_ms':lag[-1] if lag else None,
                    'p50_ms':percentile(lag,.5),'p95_ms':percentile(lag,.95),
                    'max_ms':max(lag,default=None),'min_ms':min(lag,default=None),
                    'range_ms':max(lag)-min(lag) if lag else None,
                    'method':'Receive elapsed minus source PTS elapsed, referenced to first timely unique result in this boot. Not absolute AoI; negative values can reflect backlog recovery.'},
                cloud_app_queue_p95_ms=percentile(queue,.95))
    if result['reported']:
        data['generated_minus_control_unique_received']=result['generated']-len(all_unique)
    return data


def host_network(record):
    before=record.get('control_network_before');after=record.get('control_network_after')
    seconds=record.get('control_counter_interval_s',0)
    if not before or not after or seconds<=0:
        return None
    interfaces={name:{k:after[name][k]-before[name][k] for k in ['tx_bytes','rx_bytes']}
                for name in before.keys()&after.keys()}
    return {'counter_interval_s':seconds,'interfaces':interfaces,
            'tx_bytes_s':sum(v['tx_bytes'] for v in interfaces.values())/seconds,
            'rx_bytes_s':sum(v['rx_bytes'] for v in interfaces.values())/seconds,
            'scope':'Whole control host non-loopback interfaces, including SSH and unrelated traffic. Not pure experiment bandwidth or measured link capacity.'}


def analyze_tag(tag):
    bp=ROOT/f'results/{tag}-browser.json';ep=ROOT/f'results/{tag}-events.jsonl'
    data=json.loads(bp.read_text())
    rows=deduplicate([json.loads(line) for line in ep.read_text().splitlines() if line.strip()]) if ep.exists() else []
    summaries=summarize(rows)
    output=[]
    for record in data.get('runs',[]):
        rid=record.get('run_id')
        entry={'tag':tag,'source':record.get('source',data.get('source')),
               'client_count':data.get('clients'),'repeat':record.get('repeat'),
               'warmup':bool(record.get('warmup')),'run_id':rid,
               'target_window_s':data.get('target_window_s'),
               'browser_file':str(bp),'events_file':str(ep) if ep.exists() else None,
               'browser':[per_page(client) for client in record.get('clients',[])],
               'server_boots':[server_boot(rows,r) for r in summaries if r['run_id']==rid],
               'control_host_network':host_network(record),
               'stop':record.get('stop'),'error':record.get('error'),'stop_error':record.get('stop_error'),
               'warnings':[]}
        if not ep.exists():entry['warnings'].append('Events file missing; server metrics unavailable')
        if not rid:entry['warnings'].append('Benchmark record has no owned run_id; it is unstarted or incomplete')
        if len(entry['browser'])!=data.get('clients'):entry['warnings'].append('Browser client measurement count is incomplete')
        if len(entry['server_boots'])>1:entry['warnings'].append('Multiple console boots: do not merge monotonic clocks; server metrics remain separate')
        if not entry['server_boots']:entry['warnings'].append('No matching server run evidence')
        if data.get('events_collection_error'):entry['warnings'].append(data['events_collection_error'])
        for page in entry['browser']:
            if page.get('visibility_all_recorded_visible') is False:
                entry['warnings'].append('Client '+str(page.get('index'))+' was not always recorded visible')
            if page.get('collection_error'):entry['warnings'].append('Client collection failed: '+page['collection_error'])
        output.append(entry)
    return output


def compact(entry):
    rounded=lambda v:round(v,3) if isinstance(v,(float,int)) and not isinstance(v,bool) else v
    return {'tag':entry['tag'],'repeat':entry['repeat'],'run_id':entry['run_id'],
            'source':entry['source'],'clients':entry['client_count'],
            'browser_fps':[{k:rounded(p.get(k)) for k in ['index','effective_draw_stream_fps','whole_window_fps','p95_draw_gap_ms','same_frame_mismatches','ack_requests','ack_non_200','visibility_all_recorded_visible']} for p in entry['browser']],
            'server':[{k:rounded(b.get(k)) for k in ['console_boot_id','unique_delivery_stream_hz','infer_p50_ms','infer_p95_ms','late_observations_after_stop','duplicate_observations']} for b in entry['server_boots']],
            'warnings':entry['warnings'],'error':entry['error']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tags',action='append',nargs='+',required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'results/transport-v2-analysis.json')
    args=parser.parse_args()
    tags=list(dict.fromkeys(tag for batch in args.tags for part in batch for tag in part.split(',') if tag))
    rows=[]
    for tag in tags:rows.extend(analyze_tag(tag))
    grouped=defaultdict(lambda:{'measured_run_ids':[],'warmup_run_ids':[]})
    for row in rows:
        key=(row['tag'],row['source'],row['client_count'])
        grouped[key]['warmup_run_ids' if row['warmup'] else 'measured_run_ids'].append(row['run_id'])
    result={'created_utc':datetime.now(timezone.utc).isoformat(),'tags':tags,
            'method':'Per-run, per-console-boot metrics; warmups remain separate. No pooling or averaging across source, tag or client count. Server observation identities are deduplicated independently of log duplicate flags.',
            'limits':['Browser animation-frame image updates are not physical display scanout.',
                      'Browser #obs counts are not server model throughput; 7B/garment/action branches have separate cadences.',
                      'Stream rates exclude startup and EOF idle and can be inflated by backlog bursts; whole-window rates are also provided.',
                      'Relative lag is not absolute capture-to-display AoI; cross-machine clocks were not validated.',
                      'WS/MQTT payload bytes exclude transport headers; whole-host network counters include unrelated traffic.',
                      'No labelled accuracy, absolute CLO/MET accuracy or long-run reliability conclusion is made.'],
            'groups':[{'tag':k[0],'source':k[1],'clients':k[2],**v} for k,v in grouped.items()],
            'measured_runs':[r for r in rows if not r['warmup']],
            'warmup_runs':[r for r in rows if r['warmup']]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    for row in result['measured_runs']:print(json.dumps(compact(row),ensure_ascii=False))
    print('Analysis saved:',args.output)


if __name__=='__main__':main()
