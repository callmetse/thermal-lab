import sys,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from lab.recognition_region import RecognitionRegion,settings
from lab.common import accepted_command


def track(x,y=.5,confidence=.8):
    return {'track_id':1,'confidence':confidence,'bbox':[x*100-2,y*100-2,x*100+2,y*100+2]}


class RegionTests(unittest.TestCase):
    def test_rectangle_and_confidence(self):
        r=RecognitionRegion({'confidence_min':.5,'confidence_max':.9,
             'recognition_region':{'mode':'rectangle','points':[[.2,.2],[.8,.8]]}})
        selected,_,_=r.apply([track(.1),track(.5),track(.5,confidence=.95)],(100,100),0)
        self.assertEqual(len(selected),1)

    def test_finite_directional_line_and_hysteresis(self):
        r=RecognitionRegion({'recognition_region':{'mode':'line','points':[[.5,.2],[.5,.8]],'direction':'a_to_b'}})
        self.assertEqual(r.apply([track(.7)],(100,100),0)[1],[])
        self.assertEqual(len(r.apply([track(.3)],(100,100),.1)[1]),1)
        self.assertEqual(r.apply([track(.7)],(100,100),.2)[1],[])
        self.assertEqual(r.counts['total'],1)
        r=RecognitionRegion({'recognition_region':{'mode':'line','points':[[.5,.2],[.5,.8]]}})
        r.apply([track(.7,y=.95)],(100,100),0)
        self.assertEqual(r.apply([track(.3,y=.95)],(100,100),.1)[1],[])

    def test_reused_id_after_gap_is_not_crossing(self):
        r=RecognitionRegion({'recognition_region':{'mode':'line','points':[[.5,0],[.5,1]]}})
        r.apply([track(.7)],(100,100),0)
        self.assertEqual(r.apply([track(.3)],(100,100),4)[1],[])

    def test_invalid_settings(self):
        for cfg in [{'confidence_min':.9,'confidence_max':.5},{'confidence_min':float('nan')},
                    {'recognition_region':{'mode':'line','points':[[0,0],[0,0]]}}]:
            with self.assertRaises(ValueError):settings(cfg)

    def test_unlimited_contract_and_replay_rejection(self):
        c={'project':'x','capture_fps':30}
        run={'project':'x','op':'start','run_id':'test','issued_ms':time.time()*1000,
             'arm':'C','source':'camera','seconds':0,'unlimited':True}
        self.assertTrue(accepted_command(run,c))
        self.assertFalse(accepted_command({**run,'source':'replay'},c))
        self.assertFalse(accepted_command({**run,'unlimited':'yes'},c))


if __name__=='__main__':unittest.main()
