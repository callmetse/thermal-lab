"""Independent control-cloud journal of multimodal results; audio bytes excluded."""
import json
import time
from common import TOPIC, mqtt, journal

def on_message(client, userdata, msg):
    try:
        x=json.loads(msg.payload)
        journal('events.jsonl',{'topic':msg.topic,'received_wall_ms':time.time()*1000,'event':x})
    except Exception as ex:
        print(str(ex),flush=True)

client=mqtt('collector',on_message,[TOPIC+'/results',TOPIC+'/feedback'])
while True:
    time.sleep(30)
