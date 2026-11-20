"""Build the standalone production page from the approved design; no runtime dependencies."""
from pathlib import Path
from bs4 import BeautifulSoup
ROOT = Path(__file__).resolve().parents[1]
s = BeautifulSoup((ROOT/'design/20261104_workbench/index.html').read_text(), 'html.parser')
def byid(i): return s.find(id=i)
def content(node, html):
    node.clear()
    for child in list(BeautifulSoup(html,'html.parser').contents): node.append(child)
s.title.string='Thermal Lab · 多模态实验工作台'
s.select_one('.preview-pill').string='正在连接 Pi'
s.select_one('.preview-pill')['id']='connectionBadge'
s.select_one('.view-switch').replace_with(BeautifulSoup('<a class="secondary" href="/../" id="visualLink" target="_blank" rel="noopener">视觉控制页 ↗</a>','html.parser'))
byid('noticeText').string='正在读取设备状态；尚未开启录音。'
s.select_one('.devices')['aria-label']='当前设备状态'
s.select_one('.devices .dot')['id']='piDot'
byid('start').span.string='开始声画测试'
byid('stop')['aria-label']='停止录音'
content(byid('stop'),'<svg class="icon"><use href="#i-stop"/></svg>停止录音')
mode=s.new_tag('label',attrs={'class':'time-control'})
content(mode,'<div class="control-label">采集内容</div><select id="captureMode" aria-label="采集内容"><option value="both">声画同步</option><option value="audio">仅音频</option></select>')
s.select_one('.devices').insert_before(mode)
extra=s.new_tag('p',attrs={'class':'control-help'})
extra.string='声画同步：视觉空闲时启动所选组，否则关联正在运行的组。停止录音不结束视觉；视觉按设定时长自动结束。'
s.select_one('.toolbar').insert_after(extra)
byid('roomDemo').replace_with(BeautifulSoup('<img id="video" alt="Pi 现场实时画面" hidden><svg id="videoOverlay" aria-label="同帧人物识别标注"></svg>','html.parser'))
byid('frameLabel').string='等待现场画面'
byid('frameMeta').decompose()
s.select_one('.video-wrap')['id']='videoStage'
content(byid('emptyVideo'),'<svg class="icon"><use href="#i-camera"/></svg><strong>等待现场画面</strong><p>开始视觉实验后显示；过期画面会隐藏</p>')
s.select_one('.person-tabs')['id']='personTabs'
s.select_one('.person-tabs').clear()
unknown=s.select('.facts .unknown')
unknown[-2]['id']='cloValue'; unknown[-1]['id']='metValue'
byid('personSub').string='仅显示与当前画面对应的识别'
byid('transcript')['aria-label']='可修改的语音转写'
byid('transcript')['maxlength']='500'
selector=s.new_tag('select',id='transcriptSelect',attrs={'aria-label':'选择语音片段','class':'transcript-select','disabled':''})
s.select_one('.voice-content').insert(0,selector)
byid('voiceSource').string='识别原文'
extra=s.new_tag('p',id='candidateText',attrs={'class':'note-line'})
extra.string='支持自然中文表达；候选标签由规则提取，7B 语义处理尚未接入。'
s.select_one('.voice-box').insert_after(extra)
content(byid('recordsPanel'),'''<h2>实验记录与反馈</h2><p class="panel-intro">这里显示 Pi 当前保留轮次的最近事件（接口最多保留 120 条）。完整历史在服务器日志中，不是此页面的完整档案。结束后的结果仅在这里查看。</p><div class="stat-grid"><div class="stat-card"><span>当前保留轮次</span><b id="recordSession">—</b></div><div class="stat-card"><span>已确认反馈</span><b id="feedbackCount">0</b></div><div class="stat-card"><span>事件数量</span><b id="eventCount">0</b></div></div><button class="secondary" id="export">导出本页事件 JSON ↓</button><div id="recordEvents" class="record-events"></div><details class="system-detail"><summary>数据去向</summary><p>Pi：/home/pi/mm_audio_v1/data/events.jsonl；控制云：/srv/mm_audio_v1/data/events.jsonl。页面导出只包含本次接口返回的数据，不能替代完整日志备份。</p><p>保留识别原文、人工修改、标签和来源事件 ID。原始录音在 Pi 本轮实验目录。</p></details>''')
content(byid('systemPanel'),'''<h2>设备与服务</h2><p class="panel-intro">状态来自 Pi 接口与云端心跳。麦克风仅在点击开始后采集。</p><div class="connection-list"><div class="connection"><div class="connection-info"><strong>Pi 5 · 采集服务</strong><p id="systemPiText">正在连接</p></div><span class="status-badge" id="systemPiBadge">未知</span></div><div class="connection"><div class="connection-info"><strong>音频云 · SenseVoice / 声事件识别</strong><p id="systemCloudText">等待心跳</p></div><span class="status-badge" id="systemCloudBadge">未知</span></div><div class="connection"><div class="connection-info"><strong>视觉实验</strong><p id="systemVisualText">正在读取</p></div><span class="status-badge" id="systemVisualBadge">未知</span></div><div class="connection"><div class="connection-info"><strong>7B 自然语言理解</strong><p>现有视觉 7B 链路独立运行；本页音频反馈尚未送入 7B。</p></div><span class="status-badge">待接入</span></div><div class="connection"><div class="connection-info"><strong>SHT40 温湿度 · SCD41 CO₂</strong><p>预留后续环境感知</p></div><span class="status-badge">未接入</span></div></div><details class="system-detail"><summary>采集、延迟与诊断详情</summary><p>硬件 48 kHz 单声道，转换为 16 kHz。RMS 不是 dBA；模型分数不是校准后的概率。声音与人物共同出现不代表此人发声。</p><pre id="diagnostics">等待状态</pre></details>''')
byid('about').string='使用说明 ↗'
byid('aboutTitle').string='现场采集与人工确认'
for p in byid('aboutModal').find_all('p'): p.decompose()
byid('aboutTitle').insert_after(BeautifulSoup('<p>可以自然说话，不限固定句子。转写是声音变文字，冷热标签是下一步理解；当前由规则给出候选，需你手动确认。</p><p>开始会使用 Pi 的麦克风并保存本轮录音。网页只负责控制与查看，运行不依赖这台 PC。视觉图像和识别按轮次与帧号对应；过期数据不作为当前结果。</p>','html.parser'))
s.style.append('''
.control-help{font-size:11px;color:#64766b;margin:-10px 3px 18px}.video-wrap{aspect-ratio:16/9;background:#e7ece5}#video{width:100%;height:100%;object-fit:contain;display:block}#videoOverlay{position:absolute;inset:0;width:100%;height:100%}.corner{background:#ffffffd9;padding:3px 6px;border-radius:4px;pointer-events:none}.person-tabs{overflow:auto;max-width:70%;flex-wrap:wrap;max-height:86px}.person-tabs button{min-height:30px}.transcript-select{width:100%;border:1px solid var(--line);border-radius:7px;padding:7px;margin-bottom:10px;background:white;color:var(--ink)}.record-events{margin-top:18px}.record-event{border-top:1px solid var(--line);padding:14px 0;font-size:12px;overflow-wrap:anywhere}.record-event p{color:#617468;margin:5px 0}.record-event button{margin-top:8px}.stat-card b{font-size:18px;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px}.facts b{overflow-wrap:anywhere}.notice.error{background:#fff1ed;border-color:#efc7bb;color:#914a32}.preview-pill{max-width:220px;white-space:normal}.video-caption{flex-wrap:wrap}.session{overflow-wrap:anywhere}.control-help,.note-line{line-height:1.7}.toolbar{flex-wrap:wrap}.actions button{min-height:40px}.choice{min-height:36px}@media(max-width:520px){.preview-pill{max-width:140px}.page-head>.secondary{display:inline-flex;margin-top:12px}.voice-text{font-size:16px}.video-wrap{aspect-ratio:16/9}.control-help{font-size:10px}.transcript-select{font-size:12px}}
''')
s.find('script').string=(ROOT/'dashboard.js').read_text()
(ROOT/'page.html').write_text(str(s))
print('Built',ROOT/'page.html')
