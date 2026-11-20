import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from lab.knowledge_retrieval import build,retrieve,answer


class RetrievalTests(unittest.TestCase):
    def test_chinese_retrieval_and_unreviewed_source_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);corpus=root/'corpus.json';index=root/'index.sqlite3'
            docs=[{'id':'a','title':'动作时间窗口','source':'a.md','section':'1','reviewed':True,'effective':True,'text':'动作窗口需要足够跨度与骨架覆盖率。'},
                  {'id':'b','title':'环境温湿度','source':'b.md','section':'1','reviewed':True,'effective':True,'text':'环境传感器读取温度湿度与二氧化碳。'}]
            corpus.write_text(json.dumps(docs),encoding='utf-8');build(corpus,index)
            self.assertEqual(retrieve(index,'骨架动作窗口')[0]['id'],'a')
            with self.assertRaises(FileExistsError):build(corpus,index)
            with patch('urllib.request.urlopen',side_effect=AssertionError('Should not generate')):
                result=answer(index,'给出这个人的精确CLO和MET数值','http://localhost')
            self.assertEqual(result['status'],'insufficient_evidence')
            docs[0]['reviewed']=False;corpus.write_text(json.dumps(docs),encoding='utf-8')
            with self.assertRaises(AssertionError):build(corpus,root/'bad.sqlite3')


if __name__=='__main__':unittest.main()
